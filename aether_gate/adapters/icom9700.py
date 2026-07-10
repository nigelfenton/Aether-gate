#
# Aether-gate - Icom9700Adapter: present an IC-9700 (LAN/CI-V) to AE as a Flex.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Built on the icom/ LAN transport ported from github.com/w5jwp/SDR9700 (GPL-3.0) by Justin W5JWP.
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
import os
import threading
import time

from ..core.engine import local_ip as _local_ip
from .base import RadioAdapter, AdapterCaps, Meters
from .icom.handler import Ic9700Handler
from .icom.civ import Ic9700Civ, CONTROLLER_CIV
from .icom.audio import Ic9700Audio, RADIO_RATE
from .icom.radios import _2M, _70CM, _23CM

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

    # SCOPE-ONLY MODE: in the hybrid, the USB channel is the SOLE CI-V master
    # for freq/mode (so the two masters don't corrupt each other's reads on the
    # radio's single CI-V state). When set, the LAN channel sends ONLY scope
    # traffic — no freq/mode reads. Set by the adapter when a USB channel is up.
    scope_only = False

    def _on_iamready(self):
        # open the data stream + enable the scope (on + output + MAIN), FAST
        # sweep + ±500 kHz span so the advertised pan axis starts truthful.
        self._ready_seen = True           # arm Ic9700Civ._on_tick's open-retry
        self._send_openclose(opening=True)
        self.enable_scope()
        self.set_speed(0)
        self.set_span(500_000)
        if self.scope_only:
            return                        # USB owns freq/mode — send NO reads here
        # (LAN-only mode) seed from the SELECTED VFO (25 00 / 26 00) ONLY.
        # The SUB reads (25 01 / 26 01 / 07 D2 dualwatch) are deliberately gone:
        # they fed ONLY an unused diagnostics field (leftover from the parked
        # dual-RX work — receivers() doesn't use them) AND they PROVOKE the radio's
        # scope stall earlier. Dropping them raised the freeze threshold ~1860 ->
        # ~2650 frames (+42%) and killed the extra short freezes (measured on the
        # Pi 2026-07-07). See the deaf-scope notes.
        self._send_civ(bytes([0x25, 0x00]))
        self._send_civ(bytes([0x26, 0x00]))

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
        # Chases self._tune_target for MAIN (25 00). Intermediate drag positions
        # coalesce away.
        # ⚠ SINGLE-SWAPPER RULE: the LAN channel must NEVER issue 07 B0. The swap
        # is GLOBAL to the radio (proven 2026-07-03) — two masters swapping (LAN
        # tuner + USB RX2) collide and tangle MAIN/RX2. So ALL swaps live on the
        # USB channel now. If a MAIN tune is FA'd because the target band is
        # parked on the SUB receiver, the LAN tuner simply gives up (cross-band
        # tuning is the USB channel's job); it does NOT swap.
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
            print(f"[tuner] 25 00 {tgt/1e6:.5f} -> {'FB' if ok else 'FA (no swap; USB owns cross-band)'}", flush=True)
            if ok:
                self.freq_hz = tgt
            if self._tune_target == tgt:
                self._tune_target = None        # chased once, win or lose — never re-fight, never swap

    def set_mode_civ(self, mode_byte, filt=0x01):
        self._send_civ(bytes([0x06, mode_byte, filt]))

    def poll_smeter(self):
        self._send_civ(bytes([0x15, 0x02]))


