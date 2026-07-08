#
# Aether-gate - IC-9700 LAN CI-V stream: open + scope-data subscribe + parse.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Ported from github.com/w5jwp/SDR9700 (GPL-3.0): UdpCivData.cpp + ScopeAdapter.h
# + RadioCapabilities.h (scope command map). Attribution preserved.
#
"""CI-V data stream for the IC-9700 LAN protocol.

Reuses the threaded UdpBase transport (same ping/idle/retransmit cadence the
radio insists on). On the CI-V port assigned by conninfo it:
  1. runs the are-you-there / 0x06 handshake,
  2. sends openclose(magic=0x04) to start the data stream,
  3. enables the band scope: CI-V `27 10 01` (scope ON) then `27 11 01`
     (scope waveform data output to CI-V ON)  -- bytes confirmed from
     SDR9700 RadioCapabilities.h func table,
  4. parses the `27 00 00 ... FD` waveform frames and converts each scope
     byte (0..159) to dBm via ScopeAdapter's mapping (-130 .. -10 dBm).

Exposes latest_dbm (list[float]) for the RadioAdapter (provides="spectrum").
"""
import struct
import threading

from .udpbase import UdpBase

CONTROLLER_CIV = 0xE0
SCOPE_MIN_DBM = -130.0
SCOPE_MAX_DBM = -10.0
SCOPE_SPAN = SCOPE_MAX_DBM - SCOPE_MIN_DBM

# CI-V scope frame: FE FE E0 <civ> 27 00 <rcvr> <div curr><div tot> <12-byte
# mode/bounds incl. centre freq BCD> <pixels...> FD.  Per SDR9700 (UdpCivData):
# marker "27 00 00" -> pos=marker+2; seq1 header = d[pos+3 : pos+3+12], waveform
# pixels begin at pos+3+12 = marker+17.  IC-9700 ~ 475 pixels.
_SCOPE_HDR_AFTER_MARKER = 17
_SCOPE_BOUNDS_START = 5          # marker+5 .. marker+17 = 12-byte mode/bounds


def _byte_to_dbm(b):
    if b > 159:
        b = 159
    return SCOPE_MIN_DBM + (b / 159.0) * SCOPE_SPAN


