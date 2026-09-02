"""The S-meter must measure the band the operator is listening to.

Regression cover for the 2026-08-31 finding: read_meters used to mix the slice
to DC and take |mean()| over the block — a Goertzel bin about 30 Hz wide sitting
exactly on the slice frequency. On SSB that is the SUPPRESSED CARRIER, where
there is no energy by construction, so the meter read noise in an empty gap and
barely moved with signal. It only worked for a carrier parked dead on the slice
frequency, which is why it looked fine on CW and FM.
"""
import numpy as np
import pytest

from aether_gate.adapters.soapy import SoapyAdapter

FS = 250_000.0
CENTER = 3_875_000.0


def _adapter(mode, slice_hz=CENTER, gain=20.0):
    a = SoapyAdapter(driver="none", samp_rate=FS, center_hz=CENTER, gain_db=gain)
    a._np = np
    a._init_demod()
    a._mode = mode
    a._slice_hz = slice_hz
    return a


def _tone(offset_hz, amp=1.0, n=8192, seed=None):
    """Complex tone at `offset_hz` from the tuned centre, plus faint noise."""
    t = np.arange(n) / FS
    rng = np.random.default_rng(seed if seed is not None else 1)
    noise = (rng.normal(0, 1e-3, n) + 1j * rng.normal(0, 1e-3, n))
    return (amp * np.exp(2j * np.pi * offset_hz * t) + noise).astype(np.complex128)


def _noise(n=8192, sigma=1e-3, seed=7):
    rng = np.random.default_rng(seed)
    return (rng.normal(0, sigma, n) + 1j * rng.normal(0, sigma, n)).astype(np.complex128)


def _read(a, iq):
    a._latest = iq
    return a.read_meters().s_meter_dbm


@pytest.mark.parametrize("mode,offset", [("LSB", -1500.0), ("USB", 1500.0)])
def test_ssb_voice_reads_far_above_noise(mode, offset):
    """A signal in the sideband must beat a quiet channel by a wide margin.

    THE bug. Voice energy sits 300-2700 Hz off the slice frequency, so the old
    single-bin measurement never saw it: signal and noise both read the empty
    carrier point and came out within a decibel or two of each other.
    """
    a = _adapter(mode)
    signal = _read(a, _tone(offset))
    quiet = _read(a, _noise())
    assert signal - quiet > 30.0, (
        f"{mode}: signal {signal:.1f} dBm vs noise {quiet:.1f} dBm — "
        "the meter is not seeing the sideband")


@pytest.mark.parametrize("mode,wrong_offset", [("LSB", 1500.0), ("USB", -1500.0)])
def test_ssb_rejects_the_other_sideband(mode, wrong_offset):
    """Energy on the sideband we do NOT demodulate must not move the meter.

    The operator cannot hear it, so it must not read as signal strength — this
    is what makes the reading mean 'what I am listening to'.
    """
    a = _adapter(mode)
    other = _read(a, _tone(wrong_offset))
    quiet = _read(a, _noise())
    assert other - quiet < 6.0, (
        f"{mode}: opposite sideband read {other:.1f} dBm against {quiet:.1f} dBm "
        "of noise — the meter is metering audio the operator cannot hear")


def test_stronger_signal_reads_stronger():
    """Monotonic in amplitude: 20 dB more signal is about 20 dB more reading."""
    a = _adapter("LSB")
    weak = _read(a, _tone(-1500.0, amp=0.01))
    strong = _read(a, _tone(-1500.0, amp=0.1))
    assert 15.0 < strong - weak < 25.0, (
        f"10x amplitude moved the meter {strong - weak:.1f} dB, expected ~20")


def test_fm_carrier_on_frequency_still_reads():
    """The case the old code DID handle stays handled — no regression for FM/CW."""
    a = _adapter("FM")
    signal = _read(a, _tone(0.0))
    quiet = _read(a, _noise())
    assert signal - quiet > 30.0


def test_rf_gain_does_not_masquerade_as_signal():
    """Turning the front end up must not look like the band got louder.

    The same antenna signal through 20 dB more gain arrives 20 dB larger, so
    the reading only stays put if read_meters backs the gain out again — model
    BOTH halves, or this tests nothing (the amplitude has to move too).
    """
    lo = _read(_adapter("LSB", gain=20.0), _tone(-1500.0, amp=0.01))
    hi = _read(_adapter("LSB", gain=40.0), _tone(-1500.0, amp=0.1))
    assert abs(hi - lo) < 1.0, f"gain change moved the meter {hi - lo:.1f} dB"


def test_slice_outside_the_window_reports_nothing():
    """A slice parked beyond the digitised span must not read off the edge."""
    a = _adapter("USB", slice_hz=CENTER + FS)      # a full span away
    a._latest = _tone(0.0)
    assert a.read_meters().s_meter_dbm == pytest.approx(-120.0)
