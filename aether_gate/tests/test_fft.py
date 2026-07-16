#
# Aether-gate — tests for the core IQ->dBm transform.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""iq_to_dbm is the shared path for EVERY IQ adapter (soapy/RTL, HPSDR, kenwood,
yaesu), so a regression here degrades every panadapter at once. These tests pin
the two properties that matter:

  1. a narrow carrier lands at the RIGHT FREQUENCY, and
  2. it stays clear of the noise floor after the reduction to pan columns.

⚠ Frequency is checked against numpy's OWN fftshift/fftfreq axis, binned exactly
as the implementation bins it. Do NOT hand-roll `(col/n_bins - 0.5) * sr` — that
assumes columns map linearly 1:1 onto FFT bins, which is false whenever n_bins
does not divide the block length (array_split then yields columns of 2 AND 3
bins). That wrong formula produced three false failures while writing these.
"""
import numpy as np
import pytest

from aether_gate.core.fft import iq_to_dbm

N = 4096                     # SoapyAdapter's CHUNK — the real block size
BINS = 1600                  # a typical AE pan width
MIN_DBM, MAX_DBM = -140.0, 0.0


def _synth(n, sr, tone_hz, amp=0.02, noise=0.01, seed=7):
    """A single narrow carrier in complex noise — the panadapter's real case."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / sr
    return (amp * np.exp(2j * np.pi * tone_hz * t)
            + rng.normal(0, noise, n) + 1j * rng.normal(0, noise, n))


def _column_hz(n, sr, n_bins):
    """Frequency of each output column, reduced exactly as iq_to_dbm reduces."""
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / sr))
    return np.array([c.mean() for c in np.array_split(freqs, n_bins)])


@pytest.mark.parametrize("sr", [2_040_000, 250_000, 48_000])
@pytest.mark.parametrize("tone", [1500, -8000, 20000])
def test_carrier_lands_on_the_right_frequency(sr, tone):
    if abs(tone) > sr / 2 - 1000:
        pytest.skip("tone outside this span")
    d = np.array(iq_to_dbm(_synth(N, sr, tone), BINS, MIN_DBM, MAX_DBM))
    col_hz = _column_hz(N, sr, BINS)
    got = col_hz[int(np.argmax(d))]
    # tolerance: ~3 FFT bins (hanning spreads a tone) or one column, whichever wider
    tol = max(3 * sr / N, sr / BINS)
    assert abs(got - tone) <= tol, f"peak at {got:+.1f} Hz, expected {tone:+d} Hz"


@pytest.mark.parametrize("sr", [2_040_000, 250_000, 48_000])
def test_narrow_carrier_stays_above_the_floor(sr):
    """The point of FFT-then-bin: a narrow signal must not be diluted into noise."""
    d = np.array(iq_to_dbm(_synth(N, sr, 1500), BINS, MIN_DBM, MAX_DBM))
    dyn = d.max() - np.median(d)
    assert dyn > 25.0, f"only {dyn:.1f} dB above the floor — signal is being lost"


def test_beats_the_old_subsample_then_fft():
    """Regression guard for the bug this replaced.

    The old code did `x = x[idx]` (take every Nth sample) BEFORE the FFT, which
    aliases: 61% of a 4096-sample block was discarded and its energy folded back
    onto the survivors. Modest but real — keep the new path at least as good.
    """
    sr, tone = 250_000, 1500
    x = _synth(N, sr, tone)

    idx = np.linspace(0, x.size - 1, BINS).astype(int)
    xo = x[idx]
    win = np.hanning(BINS)
    spec = np.fft.fftshift(np.fft.fft(xo * win))
    old = np.clip(20.0 * np.log10(np.maximum(np.abs(spec) / BINS, 1e-12)),
                  MIN_DBM, MAX_DBM)
    new = np.array(iq_to_dbm(x, BINS, MIN_DBM, MAX_DBM))

    old_dyn = old.max() - np.median(old)
    new_dyn = new.max() - np.median(new)
    assert new_dyn >= old_dyn, f"regressed: new {new_dyn:.1f} dB < old {old_dyn:.1f} dB"


@pytest.mark.parametrize("n_in", [512, 1600, 4096, 4097, 65536])
def test_output_contract(n_in):
    """Always exactly n_bins values, always clamped — whatever the input length.
    4097 covers the non-multiple case; 512 the fewer-samples-than-columns case."""
    d = iq_to_dbm(_synth(n_in, 250_000, 1000), BINS, MIN_DBM, MAX_DBM)
    assert len(d) == BINS
    assert all(MIN_DBM <= v <= MAX_DBM for v in d)


def test_empty_and_silent_inputs():
    assert iq_to_dbm([], BINS, MIN_DBM, MAX_DBM) == [MIN_DBM] * BINS
    d = iq_to_dbm(np.zeros(N, dtype=complex), BINS, MIN_DBM, MAX_DBM)
    assert set(d) == {MIN_DBM}, "digital silence must clamp to the floor"
