#
# Aether-gate — offset-tuning clearance and the panadapter dead band (#37).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Offset tuning buys a clean demodulator and PAYS for it in panadapter width.

The hardware centre is placed away from the slice so the DC spike — LO leakage
plus ADC offset, an artifact of every direct-conversion front end — falls
outside the demodulated channel. But the tuner still samples only
`centre ± samp_rate/2`, while the pan is labelled and painted as
`slice ± samp_rate/2`. The two windows are offset by exactly the clearance, so a
band of that width at one edge of the pan was never sampled at all and renders
as a hard-edged empty rectangle: "no signals here" when the truth is "not
looked at". On an instrument that is a measurement error, not a blemish (#37).

The clearance was `0.25 * samp_rate` — 510 kHz at 2.04 MS/s — which is ~170x
what clearing a 3 kHz SSB channel requires, and every hertz of it came off the
pan. It is now a fixed `_DC_CLEARANCE_HZ`, capped at a quarter rate for the
narrow rates where a fixed number will not fit.

Run:  python -m pytest aether_gate/tests/test_dc_clearance.py
"""
import pytest

np = pytest.importorskip("numpy")

from aether_gate.adapters.soapy import (SoapyAdapter, FM_PASS_HZ, SSB_PASS_HZ,
                                        _DC_CLEARANCE_HZ)

# Rates a real device actually runs, from the narrowest the gate advertises
# (min_span_hz = 48 kHz) up past an RTL's usual ceiling.
REAL_RATES = [48_000.0, 62_500.0, 125_000.0, 250_000.0, 384_000.0, 500_000.0,
              768_000.0, 1_020_000.0, 2_040_000.0, 2_400_000.0, 3_200_000.0,
              6_000_000.0, 8_000_000.0, 10_000_000.0]


def _adapter(samp_rate, center=145_000_000.0):
    a = SoapyAdapter(driver="none", samp_rate=samp_rate, center_hz=center)
    a._np = np
    return a


# --- the two constraints, over every rate ----------------------------------

@pytest.mark.parametrize("fs", REAL_RATES)
def test_clearance_keeps_dc_outside_the_widest_channel(fs):
    """LOWER bound: the spike must miss the channel we are demodulating.

    FM is the widest at +/-FM_PASS_HZ, so anything at or inside that would put
    the artifact in the operator's audio — which is the whole defect offset
    tuning exists to prevent (found live 2026-08-07: S9+20 of pure artifact,
    and six real S9+20 transmissions made no measurable difference).
    """
    off = _adapter(fs)._dc_offset_hz()
    assert off > FM_PASS_HZ, (
        f"{fs:.0f} S/s: clearance {off:.0f} Hz is inside the +/-{FM_PASS_HZ:.0f} Hz "
        "FM channel — the demodulator is back on the DC spike")


@pytest.mark.parametrize("fs", REAL_RATES)
def test_clearance_keeps_the_slice_inside_the_usable_window(fs):
    """UPPER bound: set_slice retunes past 0.40 * samp_rate.

    An offset at or beyond that edge would put the slice outside the window the
    moment we retuned there, and the next set_slice would retune again — a tuner
    that chases its own tail. This is the constraint a naive fixed clearance
    violates at narrow rates: 100 kHz is 0.8 of a 125 kS/s window.
    """
    off = _adapter(fs)._dc_offset_hz()
    assert off < 0.40 * fs, (
        f"{fs:.0f} S/s: clearance {off:.0f} Hz is outside the usable window")


# --- the shape of the rule -------------------------------------------------

def test_wide_rates_get_the_fixed_clearance_not_a_fraction():
    """Above ~400 kS/s the clearance stops scaling with the rate.

    This is the change that closes the dead band: at 2.04 MS/s the pan loses
    100 kHz instead of 510 kHz, and at 10 MS/s it loses 100 kHz instead of
    2.5 MHz. A wider window should not cost proportionally more spectrum — the
    spike does not get wider when the tuner opens up.
    """
    for fs in (500_000.0, 1_020_000.0, 2_040_000.0, 10_000_000.0):
        assert _adapter(fs)._dc_offset_hz() == _DC_CLEARANCE_HZ, (
            f"{fs:.0f} S/s did not get the fixed clearance")


def test_narrow_rates_fall_back_to_a_quarter_rate():
    """Below it, the old proportional rule still applies — it has to."""
    for fs in (48_000.0, 125_000.0, 250_000.0):
        assert _adapter(fs)._dc_offset_hz() == pytest.approx(0.25 * fs), (
            f"{fs:.0f} S/s should fall back to a quarter rate")


def test_the_fixed_clearance_clears_ssb_by_a_wide_margin():
    """Sanity on the constant itself: SSB is the common case at 3 kHz."""
    assert _DC_CLEARANCE_HZ > 30 * SSB_PASS_HZ


# --- the dead band, in the numbers from the issue --------------------------

def test_dead_band_at_2040k_is_the_clearance_not_a_quarter_rate():
    """#37's own worked example, asserted.

    slice 145.070, 2.040 MS/s. The pan is painted slice +/- rate/2; the tuner
    sampled centre +/- rate/2. The unsampled sliver is the offset between them.
    Before: 510 kHz of the 2.04 MHz pan (a quarter of it) was never sampled.
    """
    fs = 2_040_000.0
    slice_hz = 145_070_000.0
    a = _adapter(fs, center=140_000_000.0)
    a.set_slice(slice_hz)
    hw_center = a._retune_to

    pan_lo = slice_hz - fs / 2.0
    hw_lo = hw_center - fs / 2.0
    dead = hw_lo - pan_lo                 # painted but never sampled

    assert dead == pytest.approx(_DC_CLEARANCE_HZ)
    assert dead == pytest.approx(100_000.0)
    assert dead < 0.25 * fs, "no better than the quarter-rate offset it replaced"
    # 5.1x narrower than the 510 kHz the issue measured on the RSP1a.
    assert dead < 0.25 * fs / 5.0


# --- idempotence -----------------------------------------------------------

def test_retune_leaves_an_already_offset_centre_alone():
    """A centre sitting exactly at the clearance is correct, not "parked on DC".

    retune()'s proximity test used 0.05 * samp_rate, an unrelated second
    fraction that at 2.04 MS/s (102 kHz) sits just above the 100 kHz clearance —
    so a correctly-offset centre read as too close and got pushed again. Same
    destination here, but the rule now has one bound instead of two that
    disagree.
    """
    fs = 2_040_000.0
    slice_hz = 145_070_000.0
    a = _adapter(fs, center=140_000_000.0)
    a._slice_hz = slice_hz

    a.retune(slice_hz + a._dc_offset_hz())
    first = a._retune_to
    assert first == pytest.approx(slice_hz + _DC_CLEARANCE_HZ)

    a.retune(first)                        # feed its own answer back in
    assert a._retune_to == pytest.approx(first), "retune is not idempotent"


def test_a_centre_on_the_slice_is_still_pushed_off_dc():
    """The case retune() exists for must keep working."""
    fs = 2_040_000.0
    slice_hz = 145_070_000.0
    a = _adapter(fs, center=140_000_000.0)
    a._slice_hz = slice_hz
    a.retune(slice_hz)
    assert abs(a._retune_to - slice_hz) == pytest.approx(_DC_CLEARANCE_HZ)


# --- measured in spectrum, not in arithmetic --------------------------------
#
# The tests above assert the offset arithmetic. These two feed the adapter the
# IQ a tuner would really have produced — noise, with nothing past the analogue
# skirt at 0.40 * samp_rate — and then look at where the panadapter ends up with
# no energy. No radio, no SoapySDR, no AetherSDR: the block goes into the pan
# ring exactly as the reader thread stages it.
#
# THE FINDING THAT MATTERS, and it is not the one #37 leads with. The total dead
# width is INVARIANT: the skirt costs 0.2 * samp_rate of the pan whatever the
# offset is, and no choice of clearance recovers it. What the offset decides is
# WHERE that dead region lands:
#
#   0.25 * samp_rate  ->  a 408 kHz hole at -714..-306 kHz from centre, with
#                         live spectrum on BOTH sides of it. A black rectangle
#                         floating inside the band — an operator scanning it
#                         reads "no activity here", and is wrong.
#   100 kHz           ->  the dead region is contiguous with the pan edge, which
#                         is what band-edge rolloff is supposed to look like.
#
# So the fix is less "the dead band got smaller" than "the dead band stopped
# pretending to be signal-free spectrum in the middle of the band".

def _sampled_iq(n, fs, np_):
    """IQ as the tuner would deliver it: noise, nothing past the analogue skirt."""
    rng = np_.random.default_rng(0)
    x = rng.normal(size=n) + 1j * rng.normal(size=n)
    X = np_.fft.fft(x)
    fr = np_.fft.fftfreq(n, 1.0 / fs)
    X[np_.abs(fr) > 0.40 * fs] = 0.0
    return np_.fft.ifft(X)


def _dead_runs(fs, slice_hz, offset_override=None, n=65536):
    """Every contiguous run of empty bins in the painted pan, as
    (width_hz, lo_hz, hi_hz, touches_edge) with frequencies relative to centre."""
    a = _adapter(fs, center=140_000_000.0)
    a._slice_hz = slice_hz
    if offset_override is not None:
        a._dc_offset_hz = lambda: offset_override
    a.set_slice(slice_hz)
    a.center_hz = a._retune_to                  # the reader applies the retune
    a._pan_ring.append(_sampled_iq(n, fs, np))

    out = np.asarray(a.get_iq(n, slice_hz, fs))
    P = np.abs(np.fft.fftshift(np.fft.fft(out))) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))
    dead = P <= np.median(P[P > 0]) * 0.01

    runs, i = [], 0
    while i < len(dead):
        if dead[i]:
            j = i
            while j + 1 < len(dead) and dead[j + 1]:
                j += 1
            runs.append((f[j] - f[i], f[i], f[j], i == 0 or j == len(dead) - 1))
            i = j + 1
        else:
            i += 1
    return runs


def test_no_dead_region_sits_inside_the_band():
    """With the shipped clearance every empty region is band-edge rolloff.

    This is the assertion #37 is really about: a gap at the edge is a filter
    skirt and reads as one; a gap with live spectrum on both sides reads as
    "nothing is transmitting here", which is a measurement error on an
    instrument.
    """
    fs = 2_040_000.0
    interior = [r for r in _dead_runs(fs, 145_070_000.0)
                if not r[3] and r[0] > 0.01 * fs]
    assert not interior, (
        "panadapter has an interior dead region at "
        + ", ".join(f"{lo/1e3:+.0f}..{hi/1e3:+.0f} kHz ({w/1e3:.0f} kHz wide)"
                    for w, lo, hi, _ in interior))


def test_a_quarter_rate_offset_puts_a_hole_in_the_middle_of_the_band():
    """The defect, reproduced — so the constant above is pinned by consequence.

    Restore the old quarter-rate offset and an interior hole appears, ~0.2 * fs
    wide, with signal either side. This is what the RSP1a showed at 2.040 MS/s.
    """
    fs = 2_040_000.0
    interior = [r for r in _dead_runs(fs, 145_070_000.0, offset_override=0.25 * fs)
                if not r[3] and r[0] > 0.01 * fs]
    assert interior, "expected the old offset to strand a hole inside the band"
    widest = max(interior, key=lambda r: r[0])
    assert widest[0] > 0.15 * fs, (
        f"interior hole is only {widest[0]/1e3:.0f} kHz — expected ~{0.2*fs/1e3:.0f} kHz")


def test_the_skirt_costs_the_same_either_way():
    """Honesty check on the claim above: the fix MOVES dead spectrum, it does
    not recover any. Both offsets lose the same total width to the skirt."""
    fs = 2_040_000.0
    total_new = sum(r[0] for r in _dead_runs(fs, 145_070_000.0))
    total_old = sum(r[0] for r in _dead_runs(fs, 145_070_000.0, offset_override=0.25 * fs))
    assert abs(total_new - total_old) < 0.02 * fs, (
        f"new {total_new/1e3:.0f} kHz vs old {total_old/1e3:.0f} kHz — "
        "these should match; the skirt is inherent")
