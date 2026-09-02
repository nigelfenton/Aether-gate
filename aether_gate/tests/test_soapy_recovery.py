#
# Aether-gate — soapy liveness/recovery invariants (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The three ways the SDRplay path took a whole session off the air, 2026-08-31.

  1. get_iq compared AE's offset-free centre against the HARDWARE centre, which
     offset-tunes samp_rate/4 away. The gap is structural, so the "has AE moved?"
     test never latched and every frame re-tuned the tuner to the frequency it
     was already on: 1419 setFrequency calls in 85 s.
  2. On a no-match, SoapySDRPlay3 throws while still holding
     sdrplay_api_LockDeviceApi(), deadlocking the API service for every process
     on the machine. So "the radio is unplugged" must fail before Device().
  3. activateStream() logs its failure and returns NORMALLY. A recovery path
     that trusted it announced "back on the air" seventeen times over ~50 s on a
     stream that never produced a sample.

Run:  python -m pytest aether_gate/tests/test_soapy_recovery.py
"""
import pytest

np = pytest.importorskip("numpy")

from aether_gate.adapters.soapy import SoapyAdapter


def _adapter(samp_rate=250_000.0, center=3_860_000.0):
    a = SoapyAdapter(driver="none", samp_rate=samp_rate, center_hz=center)
    a._np = np
    return a


# --- 1. the retune storm ----------------------------------------------------

def test_a_still_panadapter_schedules_no_retune_at_all():
    a = _adapter()
    a._slice_hz = 3_860_000.0
    a.get_iq(1024, 3_860_000.0, 250_000.0)       # first frame: one legitimate retune
    a._retune_to = None                          # (the reader thread consumes it)
    for _ in range(200):                         # 200 frames on an unmoved panadapter
        a.get_iq(1024, 3_860_000.0, 250_000.0)
        assert a._retune_to is None, "re-tuned to a frequency AE never moved off"


def test_offset_tuning_does_not_look_like_a_moved_centre():
    # The regression itself: hardware sits a quarter rate from the slice, so
    # comparing AE's request against self.center_hz can never converge.
    a = _adapter()
    a._slice_hz = 3_860_000.0
    a.get_iq(1024, 3_860_000.0, 250_000.0)
    a.center_hz = 3_860_000.0 + a.samp_rate / 4.0    # what retune() actually does
    a._retune_to = None
    a.get_iq(1024, 3_860_000.0, 250_000.0)
    assert a._retune_to is None


def test_a_real_move_still_retunes():
    a = _adapter()
    a._slice_hz = 3_860_000.0
    a.get_iq(1024, 3_860_000.0, 250_000.0)
    a._retune_to = None
    a.get_iq(1024, 7_074_000.0, 250_000.0)
    assert a._retune_to is not None, "a genuine band change must still retune"


# --- 2. never hand Device() args that cannot match --------------------------

class _FakeSoapy:
    """Stands in for the SoapySDR module: enumerate() finds devices, none match."""
    class Device:
        opened = False

        def __init__(self, args):
            _FakeSoapy.Device.opened = True      # must never happen on a no-match

        @staticmethod
        def enumerate(args):
            return [{"driver": "sdrplay", "serial": "SOMEONE_ELSES"}]

    SOAPY_SDR_RX = 0
    SOAPY_SDR_CF32 = "CF32"


def test_no_match_raises_instead_of_deadlocking_the_api_service(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "SoapySDR", _FakeSoapy)
    _FakeSoapy.Device.opened = False
    a = SoapyAdapter(driver="sdrplay", device_args="serial=24051AB170")
    with pytest.raises(RuntimeError, match="deadlock"):
        a._open_hw()
    assert not _FakeSoapy.Device.opened, \
        "called Device() on a no-match — that wedges the SDRplay API service"


# --- 3. prove a restarted stream with data, not with the driver's word -------

class _MuteSdr:
    """readStream that always succeeds and always returns nothing — the exact
    shape of a stream whose activateStream() quietly failed."""
    def readStream(self, stream, buffs, n, timeoutUs=0):
        return type("R", (), {"ret": 0})()


class _LiveSdr:
    def readStream(self, stream, buffs, n, timeoutUs=0):
        return type("R", (), {"ret": n})()


def test_verify_stream_rejects_a_stream_that_produces_nothing():
    a = _adapter()
    a._sdr, a._stream = _MuteSdr(), object()
    assert a._verify_stream(timeout_s=0.05) is False


def test_verify_stream_accepts_a_stream_that_delivers():
    a = _adapter()
    a._sdr, a._stream = _LiveSdr(), object()
    assert a._verify_stream(timeout_s=0.5) is True


def test_recovery_never_drops_the_device_reference():
    # Releasing the Device runs SoapySDRPlay3's destructor, which THROWS on
    # failure — and a C++ destructor is noexcept, so that is std::terminate, not
    # a Python exception. Measured: it killed the whole gate. Recovery must stop
    # at the stream.
    a = _adapter()
    sentinel = _LiveSdr()
    a._sdr, a._stream = sentinel, object()
    a._start_stream = lambda: setattr(a, "_stream", object())
    assert a._recover_device() is True
    assert a._sdr is sentinel, "recovery released the device — that aborts the process"
