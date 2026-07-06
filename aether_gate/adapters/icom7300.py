#
# Aether-gate - Icom7300Adapter: present an IC-7300 (USB CI-V) to AE as a Flex.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""RadioAdapter for the Icom IC-7300 over USB serial CI-V.

The IC-7300 has no RS-BA1 LAN transport. wfview/wfweb drive it as a plain CI-V
serial device: FE FE <radio> <controller> <payload...> FD, with scope waveform
data under command 27 00. The waveform arrives segmented: sequence 1 carries
scope metadata, sequences 2..10 carry 50 pixels each, and sequence 11 carries the
tail, for 475 raw bytes on the IC-7300.
"""
import glob
import os
import shutil
import struct
import subprocess
import threading
import time

try:
    import serial
except Exception:
    serial = None

from .base import RadioAdapter, AdapterCaps, Meters
from .icom.radios import get as get_icom

RADIO_CIV = 0x94
CTRL_CIV = 0xE0

SCOPE_MIN_DBM = -130.0
SCOPE_MAX_DBM = -10.0
SCOPE_AMP_MAX = 160
SCOPE_LEN = 475
SCOPE_SEQ_MAX = 11
SCOPE_HALF_SPANS_HZ = (2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000)
AUDIO_RATE = 24_000
AUDIO_FRAMES = 128
AUDIO_BUFFER_FRAMES = AUDIO_FRAMES * 4

MODE_TO_CIV = {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03, "RTTY": 0x04,
               "FM": 0x05, "CW-R": 0x06, "RTTY-R": 0x07}
CIV_TO_MODE = {v: k for k, v in MODE_TO_CIV.items()}
RX_LEVELS = {"af_gain": 0x01, "rf_gain": 0x02, "squelch": 0x03, "rf_power": 0x0A}


def encode_bcd_freq(hz, nbytes=5):
    """Encode Hz as Icom little-endian digit-pair BCD."""
    out = bytearray()
    hz = int(hz)
    for _ in range(nbytes):
        lo = hz % 10; hz //= 10
        hi = hz % 10; hz //= 10
        out.append((hi << 4) | lo)
    return bytes(out)


def decode_bcd_freq(data):
    """Decode Icom little-endian digit-pair BCD bytes to Hz."""
    hz, mult = 0, 1
    for byte in data:
        hz += (byte & 0x0F) * mult; mult *= 10
        hz += ((byte >> 4) & 0x0F) * mult; mult *= 10
    return hz


def decode_bcd_int(data):
    val = 0
    for byte in data:
        val = val * 100 + ((byte >> 4) & 0x0F) * 10 + (byte & 0x0F)
    return val


def encode_bcd_level(value):
    """Encode Icom 0..255 analog level as 4-digit BCD in two bytes."""
    v = max(0, min(255, int(value)))
    return bytes([((v // 1000) << 4) | ((v // 100) % 10),
                  (((v // 10) % 10) << 4) | (v % 10)])


def decode_bcd_level(data):
    if len(data) < 2:
        return None
    return ((data[0] >> 4) & 0x0F) * 1000 + (data[0] & 0x0F) * 100 + \
        ((data[1] >> 4) & 0x0F) * 10 + (data[1] & 0x0F)


def bcd_byte_to_int(byte):
    return ((byte >> 4) & 0x0F) * 10 + (byte & 0x0F)


def int_to_bcd_byte(value):
    v = max(0, min(99, int(value)))
    return ((v // 10) << 4) | (v % 10)


def build_civ_frame(payload, radio=RADIO_CIV, controller=CTRL_CIV):
    return bytes([0xFE, 0xFE, radio, controller]) + bytes(payload) + b"\xFD"


def extract_civ_frames(buf):
    """Return complete CI-V frames plus the unconsumed tail."""
    frames = []
    pos = 0
    while True:
        start = buf.find(b"\xFE\xFE", pos)
        if start < 0:
            tail = buf[-1:] if buf.endswith(b"\xFE") else b""
            return frames, tail
        end = buf.find(b"\xFD", start + 2)
        if end < 0:
            return frames, buf[start:]
        frames.append(bytes(buf[start:end + 1]))
        pos = end + 1


def parse_civ_frame(frame):
    if len(frame) < 6 or not frame.startswith(b"\xFE\xFE") or frame[-1:] != b"\xFD":
        return None
    return frame[2], frame[3], frame[4:-1]


def raw_scope_to_dbm(raw):
    raw = max(0, min(SCOPE_AMP_MAX, int(raw)))
    return SCOPE_MIN_DBM + (raw / float(SCOPE_AMP_MAX)) * (SCOPE_MAX_DBM - SCOPE_MIN_DBM)


def resample(src, n):
    if not src:
        return None
    if len(src) == n:
        return list(src)
    m = len(src)
    return [src[(i * m) // n] for i in range(n)]


def choose_scope_half_span(full_span_hz):
    half = max(1, int(float(full_span_hz) / 2.0))
    return min(SCOPE_HALF_SPANS_HZ, key=lambda x: abs(x - half))


def filter_width_to_index(hz, mode="USB"):
    hz = max(1, int(hz))
    mode = (mode or "USB").upper()
    if mode == "AM":
        calc = min(49, hz // 200 - 1)
    elif hz >= 600:
        calc = min(40, hz // 100 + 4)
    else:
        calc = hz // 50 - 1
    return int_to_bcd_byte(max(0, calc))


def filter_index_to_width(index_bcd, mode="USB"):
    idx = bcd_byte_to_int(index_bcd)
    if (mode or "").upper() == "AM":
        return (idx + 1) * 200
    if idx <= 9:
        return (idx + 1) * 50
    return (idx - 4) * 100


class AlsaPcmCapture:
    """Small stdlib arecord wrapper returning 24 kHz mono float samples."""

    def __init__(self, device=None, rate=AUDIO_RATE, channels=1,
                 frames_per_read=AUDIO_FRAMES, buffer_frames=AUDIO_BUFFER_FRAMES):
        self.device = device
        self.rate = int(rate)
        self.channels = int(channels)
        self.frames_per_read = int(frames_per_read)
        self.buffer_frames = int(buffer_frames)
        self._proc = None
        self._buf = bytearray()
        self._last_err = None

    @staticmethod
    def default_device():
        for path in sorted(glob.glob("/proc/asound/card*/usbid")):
            try:
                usbid = open(path, "r", encoding="ascii", errors="ignore").read().strip().lower()
                if usbid in ("08bb:2901", "08bb:2902"):
                    card_dir = os.path.dirname(path)
                    try:
                        card_id = open(os.path.join(card_dir, "id"), "r",
                                       encoding="ascii", errors="ignore").read().strip()
                    except OSError:
                        card_id = os.path.basename(card_dir).replace("card", "")
                    return f"plughw:CARD={card_id},DEV=0" if card_id else "default"
            except OSError:
                pass
        for path in sorted(glob.glob("/proc/asound/card*/id")):
            try:
                if open(path, "r", encoding="ascii", errors="ignore").read().strip() == "CODEC":
                    return "plughw:CARD=CODEC,DEV=0"
            except OSError:
                pass
        return "default"

    @property
    def detail(self):
        if self._last_err:
            return self._last_err
        return self.device or self.default_device()

    def open(self):
        if self._proc and self._proc.poll() is None:
            return
        if not shutil.which("arecord"):
            self._last_err = "arecord not found"
            return
        dev = self.device or self.default_device()
        cmd = ["arecord", "-q", "-D", dev, "-f", "S16_LE", "-c", str(self.channels),
               "-r", str(self.rate), "--period-size", str(self.frames_per_read),
               "--buffer-size", str(max(self.frames_per_read * 2, self.buffer_frames)),
               "--start-delay", "0", "-t", "raw", "-"]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self._last_err = None
        except Exception as e:
            self._proc = None
            self._last_err = str(e)

    def close(self):
        self._buf.clear()
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=1.0)
            except Exception:
                p.kill()

    def restart(self):
        self.close()
        self.open()

    def read(self, n_samples):
        self.open()
        if not self._proc or not self._proc.stdout:
            return None
        need = int(n_samples) * 2 * self.channels
        try:
            while len(self._buf) < need:
                chunk = self._proc.stdout.read(
                    max(need - len(self._buf), self.frames_per_read * 2 * self.channels))
                if not chunk:
                    err = b""
                    try:
                        err = self._proc.stderr.read(200) if self._proc.stderr else b""
                    except Exception:
                        pass
                    self._last_err = err.decode(errors="replace").strip() or "arecord stopped"
                    self.close()
                    return None
                self._buf.extend(chunk)
            raw = bytes(self._buf[:need])
            del self._buf[:need]
            vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
            if self.channels != 1:
                vals = tuple(int(sum(vals[i:i + self.channels]) / self.channels)
                             for i in range(0, len(vals), self.channels))
            return [max(-1.0, min(1.0, s / 32768.0)) for s in vals[:n_samples]]
        except Exception as e:
            self._last_err = str(e)
            self.close()
            return None


class Icom7300ScopeAssembler:
    """Combine IC-7300 27 00 segmented scope replies into one raw row."""

    def __init__(self, expected_len=SCOPE_LEN, seq_max=SCOPE_SEQ_MAX):
        self.expected_len = expected_len
        self.seq_max = seq_max
        self.data = bytearray()
        self.mode = None
        self.start_hz = None
        self.end_hz = None
        self.frames = 0
        self.last_raw = None

    def feed_payload(self, payload):
        if len(payload) < 5 or payload[:3] != b"\x27\x00\x00":
            return None
        seq = bcd_byte_to_int(payload[3])
        seq_max = bcd_byte_to_int(payload[4])
        if seq_max:
            self.seq_max = seq_max

        if seq == 1:
            self.data.clear()
            if len(payload) >= 16:
                self.mode = payload[5]
                self.start_hz = decode_bcd_freq(payload[6:11])
                span_or_end = decode_bcd_freq(payload[11:16])
                if self.mode == 0:
                    self.end_hz = self.start_hz + span_or_end
                    self.start_hz = self.start_hz - span_or_end
                else:
                    self.end_hz = span_or_end
            if seq == self.seq_max and len(payload) > 17:
                self.data.extend(payload[17:])
            return None

        if seq < 2 or seq > self.seq_max:
            return None
        self.data.extend(payload[5:])
        if seq != self.seq_max:
            return None

        row = bytes(self.data[:self.expected_len])
        if len(row) < self.expected_len:
            return None
        self.frames += 1
        self.last_raw = row
        return row


class Icom7300SerialCiv:
    """Small serial CI-V client for the IC-7300."""

    def __init__(self, port, baud=115200, civ_addr=RADIO_CIV, controller=CTRL_CIV):
        self.port = port
        self.baud = int(baud)
        self.addr = int(civ_addr)
        self.controller = int(controller)
        self._ser = None
        self._run = False
        self._reader = None
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._frames = []
        self._assembler = Icom7300ScopeAssembler()
        self.latest_dbm = None
        self.freq_hz = None
        self.other_freq_hz = None
        self.mode = None
        self.filter = None
        self.other_mode = None
        self.other_filter = None
        self.smeter_raw = None
        self.levels = {}
        self.preamp = None
        self.attenuator_db = None
        self.split = None
        self.tuner = None
        self.scope_half_span_hz = None
        self.filter_width_hz = None
        self.n_fb = 0
        self.n_fa = 0
        self.last_err = None

    def open(self):
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        # Keep hardware PTT control lines inactive. pyserial may assert RTS/DTR
        # when opening a port unless told otherwise; on an IC-7300 those lines
        # can be configured as SEND/PTT, which can key the radio without any
        # CI-V 1C 00 transmit command. wfview clears both lines after open; do
        # the same, and set them before open where pyserial allows it.
        self._ser = serial.Serial()
        self._ser.port = self.port
        self._ser.baudrate = self.baud
        self._ser.timeout = 0.1
        self._ser.rtscts = False
        self._ser.dsrdtr = False
        self._ser.rts = False
        self._ser.dtr = False
        self._ser.open()
        try:
            self._ser.setRTS(False)
            self._ser.setDTR(False)
            self._ser.send_break(False)
        except Exception:
            pass
        self._run = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="icom7300-serial")
        self._reader.start()

    def close(self):
        self._run = False
        try:
            if self._ser:
                try:
                    self._ser.setRTS(False)
                    self._ser.setDTR(False)
                    self._ser.send_break(False)
                except Exception:
                    pass
                self._ser.close()
        except Exception:
            pass
        self._ser = None

    def send(self, payload):
        frame = build_civ_frame(payload, self.addr, self.controller)
        with self._lock:
            if not self._ser:
                raise RuntimeError("serial port is closed")
            self._ser.write(frame)

    def request(self, payload, matcher=None, timeout=0.8):
        payload = bytes(payload)
        start = time.monotonic()
        with self._cv:
            self._frames.clear()
            self.send(payload)
            while True:
                for i, frame_payload in enumerate(self._frames):
                    if matcher:
                        ok = matcher(frame_payload)
                    else:
                        ok = bool(frame_payload and frame_payload[0] == payload[0])
                    if ok:
                        return self._frames.pop(i)
                remain = timeout - (time.monotonic() - start)
                if remain <= 0:
                    return None
                self._cv.wait(remain)

    def _read_loop(self):
        buf = b""
        while self._run:
            try:
                chunk = self._ser.read(256)
                if not chunk:
                    continue
                buf += chunk
                frames, buf = extract_civ_frames(buf)
                for frame in frames:
                    parsed = parse_civ_frame(frame)
                    if parsed:
                        self._handle_frame(*parsed)
            except Exception as e:
                if self._run:
                    self.last_err = str(e)
                time.sleep(0.05)

    def _handle_frame(self, to_addr, from_addr, payload):
        # Drop our bus echo; keep radio replies and transceive broadcasts.
        if from_addr == self.controller:
            return
        if from_addr != self.addr or to_addr not in (self.controller, 0x00):
            return
        payload = bytes(payload)
        if payload == b"\xFB":
            self.n_fb += 1
        elif payload == b"\xFA":
            self.n_fa += 1
        self._update_state(payload)
        with self._cv:
            self._frames.append(payload)
            self._frames = self._frames[-32:]
            self._cv.notify_all()

    def _update_state(self, payload):
        if not payload:
            return
        cmd = payload[0]
        data = payload[1:]
        if cmd == 0x03 and len(data) >= 5:
            self.freq_hz = decode_bcd_freq(data[:5])
        elif cmd == 0x25 and len(data) >= 6:
            if data[0] == 0x00:
                self.freq_hz = decode_bcd_freq(data[1:6])
            elif data[0] == 0x01:
                self.other_freq_hz = decode_bcd_freq(data[1:6])
        elif cmd == 0x04 and data:
            self.mode = CIV_TO_MODE.get(data[0], self.mode)
            if len(data) >= 2:
                self.filter = data[1]
        elif cmd == 0x26 and len(data) >= 2 and data[0] == 0x00:
            self.mode = CIV_TO_MODE.get(data[1], self.mode)
            if len(data) >= 3:
                self.filter = data[2]
        elif cmd == 0x26 and len(data) >= 2 and data[0] == 0x01:
            self.other_mode = CIV_TO_MODE.get(data[1], self.other_mode)
            if len(data) >= 3:
                self.other_filter = data[2]
        elif cmd == 0x14 and len(data) >= 3:
            val = decode_bcd_level(data[1:3])
            if val is not None:
                self.levels[data[0]] = val
        elif cmd == 0x16 and len(data) >= 2 and data[0] == 0x02:
            self.preamp = data[1]
        elif cmd == 0x11 and data:
            self.attenuator_db = bcd_byte_to_int(data[0])
        elif cmd == 0x0F and data:
            self.split = bool(data[0])
        elif cmd == 0x1C and len(data) >= 2 and data[0] == 0x01:
            self.tuner = data[1]
        elif cmd == 0x1A and len(data) >= 2 and data[0] == 0x03:
            self.filter_width_hz = filter_index_to_width(data[1], self.mode)
        elif cmd == 0x27 and len(data) >= 5 and data[:2] == b"\x15\x00":
            self.scope_half_span_hz = decode_bcd_freq(data[2:5])
        elif cmd == 0x15 and len(data) >= 3 and data[0] == 0x02:
            self.smeter_raw = decode_bcd_int(data[1:3])
        elif payload[:3] == b"\x27\x00\x00":
            row = self._assembler.feed_payload(payload)
            if row is not None:
                self.latest_dbm = [raw_scope_to_dbm(x) for x in row]

    def read_initial(self):
        self.request(b"\x19\x00", timeout=0.5)
        self.read_freq()
        self.read_other_freq()
        self.read_mode()
        self.read_other_mode()
        self.read_smeter()
        for name in ("af_gain", "rf_gain", "squelch"):
            self.read_level(name)
        self.read_preamp()
        self.read_attenuator()
        self.read_split()
        self.read_filter_width()
        self.read_scope_span()

    def read_freq(self):
        p = self.request(b"\x25\x00", lambda r: r[:2] == b"\x25\x00" and len(r) >= 7)
        return decode_bcd_freq(p[2:7]) if p else self.freq_hz

    def read_mode(self):
        p = self.request(b"\x04", lambda r: r[:1] == b"\x04" and len(r) >= 2)
        if p:
            self.mode = CIV_TO_MODE.get(p[1], self.mode)
            self.filter = p[2] if len(p) >= 3 else self.filter
        return self.mode

    def read_other_freq(self):
        p = self.request(b"\x25\x01", lambda r: r[:2] == b"\x25\x01" and len(r) >= 7)
        return decode_bcd_freq(p[2:7]) if p else self.other_freq_hz

    def read_other_mode(self):
        p = self.request(b"\x26\x01", lambda r: r[:2] == b"\x26\x01" and len(r) >= 3)
        if p:
            self.other_mode = CIV_TO_MODE.get(p[2], self.other_mode)
            self.other_filter = p[3] if len(p) >= 4 else self.other_filter
        return self.other_mode

    def read_smeter(self):
        p = self.request(b"\x15\x02", lambda r: r[:2] == b"\x15\x02" and len(r) >= 4)
        if p:
            return decode_bcd_int(p[2:4])
        return self.smeter_raw

    def set_freq(self, hz):
        reply = self.request(bytes([0x05]) + encode_bcd_freq(hz),
                             lambda r: r in (b"\xFB", b"\xFA"), timeout=0.5)
        if reply == b"\xFB":
            self.freq_hz = int(hz)
            return True
        return False

    def set_other_freq(self, hz):
        reply = self.request(bytes([0x25, 0x01]) + encode_bcd_freq(hz),
                             lambda r: r in (b"\xFB", b"\xFA"), timeout=0.5)
        if reply == b"\xFB":
            self.other_freq_hz = int(hz)
            return True
        return False

    def set_mode(self, mode, filt=None):
        mode_byte = MODE_TO_CIV.get((mode or "").upper())
        if mode_byte is None:
            return False
        filt = self.filter if filt is None else filt
        if filt is None:
            filt = 1
        reply = self.request(bytes([0x06, mode_byte, int(filt) & 0xFF]),
                             lambda r: r in (b"\xFB", b"\xFA"), timeout=0.5)
        if reply == b"\xFB":
            self.mode = (mode or "").upper()
            self.filter = int(filt) & 0xFF
            return True
        return False

    def set_filter(self, filt):
        if not self.mode:
            self.read_mode()
        return self.set_mode(self.mode or "USB", filt)

    def read_level(self, name_or_sub):
        sub = RX_LEVELS.get(name_or_sub, name_or_sub)
        p = self.request(bytes([0x14, int(sub)]),
                         lambda r: r[:2] == bytes([0x14, int(sub)]) and len(r) >= 4,
                         timeout=0.5)
        if p:
            self.levels[int(sub)] = decode_bcd_level(p[2:4])
        return self.levels.get(int(sub))

    def set_level(self, name_or_sub, value):
        sub = RX_LEVELS.get(name_or_sub, name_or_sub)
        reply = self.request(bytes([0x14, int(sub)]) + encode_bcd_level(value),
                             lambda r: r in (b"\xFB", b"\xFA"), timeout=0.5)
        if reply == b"\xFB":
            self.levels[int(sub)] = max(0, min(255, int(value)))
            return True
        return False

    def read_preamp(self):
        p = self.request(b"\x16\x02", lambda r: r[:2] == b"\x16\x02" and len(r) >= 3, timeout=0.5)
        if p:
            self.preamp = p[2]
        return self.preamp

    def set_preamp(self, level):
        reply = self.request(bytes([0x16, 0x02, max(0, min(2, int(level)))]),
                             lambda r: r in (b"\xFB", b"\xFA"), timeout=0.5)
        if reply == b"\xFB":
            self.preamp = max(0, min(2, int(level)))
            return True
        return False

    def read_attenuator(self):
        p = self.request(b"\x11", lambda r: r[:1] == b"\x11" and len(r) >= 2, timeout=0.5)
        if p:
            self.attenuator_db = bcd_byte_to_int(p[1])
        return self.attenuator_db

    def set_attenuator(self, db):
        db = 20 if int(db) else 0
        reply = self.request(bytes([0x11, int_to_bcd_byte(db)]),
                             lambda r: r in (b"\xFB", b"\xFA"), timeout=0.5)
        if reply == b"\xFB":
            self.attenuator_db = db
            return True
        return False

    def read_split(self):
        p = self.request(b"\x0F", lambda r: r[:1] == b"\x0F" and len(r) >= 2, timeout=0.5)
        if p:
            self.split = bool(p[1])
        return self.split

    def read_tuner(self):
        p = self.request(b"\x1C\x01", lambda r: r[:2] == b"\x1C\x01" and len(r) >= 3, timeout=0.5)
        if p:
            self.tuner = p[2]
        return self.tuner

    def read_filter_width(self):
        p = self.request(b"\x1A\x03", lambda r: r[:2] == b"\x1A\x03" and len(r) >= 3, timeout=0.5)
        if p:
            self.filter_width_hz = filter_index_to_width(p[2], self.mode)
        return getattr(self, "filter_width_hz", None)

    def set_filter_width(self, hz):
        idx = filter_width_to_index(hz, self.mode)
        reply = self.request(bytes([0x1A, 0x03, idx]),
                             lambda r: r in (b"\xFB", b"\xFA"), timeout=0.5)
        if reply == b"\xFB":
            self.filter_width_hz = filter_index_to_width(idx, self.mode)
            return True
        return False

    def read_scope_span(self):
        p = self.request(b"\x27\x15\x00", lambda r: r[:3] == b"\x27\x15\x00" and len(r) >= 6,
                         timeout=0.5)
        if p:
            self.scope_half_span_hz = decode_bcd_freq(p[3:6])
        return self.scope_half_span_hz

    def set_scope_span(self, full_span_hz):
        half = choose_scope_half_span(full_span_hz)
        reply = self.request(bytes([0x27, 0x15, 0x00]) + encode_bcd_freq(half, 3),
                             lambda r: r in (b"\xFB", b"\xFA"), timeout=0.5)
        if reply == b"\xFB":
            self.scope_half_span_hz = half
            return True
        return False

    def enable_scope(self):
        self.request(b"\x27\x10\x01", lambda r: r in (b"\xFB", b"\xFA"), timeout=0.4)
        self.request(b"\x27\x11\x01", lambda r: r in (b"\xFB", b"\xFA"), timeout=0.4)
        self.request(b"\x27\x14\x00\x00", lambda r: r in (b"\xFB", b"\xFA"), timeout=0.4)  # center
        self.request(b"\x27\x1A\x00\x00", lambda r: r in (b"\xFB", b"\xFA"), timeout=0.4)  # fast

    def request_scope(self):
        self.send(b"\x27\x00")

    @property
    def scope_frames(self):
        return self._assembler.frames

    @property
    def scope_bounds(self):
        return self._assembler.start_hz, self._assembler.end_hz


class Icom7300Adapter(RadioAdapter):
    """IC-7300 USB CI-V adapter using the radio's own band scope."""

    provides = "spectrum"

    def __init__(self, usb_civ_port=None, usb_civ_baud=115200, civ_addr=RADIO_CIV,
                 model="FLEX-6600", serial="GATE7300", station="Icom-IC-7300",
                 usb_audio_device=None):
        row = get_icom("IC-7300")
        bands = tuple(b.name for b in row.bands) if row else ()
        self.port = usb_civ_port
        self.baud = int(usb_civ_baud)
        self.civ_addr = int(civ_addr)
        self.audio_device = usb_audio_device
        self.capabilities = AdapterCaps(model=model, serial=serial, station=station,
                                        tx_capable=False, max_slices=1,
                                        native_centered_scope=True,
                                        min_span_hz=5_000.0,
                                        max_span_hz=500_000.0, bands=bands)
        self._civ = None
        self._audio = AlsaPcmCapture(usb_audio_device)
        self._run = False
        self._poller = None
        self._last_reopen = 0.0
        self._ctl_lock = threading.Lock()
        self._target_freq_hz = None
        self._target_mode = None
        self._target_filter = None
        self._target_filter_width_hz = None
        self._target_span_hz = None

    def open(self):
        if not self.port:
            raise RuntimeError("--usb-civ-port is required for icom7300")
        self._open_civ()
        self._audio.open()
        self._run = True
        self._poller = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="icom7300-poll")
        self._poller.start()

    def _open_civ(self):
        self._civ = Icom7300SerialCiv(self.port, self.baud, self.civ_addr)
        self._civ.open()
        self._civ.read_initial()
        self._civ.enable_scope()

    def close(self):
        self._run = False
        if self._poller:
            self._poller.join(timeout=1.0)
        if self._civ:
            try:
                self._civ.request(b"\x27\x11\x00", lambda r: r in (b"\xFB", b"\xFA"), timeout=0.2)
            except Exception:
                pass
            self._civ.close()
        self._audio.close()

    def _reopen_civ(self):
        now = time.monotonic()
        if now - self._last_reopen < 3.0:
            return
        self._last_reopen = now
        old = self._civ
        if old:
            old.close()
        try:
            self._open_civ()
        except Exception as e:
            if self._civ:
                self._civ.last_err = f"reopen failed: {e}"

    def _poll_loop(self):
        last_slow = 0.0
        last_deep = 0.0
        while self._run:
            try:
                if not self._civ or self._civ.last_err:
                    self._reopen_civ()
                self._apply_control_targets()
                now = time.monotonic()
                if now - last_slow > 1.0:
                    self._civ.read_freq()
                    self._civ.read_mode()
                    self._civ.read_smeter()
                    last_slow = now
                if now - last_deep > 5.0:
                    self._civ.read_other_freq()
                    self._civ.read_other_mode()
                    self._civ.read_split()
                    last_deep = now
            except Exception as e:
                if self._civ:
                    self._civ.last_err = str(e)
            time.sleep(0.12)

    def _pop_control_targets(self):
        with self._ctl_lock:
            vals = (self._target_freq_hz, self._target_mode, self._target_filter,
                    self._target_filter_width_hz, self._target_span_hz)
            self._target_freq_hz = None
            self._target_mode = None
            self._target_filter = None
            self._target_filter_width_hz = None
            self._target_span_hz = None
        return vals

    def _apply_control_targets(self):
        c = self._civ
        if not c:
            return
        freq_hz, mode, filt, width_hz, span_hz = self._pop_control_targets()
        if freq_hz is not None and int(freq_hz) != c.freq_hz:
            if c.set_freq(int(freq_hz)):
                self._audio.restart()
        if mode is not None or filt is not None:
            next_mode = (mode or c.mode or "USB").upper()
            next_filter = c.filter if filt is None else int(filt)
            if next_mode != c.mode or (next_filter is not None and next_filter != c.filter):
                c.set_mode(next_mode, next_filter)
        if width_hz is not None:
            desired = filter_index_to_width(filter_width_to_index(width_hz, c.mode), c.mode)
            if desired != c.filter_width_hz:
                c.set_filter_width(width_hz)
        if span_hz is not None:
            desired_half = choose_scope_half_span(span_hz)
            if desired_half != c.scope_half_span_hz:
                c.set_scope_span(span_hz)

    def get_spectrum(self, ctx, t):
        levels = self._civ.latest_dbm if self._civ else None
        if levels is None:
            return [ctx.min_dbm] * ctx.n
        return resample(levels, ctx.n)

    def retune(self, center_hz):
        with self._ctl_lock:
            self._target_freq_hz = int(center_hz)

    def set_slice(self, slice_hz):
        self.retune(slice_hz)

    def set_mode(self, mode):
        with self._ctl_lock:
            self._target_mode = (mode or "").upper()

    def set_filter(self, filt):
        with self._ctl_lock:
            self._target_filter = int(filt)

    def set_filter_width_hz(self, hz):
        with self._ctl_lock:
            self._target_filter_width_hz = int(hz)

    def set_span(self, span_hz):
        effective = choose_scope_half_span(span_hz) * 2
        with self._ctl_lock:
            self._target_span_hz = float(effective)
        return float(effective)

    def effective_span_hz(self):
        c = self._civ
        if c and c.scope_half_span_hz:
            return float(c.scope_half_span_hz * 2)
        with self._ctl_lock:
            if self._target_span_hz:
                return float(self._target_span_hz)
        return None

    def set_sub_freq_hz(self, hz):
        if self._civ:
            self._civ.set_other_freq(int(hz))

    def receivers(self):
        """Return live receivers for radio->AE slice sync.

        The IC-7300 has two VFO memories but only one receiver. Exposing the
        unselected VFO here makes the core treat VFO B as a live SUB receiver,
        causing AetherSDR to spawn a slice B/pan and recreate it every time the
        user closes it. Keep VFO B in diagnostics and CI-V helpers, but publish
        only the selected receiver to the Flex slice model.
        """
        c = self._civ
        if not c:
            return []
        out = []
        if c.freq_hz:
            out.append({"freq_hz": c.freq_hz, "mode": c.mode or "USB"})
        return out

    def set_rx_level(self, name, value):
        if self._civ:
            return self._civ.set_level(name, value)
        return False

    def get_audio(self, n_samples, slice_hz=None, mode=None):
        return self._audio.read(n_samples)

    def initial_center_hz(self):
        return float(self._civ.freq_hz) if self._civ and self._civ.freq_hz else None

    def initial_mode(self):
        return self._civ.mode if self._civ else None

    def read_meters(self):
        raw = self._civ.smeter_raw if self._civ else None
        if raw is None:
            return Meters()
        # IC-7300 wfweb meter table: raw 120 ~= S9 / 0 dB over S9. Convert to
        # an HF dBm convention so AE sees a plausible receive meter.
        db_over_s9 = (raw - 120.0) * (64.0 / 121.0) if raw > 120 else (raw - 120.0) * (54.0 / 120.0)
        return Meters(s_meter_dbm=-73.0 + db_over_s9)

    def diagnostics(self):
        c = self._civ
        if not c:
            return {"radio": "IC-7300", "link": {"transport": "usb-civ", "state": "closed"}}
        lo, hi = c.scope_bounds
        return {
            "radio": "IC-7300",
            "link": {"transport": "usb-civ", "host": self.port, "state": "open",
                     "detail": c.last_err or f"{self.baud} baud"},
            "vfos": [{"name": "Selected", "freq_hz": c.freq_hz, "mode": c.mode, "selected": True},
                     {"name": "Other", "freq_hz": c.other_freq_hz, "mode": c.other_mode, "selected": False}],
            "meters": {"s_meter_raw": c.smeter_raw},
            "audio": {"source": "alsa_arecord", "device": self._audio.detail},
            "rx_controls": {
                "af_gain": c.levels.get(RX_LEVELS["af_gain"]),
                "rf_gain": c.levels.get(RX_LEVELS["rf_gain"]),
                "squelch": c.levels.get(RX_LEVELS["squelch"]),
                "preamp": c.preamp,
                "attenuator_db": c.attenuator_db,
            },
            "filters": {"selected": c.filter, "other": c.other_filter,
                        "width_hz": getattr(c, "filter_width_hz", None)},
            "flags": {"split": c.split, "tuner": c.tuner, "tx_enabled": False},
            "scope": {"frames": c.scope_frames, "bins": len(c.latest_dbm or []),
                      "low_hz": lo, "high_hz": hi,
                      "half_span_hz": c.scope_half_span_hz},
            "counters": {"fb": c.n_fb, "fa": c.n_fa},
        }
