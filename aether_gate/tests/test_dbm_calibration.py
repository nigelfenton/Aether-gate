"""The panadapter and the S-meter must agree, and neither may follow RF gain.

Regression cover for the 2026-08-31 finding. The two paths had grown separate
calibrations: core.fft.iq_to_dbm applied NO gain correction, so raising the RF
gain 20 dB relabelled the whole dBm axis 20 dB louder while the signal at the
antenna had not moved, and SoapyAdapter.read_meters applied its own. Measured on
identical white noise they agreed to 3.8 dB at 12 dB of gain and disagreed by
16.2 dB at 32 dB — which is what put a quiet 80 m band at S9 on the meter.
"""
import numpy as np
import pytest

from aether_gate.core.fft import iq_to_dbm, dbm_offset_for
from aether_gate.adapters.soapy import SoapyAdapter

FS = 250_000.0
CENTER = 3_875_000.0
BINS = 4096
BIN_HZ = FS / BINS
# Exactly on a bin for BOTH transforms (the pan's 4096 and the meter's 8192), so
# neither reading is eaten by scalloping loss and they can be compared directly.
TONE_HZ = -25.0 * BIN_HZ                      # inside the LSB passband
TONE_AMP = 0.02


def _noise(sigma=1.35e-3, n=8192, seed=3):
    rng = np.random.default_rng(seed)
    return (rng.normal(0, sigma, n) + 1j * rng.normal(0, sigma, n)).astype(np.complex128)


def _tone(amp=TONE_AMP, n=8192, hz=TONE_HZ):
    return (amp * np.exp(2j * np.pi * hz * np.arange(n) / FS)).astype(np.complex128)


def _pan_bins(iq, gain_db, trim=0.0, base=None):
    return iq_to_dbm(iq[:BINS], BINS, -200.0, 20.0, dbm_offset_for(gain_db, trim, base))


def _pan_floor(iq, gain_db, trim=0.0, base=None):
    return float(np.median(_pan_bins(iq, gain_db, trim, base)))


def _pan_peak(iq, gain_db, trim=0.0):
    return float(np.max(_pan_bins(iq, gain_db, trim)))


def _meter_full(iq, gain_db, trim=0.0, base=None):
    """The whole Meters object — signal AND the floor it was measured against."""
    a = SoapyAdapter(driver="none", samp_rate=FS, center_hz=CENTER, gain_db=gain_db)
    a._np = np
    a._init_demod()
    a._mode = "LSB"
    a._slice_hz = CENTER
    a.dbm_trim = trim
    if base is not None:
        a.dbm_base = base
    a._latest = iq
    return a.read_meters()


def _meter(iq, gain_db, trim=0.0, base=None):
    return _meter_full(iq, gain_db, trim, base).s_meter_dbm


def _at_gain(gain_db, trim=0.0, base=None):
    """The same antenna signal — noise plus one tone — through `gain_db` of front end."""
    iq = (_noise() + _tone()) * (10 ** ((gain_db - 12.0) / 20.0))
    return _pan_floor(iq, gain_db, trim, base), _meter(iq, gain_db, trim, base)


@pytest.mark.parametrize("gain", [12.0, 22.0, 32.0, 45.0])
def test_neither_scale_follows_the_rf_gain(gain):
    """Turning the front end up must not relabel the dBm axis.

    THE bug: the pan moved 1:1 with gain, so the operator's own gain setting
    read as signal strength.
    """
    ref_pan, ref_meter = _at_gain(12.0)
    pan, meter = _at_gain(gain)
    assert pan == pytest.approx(ref_pan, abs=0.5), (
        f"pan floor moved {pan - ref_pan:+.1f} dB going from 12 to {gain:.0f} dB of gain")
    assert meter == pytest.approx(ref_meter, abs=0.5), (
        f"S-meter moved {meter - ref_meter:+.1f} dB going from 12 to {gain:.0f} dB of gain")


