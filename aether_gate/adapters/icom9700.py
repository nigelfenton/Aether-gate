#
# Aether-gate - Icom9700Adapter: present an IC-9700 (LAN/CI-V) to AE as a Flex.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Built on the icom/ LAN transport ported from github.com/w5jwp/SDR9700 (GPL-3.0).
#
"""RadioAdapter for the Icom IC-9700 over its LAN interface (provides="spectrum").

Pipeline: handler.py authenticates + opens the CI-V stream; civ.py subscribes to
the band scope (CI-V 27h) and exposes latest_dbm. This adapter:
  * get_spectrum(ctx,t) -> resamples the latest scope sweep to ctx.n dBm bins,
  * retune(center_hz)   -> CI-V set-frequency (cmd 05) so AE's tuning moves the rig
    (in-band only: the 9700 FAs out-of-ham-band freq sets),
  * set_mode(mode)      -> CI-V set-mode (cmd 06),
  * set_span(span_hz)   -> AE pan zoom follows onto the rig's scope span,
  * read_meters()       -> S-meter (CI-V 15 02, VHF S9 = -93 dBm mapping).

Scope path live-proven 2026-07-01 (HT key-up on 146.52: raw 53 at RF-gain 0, pegged
at gain max). NB the waveform floor is -130 dBm/pixel and the 9700 SCALES scope
data by RF gain — a quiet band with gain low legitimately reads all-zero.
"""
import threading
import time

from ..core.engine import local_ip as _local_ip
from .base import RadioAdapter, AdapterCaps, Meters
from .icom.handler import Ic9700Handler
from .icom.civ import Ic9700Civ, CONTROLLER_CIV

MODE_TO_CIV = {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03, "RTTY": 0x04,
               "FM": 0x05, "CW-R": 0x06, "RTTY-R": 0x07, "DV": 0x08, "FM-N": 0x12}
CIV_TO_MODE = {v: k for k, v in MODE_TO_CIV.items()}


def _encode_bcd(hz):
    """Hz -> 5 CI-V BCD bytes, LSB digit-pair first."""
    out = bytearray()
    hz = int(hz)
    for _ in range(5):
        lo = hz % 10; hz //= 10
        hi = hz % 10; hz //= 10
        out.append((hi << 4) | lo)
    return bytes(out)


def _decode_bcd(b):
    f, mult = 0, 1
    for byte in b:
        f += (byte & 0x0F) * mult; mult *= 10
        f += (byte >> 4) * mult; mult *= 10
    return f