class Ic9700Civ(UdpBase):
    """CI-V stream reader. Adopts the control handler's reserved civ socket
    (handler keeps `_civ_sock` bound at the port it advertised in conninfo)."""

    def __init__(self, local_ip, radio_ip, civ_port, sock, civ_addr=0xA2):
        # Reuse the pre-bound reservation socket instead of binding a new port
        # (the radio sends data to the civ_local_port we advertised in conninfo).
        self.local_ip = local_ip
        self.radio_ip = radio_ip
        self.radio_port = civ_port
        self.name = "civ"
        self.sock = sock
        self.sock.settimeout(0.05)
        self.local_port = sock.getsockname()[1]
        o = local_ip.split(".")
        self.my_id = ((int(o[2]) << 24) | (int(o[3]) << 16) | (self.local_port & 0xFFFF))
        self.remote_id = 0
        self._send_seq = 1
        self._ping_seq = 0
        self._lock = threading.Lock()
        self._tx_hist = {}
        self._rx_last = None             # last radio->us seq (single int; see UdpBase._track_rx)
        self._rx_missing = {}
        self._run = False
        self._connected = False
        self._t_reader = None
        self._t_timers = None
        self._last_ping = 0.0
        self._last_idle = 0.0
        self._last_retx = 0.0
        self.n_sent = 0                  # tracked packets sent (deaf-scope instrumentation)
        self.n_retx_req = 0              # retransmit requests sent
        self.n_rx_clears = 0             # RX seq-tracker resets
        self.n_rx_dgrams = 0             # ALL datagrams received from the radio
        self.last_rx_at = 0.0            # monotonic time of the last radio datagram
        self.on_data = self._on_civ
        # civ-specific
        self.civ_addr = civ_addr
        self._seqB = 0                 # CI-V data-stream sequence (BE @0x13)
        self.latest_dbm = None
        self.frames = 0
        self.first_raw = None          # hex of the first scope frame (for layout check)
        self.bounds_raw = None         # the 12 mode/bounds bytes (start/end freq etc.)
        self.max_byte = 0              # peak raw scope byte seen across all frames
        self.best_raw = None           # hex of the frame with the highest peak
        self.dgram_lens = {}           # diagnostic: {datagram_len: count}
        self.samples = []              # diagnostic: full hex of first few payload datagrams
        self.n_fb = 0                  # diagnostic: OK replies seen
        self.n_fa = 0                  # diagnostic: NG (rejected) replies seen

    # --- discovery hook: stream is ready, open it + enable the scope --------
    def _on_iamready(self):
        self._send_openclose(opening=True)
        self.enable_scope()

    def enable_scope(self):
        # Full bring-up per SDR9700 RadioBackend::sendScopeEnable (it retries
        # this set until waveform data actually flows): on + output + MAIN.
        # Without 27 12 00 the radio can sit on the SUB scope and stream
        # all-zero main-scope frames.
        self._send_civ(bytes([0x27, 0x10, 0x01]))   # scope ON
        self._send_civ(bytes([0x27, 0x11, 0x01]))   # scope data output -> CI-V ON
        self._send_civ(bytes([0x27, 0x12, 0x00]))   # scope select = MAIN

    # IC-9700 center-mode spans, Hz (the radio wants the BCD frequency form on
    # the wire — the index form 27 15 00 <idx> is ACKed FB but ignored)
    SPANS_HZ = (2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000)

    def set_span(self, span_hz):
        """Center-mode span in Hz (one of SPANS_HZ; the radio snaps otherwise).
        NB the wire/UI value is the ± half-width: 500_000 = "±500k" = 1 MHz shown."""
        hz = int(span_hz)
        bcd = bytearray()
        for _ in range(5):
            lo = hz % 10; hz //= 10
            hi = hz % 10; hz //= 10
            bcd.append((hi << 4) | lo)
        self._send_civ(bytes([0x27, 0x15, 0x00]) + bytes(bcd))

    def set_speed(self, idx):
        """Scope sweep speed: 0=FAST, 1=MID, 2=SLOW (27 1A). FAST ≈ better fps;
        the radio was found on SLOW which caps waveform frames at ~4/s."""
        self._send_civ(bytes([0x27, 0x1A, 0x00, idx & 0x03]))

    # --- packet builders ---------------------------------------------------
    def _send_openclose(self, opening):
        b = bytearray(0x16)
        struct.pack_into("<IHHII", b, 0, 0x16, 0x0000, 0x0000, self.my_id, self.remote_id)
        struct.pack_into("<H", b, 0x10, 0x01C0)            # data
        struct.pack_into(">H", b, 0x13, self._seqB & 0xFFFF)
        b[0x15] = 0x04 if opening else 0x00               # magic
        self._seqB = (self._seqB + 1) & 0xFFFF
        self.send_tracked(bytes(b))

    def _send_civ(self, civ_cmd):
        frame = bytes([0xFE, 0xFE, self.civ_addr, CONTROLLER_CIV]) + civ_cmd + bytes([0xFD])
        hdr = bytearray(0x15)
        struct.pack_into("<IHHII", hdr, 0, 0x15 + len(frame), 0x0000, 0x0000,
                         self.my_id, self.remote_id)
        hdr[0x10] = 0xC1                                   # reply
        struct.pack_into("<H", hdr, 0x11, len(frame))     # datalen (LE)
        struct.pack_into(">H", hdr, 0x13, self._seqB & 0xFFFF)
        self._seqB = (self._seqB + 1) & 0xFFFF
        self.send_tracked(bytes(hdr) + frame)

    # --- scope frame parse -------------------------------------------------
    def _on_civ(self, d):
        # diagnostics: tally datagram sizes + keep the first couple of samples
        # of EVERY size bucket (short ones = CI-V echoes + FB/FA replies to our
        # scope-enable commands — an FA there means the radio rejected them)
        self.dgram_lens[len(d)] = self.dgram_lens.get(len(d), 0) + 1
        if self.dgram_lens[len(d)] <= 2:
            self.samples.append((len(d), d.hex()))
        to_us = bytes([0xFE, 0xFE, CONTROLLER_CIV, self.civ_addr])
        if d.find(to_us + b"\xfb") >= 0:
            self.n_fb += 1
        if d.find(to_us + b"\xfa") >= 0:
            self.n_fa += 1
        m = d.find(b"\x27\x00\x00")
        if m < 0:
            return
        end = d.find(b"\xfd", m)
        if end < 0:
            return
        if self.first_raw is None:
            self.first_raw = bytes(d[m:end + 1]).hex()
            self.bounds_raw = bytes(d[m + _SCOPE_BOUNDS_START:m + _SCOPE_HDR_AFTER_MARKER]).hex()
        pixels = d[m + _SCOPE_HDR_AFTER_MARKER:end]
        if pixels:
            self.latest_dbm = [_byte_to_dbm(x) for x in pixels]
            self.frames += 1
            peak = max(pixels)
            if peak > self.max_byte:
                self.max_byte = peak
                self.best_raw = bytes(d[m:end + 1]).hex()
