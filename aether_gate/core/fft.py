#
# Aether-gate — IQ -> dBm spectrum transform (the core-side FFT for IQ adapters).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Core-side FFT so every IQ adapter shares one transform.

A spectrum adapter returns dBm bins directly; an IQ adapter returns complex
samples and the core calls iq_to_dbm() here. Keeping the transform in the core
(not in each adapter) is the point of the "narrow waist": one well-tested
IQ->panadapter path, not N.

numpy is used when available (the real path for SoapySDR/RTL-SDR IQ). A pure-
stdlib fallback keeps the core importable with no third-party deps so the sim
adapter and unit tests run anywhere; it is not meant to be fast.
"""
import math

try:
    import numpy as _np
except Exception:                                  # pragma: no cover - exercised when numpy absent
    _np = None


# ---- dBFS -> dBm calibration -------------------------------------------------
#
# Normalised sample units carry no absolute power reference: a Soapy CF32 stream
# is +/-1.0 full scale whatever the front end is doing. Turning that into dBm
# needs a constant that depends on the device, its gain and its antenna, so
# there is exactly ONE of them and BOTH the panadapter and the S-meter apply it.
#
# Before 2026-08-31 each path had its own. The panadapter applied no gain
# correction at all, so turning the RF gain up 20 dB relabelled the entire dBm
# axis 20 dB louder while the physical noise had not moved; the S-meter did back
# the gain out. Measured on identical white noise, the two agreed to 3.8 dB at
# 12 dB of gain and disagreed by 16.2 dB at 32 dB.
#
# WHICH device is the whole question: the anchor is a property of the front
# end, so it is keyed by SoapySDR driver (DBFS_TO_DBM_BY_DRIVER, below) and the
# bare constant here is only the fallback for a driver nobody has put a
# reference receiver against. It is the pre-2026-08-31 guess, anchored on
# ITU-R P.372, and it stays a guess until a second device family is measured.
DBFS_TO_DBM = -30.0

# SDRplay: MEASURED, not assumed (2026-08-31). The -30.0 fallback read ~11 dB
# hot on an RSPdx-R2: static on 80 m pegged the S-meter at S9.
#
# Calibrated against SDRconnect on the same RSPdx-R2, antenna and 12 dB gain,
# at 3.722 MHz with SDRconnect's AGC OFF so both radios saw one front end.
# Its spectrum floor there is -110 dBm at 10.07 Hz RBW. Two independent paths
# through this gate were converted to true mean noise power and compared:
#
#   panadapter  -104.18 dBm displayed at 7.63 Hz bins. AE's floor readout is a
#               two-pass trimmed mean, which for exponentially distributed bin
#               powers sits 7.47 dB under the true mean (simulated), so true
#               power is -96.71 dBm in an 11.44 Hz ENBW = -97.26 at 10.07 Hz.
#               -> -12.7 dB
#   S-meter     -75.0 dBm of noise in a 3007 Hz passband, from a median
#               estimator whose /ln(2) correction makes it an exact mean.
#               -110 dBm at 10.07 Hz is -85.25 dBm in 3007 Hz. -> -10.25 dB
#
# Two estimators with different biases landing 2.5 dB apart is the check that
# the model holds; the midpoint is the constant. Good to about +/-2 dB, which is
# the slop in reading a noise floor off any spectrum display. Per-station
# adjustment stays with --dbm-trim.
#
# Repeated 2026-09-01 on an RSPduo (Tuner 2, a different antenna, the same
# IFGR 47 / LNA 0): S-meter path +1.75 dB and pan path +1.4 dB against
# SDRconnect's -117.5 dBm floor, i.e. about -42.5 for that unit. Two SDRplay
# units agreeing to 1.5 dB is what makes -41.0 a family number rather than
# one bench's.
#
# Do NOT re-derive this from SDRconnect's PWR/SNR readout. That is
# AGC/detector-derived and sits 8-14 dB below its own integrated spectrum;
# anchoring on it once produced a -24 dB "correction" that would have put this
# axis 20 dB into fiction.
#
# An entry belongs in this table only with a reference receiver behind it: a
# value that is right for one front end moves every other device's numbers,
# including hardware nobody has checked it against. Drivers not listed use
# DBFS_TO_DBM, and --dbm-base overrides either for a device the operator has
# measured themselves.
DBFS_TO_DBM_BY_DRIVER = {
    "sdrplay": -41.0,      # hw-measured: RSPdx-R2 2026-08-31, RSPduo 2026-09-01
}


def dbfs_to_dbm_for(driver):
    """The dBFS->dBm anchor for a SoapySDR driver name, or the fallback."""
    return DBFS_TO_DBM_BY_DRIVER.get(str(driver or "").lower(), DBFS_TO_DBM)

# The front-end gain the constant above is referenced to. Gain is backed out
# relative to this so the dBm scale reports what is at the ANTENNA rather than
# where the operator left the gain knob.
GAIN_REF_DB = 20.0

# Hanning coherent gain (mean of the window). The panadapter divides by this so
# a full-scale carrier reads 0 dBFS. Deliberately COHERENT gain, not power gain:
# a panadapter exists to show carrier amplitude correctly. Noise consequently
# reads 1.76 dB high relative to a power-correct measure, which is the standard
# Hanning noise-bandwidth penalty and not an error.
WINDOW_COHERENT_GAIN = 0.5


def dbm_offset_for(gain_db, trim_db=0.0, base_db=None):
    """Total dB to add to a dBFS figure to get dBm at this front-end gain.

    The single seam both the panadapter and the S-meter go through, so the two
    scales cannot drift apart again. `base_db` is the device's anchor (an
    adapter's dbm_base); None means the unkeyed fallback.
    """
    base = DBFS_TO_DBM if base_db is None else float(base_db)
    return base + float(trim_db) - (float(gain_db) - GAIN_REF_DB)


def iq_to_dbm(iq, n_bins, min_dbm, max_dbm, dbm_offset=0.0):
    """Convert a block of complex IQ samples to `n_bins` dBm magnitudes.

    Windowed FFT -> fftshift (DC centre) -> 20*log10 magnitude -> clamp to the
    AE display range [min_dbm, max_dbm]. Returns a list of length n_bins.
    """
    if _np is not None:
        x = _np.asarray(iq, dtype=_np.complex128)
        if x.size == 0:
            return [min_dbm] * n_bins
        # FFT THE WHOLE BLOCK, then reduce to n_bins — never subsample first.
        #
        # The previous code did `x = x[idx]` (take every Nth sample) before the
        # FFT, to "resample length to the pan width". That is not decimation: it
        # is aliasing. Everything between the picked samples is discarded and its
        # energy folds back onto the surviving bins, so the noise floor rises and
        # narrow signals are lost. With a 4096-sample block and a ~1600-bin pan it
        # threw away ~61% of every block and cost ~9 dB of dynamic range (measured
        # against this implementation on a synthetic carrier-in-noise).
        #
        # Instead: window and transform ALL the samples, then bin down by taking
        # the PEAK of each column. Peak (not mean) because a panadapter must show
        # a narrow carrier that lands inside one column — averaging would dilute
        # it into the surrounding noise, which is the very thing being fixed.
        # array_split distributes the remainder, so no high-frequency bins are
        # dropped when x.size is not a multiple of n_bins.
        win = _np.hanning(x.size)
        spec = _np.fft.fftshift(_np.fft.fft(x * win))
        # Divide by the window's coherent gain as well as the length, so a
        # full-scale carrier reads 0 dBFS and the axis means something before
        # dbm_offset is added. Without it the pan sat 6 dB low and the S-meter,
        # which does correct, disagreed by exactly that much.
        mag = _np.abs(spec) / (x.size * WINDOW_COHERENT_GAIN)
        dbm = 20.0 * _np.log10(_np.maximum(mag, 1e-12))
        if dbm.size != n_bins:
            if dbm.size < n_bins:
                # Fewer samples than pan columns: interpolate up. Nothing is lost
                # (there is simply less resolution than the pan can display).
                idx = _np.linspace(0, dbm.size - 1, n_bins)
                dbm = _np.interp(idx, _np.arange(dbm.size), dbm)
            else:
                # VECTORISED PEAK-PER-COLUMN. The list comprehension this
                # replaces called .max() once per pan column - ~1600 NumPy calls
                # per frame, each dominated by call overhead rather than by work.
                # Measured on a Pi 4 (4096 samples -> 1600 bins): 69.6 ms vs
                # 0.37 ms, a 187x speedup. That one line was consuming ~82% of
                # the engine loop (levels=54 ms of a 50 ms budget), holding the
                # loop at 15 Hz against its 20 Hz target.
                #
                # Semantics are UNCHANGED: still the PEAK of each column, never
                # the mean, so a narrow carrier landing inside one column still
                # survives the binning.
                q, r = divmod(dbm.size, n_bins)
                if r == 0:
                    dbm = dbm.reshape(n_bins, q).max(axis=1)
                else:
                    # Uneven split: array_split puts the extra sample in the
                    # FIRST r columns. Reshape each run separately rather than
                    # dropping the remainder - dropping it would silently lose
                    # the top of the span.
                    head = dbm[:r * (q + 1)].reshape(r, q + 1).max(axis=1)
                    tail = dbm[r * (q + 1):].reshape(n_bins - r, q).max(axis=1)
                    dbm = _np.concatenate([head, tail])
        # Offset AFTER the binning and BEFORE the clamp: the clamp is AE's
        # display range in real dBm, so applying it to an uncalibrated figure
        # would clip against the wrong window.
        dbm = _np.clip(dbm + dbm_offset, min_dbm, max_dbm)
        return dbm.tolist()
    return _iq_to_dbm_stdlib(iq, n_bins, min_dbm, max_dbm, dbm_offset)


def _iq_to_dbm_stdlib(iq, n_bins, min_dbm, max_dbm, dbm_offset=0.0):
    """Pure-stdlib DFT fallback (slow; for tests / numpy-less hosts)."""
    seq = list(iq)
    if not seq:
        return [min_dbm] * n_bins
    if len(seq) != n_bins:
        step = (len(seq) - 1) / max(1, n_bins - 1)
        seq = [seq[int(round(i * step))] for i in range(n_bins)]
    N = n_bins
    win = [0.5 - 0.5 * math.cos(2 * math.pi * i / (N - 1)) for i in range(N)] if N > 1 else [1.0]
    xs = [complex(seq[i]) * win[i] for i in range(N)]
    out = [0.0] * N
    for k in range(N):
        acc = 0j
        ang = -2j * math.pi * k / N
        for nidx in range(N):
            acc += xs[nidx] * complex(math.cos(ang.imag * nidx), math.sin(ang.imag * nidx))
        out[k] = abs(acc) / N
    half = N // 2                                  # fftshift: DC to centre
    out = out[half:] + out[:half]
    res = []
    for m in out:
        d = 20.0 * math.log10((m / WINDOW_COHERENT_GAIN) if m > 1e-12 else 1e-12)
        res.append(max(min_dbm, min(max_dbm, d + dbm_offset)))
    return res
