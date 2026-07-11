#
# Aether-gate - IC-9700 CI-V/scope stream: the 9700-specific adapter layer.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Transport (UdpCivData/UdpBase) ported from github.com/w5jwp/SDR9700 (GPL-3.0) by Justin W5JWP.
#
"""Ic9700Civ = the IC-9700 band-scope + control adapter on top of the ported
SDR9700 UDP CI-V data transport.

The heavy lifting (session seq/ack/retransmit, ping/idle/token cadence, the
sendOpenClose data-stream open + 2 s stall watchdog, scope-frame extraction into
`latest_dbm`/`frames`) all lives in the ported `UdpCivData` base (civ_transport.py
<- SDR9700 UdpCivData.cpp). This subclass adds ONLY the 9700-specific CI-V command
builders — scope enable, span, sweep speed — via the inherited `_send_civ()` seam.

The `_Ic9700Stream` subclass in icom9700.py adds freq/mode tracking, the tuner,
RX2 swap and the poll_smeter helper on top of THIS class.
"""
from .civ_transport import UdpCivData

CONTROLLER_CIV = 0xE0   # our controller CI-V address (the "E0" in FE FE <radio> E0 ...)


class Ic9700Civ(UdpCivData):
    """The 9700 band-scope stream + its scope-config commands.

    Inherits the full SDR9700-ported transport from UdpCivData; adds the
    9700-specific scope-enable / span / speed CI-V commands.
    """

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
        """Scope sweep speed: 0=FAST, 1=MID, 2=SLOW (27 1A). FAST ~ better fps;
        the radio was found on SLOW which caps waveform frames at ~4/s."""
        self._send_civ(bytes([0x27, 0x1A, 0x00, idx & 0x03]))
