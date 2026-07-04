#
# Aether-gate — IC-9700 USB CI-V control channel (RX2 via a NON-destructive swap).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""A second, USB-serial CI-V channel to the IC-9700, used ONLY for reading the
2nd receiver (RX2).

Why this exists: RX2 is reachable only by the CI-V `07 B0` main/sub swap, and
over the LAN/RS-BA1 session that swap YANKS the scope stream -> deaf sessions
(proven destructive 2026-07-03). But CI-V over the 9700's USB port carries NO
scope stream, so the SAME swap is harmless there: proven 5/5 on HW, MAIN held,
RX2 (146.025) read cleanly, swap-back perfect. So the hybrid gate keeps the LAN
channel for MAIN's waterfall and uses THIS USB channel to swap-read RX2 in the
background without disturbing the scope.

Raw pyserial (no hamlib): the 9700's USB CI-V is a plain serial CI-V bus.
  * COM7 / ..._A is the CI-V port (COM6 / ..._B is audio). 115200 8N1, addr A2.
  * Frame to radio:  FE FE A2 E0 <cmd...> FD
  * Reply from radio: FE FE E0 A2 <cmd...> FD   (plus a bus echo of our own frame)
"""
import threading
import time

try:
    import serial               # pyserial
except Exception:               # optional dep — adapter degrades to LAN-only RX2-off
    serial = None

RADIO = 0xA2                     # default 9700 CI-V address
CTRL = 0xE0                     # our controller address

MODE_BYTES = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY",
              0x05: "FM", 0x06: "CW-R", 0x07: "RTTY-R", 0x08: "DV", 0x12: "FM-N"}


def _unbcd(b):
    f, m = 0, 1
    for x in b:
        f += (x & 0xF) * m; m *= 10
        f += (x >> 4) * m; m *= 10
    return f


def _bcd(hz):
    out = bytearray(); hz = int(hz)
    for _ in range(5):
        lo = hz % 10; hz //= 10
        hi = hz % 10; hz //= 10
        out.append((hi << 4) | lo)
    return bytes(out)


def _diff_band(a, b):
    """True if a and b are on different bands (>1 MHz apart). Used to verify a
    07 B0 swap actually changed the selected receiver (MAIN and RX2 are always
    on different bands on the 9700)."""
    return a is not None and b is not None and abs(a - b) > 1_000_000


class UsbCiv:
    """Serial CI-V control channel. Thread-safe (one lock serialises transactions).
    Holds a background poller that swap-reads RX2 at a gentle cadence."""

    def __init__(self, port, baud=115200, civ_addr=RADIO, poll_s=8.0):
        # poll_s: RX2 swap-read cadence. Each swap blinks the LAN scope to
        # RX2's band for ~0.6s (the 07 B0 is global), so a fast cadence
        # visibly flickers the waterfall AND stresses the scope stream
        # (3s cadence correlated with deaf events under active tuning).
        # 8s keeps RX2 live enough while cutting the churn ~3x.
        self.port = port
        self.baud = baud
        self.addr = civ_addr
        self.poll_s = poll_s
        self._ser = None
        self._lock = threading.Lock()
        self._run = False
        self._poller = None
        # True while a swap sequence has the radio flipped to RX2. The LAN
        # channel reads this to SUPPRESS its freq-follow during the blink (the
        # 07 B0 swap is global, so LAN would otherwise read RX2's freq and jerk
        # slice 0). A little slop is added around the window by the adapter.
        self.swapping = False
        self.swap_ended_at = 0.0
        # SINGLE SOURCE OF TRUTH for freq/mode of BOTH receivers (2026-07-03):
        # the LAN channel does ONLY the scope waterfall; USB owns all freq/mode
        # reads + tunes, so the two CI-V masters can't race over the selected
        # receiver. main_* = the selected (MAIN) receiver; rx2_* = the other.
        self.rx2_present = False
        self.rx2_freq_hz = None
        self.rx2_mode = None
        self.main_freq_hz = None        # MAIN (selected) freq — authoritative
        self.main_mode = None           # MAIN (selected) mode — authoritative
        self.ok = False                 # serial opened + radio answered
        self.last_err = None

    # --- lifecycle --------------------------------------------------------
    def open(self):
        if serial is None:
            self.last_err = "pyserial not installed"; return False
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.5)
        except Exception as e:
            self.last_err = f"open {self.port}: {e}"; return False
        # prove the radio answers (read MAIN freq 03)
        f = self._read_freq(bytes([0x03]))
        if f is None:
            self.last_err = "no CI-V reply on USB"
            try: self._ser.close()
            except Exception: pass
            self._ser = None
            return False
        self.main_freq_hz = f
        self.ok = True
        self.read_main()                # refine to the SELECTED VFO (25 00/26 00)
        return True

    def start(self):
        """Open + kick off the background RX2 poller. Returns True if live."""
        if not self.open():
            return False
        self._run = True
        self._poller = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="ic9700-usb-rx2")
        self._poller.start()
        return True

    def stop(self):
        self._run = False
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass
        self._ser = None

    # --- low-level CI-V ---------------------------------------------------
    def _civ(self, payload, wait=0.15):
        """Send one CI-V frame, return the raw bytes read back (echo + reply)."""
        if not self._ser:
            return b""
        frame = bytes([0xFE, 0xFE, self.addr, CTRL]) + payload + bytes([0xFD])
        try:
            self._ser.reset_input_buffer()
            self._ser.write(frame)
            time.sleep(wait)
            return self._ser.read(96)
        except Exception as e:
            self.last_err = str(e); return b""

    def _reply_body(self, raw, cmd):
        """Extract the body of the radio's reply frame for <cmd> (skip our echo)."""
        marker = bytes([0xFE, 0xFE, CTRL, self.addr, cmd])
        i = raw.find(marker)
        if i < 0:
            return None
        body = raw[i + 5:]
        end = body.find(0xFD)
        return body[:end] if end >= 0 else None

    def _read_freq(self, cmd):
        raw = self._civ(cmd)
        b = self._reply_body(raw, cmd[0])
        if cmd[0] == 0x03:
            return _unbcd(b) if b and len(b) >= 5 else None
        if cmd[0] == 0x25:
            return _unbcd(b[1:6]) if b and len(b) >= 6 else None
        return None

    def _read_mode(self, cmd):
        raw = self._civ(cmd)
        b = self._reply_body(raw, cmd[0])
        if cmd[0] == 0x26 and b and len(b) >= 2:
            return MODE_BYTES.get(b[1])
        return None

    def _swap_until(self, pred, settle=0.3, tries=4):
        """Fire 07 B0, read the selected freq, repeat until pred(freq) is True
        (max `tries`). Returns True if pred held. This makes the toggle
        self-correcting: a missed/duplicated swap is caught and fixed here, so
        MAIN/RX2 can never end up permanently inverted."""
        for _ in range(tries):
            self._civ(bytes([0x07, 0xB0]), settle)
            f = self._read_freq(bytes([0x25, 0x00]))
            if pred(f):
                return True
        return False

    # --- the RX2 swap-read (harmless over USB) ----------------------------
    def read_rx2(self, settle=0.3):
        """07 B0 -> read RX2 (25 00 / 26 00) -> 07 B0 back. Serialised. Sets
        rx2_* + rx2_present (RX2 on a real, DIFFERENT band than MAIN)."""
        with self._lock:
            main = self._read_freq(bytes([0x25, 0x00]))
            mainm = self._read_mode(bytes([0x26, 0x00]))
            if main:
                self.main_freq_hz = main
                self.main_mode = mainm or self.main_mode
            self.swapping = True                             # LAN: suppress freq-follow
            rx2 = rx2m = None
            try:
                # VERIFIED TOGGLE. 07 B0 is a toggle (swap MAIN<->SUB); a blind
                # swap+swap-back occasionally leaves the radio INVERTED (a missed
                # 07 B0 under load) -> MAIN/RX2 flip permanently. So swap, then
                # VERIFY the selected freq actually moved to a different band; if
                # not, the swap didn't take -> retry. Same on the way back:
                # confirm we're on MAIN again, else swap until we are. Can't drift.
                if self._swap_until(lambda f: main is None or _diff_band(f, main),
                                    settle):
                    rx2 = self._read_freq(bytes([0x25, 0x00]))
                    rx2m = self._read_mode(bytes([0x26, 0x00]))
                # restore MAIN: swap until the selected freq is MAIN's again
                if main is not None:
                    self._swap_until(lambda f: f is not None and not _diff_band(f, main),
                                     settle)
                else:
                    self._civ(bytes([0x07, 0xB0]), settle)
            finally:
                self.swapping = False
                self.swap_ended_at = time.monotonic()
            if rx2:
                self.rx2_freq_hz = rx2
                self.rx2_mode = rx2m
                self.rx2_present = bool(rx2 > 1_000_000
                                        and (main is None or _diff_band(rx2, main)))
            return self.rx2_present

    def read_main(self):
        """Read the MAIN (selected) receiver freq+mode — NO swap. Fast; safe to
        call often. This is the authoritative MAIN for slice 0."""
        with self._lock:
            f = self._read_freq(bytes([0x25, 0x00]))
            m = self._read_mode(bytes([0x26, 0x00]))
        if f:
            self.main_freq_hz = f
            if m:
                self.main_mode = m
        return f

    def tune_main(self, hz):
        """Tune the MAIN (selected) receiver — NO swap (25 00 write). Returns
        True if not FA'd."""
        with self._lock:
            raw = self._civ(bytes([0x25, 0x00]) + _bcd(int(hz)))
            fa = bytes([0xFE, 0xFE, CTRL, self.addr, 0xFA]) in raw
        if not fa:
            self.main_freq_hz = int(hz)
        return not fa

    def set_main_mode(self, mode_byte, filt=0x01):
        with self._lock:
            self._civ(bytes([0x06, mode_byte, filt]))

    def write_rx2(self, hz, settle=0.3):
        """Tune RX2 via the swap (07 B0 -> 25 00 <bcd> -> back). Returns True if
        no FA seen in the reply."""
        with self._lock:
            main = self.main_freq_hz
            self.swapping = True
            fa = True
            try:
                # verified toggle to RX2 (must land on a different band than MAIN)
                if self._swap_until(lambda f: main is None or _diff_band(f, main),
                                    settle):
                    raw = self._civ(bytes([0x25, 0x00]) + _bcd(int(hz)))
                    fa = bytes([0xFE, 0xFE, CTRL, self.addr, 0xFA]) in raw
                # verified restore to MAIN
                if main is not None:
                    self._swap_until(lambda f: f is not None and not _diff_band(f, main),
                                     settle)
                else:
                    self._civ(bytes([0x07, 0xB0]), settle)
            finally:
                self.swapping = False
                self.swap_ended_at = time.monotonic()
            if not fa:
                self.rx2_freq_hz = int(hz)
            return not fa

    def _poll_loop(self):
        # USB owns all freq/mode. Read MAIN OFTEN (fast, no swap -> slice 0
        # tracks the dial smoothly), and RX2 GENTLY (a swap every poll_s -> RX2
        # tracks live but the brief scope-blink is infrequent). Both safe over
        # USB (no scope stream on this channel).
        last_rx2 = 0.0
        while self._run:
            try:
                self.read_main()                             # fast, no swap
                now = time.monotonic()
                if now - last_rx2 >= self.poll_s:
                    self.read_rx2()                          # one swap
                    last_rx2 = now
            except Exception as e:
                self.last_err = str(e)
            # MAIN poll cadence ~0.4s; stop() stays responsive
            t = 0.0
            while self._run and t < 0.4:
                time.sleep(0.1); t += 0.1