@pytest.mark.parametrize("gain", [12.0, 32.0])
def test_pan_and_meter_agree_on_the_same_signal(gain):
    """Both report the tone's power, so they must land on the same number.

    They look at identical samples; a disagreement is a calibration split, which
    is exactly what dbm_offset_for exists to make impossible. No bandwidth term
    here — the meter subtracts the noise floor's share of its passband, so what
    is left is the tone, and a coherent-gain-normalised FFT peak is the tone too.
    """
    iq = (_noise() + _tone()) * (10 ** ((gain - 12.0) / 20.0))
    peak, meter = _pan_peak(iq, gain), _meter(iq, gain)
    assert meter == pytest.approx(peak, abs=1.0), (
        f"pan peak says {peak:.1f} dBm, meter says {meter:.1f} dBm")


def test_static_alone_reads_at_the_bottom_of_the_scale():
    """Noise with no signal in it is what the meter must NOT report.

    The whole reason for subtracting the floor: a 3 kHz slice of band noise
    genuinely carries about -85 dBm, so reporting total passband power pinned
    the needle at S8 on dead static and left it nowhere to go for real signal.
    """
    assert _meter(_noise(), 12.0) == -140.0


def test_the_meter_reports_the_signal_not_the_noise_around_it():
    """Raising the noise floor under an unchanged signal must not move the needle.

    This is the property the subtraction buys, and the one a naive
    total-power meter fails: there, 10 dB more noise is 10 dB more meter.
    """
    quiet = _meter(_noise() + _tone(), 12.0)
    loud = _meter(_noise(sigma=1.35e-3 * (10 ** 0.5)) + _tone(), 12.0)
    assert loud == pytest.approx(quiet, abs=1.0), (
        f"10 dB more noise moved the meter {loud - quiet:+.1f} dB")


def test_trim_moves_both_scales_together():
    """Operator calibration must not re-open the split it was added to close."""
    base_pan, base_meter = _at_gain(12.0)
    trim_pan, trim_meter = _at_gain(12.0, trim=-12.0)
    assert trim_pan == pytest.approx(base_pan - 12.0, abs=0.2)
    assert trim_meter == pytest.approx(base_meter - 12.0, abs=0.2)


def test_a_devices_anchor_moves_both_scales_together():
    """The anchor is per front end (core.fft.DBFS_TO_DBM_BY_DRIVER), so a
    different device's number must shift the pan and the meter as one — the
    same contract trim has, through the same seam."""
    from aether_gate.core.fft import DBFS_TO_DBM
    ref_pan, ref_meter = _at_gain(12.0)
    pan, meter = _at_gain(12.0, base=DBFS_TO_DBM - 11.0)
    assert pan == pytest.approx(ref_pan - 11.0, abs=0.2)
    assert meter == pytest.approx(ref_meter - 11.0, abs=0.2)


def test_full_scale_carrier_reads_zero_dbfs():
    """With calibration backed out, a full-scale carrier is the 0 dBFS anchor.

    Guards the window coherent-gain division: without it the pan sat 6 dB low
    and every absolute reading inherited the error.
    """
    n = BINS
    carrier = np.exp(2j * np.pi * 10_000.0 * np.arange(n) / FS).astype(np.complex128)
    peak = max(iq_to_dbm(carrier, BINS, -200.0, 20.0))
    assert peak == pytest.approx(0.0, abs=0.5), f"full-scale carrier read {peak:.1f} dBFS"


def test_the_meter_reports_the_floor_it_measured_against():
    """SNR is the number an antenna change has to move, so both halves ship.

    A better antenna raises signal AND noise, and so does turning the gain up;
    only their difference says whether anything was gained. The adapter already
    computes the floor to subtract it, so throwing it away was the waste.
    """
    iq = (_noise() + _tone()) * (10 ** ((12.0 - 12.0) / 20.0))
    m = _meter_full(iq, 12.0, 0.0)
    assert m.noise_dbm is not None
    # The tone is well clear of the floor it was measured against.
    assert m.s_meter_dbm - m.noise_dbm > 6.0


def test_static_alone_still_reports_a_floor():
    """No signal is not the same as no measurement. On a quiet band the floor
    is the only real number there is, and it is the half being tuned against."""
    m = _meter_full(_noise(), 12.0, 0.0)
    assert m.s_meter_dbm == -140.0          # nothing above the floor
    assert m.noise_dbm is not None and m.noise_dbm > -140.0
