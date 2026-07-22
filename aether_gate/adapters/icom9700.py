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
from .icom.radios import get as get_icom

MODE_TO_CIV = {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03, "RTTY": 0x04,
               "FM": 0x05, "CW-R": 0x06, "RTTY-R": 0x07, "DV": 0x08, "FM-N": 0x12}
CIV_TO_MODE = {v: k for k, v in MODE_TO_CIV.items()}

# AE speaks Flex's mode vocabulary, which is a SUPERSET of the IC-9700's CI-V
# modes: the Flex "data" variants (DFM = data-FM, DIGU/DIGL = data USB/LSB) and
# a few aliases have no distinct CI-V mode byte on the 9700 (the 9700 has no
# separate data-mode byte — data is just the base mode with an external audio
# path). Fold each AE mode onto the nearest CI-V base mode so a dropdown pick
# actually keys the radio instead of being silently dropped (which let the
# radio->AE sync yank AE's choice straight back). The gate still ECHOES the AE
# alias back to AE (via _ae_mode_echo) so e.g. DFM stays showing DFM even though
# the rig sits on plain FM.
AE_MODE_ALIASES = {
    "DFM": "FM", "NFM": "FM", "FMN": "FM-N",          # FM family
    "DIGU": "USB", "DIGL": "LSB",                     # data SSB -> base SSB
    "SAM": "AM", "DSB": "AM", "AME": "AM",            # AM family
    "DCW": "CW", "CWL": "CW-R", "CWU": "CW",          # CW family
    "FDV": "DV",                                      # data-voice -> DV
}


def _civ_mode_name(ae_mode):
    """AE mode string -> the CI-V base mode name the 9700 understands (or None
    if there's genuinely no equivalent). Applies the Flex->Icom alias fold."""
    m = (ae_mode or "").upper()
    m = AE_MODE_ALIASES.get(m, m)
    return m if m in MODE_TO_CIV else None


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