def _resample(src, n):
    """Nearest-neighbour resample a dBm list to exactly n bins."""
    m = len(src)
    if m == n:
        return list(src)
    return [src[(i * m) // n] for i in range(n)]


class _Ic9700Stream(Ic9700Civ):
    """One CI-V stream doing BOTH scope (inherited) and control (added here)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._dispatch
        self.freq_hz = None
        self.mode = None
        self.smeter_raw = None         # last CI-V 15 02 reading, 0..255
        self._tune_target = None       # latest AE-requested freq (tuner thread chases it)
        self._tune_evt = threading.Event()
        self._tuner = None

    def _on_iamready(self):
        # open the data stream, enable the scope (on + output + MAIN), then
        # FAST sweep (radio was found on SLOW = ~4 waveform frames/s) and a
        # known ±500 kHz span so the advertised pan axis starts truthful;
        # finally read current freq+mode
        self._send_openclose(opening=True)
        self.enable_scope()
        self.set_speed(0)
        self.set_span(500_000)
        self._send_civ(bytes([0x03]))
        self._send_civ(bytes([0x04]))

    def _dispatch(self, d):
        if d.find(b"\x27\x00\x00") >= 0:        # scope waveform frame
            self._on_civ(d)                     # (no early return: a datagram can
                                                # carry control replies alongside)
        # control CI-V: replies to us (fe fe E0 a2 ...) AND transceive
        # broadcasts (fe fe 00 a2 ...) the rig sends when its dial/mode is
        # changed at the front panel — that's how the radio drives AE.
        i = d.find(b"\xfe\xfe")
        while i >= 0:
            end = d.find(b"\xfd", i)
            if end < 0:
                break
            f = d[i:end + 1]
            if len(f) >= 6 and f[2] in (CONTROLLER_CIV, 0x00):
                cmd, data = f[4], f[5:-1]
                if cmd in (0x00, 0x03) and len(data) >= 5:
                    # 03 = freq reply; 00 = freq transceive (dial turned)
                    self.freq_hz = _decode_bcd(data[:5])
                elif cmd == 0x25 and len(data) >= 6 and data[0] == 0x00:
                    self.freq_hz = _decode_bcd(data[1:6])   # selected-VFO read
                elif cmd in (0x01, 0x04) and len(data) >= 1:
                    # 04 = mode reply; 01 = mode transceive
                    self.mode = CIV_TO_MODE.get(data[0])
                elif cmd == 0x15 and len(data) >= 3 and data[0] == 0x02:
                    # S-meter reply: 15 02 <2-byte BCD 0000-0255>
                    self.smeter_raw = (data[1] >> 4) * 1000 + (data[1] & 0xF) * 100 + \
                                      (data[2] >> 4) * 10 + (data[2] & 0xF)
                elif cmd == 0xFB:
                    self.n_fb += 1
                elif cmd == 0xFA:
                    self.n_fa += 1
            i = d.find(b"\xfe\xfe", end)

    def _try_freq(self, hz, settle=0.25):
        """Selected-VFO tune (25 00). Returns False if the radio FA'd it."""
        fa0 = self.n_fa
        self._send_civ(bytes([0x25, 0x00]) + _encode_bcd(hz))
        time.sleep(settle)
        return self.n_fa == fa0

    def set_freq_hz(self, hz):
        # ASYNC + COALESCING: AE streams slice tunes while dragging, and the
        # cross-band recipe needs FA-check waits — blocking the engine's
        # command thread here seized the whole gate (2026-07-01 lockup).
        # Record the latest target and let the tuner thread chase it.
        self._tune_target = int(hz)
        print(f"[tuner] target <- {hz/1e6:.5f} MHz", flush=True)
        if self._tuner is None or not self._tuner.is_alive():
            self._tuner = threading.Thread(target=self._tuner_loop, daemon=True,
                                           name="ic9700-tuner")
            self._tuner.start()
            print("[tuner] thread started", flush=True)
        self._tune_evt.set()

    def _tuner_loop(self):
        # Chases self._tune_target; intermediate drag positions coalesce away.
        # Cross-band recipe proven on HW 2026-07-01 (dev/ic9700_xband2):
        # 25 00 tunes same-band and any UNHELD band; a band parked on the
        # SUB receiver is refused (FA) — swap main/sub (07 B0) and retry.
        print(f"[tuner] loop running (_run={self._run})", flush=True)
        while self._run:
            self._tune_evt.wait(timeout=0.5)
            self._tune_evt.clear()
            tgt = self._tune_target
            if tgt is None:
                continue
            # A target is chased ONCE and then cleared — a lingering setpoint
            # must never fight the user's hand on the rig's dial (2026-07-01:
            # the stale target kept snapping the rig back after every dial turn).
            if self.freq_hz is not None and abs(self.freq_hz - tgt) < 1:
                if self._tune_target == tgt:
                    self._tune_target = None
                continue
            ok = self._try_freq(tgt)
            print(f"[tuner] 25 00 {tgt/1e6:.5f} -> {'FB' if ok else 'FA'}", flush=True)
            if ok:
                self.freq_hz = tgt
                if self._tune_target == tgt:
                    self._tune_target = None    # done — release the rig
                continue
            if self._tune_target != tgt:
                continue                        # target moved on — chase that instead
            print("[tuner] band held by SUB -> 07 B0 swap + retry", flush=True)
            self._send_civ(bytes([0x07, 0xB0]))  # swap main/sub, then retry
            time.sleep(0.4)
            if self._try_freq(tgt):
                self.freq_hz = tgt
                print(f"[tuner] tuned {tgt/1e6:.5f} after swap", flush=True)
            if self._tune_target == tgt:
                self._tune_target = None        # chased once, win or lose — never re-fight

    def set_mode_civ(self, mode_byte, filt=0x01):
        self._send_civ(bytes([0x06, mode_byte, filt]))

    def poll_smeter(self):
        self._send_civ(bytes([0x15, 0x02]))


class Icom9700Adapter(RadioAdapter):
    """IC-9700 LAN adapter. RX panadapter (scope) + freq/mode control."""

    provides = "spectrum"

    def __init__(self, radio_ip, username, password, local_ip=None,
                 radio_port=50001, civ_addr=0xA2, model="FLEX-6700",
                 serial="GATE9700", station="aether-gate 9700"):
        # FLEX-6700 is the only Flex model that covers 2m (~135-165 MHz), so AE will
        # offer the IC-9700's 2m band. (6300/6400/6600 = HF+6m only.) 70cm/23cm still
        # need frequency translation - no Flex covers them.
        self.radio_ip = radio_ip
        self.username = username
        self.password = password
        self.local_ip = local_ip
        self.radio_port = radio_port
        self.civ_addr = civ_addr
        # IC-9700 covers 2m/70cm/23cm; RX-only here (no TX/PTT wired -> never keys the rig).
        # Span honesty: the 9700 scope does ±2.5k..±500k -> pan width 5 kHz..1 MHz;
        # don't let AE zoom the axis past what the scope can actually show.
        # bands: AE BandDefs vocabulary — the registry's "70cm" is AE's "440".
        # With a radio-declared-bands AE the menu offers exactly these three;
        # older AE ignores the key and falls back to the FLEX-6700's 2m.
        self.capabilities = AdapterCaps(model=model, serial=serial, station=station,
                                        tx_capable=False,
                                        min_span_hz=5_000.0, max_span_hz=1_000_000.0,
                                        bands=("2m", "440", "23cm"))
        self._handler = None
        self._civ = None
        self._span_half_hz = 500_000      # what the scope is set to (± half-width)
        self._span_sent_at = 0.0
        self._smeter_sent_at = 0.0        # rate-limit the 15 02 poll (10 Hz like SDR9700)
        self._freq_polled_at = 0.0        # slow freq poll (dial-sync when transceive is off)

    def open(self):
        lip = self.local_ip or _local_ip()
        self.local_ip = lip
        self._handler = Ic9700Handler(lip, self.radio_ip, self.radio_port,
                                      self.username, self.password)
        if not self._handler.connect(timeout=10.0):
            raise RuntimeError(f"IC-9700 connect failed: {self._handler._fail!r} "
                               f"(authed={self._handler.authenticated.is_set()})")
        self._civ = _Ic9700Stream(lip, self.radio_ip, self._handler.civ_port,
                                  self._handler._civ_sock, self.civ_addr)
        self._civ.start()

    def close(self):
        for obj in (self._civ, self._handler):
            try:
                if obj:
                    obj.stop()
            except Exception:
                pass

    # The rig's real coverage; AE can ask for anything (e.g. its restored
    # 40m profile at connect) — chasing an out-of-range target just earns
    # an FA from the radio, so drop it here and let the radio->AE dial
    # sync snap AE back to where the rig actually is.
    BAND_RANGES_MHZ = ((144.0, 148.0), (420.0, 450.0), (1240.0, 1300.0))

    # --- control (AE -> radio) -----------------------------------------
    def retune(self, center_hz):
        if not self._civ:
            return
        mhz = center_hz / 1e6
        if not any(lo <= mhz <= hi for lo, hi in self.BAND_RANGES_MHZ):
            print(f"[tuner] ignoring out-of-range target {mhz:.4f} MHz "
                  f"(rig covers 2m/70cm/23cm)", flush=True)
            return
        self._civ.set_freq_hz(int(center_hz))

    def set_mode(self, mode):
        mb = MODE_TO_CIV.get((mode or "").upper())
        if mb is not None and self._civ:
            self._civ.set_mode_civ(mb)

    def set_span(self, span_hz):
        """Follow AE's pan zoom: full width -> nearest Icom ± half-width setting."""
        if not self._civ:
            return
        half = span_hz / 2.0
        want = min(Ic9700Civ.SPANS_HZ, key=lambda s: abs(s - half))
        now = time.monotonic()
        if want == self._span_half_hz or now - self._span_sent_at < 0.5:
            return                          # unchanged, or rate-limit zoom drags
        self._civ.set_span(want)
        self._span_half_hz = want
        self._span_sent_at = now

    # --- spectrum (radio -> AE) ----------------------------------------
    def get_spectrum(self, ctx, t):
        dbm = self._civ.latest_dbm if self._civ else None
        if not dbm:
            return [ctx.floor] * ctx.n          # flat floor until scope produces pixels
        return _resample(dbm, ctx.n)

    def read_meters(self):
        # Async poll: send the read (rate-limited to 10 Hz — the engine calls
        # per frame), return the last parsed value; one poll behind is fine
        # for a meter. None (no data yet) -> engine falls back to spectrum.
        if not self._civ:
            return None
        now = time.monotonic()
        if now - self._smeter_sent_at >= 0.1:
            self._civ.poll_smeter()
            self._smeter_sent_at = now
        if now - self._freq_polled_at >= 1.0:
            # keep freq_hz fresh even when the rig's CI-V transceive is off,
            # so the dial drives AE (see engine radio->AE sync)
            self._civ._send_civ(bytes([0x03]))
            self._freq_polled_at = now
        raw = self._civ.smeter_raw
        if raw is None:
            return None
        # Icom S-meter: 0=S0, 120=S9, 241=S9+60dB. VHF convention S9 = -93 dBm
        # (6 dB/S-unit -> S0 = -147), then linear dB above S9.
        if raw <= 120:
            dbm = -147.0 + raw * (54.0 / 120.0)
        else:
            dbm = -93.0 + (raw - 120) * (60.0 / 121.0)
        return Meters(s_meter_dbm=dbm)

    def initial_center_hz(self):
        # The freq read (CI-V 03) is fired at stream-open; its reply lands
        # asynchronously — wait briefly so the engine can seed AE on the
        # rig's real band instead of the sim default.
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            if self._civ and self._civ.freq_hz:
                return float(self._civ.freq_hz)
            time.sleep(0.1)
        return None

    def initial_mode(self):
        return self._civ.mode if self._civ else None

    # --- radio -> AE (engine polls these each second) --------------------
    def radio_freq_hz(self):
        return self._civ.freq_hz if self._civ else None

    def radio_mode(self):
        return self._civ.mode if self._civ else None
