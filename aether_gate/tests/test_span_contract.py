#
# Aether-gate — the set_span() contract: never advertise a span you don't deliver.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""An IQ adapter MUST report the span it actually delivers.

The engine (`Radio._set_pan_span_hz`) does:

    effective = self.adapter.set_span(self.span_mhz * 1e6)
    if effective:                       # <-- falsy => AE's REQUESTED span is kept
        self.span_mhz = float(effective) / 1e6

So an adapter whose `set_span` returns None/0 leaves the engine advertising
whatever AE asked for, while the hardware delivers its own fixed width. `iq_to_dbm`
then stretches that block across the pan regardless, and AE's frequency axis is
wrong by (delivered / requested) — signals land in the wrong place, smeared.

This is not hypothetical: KenwoodAdapter.set_span was a bare `pass`. It went
unnoticed at the 2.04 MHz RTL default only because that ~matches AE's default full
span (ratio ~1). Narrowing the dongle to 250 kHz exposed it — AE kept painting a
2.04 MHz axis with 250 kHz of data, which looked like "fewer signals".
"""
import pytest


class _FakeSdr:
    def __init__(self, samp_rate):
        self.samp_rate = float(samp_rate)


def _engine_set_pan_span(adapter, span_hz):
    """Faithful copy of Radio._set_pan_span_hz's span negotiation."""
    span_mhz = max(0.001, float(span_hz) / 1e6)
    if adapter is not None:
        try:
            effective = adapter.set_span(span_mhz * 1e6)
            if effective:
                span_mhz = float(effective) / 1e6
        except Exception:
            pass
    return span_mhz


@pytest.mark.parametrize("samp_rate", [2_040_000, 250_000, 48_000])
@pytest.mark.parametrize("ae_asks_hz", [14_000, 48_000, 200_000, 2_040_000])
def test_kenwood_reports_its_real_span(samp_rate, ae_asks_hz):
    """Whatever AE zooms to, the engine must end up advertising the DELIVERED width."""
    from aether_gate.adapters.kenwood.adapter import KenwoodAdapter

    a = KenwoodAdapter.__new__(KenwoodAdapter)      # no rig/dongle needed
    a._sdr = _FakeSdr(samp_rate)

    got_mhz = _engine_set_pan_span(a, ae_asks_hz)
    assert got_mhz * 1e6 == pytest.approx(samp_rate), (
        f"AE asked {ae_asks_hz} Hz, dongle delivers {samp_rate} Hz, "
        f"engine advertised {got_mhz*1e6:.0f} Hz")


def test_a_bare_pass_would_regress_this():
    """Guard the exact shape of the bug: set_span -> None keeps AE's wrong span."""
    class Broken:
        def set_span(self, span_hz):
            pass

    got_mhz = _engine_set_pan_span(Broken(), 48_000)
    assert got_mhz * 1e6 == 48_000, "sanity: a falsy return keeps AE's requested span"
    # ...which is precisely why an adapter must return its real width.


def test_hpsdr_already_honours_the_contract():
    from aether_gate.adapters.hpsdr.adapter import HpsdrAdapter

    a = HpsdrAdapter.__new__(HpsdrAdapter)
    a.samp_rate = 48_000
    assert _engine_set_pan_span(a, 2_040_000) * 1e6 == pytest.approx(48_000)


# --- USB lump sizing (the 0.5 s waterfall tick) ------------------------------
def test_rtl_bufflen_tracks_sample_rate():
    """librtlsdr's default 262144-byte transfer is 524 ms of signal at 250 kS/s —
    the panadapter can only update when a lump lands, so the display ticked at
    ~2 Hz while every layer above measured healthy. bufflen must scale with the
    sample rate (~30 ms of signal), stay on 16384-byte URB granules, and never
    fall below the 16384 floor."""
    from aether_gate.adapters.soapy import rtl_bufflen

    assert rtl_bufflen(250_000) == 16384          # 32.8 ms — was 524 ms
    assert rtl_bufflen(2_040_000) == 114688       # 28.1 ms — was 64 ms
    assert rtl_bufflen(48_000) == 16384           # floor
    for sr in (250_000, 1_020_000, 2_040_000, 3_200_000):
        bl = rtl_bufflen(sr)
        assert bl % 16384 == 0 and bl >= 16384
        assert (bl / 2 / sr) <= 0.035             # never lumpier than ~35 ms