def _bcd2(n):
    """int 0..9999 -> 2 CI-V BCD bytes, MSB digit-pair first (menu-value order)."""
    n = int(n)
    d = [(n // 1000) % 10, (n // 100) % 10, (n // 10) % 10, n % 10]
    return bytes([(d[0] << 4) | d[1], (d[2] << 4) | d[3]])


def _unbcd(b):
    """CI-V BCD value bytes (MSB pair first) -> int. Empty -> None."""
    if not b:
        return None
    n = 0
    for byte in b:
        n = n * 100 + (byte >> 4) * 10 + (byte & 0x0F)
    return n


# IC-9700 SET-menu items reachable via CI-V 1A 05 <subaddr>. Addresses + value
# semantics are from the official IC-9700 CI-V Reference Guide (cached on the NAS
# at _claude/IC-9700_CI-V_Reference_Guide.pdf). Each entry:
#   subaddr : 16-bit menu index
#   width   : value byte-count (1 = single BCD byte; 2 = 0000..0255 level)
#   kind    : "level" (0..255 = 0..100%) or "enum" (choice index)
#   choices : for enum, index -> human label
_MOD_SRC = {0: "MIC", 1: "ACC", 2: "MIC,ACC", 3: "USB", 4: "MIC,USB", 5: "LAN"}
IC9700_SETTINGS = {
    "data_mod":      {"subaddr": 0x0116, "width": 1, "kind": "enum",  "choices": _MOD_SRC},
    "data_off_mod":  {"subaddr": 0x0115, "width": 1, "kind": "enum",  "choices": _MOD_SRC},
    "lan_mod_level": {"subaddr": 0x0114, "width": 2, "kind": "level"},
    "usb_mod_level": {"subaddr": 0x0113, "width": 2, "kind": "level"},
    "acc_mod_level": {"subaddr": 0x0112, "width": 2, "kind": "level"},
}


def _resample(src, n):
    """Nearest-neighbour resample a dBm list to exactly n bins."""
    m = len(src)
    if m == n:
        return list(src)
    return [src[(i * m) // n] for i in range(n)]


class _Ic9700Stream(Ic9700Civ):
    """One CI-V stream doing BOTH scope (inherited) and control (added here)."""

    # Set by Icom9700Adapter when --rx-only is in force. Class-level default so
    # the attribute always resolves, including on instances a test builds by hand.
    rx_only = False

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
        self.rfpower_raw = None         # last CI-V 14 0A RF-power SETTING, 0..255 (read-only)
        self.fwdpwr_raw = None           # last CI-V 15 11 Po (forward-power) METER, 0..255 (TX only)
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
        # --- generic CI-V menu (1A 05) read/write facility ---
        # Sends 1A 05 <2-byte-subaddr> [value...] through the LIVE session (no
        # competing login) and correlates the reply by sub-address. Used to read/
        # write rig SET-menu items (e.g. LAN MOD Level) for diagnostics + config.
        self._menu_replies = {}        # subaddr-int -> value bytes (last reply)
        self._menu_evt = threading.Event()   # set when any 1A 05 reply lands
        self._menu_lock = threading.Lock()   # serialise concurrent menu requests

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
            self._on_civ(d)                     # extract the waterfall pixels
            # A scope frame is `FE FE E0 A2 27 00 00 <~500 raw amplitude bytes> FD`.
            # It is its OWN CI-V message — freq/mode/S-meter replies (25/26/15) always
            # arrive in SEPARATE datagrams, never mixed into a waveform frame. Do NOT
            # fall through to the generic `fe fe ... fd` scan below: the raw amplitude
            # bytes routinely contain stray `FD` terminators and `FE FE` sequences, so
            # the scanner mis-frames the waveform and decodes 5 random amplitude bytes
            # as a `25 00`/`00` BCD frequency — which is exactly what made the reported
            # freq JUMP wildly around the 2m band (144.4 -> 144.6 -> 145.1 in ~1s) and
            # AE could never land on the real receive frequency. (Regression from the
            # SDR9700 transport port: the ported UdpCivData now hands the whole frame,
            # scope bytes included, to on_data; before, control replies came pre-split.)
            return
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
                elif cmd == 0x15 and len(data) >= 3 and data[0] == 0x11:
                    # Po (forward-power) meter reply: 15 11 <2-byte BCD 0000-0255>.
                    # Only meaningful while keyed; 0 in RX. Icom Po curve is
                    # non-linear (see _fwd_power_w). Poll only during TX.
                    self.fwdpwr_raw = (data[1] >> 4) * 1000 + (data[1] & 0xF) * 100 + \
                                      (data[2] >> 4) * 10 + (data[2] & 0xF)
                elif cmd == 0x14 and len(data) >= 3 and data[0] == 0x0A:
                    # RF power SETTING reply: 14 0A <2-byte BCD 0000-0255> = 0-100%.
                    # Read-only here (we report it to AE; AE cannot set it back —
                    # power stays controlled at the rig's front panel).
                    self.rfpower_raw = (data[1] >> 4) * 1000 + (data[1] & 0xF) * 100 + \
                                       (data[2] >> 4) * 10 + (data[2] & 0xF)
                elif cmd == 0x1A and len(data) >= 3 and data[0] == 0x05:
                    # Menu (1A 05 <2-byte subaddr> <value...>) read reply. Store
                    # the value bytes keyed by the 16-bit sub-address so a waiting
                    # read_menu() can pick it up. A WRITE is acked with FB (no data).
                    subaddr = (data[1] << 8) | data[2]
                    self._menu_replies[subaddr] = bytes(data[3:])
                    self._menu_evt.set()
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

    # --- generic CI-V SET-menu (1A 05) read/write ----------------------------
    # Sends over the LIVE CI-V session (no competing login — this is what makes
    # it work where a standalone probe failed). read returns the raw value bytes;
    # write returns True on the radio's FB ack. subaddr is the 16-bit menu index
    # (e.g. 0x0114 = LAN MOD Level) from the IC-9700 CI-V reference.
    def read_menu(self, subaddr, timeout=1.5):
        """Read a 1A 05 <subaddr> menu item. Returns value bytes, or None on
        timeout / no session."""
        with self._menu_lock:
            self._menu_replies.pop(subaddr, None)
            self._menu_evt.clear()
            self._send_civ(bytes([0x1A, 0x05, (subaddr >> 8) & 0xFF, subaddr & 0xFF]))
            deadline = time.time() + timeout
            while time.time() < deadline:
                if subaddr in self._menu_replies:
                    return self._menu_replies[subaddr]
                self._menu_evt.wait(0.1)
                self._menu_evt.clear()
            return None

    def write_menu(self, subaddr, value_bytes, settle=0.25):
        """Write a 1A 05 <subaddr> menu item. `value_bytes` is the raw value
        payload (already BCD-encoded to the item's width). Returns True if the
        radio did not FA (reject) it within settle."""
        fa0 = self.n_fa
        self._send_civ(bytes([0x1A, 0x05, (subaddr >> 8) & 0xFF, subaddr & 0xFF])
                       + bytes(value_bytes))
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
        # Optimistically reflect the new mode straight away (mirrors the freq
        # tuner's `self.freq_hz = tgt`). Without this, receivers()/radio_mode()
        # keep returning the OLD self.mode until the next 26 00 poll lands, so
        # the engine re-emits the stale mode to AE and AE's display snaps back
        # to it — the "software mode change reverts, front-panel change sticks"
        # bug (front-panel changes arrive as a 01/26 transceive that _dispatch
        # updates self.mode from, which is why those never snapped back).
        m = CIV_TO_MODE.get(mode_byte)
        if m and not self._reading_rx2:
            self.mode = m

    def poll_smeter(self):
        self._send_civ(bytes([0x15, 0x02]))

    def poll_power(self):
        # Read the rig's RF-power SETTING (14 0A). Read-only: we report it to AE
        # so its power display is honest; AE never writes power back to the rig.
        self._send_civ(bytes([0x14, 0x0A]))

    def poll_fwdpower(self):
        # Read the Po (forward-power) METER (15 11). Only meaningful during TX;
        # polled from read_meters ONLY while keyed so we don't flood CI-V in RX.
        self._send_civ(bytes([0x15, 0x11]))

    # --- PTT (CI-V 1C 00) — the raw key primitives. The SAFETY layer lives on
    # the adapter (Icom9700Adapter.key_tx/unkey_tx): arm-gate, band-check,
    # 10 s watchdog. These two just put the CI-V on the wire; never call them
    # directly from outside the adapter's guarded path.
    def _ptt_raw(self, on):
        """CI-V 1C 00 <01|00>: key (on=True) / unkey the transmitter."""
        if on and self.rx_only:
            # THE load-bearing rx-only guard: this is the only place the gate
            # asserts PTT on the wire, so it is the only refusal that cannot be
            # routed around. The checks in arm_tx/key_tx are layered on top for
            # a clear log and an honest capability advert -- but key_tx returns a
            # value the engine DISCARDS, so a refusal higher up cannot be relied
            # on by itself. UNKEY is never blocked: a latched transmitter must
            # always be able to drop, whatever the flags say.
            print("[tx] REFUSED: --rx-only is in force (no PTT asserted)", flush=True)
            return
        self._send_civ(bytes([0x1C, 0x00, 0x01 if on else 0x00]))


class Icom9700Adapter(RadioAdapter):
    """IC-9700 LAN adapter. RX panadapter (scope) + freq/mode control."""

    provides = "spectrum"

    # RX-ONLY LATCH -- a CLASS attribute deliberately. The TX safety suite builds
    # adapters with Icom9700Adapter.__new__(Icom9700Adapter), which never runs
    # __init__; an instance-only attribute would simply not exist there, and a
    # defensive getattr(..., False) would read False and leave the whole rx-only
    # suite green while exercising nothing. __init__ overrides it per instance.
    _rx_only = False

    def __init__(self, radio_ip, username, password, local_ip=None,
                 radio_port=50001, civ_addr=0xA2, icom_model="IC-9700",
                 model="FLEX-6700",
                 serial="GATE9700", station="Icom-IC-9700",
                 usb_civ_port=None, usb_civ_baud=115200, rx_only=False):
        # FLEX-6700 is the only Flex model that covers 2m (~135-165 MHz), so AE will
        # offer the IC-9700's 2m band. (6300/6400/6600 = HF+6m only.) 70cm/23cm still
        # need frequency translation - no Flex covers them.
        # Set before capabilities are built below -- tx_capable reads it.
        self._rx_only = bool(rx_only)
        self.radio_ip = radio_ip
        self.username = username
        self.password = password
        self.local_ip = local_ip
        self.radio_port = radio_port
        self.civ_addr = civ_addr
        # Which Icom this is. The LAN transport is model-agnostic (the radio's own
        # capabilities packet names it), so everything model-specific comes from ONE
        # radios.py row: retune coverage, the bands= AE advert, and the identity we
        # report. Unknown model -> fall back to the 9700 so nothing regresses.
        self._row = get_icom(icom_model)
        if self._row is None:
            print(f"[icom] unknown --icom-model {icom_model!r}; falling back to IC-9700",
                  flush=True)
            self._row = get_icom("IC-9700")
        self.icom_model = self._row.model
        if self._row.transport != "lan":
            raise RuntimeError(
                f"{self._row.model} is a {self._row.transport}-transport radio; this "
                f"adapter is the RS-BA1 LAN path. Use --adapter icom7300 for USB CI-V.")
        # retune() coverage comes from the row, so an HF rig is not silently clamped
        # to the 9700's VHF/UHF set. TX_BANDS_MHZ is deliberately NOT derived -- see
        # the note on that constant.
        self.BAND_RANGES_MHZ = tuple((b.low_mhz, b.high_mhz) for b in self._row.bands)
        # IC-9700 covers 2m/70cm/23cm; RX-only here (no TX/PTT wired -> never keys the rig).
        # Span honesty: the 9700 scope does ±2.5k..±500k -> pan width 5 kHz..1 MHz;
        # don't let AE zoom the axis past what the scope can actually show.
        # bands: from the shared band constants (their wire names are AE BandDefs
        # vocabulary — _70CM declares "440"). With a radio-declared-bands AE the
        # menu offers exactly these three; older AE ignores the key and falls back
        # to the FLEX-6700's 2m.
        _bands = tuple(b.name for b in self._row.bands)   # 9700: ("2m","440","23cm")
        # tx_capable=True now that real guarded PTT is wired (key_tx: armed +
        # 2m/70cm only, 23cm refused, 10 s watchdog). This makes the engine
        # advertise tx=1 on the active slice so AE un-greys the TX button; MOX
        # then routes to key_tx(). TX audio (Stage 2) is wired too: while keyed,
        # a drain thread streams AE's dax_tx modem audio to the rig's RS-BA1 TX
        # audio session (txenable=1), so the carrier is MODULATED (AX.25/RADE),
        # not bare — see _tx_audio_loop + Ic9700Audio.send_audio.
        # Under --rx-only advertise tx_capable=False so AE greys its TX button
        # rather than offering a control whose PTT we will refuse anyway.
        self.capabilities = AdapterCaps(model=model, serial=serial, station=station,
                                        tx_capable=not self._rx_only,
                                        min_span_hz=5_000.0, max_span_hz=1_000_000.0,
                                        bands=_bands)
        self._handler = None
        self._civ = None
        self._audio = None             # LAN RX-audio session (Ic9700Audio)
        # On connect, if the rig's LAN MOD Level is below this (0..255), raise it
        # to it so TX audio actually modulates (0 = bare carrier). A user who has
        # deliberately set a higher level is left alone. 128 = 50%, matching the
        # USB/ACC defaults. Set to 0 to disable the auto-fix entirely.
        self.lan_mod_min = 128
        # HYBRID RX2: optional USB CI-V channel. RX2 needs the 07 B0 swap, which
        # is destructive over LAN (yanks the scope) but HARMLESS over USB (no
        # scope stream) — proven 5/5 on HW 2026-07-03. When a USB CI-V port is
        # given, RX2 is read/controlled over USB while the LAN channel keeps
        # MAIN's waterfall. Without it, dual-RX stays off (MAIN-only).
        self.usb_civ_port = usb_civ_port
        self.usb_civ_baud = usb_civ_baud
        self._usb = None
        # --- TX / PTT safety state (see key_tx). DISARMED by default: nothing
        # can key the rig until arm_tx() is called explicitly. This is the FIRST
        # place the gate keys real RF, so it starts locked down. ---
        self._tx_armed = False            # must arm_tx() before any key-down
        self._tx_keyed = False            # are we currently keyed?
        self._tx_watchdog = None          # threading.Timer that force-unkeys
        self._tx_lock = threading.Lock()
        # --- TX AUDIO (Stage 2): drain AE's dax_tx ring -> 9700 while keyed. ---
        self._tx_audio_source = None      # engine.drain_tx_audio (set via set_tx_audio_source)
        self._tx_audio_ready = None       # engine.tx_audio_ready probe (bare-carrier guard)
        self._tx_audio_thread = None      # per-key drain thread
        self._tx_audio_stop = None        # threading.Event to stop the drain thread
        self._tx_resample_carry = b""     # 24k->48k upsampler state (last sample)
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
                                  self._handler._civ_sock, civ_addr=self.civ_addr)
        self._civ.rx_only = self._rx_only
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
        # TX-AUDIO PRECONDITION: the gate modulates the rig over its LAN audio
        # path, which only produces RF modulation if the rig's LAN MOD Level is
        # non-zero (a factory-fresh / RS-BA1-defaulted 9700 leaves it at 0 -> the
        # rig keys a BARE carrier, no modulation; cost a long debug session
        # 2026-07-15). Ensure a usable level on connect so digital TX just works.
        # Never let this optional convenience break an otherwise-good connect.
        try:
            self._ensure_lan_mod_ready()
        except Exception as e:
            print(f"[civ] LAN MOD auto-set skipped: {e}", flush=True)
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
                # Log the LOCAL listen port (where the radio streams audio TO) —
                # this must match the fixed audio_local_port we advertised, else
                # the radio's remembered (stale) port wins and AE gets silence.
                print(f"[audio] LAN RX-audio session up: radio :{self._handler.audio_port} "
                      f"-> local :{self._handler.audio_local_port}", flush=True)
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
        # SAFETY FIRST: never tear down a session with the rig still keyed. Unkey
        # + re-latch the arm before we drop the CI-V transport (after which we
        # could no longer send 1C 00 00). A disconnect/reconnect must always
        # leave the transmitter in RX.
        try:
            self.disarm_tx()
        except Exception:
            pass
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

    # NOT derived from the radios.py row, deliberately. That table is documentation
    # ("band edges here are indicative and region-neutral", and every row but the
    # 9700 was verified=False when this was written) -- it is RX/coverage data with
    # no TX field, so it cannot carry transmit authority. needs_xvtr also cannot
    # separate TX-allowed 70cm from TX-forbidden 23cm: both are True.
    # TX-ALLOWED bands (key_tx checks THIS, not BAND_RANGES_MHZ). DELIBERATELY
    # EXCLUDES 23cm/1.2 GHz per Nigel's instruction — RX on 1.2 GHz is fine, but
    # the gate must REFUSE to key the transmitter there. A hard guard, not a
    # remembered rule. 2m + 70cm only.
    TX_BANDS_MHZ = ((144.0, 148.0), (420.0, 450.0))

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
        # Fold AE's Flex mode (incl. data variants: DFM/DIGU/DIGL) onto the
        # nearest CI-V base mode the 9700 can key. Remember the exact AE string
        # so radio_mode()/receivers() can echo it back verbatim — otherwise AE
        # picks DFM, the rig goes to plain FM, the radio->AE sync reports "FM",
        # and AE's display snaps DFM back to FM.
        base = _civ_mode_name(mode)
        mb = MODE_TO_CIV.get(base) if base else None
        if mb is None:
            print(f"[mode] AE asked for {mode!r} - no CI-V equivalent on the 9700, ignored",
                  flush=True)
            return
        self._ae_mode_echo = (mode or "").upper()          # what AE should keep showing
        self._ae_mode_base = base                          # the CI-V base it maps to
        if self._usb:
            self._usb.set_main_mode(mb)
        elif self._civ:
            self._civ.set_mode_civ(mb)

    # --- CI-V SET-menu settings (1A 05) read/write ---------------------------
    # Named accessors over the live CI-V session for rig SET-menu items (see
    # IC9700_SETTINGS). Purpose: diagnose TX-audio routing (e.g. is LAN MOD Level
    # 0? that gives a keyed-but-unmodulated carrier) and, later, auto-configure
    # the rig (e.g. force DATA MOD=LAN on connect). Reads/writes go through the
    # running gate session — NOT a competing login.
    def read_setting(self, name):
        """Read a named SET-menu item. Returns a dict {raw, value, label} or
        None (unknown name / no CI-V / timeout)."""
        spec = IC9700_SETTINGS.get(name)
        if spec is None or self._civ is None:
            return None
        raw = self._civ.read_menu(spec["subaddr"])
        if raw is None:
            return None
        val = _unbcd(raw)
        out = {"raw": raw.hex(), "value": val}
        if spec["kind"] == "enum":
            out["label"] = spec.get("choices", {}).get(val, f"?{val}")
        else:                                   # level: 0..255 -> 0..100%
            out["label"] = None if val is None else f"{round(val / 255 * 100)}%"
        return out

    def write_setting(self, name, value):
        """Write a named SET-menu item. `value` is the numeric setting (enum
        index, or 0..255 level). Returns True if the radio accepted it."""
        spec = IC9700_SETTINGS.get(name)
        if spec is None or self._civ is None:
            return False
        payload = _bcd2(value) if spec["width"] == 2 else bytes([int(value) & 0xFF])
        return self._civ.write_menu(spec["subaddr"], payload)

    def read_all_settings(self):
        """Read every known SET-menu item -> {name: {...} | None}. For the
        diagnostics dump (web panel / status)."""
        return {name: self.read_setting(name) for name in IC9700_SETTINGS}

    def _ensure_lan_mod_ready(self):
        """On connect, guarantee the rig can actually modulate over its LAN audio
        path: raise LAN MOD Level to lan_mod_min if it's below that (0 = bare
        carrier). Read-only + non-fatal — never blocks the connect. Leaves a
        deliberately-higher level untouched; skips entirely if lan_mod_min == 0."""
        if not self.lan_mod_min or self._civ is None:
            return
        if not hasattr(self._civ, "read_menu"):
            return                              # CI-V transport predates the facility
        cur = self.read_setting("lan_mod_level")
        if cur is None:
            print("[civ] LAN MOD Level: could not read (skipping auto-set)", flush=True)
            return
        level = cur.get("value")
        if level is not None and level >= self.lan_mod_min:
            print(f"[civ] LAN MOD Level OK ({cur['label']}) — TX audio can modulate",
                  flush=True)
            return
        # Too low (typically 0 -> bare carrier). Raise it.
        ok = self.write_setting("lan_mod_level", self.lan_mod_min)
        rb = self.read_setting("lan_mod_level")
        rb_label = rb["label"] if rb else "?"
        print(f"[civ] LAN MOD Level was {cur['label']} -> set {self.lan_mod_min} "
              f"(now {rb_label}); {'ok' if ok else 'WRITE REJECTED'}. "
              f"Fixes the bare-carrier trap for digital TX.", flush=True)

    # ==================================================================== #
    #  TX / PTT — GUARDED. This is the first place the gate keys real RF.    #
    #  Layered safety (ALL enforced here, none optional):                   #
    #    1. DISARMED by default — key_tx() refuses unless arm_tx() was       #
    #       called. AE is NOT wired to this (tx_capable stays False), so     #
    #       nothing keys the rig unless you invoke key_tx() yourself.        #
    #    2. BAND CHECK — refuse to key unless the rig's freq is in a legal   #
    #       9700 segment (2m/70cm/23cm).                                     #
    #    3. WATCHDOG — a Timer force-unkeys after TX_MAX_KEY_S even if        #
    #       nothing else releases (stuck-PTT / hung-stream protection).      #
    #    4. Auto-unkey + disarm on close/disconnect (see close/_close_lan).  #
    # ==================================================================== #
    TX_MAX_KEY_S = 10.0                    # watchdog: hard cap on continuous TX

    def arm_tx(self):
        """Explicitly enable TX. Until this is called, key_tx() is a no-op that
        refuses. Arming does NOT key the rig — it only lifts the safety latch."""
        if self._rx_only:
            # The engine auto-arms on every AE connect, so this is the arm that
            # actually has to be refused for an unattended gateway.
            print("[tx] arm ignored: --rx-only is in force", flush=True)
            return
        self._tx_armed = True
        print("[tx] ARMED (key_tx now permitted; watchdog "
              f"{self.TX_MAX_KEY_S:.0f}s)", flush=True)

    def disarm_tx(self):
        """Force RX + re-latch the safety. Safe to call anytime."""
        self.unkey_tx()
        self._tx_armed = False
        print("[tx] DISARMED", flush=True)

    def tx_ready(self):
        """(armed, in_band, freq_mhz) — why key_tx would/wouldn't fire, for UI."""
        f = self._civ.freq_hz if self._civ else None
        mhz = (f / 1e6) if f else None
        in_band = bool(mhz and any(lo <= mhz <= hi for lo, hi in self.TX_BANDS_MHZ))
        digital = self._is_digital_mode()
        dax_ok = self._dax_tx_registered()
        return {"armed": self._tx_armed, "in_band": in_band, "freq_mhz": mhz,
                "keyed": self._tx_keyed,
                # bare-carrier guard state: in a digital mode with no dax_tx
                # stream, key_tx will refuse (AE would key an unmodulated carrier)
                "digital_mode": digital, "dax_tx_registered": dax_ok,
                "would_be_bare_carrier": bool(digital and not dax_ok)}

    # Modes where the ONLY TX audio source is AE's dax_tx stream. In these, no
    # registered stream == guaranteed bare carrier. Voice modes (USB/LSB/FM/AM)
    # are driven by the rig's own mic and are deliberately NOT listed.
    DIGITAL_MODES = ("DFM", "DIGU", "DIGL", "RTTY", "FDV", "DATA-U", "DATA-L")

    def _is_digital_mode(self):
        """True if the active mode gets its TX audio from AE, not the rig's mic."""
        m = (getattr(self, "_mode", None) or "").upper()
        return any(m == d or m.startswith(d) for d in self.DIGITAL_MODES)

    def _dax_tx_registered(self):
        """True if AE has a live dax_tx stream (engine probe). Fail SAFE: if the
        probe is missing (older engine / not wired), assume registered so this
        guard can never wedge a working setup."""
        probe = getattr(self, "_tx_audio_ready", None)
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception:
            return True

    def key_tx(self, force=False):
        """Key the transmitter — ONLY if armed AND in a legal band. Arms a
        watchdog that force-unkeys after TX_MAX_KEY_S. Returns True if keyed.

        force=True skips ONLY the bare-carrier guard (for a deliberate carrier,
        e.g. tuning). It does NOT bypass arm, band-check or the watchdog."""
        with self._tx_lock:
            if self._rx_only:
                print("[tx] REFUSED: --rx-only is in force", flush=True)
                return False
            if not self._tx_armed:
                print("[tx] REFUSED: not armed (call arm_tx first)", flush=True)
                return False
            if self._civ is None:
                print("[tx] REFUSED: no CI-V session", flush=True)
                return False
            f = self._civ.freq_hz
            mhz = (f / 1e6) if f else None
            if not (mhz and any(lo <= mhz <= hi for lo, hi in self.TX_BANDS_MHZ)):
                print(f"[tx] REFUSED: {mhz} MHz not a TX-allowed band "
                      f"{self.TX_BANDS_MHZ} (23cm/1.2GHz TX is disabled)", flush=True)
                return False
            # BARE-CARRIER GUARD. In a DIGITAL mode the only TX audio source is
            # AE's dax_tx stream. If AE never registered one, no audio can ever
            # arrive (the prime loop drops every VITA packet without a stream id),
            # so keying would radiate an UNMODULATED CARRIER for the full watchdog
            # period. Measured 2026-07-15: 127 of 261 keys ran exactly like this —
            # AE sent `transmit set dax=1` + `xmit 1` with no `stream create
            # type=dax_tx`, and every one produced `drain END (0 real audio)`.
            # Voice modes are unaffected: the rig's own mic is the source there,
            # so no dax_tx is expected and this guard must not fire.
            if not force and self._is_digital_mode() and not self._dax_tx_registered():
                print("[tx] REFUSED: digital mode but AE has registered no dax_tx "
                      "stream — keying now would transmit a BARE CARRIER. "
                      "(AE sometimes keys without `stream create type=dax_tx`; "
                      "re-open the digital/modem panel in AE, or use force=True "
                      "if you really want an unmodulated carrier.)", flush=True)
                return False
            if self._tx_keyed:
                return True                            # already keyed
            self._civ._ptt_raw(True)
            self._tx_keyed = True
            self._civ._tx_active = True                # gate the 15 11 Po poll on
            # WATCHDOG: force-unkey after the hard cap no matter what.
            self._tx_watchdog = threading.Timer(self.TX_MAX_KEY_S, self._tx_timeout)
            self._tx_watchdog.daemon = True
            self._tx_watchdog.start()
            # Start draining AE's TX audio to the rig so the carrier is MODULATED
            # (AX.25/RADE), not bare. No-op if no TX-audio source/session is wired.
            self._start_tx_audio()
            print(f"[tx] KEYED @ {mhz:.5f} MHz (watchdog {self.TX_MAX_KEY_S:.0f}s)",
                  flush=True)
            return True

    def unkey_tx(self):
        """Unkey the transmitter (CI-V 1C 00 00). Always safe; cancels watchdog."""
        with self._tx_lock:
            # Stop the TX-audio drain FIRST so no more audio is sent after unkey.
            self._stop_tx_audio()
            if self._tx_watchdog is not None:
                self._tx_watchdog.cancel()
                self._tx_watchdog = None
            if self._civ is not None:
                try:
                    self._civ._ptt_raw(False)
                    self._civ._tx_active = False       # stop the 15 11 Po poll
                    self._civ.fwdpwr_raw = None        # clear stale reading -> RX shows 0 W
                except Exception as e:
                    print(f"[tx] unkey send error: {e}", flush=True)
            if self._tx_keyed:
                print("[tx] UNKEYED", flush=True)
            self._tx_keyed = False

    def _tx_timeout(self):
        print(f"[tx] WATCHDOG fired ({self.TX_MAX_KEY_S:.0f}s) -> forcing RX", flush=True)
        self.unkey_tx()

    # --- TX AUDIO (Stage 2): drain AE's dax_tx ring -> 9700 while keyed --------
    def set_tx_audio_source(self, source):
        """Engine seam: `source()` (or source(max_bytes)) returns buffered AE TX
        audio as mono int16 LE @ 24 kHz (the dax_tx rate). Called once at wiring."""
        self._tx_audio_source = source

    def set_tx_audio_ready_probe(self, probe):
        """Engine seam: `probe()` -> True if AE has registered a dax_tx stream, so
        TX audio CAN arrive. Used by key_tx to refuse a bare-carrier key in a
        digital mode. Called once at wiring."""
        self._tx_audio_ready = probe

    def _start_tx_audio(self):
        """Spawn the drain thread that streams AE's modem audio to the rig for as
        long as we're keyed. No-op if there's no source or no audio session."""
        if getattr(self, "_tx_audio_source", None) is None or self._audio is None:
            return
        if self._tx_audio_thread is not None and self._tx_audio_thread.is_alive():
            return
        self._tx_resample_carry = b""
        self._audio._send_audio_seq = 0            # fresh audio-seq window per key
        self._tx_audio_stop = threading.Event()
        self._tx_audio_thread = threading.Thread(
            target=self._tx_audio_loop, args=(self._tx_audio_stop,),
            name="ic9700-tx-audio", daemon=True)
        self._tx_audio_thread.start()

    def _stop_tx_audio(self):
        """Signal the drain thread to finish; it sends a short fade-out of silence
        so the AFSK tail doesn't click when the rig unkeys."""
        ev = getattr(self, "_tx_audio_stop", None)
        if ev is not None:
            ev.set()
        # Don't join under _tx_lock (unkey_tx holds it) — the daemon thread exits
        # on the event; a lingering send is harmless (radio is about to unkey).
        self._tx_audio_thread = None
        self._tx_audio_stop = None

    def _tx_audio_loop(self, stop):
        """Pull 24 kHz mono int16 from AE's ring, upsample 2x -> 48 kHz, and send
        to the radio in ~20 ms chunks. A short silence fade opens and closes the
        burst so the modem's AFSK doesn't start/stop with a click. Runs only while
        keyed; exits when stop is set (unkey/close/watchdog)."""
        au = self._audio
        src = self._tx_audio_source
        if au is None or src is None:
            return
        # ~20 ms at 48 kHz mono = 960 samples = 1920 bytes per send tick.
        tick_s = 0.02
        radio_bytes_per_tick = int(RADIO_RATE * tick_s) * 2
        ae_bytes_per_tick = radio_bytes_per_tick // 2    # 24k -> 48k is 2x
        f0 = au.tx_frames
        real = 0
        seen_real = False
        print(f"[tx-audio] drain START (tx_frames={f0})", flush=True)
        try:
            au.send_audio(self._silence(RADIO_RATE // 100))   # ~10 ms lead-in silence
            # LATENCY GRACE: AE keys, THEN sends the AFSK over dax_tx (:4991) with
            # network+decode latency, then unkeys ~150 ms after queuing. Our drain
            # starts on xmit 1 — BEFORE the audio lands — so it used to blast
            # silence and stop on xmit 0 before the audio ever arrived (0 real
            # audio -> unmodulated carrier). Fix: keep draining until we've seen
            # real audio AND the ring has run dry, with a bounded post-key flush,
            # so late-arriving AFSK is still sent while keyed.
            FLUSH_GRACE_S = 0.6            # keep sending after stop until ring dry (bounded)
            flush_deadline = None
            while True:
                stopped = stop.is_set()
                try:
                    pcm24 = src(ae_bytes_per_tick)
                except Exception:
                    pcm24 = b""
                if pcm24:
                    au.send_audio(self._upsample_2x(pcm24))
                    real += 1
                    seen_real = True
                    flush_deadline = None      # got audio -> reset the flush timer
                elif stopped:
                    # unkey requested + ring empty: start/continue the flush grace,
                    # then exit once it elapses (gives late AFSK a chance to arrive)
                    if flush_deadline is None:
                        flush_deadline = time.monotonic() + FLUSH_GRACE_S
                    elif time.monotonic() >= flush_deadline:
                        break
                    au.send_audio(self._silence(radio_bytes_per_tick // 2))
                else:
                    # still keyed, ring momentarily empty (modem gap): fill silence
                    # so the radio's TX audio buffer never underruns mid-burst.
                    au.send_audio(self._silence(radio_bytes_per_tick // 2))
                time.sleep(tick_s)
            au.send_audio(self._silence(RADIO_RATE // 100))   # ~10 ms fade-out silence
            print(f"[tx-audio] drain END sent={au.tx_frames-f0} frames "
                  f"({real} real audio, seen_real={seen_real}, {au.tx_bytes} tx_bytes total)",
                  flush=True)
        except Exception as e:
            print(f"[tx-audio] drain error: {e}", flush=True)

    @staticmethod
    def _silence(n_samples):
        return b"\x00\x00" * int(n_samples)

    def _upsample_2x(self, pcm24):
        """Linear-interpolate 24 kHz mono int16 LE -> 48 kHz. Carries the last
        sample across calls so chunk boundaries interpolate correctly (no seam
        click). Good enough for AFSK/data; not a brick-wall resampler."""
        import struct as _struct
        prev = self._tx_resample_carry
        buf = prev + pcm24
        n = len(buf) // 2
        if n < 2:
            self._tx_resample_carry = buf
            return b""
        s = _struct.unpack(f"<{n}h", buf[:n * 2])
        out = []
        for i in range(n - 1):
            a, b = s[i], s[i + 1]
            out.append(a)
            out.append((a + b) // 2)
        # keep the last input sample as carry for the next chunk
        self._tx_resample_carry = _struct.pack("<h", s[-1])
        return _struct.pack(f"<{len(out)}h", *out)

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
            # No live scope frames yet (fresh session, or _civ=None during a
            # reconnect window). Returning None makes engine.py's stream_loop
            # skip the pan/wf emit for this tick, so AE keeps its prior
            # waterfall history instead of being repainted with the noise
            # floor (which read as a dead-black waterfall in v26.7.x AE).
            return None
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
        # S-meter poll at 10 Hz — THIS IS THE 9700's SCOPE KEEPALIVE. Packet capture
        # of SDR9700 (2026-07-10, capture B) proved it: SDR9700 polls 15 02 at ~10/s
        # continuously and its scope NEVER freezes over 150s+. Our gate had throttled
        # this to 1 Hz (the "flood" theory — which capture B showed was BACKWARDS:
        # SDR9700 sends MORE CI-V, not less, and never freezes). Without the regular
        # selected-receiver poll the radio stops its 27h scope output after ~101s
        # (our deaf-scope freeze). Restore 10 Hz to match the working reference.
        if not scope_only and now - self._smeter_sent_at >= 0.1:
            self._civ.poll_smeter()                        # 15 02 (S-meter) 10 Hz — scope keepalive
            self._smeter_sent_at = now
        # FORWARD-POWER (15 11) — poll ~5 Hz ONLY while keyed. Gated on TX so it
        # never adds CI-V load in RX (and the meter reads 0 in RX anyway). The
        # adapter's key_tx/unkey_tx toggle _civ._tx_active.
        if (not scope_only and getattr(self._civ, "_tx_active", False)
                and now - getattr(self, "_fwd_sent_at", 0.0) >= 0.2):
            self._civ.poll_fwdpower()                      # 15 11 (Po meter)
            self._fwd_sent_at = now
        if now - self._freq_polled_at >= 1.0:
            self._freq_polled_at = now
            if not scope_only:
                # round-robin ONE read per tick (LAN-only mode dial-sync).
                # MAIN-only: the SUB reads (25 01 / 26 01 / 07 D2) are gone — they
                # fed only an unused diagnostics field AND provoked the scope stall
                # sooner (see _on_iamready + the deaf-scope notes). Fewer freezes,
                # zero functional loss.
                READS = (bytes([0x25, 0x00]),   # selected freq (authority)
                         bytes([0x26, 0x00]),   # selected mode
                         bytes([0x14, 0x0A]))   # RF power SETTING (read-only display)
                i = getattr(self, "_read_rr", 0)
                self._civ._send_civ(READS[i % len(READS)])
                self._read_rr = i + 1

        # NOTE: the old scope-freeze watchdog (frame-progress timer -> 2 s openclose
        # re-open -> 10 s session recycle) was RETIRED 2026-07-10 once the SDR9700
        # transport port (PR #19) fixed the underlying deaf-scope stall. The 9700's
        # LAN scope no longer freezes every ~101 s, so there is nothing to re-open or
        # recycle for. The transport's own UdpCivData watchdog (a faithful port of
        # SDR9700's: 2 s no-data -> re-send openclose every 100 ms, in-session, no
        # churn) handles any genuine transient stall. We keep ONLY the real
        # radio-initiated-disconnect recovery below.
        #
        # RADIO KICKED US: the handler detects a radio-initiated disconnect
        # (status disc=1). A dead session can't be revived in place -> recycle it
        # (a fresh handler resets the flag).
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
        # WAIT for the real mode read (like initial_center_hz waits for freq).
        # Without the wait this returned None at connect (the 26 00 reply hadn't
        # landed yet), so the engine seeded AE with its "USB" default even when
        # the rig was on FM -> AE opened on the wrong mode. Prefer USB channel.
        if self._usb and self._usb.main_mode:
            return self._usb.main_mode
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            if self._civ and self._civ.mode:
                return self._civ.mode
            time.sleep(0.1)
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

    def _echo_mode(self, radio_mode):
        """Map the rig's actual (CI-V base) mode back to the AE string to report.
        If AE last asked for a data variant (DFM/DIGU/...) and the rig is still on
        the base mode that variant folds to, report the AE alias so its display
        keeps the data mode. If the rig's base mode has since diverged (a
        front-panel change), report the rig's real mode so that reaches AE too."""
        echo = getattr(self, "_ae_mode_echo", None)
        base = getattr(self, "_ae_mode_base", None)
        if echo and base and radio_mode == base:
            return echo
        return radio_mode

    def radio_mode(self):
        if self._usb:
            return self._echo_mode(self._usb.main_mode)
        return self._echo_mode(self._civ.mode) if self._civ else None

    def radio_power_level(self):
        """The rig's RF-power SETTING as a 0..100 LEVEL (read-only, from 14 0A).

        AE's 'RF Power' is a 0..100 PERCENT-of-maximum level (TxApplet
        setRange(0,100), 'percent of maximum'), NOT watts — so we report the
        percent, not a watts conversion. CI-V 14 0A gives 0..255 = 0..100%, so
        level = raw/255*100 (e.g. raw=3 -> 1%, which is what the rig shows and
        what measured ~0.78 W out). Actual forward watts belong on the FWDPWR
        meter during TX, a separate field. None until a reading lands."""
        raw = self._civ.rfpower_raw if self._civ else None
        if raw is None:
            return None
        return round(raw / 255.0 * 100.0)

    # Icom Po (forward-power) meter calibration: the 0..255 CI-V reading maps
    # NON-LINEARLY to a fraction of rated output. These breakpoints are the
    # standard Icom Po scale (0/50/100% at raw 0/143/213), interpolated. Scaled
    # to the band's rated max in _fwd_power_w. (Approximate; a bench cal per band
    # would refine it, but far better than the flat level% estimate.)
    _PO_CURVE = ((0, 0.0), (41, 0.10), (75, 0.25), (100, 0.33),
                 (143, 0.50), (190, 0.75), (213, 1.00), (255, 1.00))

    def _fwd_power_w(self):
        """Measured forward power in WATTS from the 15 11 Po meter, or None.
        Non-linear Icom Po curve -> fraction of rated, scaled to band max."""
        raw = self._civ.fwdpwr_raw if self._civ else None
        if raw is None:
            return None
        frac = self._PO_CURVE[-1][1]
        for (r0, f0), (r1, f1) in zip(self._PO_CURVE, self._PO_CURVE[1:]):
            if raw <= r1:
                frac = f0 + (f1 - f0) * ((raw - r0) / (r1 - r0)) if r1 > r0 else f0
                break
        f = self._civ.freq_hz if self._civ else None
        mhz = (f / 1e6) if f else 145.0
        band_max = 10.0 if 1200.0 <= mhz <= 1400.0 else 100.0      # 23cm = 10 W
        return round(frac * band_max, 2)

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
        # MAIN mode goes through _echo_mode so a data variant AE picked (DFM/
        # DIGU/...) is reported back as that variant, not the rig's coarser base.
        if self._usb:
            out = [{"freq_hz": self._usb.main_freq_hz,
                    "mode": self._echo_mode(self._usb.main_mode)}]
            if self.sub_active():
                out.append({"freq_hz": self._usb.rx2_freq_hz, "mode": self._usb.rx2_mode})
        else:
            out = [{"freq_hz": self._civ.freq_hz,
                    "mode": self._echo_mode(self._civ.mode)}]
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
            "radio": self.icom_model,
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
                      "dropped": (self._audio.dropped if self._audio else 0),
                      "tx_frames": (self._audio.tx_frames if self._audio else 0),
                      "tx_bytes": (self._audio.tx_bytes if self._audio else 0),
                      "tx_draining": (self._tx_audio_thread is not None
                                      and self._tx_audio_thread.is_alive())},
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