class Icom9700Adapter(RadioAdapter):
    """IC-9700 LAN adapter. RX panadapter (scope) + freq/mode control."""

    provides = "spectrum"

    def __init__(self, radio_ip, username, password, local_ip=None,
                 radio_port=50001, civ_addr=0xA2, model="FLEX-6700",
                 serial="GATE9700", station="Icom-IC-9700",
                 usb_civ_port=None, usb_civ_baud=115200):
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
        # bands: from the shared band constants (their wire names are AE BandDefs
        # vocabulary — _70CM declares "440"). With a radio-declared-bands AE the
        # menu offers exactly these three; older AE ignores the key and falls back
        # to the FLEX-6700's 2m.
        _bands = tuple(b.name for b in (_2M + _70CM + _23CM))   # ("2m","440","23cm")
        self.capabilities = AdapterCaps(model=model, serial=serial, station=station,
                                        tx_capable=False,
                                        min_span_hz=5_000.0, max_span_hz=1_000_000.0,
                                        bands=_bands)
        self._handler = None
        self._civ = None
        self._audio = None             # LAN RX-audio session (Ic9700Audio)
        # HYBRID RX2: optional USB CI-V channel. RX2 needs the 07 B0 swap, which
        # is destructive over LAN (yanks the scope) but HARMLESS over USB (no
        # scope stream) — proven 5/5 on HW 2026-07-03. When a USB CI-V port is
        # given, RX2 is read/controlled over USB while the LAN channel keeps
        # MAIN's waterfall. Without it, dual-RX stays off (MAIN-only).
        self.usb_civ_port = usb_civ_port
        self.usb_civ_baud = usb_civ_baud
        self._usb = None
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
        # LAN RX AUDIO: the handler negotiated a 48 kHz LPCM16 RX-audio stream in
        # conninfo (rxenable=1) and the radio assigned an audio port; bring up the
        # audio session (its own are-you-there handshake, like the CI-V stream) so
        # the radio actually streams audio. get_audio() then feeds it to AE. If it
        # can't come up, the gate still runs RX/scope — audio just stays silent.
        if self._handler.audio_port and self._handler._audio_sock is not None:
            try:
                self._audio = Ic9700Audio(lip, self.radio_ip, self._handler.audio_port,
                                          self._handler._audio_sock)
                self._audio.start()
                print(f"[audio] LAN RX-audio session up on port "
                      f"{self._handler.audio_port}", flush=True)
            except Exception as e:
                self._audio = None
                print(f"[audio] RX-audio bring-up failed: {e} (RX stays silent)", flush=True)
        # HYBRID RX2 over USB — if a USB CI-V port was given, bring it up. The
        # 07 B0 swap to reach RX2 is harmless here (no scope stream on USB), so
        # its background poller tracks RX2 live without touching the LAN scope.
        if self.usb_civ_port and self._usb is None:        # skip if already up (reconnect)
            from .icom.usbciv import UsbCiv
            self._usb = UsbCiv(self.usb_civ_port, self.usb_civ_baud, self.civ_addr)
            if self._usb.start():
                print(f"[usb] RX2 channel up on {self.usb_civ_port} "
                      f"(MAIN={(self._usb.main_freq_hz or 0)/1e6:.4f}); "
                      f"LAN -> scope-only", flush=True)
            else:
                print(f"[usb] RX2 channel FAILED: {self._usb.last_err} "
                      f"-> MAIN-only", flush=True)
                self._usb = None
        # HAND OFF ALL freq/mode CI-V to USB whenever the USB channel is up
        # (also on a LAN reconnect, where _civ is fresh): the LAN channel goes
        # scope-only so the two CI-V masters don't corrupt each other's reads on
        # the radio's single CI-V state (USB-alone RX2 read is flawless; LAN CI-V
        # traffic breaks it).
        if self._usb and self._civ:
            self._civ.scope_only = True
        # DUAL-RX (RX2) over the LAN swap is PARKED — MAIN-only unless USB above. The 07 B0 swap-read that
        # reaches RX2 physically moves the operating receiver AND races the
        # radio's transceive broadcasts (cmd 0x00), which corrupted MAIN's freq
        # (slice A/B swapped) and worsened the scope-deaf sessions on a live
        # gate (seen 2026-07-03). RX2 has NO waterfall anyway (only MAIN streams),
        # so the swap buys little for RX. The swap_read_rx2()/write_rx2_freq()
        # machinery is left in place (proven in isolation by dev/ic9700_rx2probe)
        # for a future dual-RX/Doppler project, but is NOT invoked live. See
        # sub_active() -> False. Do NOT re-enable without solving: (a) guard the
        # cmd 0x00 transceive broadcast under _reading_rx2, (b) a swap cadence
        # that doesn't stress the scope stream.

    def _close_lan(self):
        # Stop ALL the LAN halves (scope + audio + handler). stop() sends the 0x05
        # disconnect the radio needs to release its RS-BA1 session. Leaves the
        # USB CI-V channel untouched (used by reconnect()). Closing the audio
        # session here also stops the per-reconnect socket leak (each _open bound
        # fresh civ/audio sockets; without this they piled up unread).
        for obj in (self._civ, self._audio, self._handler):
            try:
                if obj:
                    obj.stop()
            except Exception:
                pass
        self._audio = None
        time.sleep(0.3)

    def close(self):
        # Full shutdown: LAN (0x05 disconnect) + the USB CI-V channel.
        if self._usb:
            try: self._usb.stop()
            except Exception: pass
            self._usb = None
        self._close_lan()

    def needs_reconnect(self):
        """True when the deaf-session watchdog wants a full CI-V reconnect."""
        return bool(getattr(self, "_want_reconnect", False))

    # Backoff BEFORE each reconnect attempt (seconds). Attempt 1 = 0 = IMMEDIATE.
    # WHY: the deaf-scope stall is the radio pausing its 27h scope output while the
    # SESSION STAYS ALIVE (proven by pcap 2026-07-07: rx_dgrams keeps climbing, radio
    # keeps pinging). Only a FRESH session restarts scope output — but the capture
    # shows the radio accepts a new session ~1 MILLISECOND after our 0x05 disconnect
    # (are-you-there -> i-am-here, instant). The old unconditional sleep(30) before
    # attempt 1 was cargo-culted from the SEPARATE phantom-session/auth-wedge cases
    # (see the recovery playbook) — it made every stall a ~30 s dead waterfall for
    # nothing. So: try immediately; only wait if a retry is actually needed (a
    # genuinely wedged session does still want the stale window to age out).
    _RECONNECT_BACKOFF_S = (0.0, 2.0, 8.0, 20.0)
    # Minimum gap between session recycles. The freeze is a radio-side ~2650-frame
    # scope cap (unavoidable) that recurs roughly every 60-90 s, so a healthy
    # recycle cadence is ~1/minute. If recycles start coming FASTER than this floor,
    # we're not recovering — we're churning, and hammering fresh sessions at the
    # radio wedges its RS-BA1 stack into the authed=True/None phantom state (seen
    # live 2026-07-07: a recycle storm needed a manual restart to clear). So rate-
    # limit: never recycle more often than this; if the scope re-freezes within the
    # cooldown, ride it out (a longer blip) rather than pile on and wedge the radio.
    _RECYCLE_MIN_GAP_S = 25.0

    def reconnect(self):
        """Tear down + re-establish the CI-V session after the scope went deaf.
        Fast path: immediate fresh session (the radio accepts it in ~1 ms); only
        the RETRY attempts back off, for the rarer wedged-session case.
        Called by the engine off the stream/poll thread (open() blocks)."""
        print("[civ] reconnecting the radio session...", flush=True)
        self._want_reconnect = False
        # LAN-ONLY teardown: the USB CI-V channel is a SEPARATE transport that
        # doesn't go deaf with the LAN scope — leave it running. (Bug 2026-07-03:
        # reconnect() used full close() -> stopped USB, then _open() built a NEW
        # UsbCiv that fought the old one for COM7 -> RX2 died after any LAN
        # reconnect.) _open() sees self._usb already up and skips re-creating it.
        # This ALSO sends the 0x05 disconnect the radio needs to release its
        # session cleanly (phantom-session avoidance) before we build a fresh one.
        try:
            self._close_lan()
        except Exception:
            pass
        self._civ = self._handler = None
        for attempt, backoff in enumerate(self._RECONNECT_BACKOFF_S):
            if backoff:
                time.sleep(backoff)                        # only retries wait; attempt 1 is immediate
            try:
                # _open() DIRECTLY, not open(): open()'s failure wrapper does a
                # FULL close() which killed the USB channel on every failed
                # attempt (then the next success rebuilt it -> RX2 cache blanked
                # -> slice 1 torn down/recreated = "slices swapping" churn).
                # Here a failed attempt only tears down the LAN halves.
                self._open()                               # re-auth + civ + health gate
                self._wd_freq_t = time.monotonic()         # reset the watchdog clock
                self._last_recycle_t = time.monotonic()    # rate-limit clock (see _RECYCLE_MIN_GAP_S)
                took = "immediate" if attempt == 0 else f"attempt {attempt+1}"
                print(f"[civ] reconnect OK ({took})", flush=True)
                return True
            except Exception as e:
                print(f"[civ] reconnect attempt {attempt+1} failed: {e}", flush=True)
                try:
                    self._close_lan()                      # LAN-only cleanup (0x05); USB untouched
                except Exception:
                    pass
                self._civ = self._handler = None
        print("[civ] reconnect gave up after 4 tries", flush=True)
        return False

    # The rig's real coverage; AE can ask for anything (e.g. its restored
    # 40m profile at connect) — chasing an out-of-range target just earns
    # an FA from the radio, so drop it here and let the radio->AE dial
    # sync snap AE back to where the rig actually is.
    BAND_RANGES_MHZ = ((144.0, 148.0), (420.0, 450.0), (1240.0, 1300.0))

    # --- control (AE -> radio) -----------------------------------------
    def retune(self, center_hz):
        # MAIN tune. Over USB (single source of truth) write 25 00 with NO swap
        # — off-thread so the engine command path isn't blocked. Over LAN-only,
        # use the LAN async tuner.
        mhz = center_hz / 1e6
        if not any(lo <= mhz <= hi for lo, hi in self.BAND_RANGES_MHZ):
            print(f"[tuner] ignoring out-of-range target {mhz:.4f} MHz "
                  f"(rig covers 2m/70cm/23cm)", flush=True)
            return
        if self._usb:
            self._usb.main_freq_hz = int(center_hz)        # optimistic
            threading.Thread(target=self._usb.tune_main, args=(int(center_hz),),
                             daemon=True, name="ic9700-main-tune").start()
        elif self._civ:
            self._civ.set_freq_hz(int(center_hz))

    def set_mode(self, mode):
        mb = MODE_TO_CIV.get((mode or "").upper())
        if mb is None:
            return
        if self._usb:
            self._usb.set_main_mode(mb)
        elif self._civ:
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

    def get_audio(self, n_samples, slice_hz=None, mode=None):
        """Return n_samples of 24 kHz mono float audio for AE, decimated 2:1 from
        the radio's 48 kHz LPCM16 LAN stream. Returns None when the audio session
        isn't up or hasn't buffered enough yet (engine then falls back to silence).

        NB the radio already demodulates for the SELECTED VFO, so slice_hz/mode are
        ignored here — unlike the SDR adapters, we don't demodulate; we relay."""
        au = self._audio
        if au is None:
            return None
        # AE wants n_samples @ 24 kHz; the radio streams 48 kHz -> pull 2x, decimate.
        want48 = int(n_samples) * (RADIO_RATE // 24000)
        src = au.read_samples(want48)
        if not src:
            return None
        step = RADIO_RATE // 24000              # 2
        out = src[::step][:n_samples]
        # If the ring was a little short, pad with the last sample (avoids a click;
        # the engine treats a short/None frame gracefully). Only pad small gaps.
        if len(out) < n_samples and out:
            out += [out[-1]] * (n_samples - len(out))
        return out or None

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
        # SCOPE-ONLY (hybrid): USB owns all freq/mode + S-meter. Send NO CI-V
        # reads from the LAN channel — they corrupt the USB channel's swap-verify
        # (two masters on the radio's one CI-V state). Keep ONLY the scope
        # frame-progress watchdog (local counter + scope-enable re-fire).
        scope_only = bool(self._usb)
        if not scope_only and now - self._smeter_sent_at >= 1.0:
            self._civ.poll_smeter()                        # 15 02 (S-meter) ~1 Hz
            self._smeter_sent_at = now
        if now - self._freq_polled_at >= 1.0:
            self._freq_polled_at = now
            if not scope_only:
                # round-robin ONE read per tick (LAN-only mode dial-sync).
                # MAIN-only: the SUB reads (25 01 / 26 01 / 07 D2) are gone — they
                # fed only an unused diagnostics field AND provoked the scope stall
                # sooner (see _on_iamready + the deaf-scope notes). Fewer freezes,
                # zero functional loss.
                READS = (bytes([0x25, 0x00]),   # selected freq (authority)
                         bytes([0x26, 0x00]))   # selected mode
                i = getattr(self, "_read_rr", 0)
                self._civ._send_civ(READS[i % len(READS)])
                self._read_rr = i + 1

        # ── SCOPE WATCHDOG — runs EVERY call, NOT gated by the 1 s read tick. ──
        # BUG FOUND (packet capture 2026-07-10): the openclose re-open below lived
        # INSIDE the `>= 1.0` block above, so its "100 ms" retry actually fired only
        # ONCE PER SECOND. The radio ignored the slow 1 Hz re-opens (capture: 1.001s
        # apart, scope stayed dead the full 11 s until the session recycle). SDR9700
        # fires sendOpenClose every 100 ms (its own startCivDataTimer) and recovers
        # seamlessly. Dedented so read_meters (called per frame) drives the true
        # 100 ms cadence.
        if True:
            # WATCHDOG (runs in BOTH modes — scope liveness is transport-agnostic) — liveness is measured by SCOPE FRAME PROGRESS, NOT by
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
                self._reopen_active = False                # frames back -> stop re-opening
            frozen_for = now - getattr(self, "_wd_frames_t", now)
            # WATCHDOG = SDR9700's actual mechanism (audited from UdpCivData.cpp,
            # 2026-07-08). The 9700 pauses its 27h scope output while the SESSION
            # STAYS ALIVE. SDR9700 does NOT reconnect for this — its watchdog, on
            # >2 s with no CI-V data, re-sends the data-stream OPEN (0x1C0 magic
            # 0x04, sendOpenClose(false)) on the SAME session every 100 ms until
            # frames resume. That's it — no 0x05, no re-login, no session churn.
            # (Our earlier "re-open didn't work" was too slow / paired with a
            # reconnect; done SDR9700's way — tight 100 ms in-session retries — the
            # radio resumes the scope with no visible gap.)
            if frozen_for >= 2.0:
                if now - getattr(self, "_reopen_last", 0.0) >= 0.1:   # 100 ms cadence
                    try:
                        self._civ._send_openclose(opening=True)       # sendOpenClose(false)
                    except Exception as e:
                        log("[civ] scope re-open error:", e)
                    self._reopen_last = now
                    if not getattr(self, "_reopen_active", False):
                        self._reopen_active = True
                        print(f"[civ] scope frozen {frozen_for:.0f}s -> re-opening "
                              f"data stream (100ms retries, in-session)", flush=True)
            # FALLBACK: with token renewal (handler.py) the scope should never
            # pause at all — but if it stays dead >10 s despite the re-opens
            # (renewal rejected / session genuinely wedged), recycle the session
            # rather than sit dead forever. Rate-limited to avoid the recycle
            # storm that wedges the radio's RS-BA1 stack.
            if frozen_for >= 10.0 and not getattr(self, "_want_reconnect", False):
                if now - getattr(self, "_last_recycle_t", 0.0) >= self._RECYCLE_MIN_GAP_S:
                    self._want_reconnect = True
                    print(f"[civ] scope frozen {frozen_for:.0f}s despite re-opens "
                          f"-> session recycle (fallback)", flush=True)
            # RADIO KICKED US (transport-audit find): the handler now detects the
            # radio-initiated disconnect (status disc=1). A dead session can't be
            # revived in place — recycle it (fresh handler resets the flag).
            if (self._handler is not None
                    and getattr(self._handler, "radio_disconnected", False)
                    and not getattr(self, "_want_reconnect", False)):
                self._want_reconnect = True
                print("[civ] radio-initiated disconnect -> session recycle", flush=True)
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
        # Seed AE on the rig's real MAIN band. Prefer USB (the truth source);
        # it's read at connect in _open() so it's ready. Fall back to LAN.
        if self._usb and self._usb.main_freq_hz:
            return float(self._usb.main_freq_hz)
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            if self._civ and self._civ.freq_hz:
                return float(self._civ.freq_hz)
            time.sleep(0.1)
        return None

    def initial_mode(self):
        if self._usb and self._usb.main_mode:
            return self._usb.main_mode
        return self._civ.mode if self._civ else None

    # --- radio -> AE (engine polls these each second) --------------------
    # SINGLE SOURCE OF TRUTH: when the USB channel is present it owns ALL
    # freq/mode for BOTH receivers (MAIN + RX2). The LAN channel then does ONLY
    # the scope waterfall — it never drives freq — so the two CI-V masters can't
    # race over the selected receiver (the tangle that put both slices on one
    # band). Without USB, fall back to the LAN reads (MAIN-only).
    def radio_freq_hz(self):
        if self._usb:
            return self._usb.main_freq_hz
        return self._civ.freq_hz if self._civ else None

    def radio_mode(self):
        if self._usb:
            return self._usb.main_mode
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
        # MAIN + RX2 both from USB (single source of truth) when present.
        if self._usb:
            out = [{"freq_hz": self._usb.main_freq_hz, "mode": self._usb.main_mode}]
            if self.sub_active():
                out.append({"freq_hz": self._usb.rx2_freq_hz, "mode": self._usb.rx2_mode})
        else:
            out = [{"freq_hz": self._civ.freq_hz, "mode": self._civ.mode}]
        return [r for r in out if r["freq_hz"]]

    # --- dual-receiver (RX2) — drives the second slice -------------------
    def sub_active(self):
        """True when RX2 is present — sourced from the USB CI-V channel (safe
        07 B0 swap), NOT the LAN swap (destructive, parked). MAIN-only if no USB
        channel. The USB poller keeps rx2_present live without touching the scope."""
        return bool(self._usb and self._usb.rx2_present and self._usb.rx2_freq_hz)

    def sub_freq_hz(self):
        return self._usb.rx2_freq_hz if self._usb else None

    def sub_mode(self):
        return self._usb.rx2_mode if self._usb else None

    def rx2_readout(self):
        """Cached RX2 state for the breakout display — now from the USB channel
        (the safe swap-read source). No radio access here (just the cache)."""
        u = self._usb
        return {
            "present": bool(u and u.rx2_present),
            "freq_hz": (u.rx2_freq_hz if u else None),
            "mode": (u.rx2_mode if u else None),
            "usb": bool(u),
            "last_read_mono": getattr(self, "_rx2_read_at", None),
        }

    def rx2_refresh(self):
        """ON-DEMAND RX2 read over the USB channel (safe swap — no scope stream
        to disturb). The USB poller already refreshes RX2 in the background; this
        is the breakout window's manual 'read now'."""
        u = self._usb
        if not u:
            return {"ok": False, "error": "no USB RX2 channel (pass --usb-civ-port)"}
        try:
            u.read_rx2()
            self._rx2_read_at = time.monotonic()
            r = self.rx2_readout()
            r["ok"] = True
            return r
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_sub_freq_hz(self, hz):
        """Tune RX2 over the USB channel (safe swap). Off-thread so the engine
        command path isn't blocked; UsbCiv.write_rx2 updates its cache."""
        u = self._usb
        if not u:
            return
        u.rx2_freq_hz = int(hz)                            # optimistic
        threading.Thread(target=u.write_rx2, args=(int(hz),),
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

        # Sustained tracked-packet SEND rate (deaf-scope instrumentation): how
        # many CI-V/control packets/s we push at the radio. This is the number
        # the "flood wedges the scope" theory needed and never measured.
        sent_rate = None
        if civ is not None:
            now2 = time.monotonic()
            lt = getattr(self, "_diag_sent_t", None)
            ln2 = getattr(self, "_diag_sent_n", 0)
            if lt is not None and now2 > lt:
                sent_rate = round((civ.n_sent - ln2) / (now2 - lt), 1)
            self._diag_sent_t, self._diag_sent_n = now2, civ.n_sent

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
            "audio": {"lan_stream": (self._audio is not None),
                      "packets": (self._audio.audio_frames if self._audio else 0),
                      "bytes": (self._audio.audio_bytes if self._audio else 0),
                      "ring_samples": (self._audio.ring_samples if self._audio else 0),
                      "dropped": (self._audio.dropped if self._audio else 0)},
            "flags": {"sub_receiver": (self.sub_active() if civ else False),
                      "dualwatch_reg": bool(civ.dualwatch) if civ else False},
            "counters": {"tune_ok": (civ.n_fb if civ else 0),
                         "tune_refused": (civ.n_fa if civ else 0),
                         # deaf-scope instrumentation:
                         "sent_total": (civ.n_sent if civ else 0),
                         "sent_per_s": sent_rate,
                         "retransmit_reqs": (civ.n_retx_req if civ else 0),
                         "rx_tracker_resets": (civ.n_rx_clears if civ else 0),
                         # ground truth for the deaf-scope stall (2026-07-07): if
                         # rx_dgrams keeps climbing while fps=0 the radio is still
                         # sending (session alive, only scope output paused); if it
                         # freezes + since_last_rx grows, the session truly died.
                         "rx_dgrams": (civ.n_rx_dgrams if civ else 0),
                         "since_last_rx_s": (round(time.monotonic() - civ.last_rx_at, 1)
                                             if civ and civ.last_rx_at else None)},
        }
