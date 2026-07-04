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
        # ONE coherent view of the radio, all sourced from the SELECTED VFO
        # (25 00 freq, 26 00 mode). The old code seeded from 03 (MAIN) but
        # tuned/polled via 25 00 (SELECTED) — when MAIN and SUB sit on
        # different bands (esp. 23cm after a main/sub swap) those are
        # DIFFERENT receivers, so the gate read one and drove the other and
        # fine-tune fell apart. Proven on HW 2026-07-02 (dev/ic9700_state):
        # 03 and 25 00 diverge constantly; 25 00/25 01/26 00 all read every
        # band (incl 23cm) correctly. So: SELECTED VFO is the single source
        # of truth for what AE sees and what we tune.
        self.freq_hz = None            # SELECTED vfo freq (25 00) — the authority
        self.mode = None               # SELECTED vfo mode (26 00)
        self.other_freq_hz = None      # UNSELECTED (SUB) vfo freq (25 01) — same-rx VFO B
        self.other_mode = None         # UNSELECTED (SUB) vfo mode (26 01) — same-rx VFO B
        self.dualwatch = None          # 07 D2: True/False = SUB receiver active (dual-slice)
        self.smeter_raw = None         # last CI-V 15 02 reading, 0..255
        # --- true SECOND RECEIVER (RX2) — reached ONLY via 07 B0 swap-read ---
        # 25 00/25 01 are VFO A/B of the SELECTED receiver (both belong to
        # whichever RX is MAIN); they do NOT reach RX2. Proven on HW
        # (dev/ic9700_rx2probe 2026-07-03): MAIN 437.191 / RX2 1270.0 — 07 B0
        # swaps which RX is selected, so `07 B0 -> read 25 00 -> 07 B0 back`
        # reads RX2 and restores MAIN cleanly (437.191 identical before/after).
        # These caches are refreshed ON-DEMAND only (connect + when AE tunes
        # slice B) — NEVER on the periodic poll, because the swap briefly yanks
        # the live scope to RX2 and back (would worsen the scope-stall).
        self.rx2_present = False       # a real 2nd receiver on a different band?
        self.rx2_freq_hz = None        # RX2 selected-VFO freq (via swap-read)
        self.rx2_mode = None           # RX2 selected-VFO mode
        self._reading_rx2 = False      # True while swapped to RX2 (reroutes 25/26 replies)
        self._civ_lock = threading.Lock()   # serialises swap sequences vs each other
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
        # Seed from the SELECTED VFO (25 00 freq + 26 00 mode), NOT 03/04:
        # 03/04 read MAIN, but we tune the SELECTED vfo — seeding from a
        # different receiver than we drive is the 23cm mismatch bug. Also
        # read the OTHER vfo (25 01) for the future dual-slice model.
        self._send_civ(bytes([0x25, 0x00]))
        self._send_civ(bytes([0x25, 0x01]))
        self._send_civ(bytes([0x26, 0x00]))
        self._send_civ(bytes([0x26, 0x01]))       # other-vfo mode (dual-slice)
        self._send_civ(bytes([0x07, 0xD2]))       # dualwatch on/off (dual-slice)

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
                # freq_hz + mode ALWAYS track the SELECTED VFO (25 00 / 26 00),
                # never MAIN-only 03/04 — see the state-model note in __init__.
                if cmd == 0x00 and len(data) >= 5:
                    # 00 = freq transceive broadcast; on the 9700 it reflects
                    # the operated (selected) VFO, so it's a valid selected read
                    self.freq_hz = _decode_bcd(data[:5])
                elif cmd == 0x25 and len(data) >= 6:
                    if data[0] == 0x00:
                        # While a swap-read has RX2 selected, 25 00 IS RX2's
                        # freq — route it to the RX2 cache, NOT MAIN's freq_hz
                        # (else the swap would corrupt slice 0 with RX2's band).
                        if self._reading_rx2:
                            self.rx2_freq_hz = _decode_bcd(data[1:6])
                        else:
                            self.freq_hz = _decode_bcd(data[1:6])   # SELECTED vfo
                    elif data[0] == 0x01:
                        self.other_freq_hz = _decode_bcd(data[1:6])  # same-rx VFO B
                elif cmd == 0x01 and len(data) >= 1:
                    # 01 = mode transceive (selected vfo)
                    if not self._reading_rx2:
                        self.mode = CIV_TO_MODE.get(data[0])
                elif cmd == 0x26 and len(data) >= 2:
                    # 26 00 = selected-vfo mode; 26 01 = other (SUB) vfo mode
                    if data[0] == 0x00:
                        if self._reading_rx2:
                            self.rx2_mode = CIV_TO_MODE.get(data[1])
                        else:
                            self.mode = CIV_TO_MODE.get(data[1])
                    elif data[0] == 0x01:
                        self.other_mode = CIV_TO_MODE.get(data[1])
                elif cmd == 0x07 and len(data) >= 2 and data[0] == 0xD2:
                    # dualwatch on/off — the trigger for the SUB slice
                    self.dualwatch = bool(data[1])
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

    # --- RX2 (true second receiver) swap-read / swap-write --------------------
    # RX2 is reached ONLY by swapping which receiver is MAIN (07 B0), reading
    # 25 00/26 00 (now RX2), then swapping back. Proven on HW (ic9700_rx2probe).
    # These BLOCK briefly (a couple of 07 B0 + a read) and hold _civ_lock, so
    # they must be called on-demand off the poll thread, NOT every tick.
    def swap_read_rx2(self, settle=0.35):
        """Swap to RX2, read its freq+mode into rx2_* caches, swap back to MAIN.
        Sets rx2_present True iff RX2 read a real ham freq on a DIFFERENT band
        than MAIN (a genuine second receiver, not RX1's own VFO B)."""
        with self._civ_lock:
            main_before = self.freq_hz
            self._send_civ(bytes([0x07, 0xB0]))            # MAIN <-> SUB swap
            time.sleep(settle)
            self._reading_rx2 = True
            self.rx2_freq_hz = self.rx2_mode = None
            self._send_civ(bytes([0x25, 0x00])); time.sleep(settle / 2)
            self._send_civ(bytes([0x26, 0x00])); time.sleep(settle / 2)
            self._reading_rx2 = False
            self._send_civ(bytes([0x07, 0xB0]))            # swap back to MAIN
            time.sleep(settle)
            o = self.rx2_freq_hz
            m = main_before
            self.rx2_present = bool(o and o > 1_000_000
                                    and (m is None or abs(o - m) > 1_000_000))
            return self.rx2_present

    def write_rx2_freq(self, hz, settle=0.35):
        """Tune RX2 (the real 2nd receiver) via the swap: 07 B0 -> 25 00 <bcd>
        -> 07 B0 back. Updates the rx2_freq_hz cache optimistically so the next
        receivers() read doesn't snap AE back. Returns True if not FA'd."""
        with self._civ_lock:
            self._send_civ(bytes([0x07, 0xB0]))            # swap RX2 -> selected
            time.sleep(settle)
            fa0 = self.n_fa
            self._send_civ(bytes([0x25, 0x00]) + _encode_bcd(int(hz)))
            time.sleep(settle)
            ok = self.n_fa == fa0
            self._send_civ(bytes([0x07, 0xB0]))            # swap back to MAIN
            time.sleep(settle)
            if ok:
                self.rx2_freq_hz = int(hz)                 # optimistic cache
            return ok

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
                 serial="GATE9700", station="Aether-gate IC-9700"):
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
        # ANY failure in here MUST tear the session down cleanly, or the radio
        # is left holding a phantom RS-BA1 session that refuses the next login
        # ("comes up then swaps to the other radio after 2-3 s"). open() is
        # called OUTSIDE __main__'s try/finally, and the connect-failed path
        # below used to raise without calling close() — so the 0x05 disconnect
        # never went out. Wrap the whole thing: on any exception, close()
        # (which sends 0x05 + flushes) then re-raise. close() already no-ops on
        # the None halves, so a half-built session cleans up fine.
        try:
            self._open()
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _open(self):
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
        # The CIV bring-up can race the stream handshake, and a glitched
        # session start can poison the tracked-seq layer: the radio then
        # drops every packet WE send while its own streams keep flowing
        # (scope frames arrive because the scope was left enabled by a
        # previous session — so frames alone prove nothing). Health =
        # a reply to OUR OWN freq read. SDR9700 retries its bring-up on a
        # timer for the same reason; if replies never come, refuse to open
        # — the caller/launcher retries after the radio's stale window.
        end = time.monotonic() + 12.0
        while time.monotonic() < end and self._civ.freq_hz is None:
            time.sleep(1.0)
            if self._civ.freq_hz is None:
                print("[civ] no reply to our reads yet - re-firing bring-up", flush=True)
                self._civ._on_iamready()
        if self._civ.freq_hz is None:
            # teardown is handled by open()'s wrapper (which sends 0x05 + flushes)
            raise RuntimeError("IC-9700 accepted the session but ignores our "
                               "commands (poisoned seq layer / stale session) - "
                               "wait ~40s and retry")
        print(f"[civ] stream healthy (freq={self._civ.freq_hz/1e6:.4f} MHz, "
              f"{self._civ.frames} scope frames)", flush=True)
        # ONE-TIME RX2 swap-probe: is there a real 2nd receiver on a different
        # band? (07 B0 -> read -> back). On-demand model: we detect RX2 here and
        # then only re-read it when AE tunes slice B — never on the poll, so the
        # scope isn't disrupted. (dev/ic9700_rx2probe proved the recipe + clean
        # swap-back.)
        try:
            if self._civ.swap_read_rx2():
                print(f"[civ] RX2 present @ {self._civ.rx2_freq_hz/1e6:.4f} MHz "
                      f"{self._civ.rx2_mode} (2nd receiver)", flush=True)
        except Exception as e:
            print(f"[civ] RX2 probe skipped: {e}", flush=True)

    def close(self):
        # stop() on each stream sends the 0x05 disconnect the radio needs to
        # release its session (else it lingers as a phantom and blocks the next
        # login). stop() sets _run=False and fires 0x05 as one best-effort UDP
        # datagram (same single-shot the SDR9700 reference does in its dtor) —
        # give it a beat to actually leave the socket before the daemon threads
        # wind down, so a fast process exit can't swallow the disconnect.
        for obj in (self._civ, self._handler):
            try:
                if obj:
                    obj.stop()
            except Exception:
                pass
        time.sleep(0.3)

    def needs_reconnect(self):
        """True when the deaf-session watchdog wants a full CI-V reconnect."""
        return bool(getattr(self, "_want_reconnect", False))

    def reconnect(self):
        """Tear down + re-establish the CI-V session after it went deaf mid-run.
        The radio needs its ~15-25 s stale window to clear, so retry patiently.
        Called by the engine off the stream/poll thread (open() blocks)."""
        print("[civ] reconnecting the radio session...", flush=True)
        self._want_reconnect = False
        try:
            self.close()
        except Exception:
            pass
        self._civ = self._handler = None
        for attempt in range(4):
            time.sleep(30.0)                               # let the radio's stale session fully age out
                                                           # (tonight: needs ≥30s untouched to accept a fresh session)
            try:
                self.open()                                # re-auth + civ + health gate
                self._wd_freq_t = time.monotonic()         # reset the watchdog clock
                print("[civ] reconnect OK", flush=True)
                return True
            except Exception as e:
                print(f"[civ] reconnect attempt {attempt+1} failed: {e}", flush=True)
        print("[civ] reconnect gave up after 4 tries", flush=True)
        return False

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
        # THROTTLED + STAGGERED polling. The old code flooded the 9700's CI-V
        # (S-meter at 10 Hz + 5 reads/sec bursting together + the scope
        # watchdog re-firing 3 cmds/sec) → ~18 msg/s wedged the RS-BA1 session
        # within seconds (deaf session). The radio can't sustain that. Now:
        # ONE read per ~0.4 s tick in round-robin, S-meter ~2.5 Hz, so the
        # sustained CI-V rate is a few msg/s the radio tolerates.
        if now - self._smeter_sent_at >= 1.0:
            self._civ.poll_smeter()                        # 15 02 (S-meter) ~1 Hz
            self._smeter_sent_at = now
        if now - self._freq_polled_at >= 1.0:
            self._freq_polled_at = now
            # round-robin ONE read per tick (not a 5-read burst): full cycle
            # of freq/mode/sub every ~2 s — plenty fresh for dial/band sync.
            READS = (bytes([0x25, 0x00]),   # selected freq (authority)
                     bytes([0x26, 0x00]),   # selected mode
                     bytes([0x25, 0x01]),   # SUB freq
                     bytes([0x26, 0x01]),   # SUB mode
                     bytes([0x07, 0xD2]))   # dualwatch reg
            i = getattr(self, "_read_rr", 0)
            self._civ._send_civ(READS[i % len(READS)])
            self._read_rr = i + 1

            # WATCHDOG — liveness is measured by SCOPE FRAME PROGRESS, NOT by
            # whether the freq VALUE changed. (Bug 2026-07-02: the old check
            # reset its timer only when freq_hz *changed*, so a STATIONARY rig —
            # freq never changing — looked "deaf" after 8 s and we tore down a
            # perfectly healthy session ourselves. Pings/echo were fine the
            # whole time; the session was never actually deaf.) The scope
            # streams continuously while the session is alive; frozen frames =
            # real trouble.
            frames = self._civ.frames
            if frames != getattr(self, "_wd_frames_last", -1):
                self._wd_frames_last = frames
                self._wd_frames_t = now                    # scope is flowing = session alive
            frozen_for = now - getattr(self, "_wd_frames_t", now)
            if 3.0 <= frozen_for < 10.0 \
                    and now - getattr(self, "_scope_reenable_t", 0) >= 4.0:
                self._civ.enable_scope()                   # tier 1: nudge the scope
                self._scope_reenable_t = now
                print("[scope] frames stalled -> re-enabling", flush=True)
            elif frozen_for >= 12.0 and not getattr(self, "_want_reconnect", False):
                self._want_reconnect = True                # tier 2: session deaf -> reconnect
                print(f"[civ] scope frozen {frozen_for:.0f}s "
                      f"-> requesting reconnect", flush=True)
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

    def receivers(self):
        """The active receivers as {freq_hz, mode}: MAIN first (slice 0), RX2
        second (slice 1) when a real 2nd receiver is present. ORDERED now — RX2
        is identified reliably via the 07 B0 swap-read (rx2_* cache), so the old
        continuity-matching guesswork against the ambiguous 25 01 is gone.

        RX2 comes from the CACHE (refreshed at connect + when AE tunes slice B),
        NOT a live read — so this is cheap to call every poll and never disturbs
        the scope. The engine still stacks slice 1 on its own pan."""
        if not self._civ:
            return []
        out = [{"freq_hz": self._civ.freq_hz, "mode": self._civ.mode}]
        if self.sub_active():
            out.append({"freq_hz": self._civ.rx2_freq_hz, "mode": self._civ.rx2_mode})
        return [r for r in out if r["freq_hz"]]

    # --- dual-receiver (RX2) — drives the second slice -------------------
    def sub_active(self):
        """True when a real 2nd RECEIVER (RX2) is present — established by the
        07 B0 swap-probe (rx2_present), NOT by 25 01 (which is RX1's own VFO B
        and made slice 1 collapse onto RX1 after a few seconds — the bug this
        fixes). Proven on HW (dev/ic9700_rx2probe): MAIN 437.191 / RX2 1270.0."""
        return bool(self._civ and self._civ.rx2_present and self._civ.rx2_freq_hz)

    def sub_freq_hz(self):
        return self._civ.rx2_freq_hz if self._civ else None

    def sub_mode(self):
        return self._civ.rx2_mode if self._civ else None

    def set_sub_freq_hz(self, hz):
        """Tune the REAL 2nd receiver (RX2) via the 07 B0 swap — NOT 25 01 (that
        tuned RX1's VFO B, so AE's slice-B tune reverted). The swap-write blocks
        briefly, so run it off-thread to keep the engine command path free; it
        updates the rx2_freq_hz cache optimistically so receivers() won't snap
        AE back before the write lands."""
        civ = self._civ
        if not civ:
            return
        civ.rx2_freq_hz = int(hz)                          # optimistic (pre-write)
        threading.Thread(target=civ.write_rx2_freq, args=(int(hz),),
                         daemon=True, name="ic9700-rx2-tune").start()

    # --- diagnostics: 'what the gate sees from the radio' (web panel) -----
    @staticmethod
    def _smeter_dbm(raw):
        if raw is None:
            return None
        return (-147.0 + raw * (54.0 / 120.0)) if raw <= 120 \
            else (-93.0 + (raw - 120) * (60.0 / 121.0))

    @staticmethod
    def _s_unit(dbm):
        if dbm is None:
            return None
        if dbm >= -93.0:
            return f"S9+{round(dbm + 93):d}"
        return f"S{max(0, round(9 + (dbm + 93) / 6.0)):d}"

    def diagnostics(self):
        civ = self._civ
        h = self._handler
        authed = bool(h and h.authenticated.is_set())
        # scope fps: frames counted since last call / elapsed
        fps = None
        if civ is not None:
            now = time.monotonic()
            last_t = getattr(self, "_diag_t", None)
            last_n = getattr(self, "_diag_n", 0)
            if last_t is not None and now > last_t:
                fps = round((civ.frames - last_n) / (now - last_t), 1)
            self._diag_t, self._diag_n = now, civ.frames

        sel_dbm = self._smeter_dbm(civ.smeter_raw) if civ else None
        # NB these are the two VFOs (A/B) of the SELECTED receiver — NOT the two
        # RECEIVERS. 25 00 = active VFO, 25 01 = the other VFO of the same rx.
        # The 2nd RECEIVER (RX2) is only reachable via a 07 B0 main/sub swap
        # (proven ic9700_findrx2); it does NOT appear in 25 00/25 01. (Earlier
        # this was mislabelled MAIN/SUB — corrected 2026-07-02.)
        vfos = []
        if civ:
            # RX1 (MAIN) selected VFO — what slice 0 shows
            vfos.append({"name": "RX1 (MAIN)", "freq_hz": civ.freq_hz,
                         "mode": civ.mode, "selected": True})
            # RX2 — the true 2nd receiver, from the 07 B0 swap-read cache (slice 1)
            if civ.rx2_present and civ.rx2_freq_hz:
                vfos.append({"name": "RX2 (SUB)", "freq_hz": civ.rx2_freq_hz,
                             "mode": civ.rx2_mode, "selected": False})
        return {
            "radio": "IC-9700",
            "presented_as": self.capabilities.model,
            "link": {"transport": "Icom RS-BA1 / CI-V LAN",
                     "host": f"{self.radio_ip}:{self.radio_port}",
                     "state": "authenticated" if authed else "connecting",
                     "civ_port": (h.civ_port if h else None),
                     "audio_port": (h.audio_port if h else None),
                     "token": (f"0x{h.token:08x}" if h and h.token else None)},
            "vfos": vfos,
            "meters": {"s_meter_dbm": (round(sel_dbm, 1) if sel_dbm is not None else None),
                       "s_unit": self._s_unit(sel_dbm),
                       "raw": (civ.smeter_raw if civ else None)},
            "scope": {"fps": fps,
                      "live": (fps is not None and fps > 0.5),
                      "bins": (len(civ.latest_dbm) if civ and civ.latest_dbm else None),
                      "total_frames": (civ.frames if civ else 0)},
            "flags": {"sub_receiver": (self.sub_active() if civ else False),
                      "dualwatch_reg": bool(civ.dualwatch) if civ else False},
            "counters": {"tune_ok": (civ.n_fb if civ else 0),
                         "tune_refused": (civ.n_fa if civ else 0)},
        }
