#
# Aether-gate — equivalence tests for the audio-path fast forms.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The audio path was rewritten for SPEED, not for behaviour, so every one of
these tests asserts the new form against the OLD one it replaced. If a fast form
ever stops matching its reference, that is a defect no matter how quick it is.

Why the speed work was needed at all: `get_audio()` is pull-based on a
128/24000 = **5.33 ms** budget and was taking **38.95 ms** on a Pi 4, so the
audio thread ran at 13% of real time, the bounded `_audio_q` deque silently
discarded the surplus, and the operator heard chopped audio ("clip-clop").
The Pi was NOT short of CPU — 40% of the machine was idle.

⚠ THE TRAP THESE PIN: `_decim = samp_rate // AUDIO_RATE` can land on a PRIME.
At 2.000 MS/s it is 83, so `_factor_decim` returns [83] and the staged-decimation
design collapses into the single full-rate FIR its own docstring calls "~13x too
slow". 2.040 MS/s gives 85 = 5*17 and is 3.6x cheaper for a one-number change.
`test_prime_decimation_is_a_single_stage` documents that cliff so it is a known
property rather than a surprise.

⚠ NUMPY IS OPTIONAL IN THIS PROJECT. The gate runs pure-stdlib when numpy is
absent (see core/fft.py), and the CI matrix has a job with no numpy installed —
so this file must SKIP cleanly rather than crash. It is run BOTH ways: under
pytest, and as a bare module from the CI allow-list, so neither pytest nor numpy
can be imported unconditionally at module scope.
"""
import sys

try:
    import numpy as np
except ImportError:                                  # pragma: no cover
    np = None

AUDIO_RATE = 24000


def _skip(reason):
    """Skip under pytest; exit 0 as a bare module. Both are how this file runs.

    ⚠ Keyed on whether pytest is DRIVING this run, not on whether it is
    importable: pytest is often installed on a host that is executing the file
    as a plain module, and calling pytest.skip() there raises Skipped and exits
    NON-ZERO — which reads to CI as a failing test rather than a skipped one.
    """
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(reason, allow_module_level=True)
    print(f"SKIP: {reason}")
    raise SystemExit(0)


if np is None:
    _skip("numpy not installed - the demod path it tests is numpy-only")


# --------------------------------------------------------------------------
# reference implementations: exactly what the code did BEFORE the speed work
# --------------------------------------------------------------------------
def _ref_bin_peaks(dbm, n_bins):
    """The original per-column list comprehension."""
    return np.array([c.max() for c in np.array_split(dbm, n_bins)])


def _fast_bin_peaks(dbm, n_bins):
    """The vectorised form now in core/fft.py."""
    q, r = divmod(dbm.size, n_bins)
    if r == 0:
        return dbm.reshape(n_bins, q).max(axis=1)
    head = dbm[:r * (q + 1)].reshape(r, q + 1).max(axis=1)
    tail = dbm[r * (q + 1):].reshape(n_bins - r, q).max(axis=1)
    return np.concatenate([head, tail])


def _ref_nco(blocks, step):
    """The original per-sample np.exp(1j*ph) mixer."""
    ph0 = 0.0
    out = []
    for b in blocks:
        ph = ph0 + step * np.arange(len(b))
        out.append(b * np.exp(1j * ph))
        ph0 = (ph[-1] + step) % (2.0 * np.pi)
    return np.concatenate(out)


def _fast_nco(blocks, step):
    """The cached-ramp mixer now in adapters/soapy.py."""
    ph0, ramp, rn, rs, out = 0.0, None, 0, None, []
    for b in blocks:
        n = len(b)
        if ramp is None or rn != n or rs != step:
            ramp, rn, rs = np.exp(1j * step * np.arange(n)), n, step
        out.append(b * (np.exp(1j * ph0) * ramp))
        ph0 = (ph0 + step * n) % (2.0 * np.pi)
    return np.concatenate(out)


def _taps(M):
    """Same anti-alias FIR the adapter builds per stage."""
    nt = 4 * M + 1
    idx = np.arange(nt) - (nt - 1) / 2.0
    h = np.sinc(2 * (0.45 / M) * idx) * np.hamming(nt)
    return (h / h.sum()).astype(np.float64)


def _run_stages(stages, blocks, fast):
    """Drive the stage loop over MANY blocks, so overlap-save state and comb
    phase have to carry correctly — a single block would not catch that."""
    firs = [[_taps(M), np.zeros(4 * M, dtype=np.complex128), M, 0] for M in stages]
    out = []
    for blk in blocks:
        sig = blk.astype(np.complex128)
        for fir in firs:
            taps, state, M, offs = fir
            x = np.concatenate([state, sig])
            if fast:
                n_out = 0 if len(x) < len(taps) else len(x) - len(taps) + 1
                n_keep = 0 if n_out <= offs else (n_out - offs + M - 1) // M
                if n_keep > 0:
                    starts = offs + np.arange(n_keep) * M
                    win = x[starts[:, None] + np.arange(len(taps))]
                    nxt = win @ taps[::-1]
                else:
                    nxt = np.zeros(0, dtype=x.dtype)
                fir[3] = (offs - n_out) % M
            else:
                y = np.convolve(x, taps, mode="valid")
                nxt = y[offs::M]
                fir[3] = (offs - len(y)) % M
            fir[1] = x[len(x) - (len(taps) - 1):]
            sig = nxt
        out.append(sig)
    return np.concatenate(out)


def _blocks(count, n=4096, seed=7):
    rng = np.random.default_rng(seed)
    return [rng.standard_normal(n) + 1j * rng.standard_normal(n) for _ in range(count)]


def _real_adapter(samp_rate, slice_off_hz=1234.0):
    """A SoapyAdapter wired up for demod WITHOUT touching hardware.

    ⚠ This is the point of the file: the tests must drive the SHIPPED
    `_demod_block`, not a copy of it living in the test. An earlier version of
    these tests compared two local reference functions and therefore passed
    happily with the real code deliberately broken.
    """
    from aether_gate.adapters.soapy import SoapyAdapter

    a = SoapyAdapter(driver="none", samp_rate=samp_rate, center_hz=145_000_000)
    a._np = np                      # normally set in open()
    a._init_demod()                 # builds stages, SSB taps, resampler state
    a._slice_hz = a.center_hz + slice_off_hz
    a._mode = "USB"
    return a


def _ref_demod_block(a, block):
    """What `_demod_block` did BEFORE the speed work, driven off the adapter's
    own live state so the two forms share taps, decimation and SSB filter."""
    iq = block.astype(np.complex128)
    f_off = a._slice_hz - a.center_hz
    step = 2.0 * np.pi * (-f_off) / a.samp_rate
    ph = a._nco_phase + step * np.arange(len(iq))
    iq = iq * np.exp(1j * ph)
    a._nco_phase = (ph[-1] + step) % (2.0 * np.pi)
    sig = iq
    for fir in a._stage_firs:
        taps, state, M, offs = fir
        x = np.concatenate([state, sig])
        y = np.convolve(x, taps, mode="valid")
        fir[1] = x[len(x) - (len(taps) - 1):]
        fir[3] = (offs - len(y)) % M
        sig = y[offs::M]
    if a._is_fm_mode(a._mode):
        return a._demod_fm(sig)
    taps = a._ssb_lsb if a._mode.startswith("LSB") else a._ssb_usb
    x = np.concatenate([a._ssb_state, sig])
    y = np.convolve(x, taps, mode="valid")
    a._ssb_state = x[len(x) - (len(taps) - 1):]
    return 2.0 * np.real(y)


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_pan_binning_matches_the_list_comprehension():
    """Vectorised peak-per-column == the original, INCLUDING uneven splits.

    The uneven case is the one worth pinning: array_split puts the extra sample
    in the FIRST r columns, so a naive reshape that drops the remainder would
    silently lose the top of the span.
    """
    rng = np.random.default_rng(1)
    for size, bins in [(4096, 1600), (4096, 475), (8192, 1600), (1000, 7),
                       (4096, 1000), (999, 100), (2048, 333), (4097, 1600)]:
        dbm = rng.standard_normal(size)
        ref, fast = _ref_bin_peaks(dbm, bins), _fast_bin_peaks(dbm, bins)
        assert ref.shape == fast.shape, f"shape differs at {size}->{bins}"
        assert np.array_equal(ref, fast), f"values differ at {size}->{bins}"


def test_nco_ramp_matches_per_sample_exp_over_many_blocks():
    """Cached ramp == per-sample exp, with phase continuity across 40 blocks.

    40 blocks matters: the ramp is reused and only the start phase advances, so
    any drift in the phase bookkeeping compounds and shows up here rather than
    in a single-block test.
    """
    for f_off in (0.0, 1234.0, -45678.0, 250000.0):
        step = 2.0 * np.pi * (-f_off) / 2040000.0
        blocks = _blocks(40)
        ref, fast = _ref_nco(blocks, step), _fast_nco(blocks, step)
        # float noise only: the ramp multiplies where the reference re-evaluates
        assert np.max(np.abs(ref - fast)) < 1e-9, f"NCO diverges at {f_off} Hz"


def test_strided_decimation_matches_convolve_then_discard():
    """Computing only the kept samples == convolving then discarding.

    Includes [83] (the prime-decimation cliff at 2.000 MS/s) and [5, 17] (the
    2.040 MS/s case), driven over 8 blocks so overlap-save and comb phase carry.
    """
    for stages in ([5, 17], [83], [5, 4, 3], [17], [2, 2, 5], [85]):
        blocks = _blocks(8)
        ref = _run_stages(stages, blocks, fast=False)
        fast = _run_stages(stages, blocks, fast=True)
        assert ref.shape == fast.shape, f"length differs for {stages}"
        assert np.allclose(ref, fast, rtol=0, atol=1e-12), f"values differ for {stages}"


def test_real_demod_block_matches_the_pre_speedup_reference():
    """THE LOAD-BEARING TEST: the SHIPPED `_demod_block` against the old form.

    Both rates are exercised because they take different paths through
    `_factor_decim`: 2.040 MS/s -> [5, 17], and 2.000 MS/s -> [83], the prime
    case where the strided form matters most (6.1x measured).

    Two independent adapters are used so the reference cannot be contaminated by
    the state the fast path advances (NCO phase, overlap-save, comb offsets).
    """
    for samp_rate in (2_040_000, 2_000_000):
        a_fast = _real_adapter(samp_rate)
        a_ref = _real_adapter(samp_rate)
        for blk in _blocks(8):
            got = a_fast._demod_block(blk)
            want = _ref_demod_block(a_ref, blk)
            assert got.shape == want.shape, (
                f"length differs at {samp_rate}: {got.shape} vs {want.shape}")
            assert np.allclose(got, want, rtol=0, atol=1e-9), (
                f"demod output differs at {samp_rate}, "
                f"maxdiff={np.max(np.abs(got - want)):.3e}")


def test_real_iq_to_dbm_binning_handles_an_uneven_split():
    """THE OTHER LOAD-BEARING TEST: the SHIPPED iq_to_dbm, uneven split.

    4096 samples into 1600 columns does NOT divide evenly, so a fast form that
    drops the remainder loses the top of the span. Compared against the original
    per-column reduction applied to the same spectrum.
    """
    from aether_gate.core.fft import iq_to_dbm, WINDOW_COHERENT_GAIN

    rng = np.random.default_rng(3)
    n, bins = 4096, 1600
    iq = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    iq[100] += 50.0                                  # a peak that must survive binning
    got = np.array(iq_to_dbm(iq, bins, -140.0, 0.0))

    win = np.hanning(n)
    spec = np.fft.fftshift(np.fft.fft(iq * win))
    # Same normalisation the shipped path uses — length AND the window's
    # coherent gain, so a full-scale carrier anchors at 0 dBFS. This test is
    # about the BINNING being identical, so the reference has to share the
    # scaling or it measures the calibration instead. (Added 2026-08-31 with
    # the shared dBFS->dBm seam.)
    mag = np.abs(spec) / (n * WINDOW_COHERENT_GAIN)
    dbm = 20.0 * np.log10(np.maximum(mag, 1e-12))
    want = np.clip(_ref_bin_peaks(dbm, bins), -140.0, 0.0)

    assert got.shape == want.shape, f"{got.shape} vs {want.shape}"
    assert np.allclose(got, want, rtol=0, atol=1e-12), (
        f"binning differs, maxdiff={np.max(np.abs(got - want)):.3e}")


def test_prime_decimation_is_a_single_stage():
    """Document the cliff: a prime decimation cannot be split into cheap stages.

    This is not a bug in _factor_decim — it is arithmetic. The point is that the
    RATE CHOICE decides whether the audio path is affordable, so it is asserted
    rather than left to be rediscovered by ear.
    """
    from aether_gate.adapters.soapy import SoapyAdapter

    assert SoapyAdapter._factor_decim(2000000 // AUDIO_RATE) == [83]      # prime: one huge stage
    assert SoapyAdapter._factor_decim(2040000 // AUDIO_RATE) == [5, 17]   # composite: cheap stages


def _main():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    print("test_demod_equivalence:", "all checks passed" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())
