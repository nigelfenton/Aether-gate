#
# Aether-gate — device-lost signalling in the adapter base class.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The base-class device-lost contract (no hardware, no network).

`device_lost` used to be a bare attribute on RadioAdapter that only soapy.py
ever set, so core/engine.py's two guards — refuse an AE connection when the
radio is gone, and drop AE rather than serve a dead stream — were dead code for
every other adapter (#41). These tests lock the promoted behaviour:

  * silence shorter than the threshold is NOT a lost device (transients);
  * sustained silence IS, and says so exactly once;
  * the clock runs from the last EVIDENCE OF LIFE, not from the first silent
    call, so alternating one good read with many failures still converges.

Run:  python3 -m aether_gate.tests.test_device_lost
Exits non-zero on first failure.
"""
import sys
import time

from aether_gate.adapters.base import RadioAdapter


class _Probe(RadioAdapter):
    """Bare adapter: the base class is the subject, not any real hardware."""
    provides = "iq"


def test_a_fresh_adapter_is_not_lost():
    a = _Probe()
    assert a.device_lost is False
    assert a.device_lost_reason == ""
    print("ok  device-lost: a fresh adapter is not lost")


def test_silence_before_the_threshold_is_not_a_loss():
    a = _Probe()
    a.note_device_alive()
    # A transient — a dropped packet, one timeout — must cost nothing.
    assert a.note_device_silent("transient") is False
    assert a.device_lost is False
    print("ok  device-lost: a transient is not a loss")


def test_sustained_silence_declares_the_device_lost_once():
    a = _Probe()
    a.device_lost_after_s = 0.05          # keep the test fast; same code path
    a.note_device_alive()
    time.sleep(0.06)
    first = a.note_device_silent("the device stopped sending")
    assert first is True, "sustained silence must declare the device lost"
    assert a.device_lost is True
    assert a.device_lost_reason == "the device stopped sending"
    # Exactly once: a hot read loop calls this thousands of times and must not
    # re-log or re-declare on every pass.
    assert a.note_device_silent("again") is False
    assert a.device_lost_reason == "the device stopped sending"
    print("ok  device-lost: sustained silence declares the loss exactly once")


def test_the_clock_runs_from_the_last_evidence_of_life():
    # THE POINT OF THE WHOLE HELPER. A source that alternates one good read
    # with a burst of failures is NOT healthy. Measuring from the first silent
    # call would reset on every good read and never fire; measuring from the
    # last note_device_alive() converges.
    a = _Probe()
    a.device_lost_after_s = 0.05
    a.note_device_alive()
    deadline = time.monotonic() + 0.12
    while time.monotonic() < deadline:
        a.note_device_silent("intermittent")
        time.sleep(0.005)
    assert a.device_lost is True, (
        "a source that never produces data again must be declared lost even "
        "while its read calls keep returning")
    print("ok  device-lost: the clock runs from the last evidence of life")


def test_an_adapter_never_seen_alive_starts_its_clock_instead_of_firing():
    # "It was never there" is open()'s job — it has a better error than this.
    # A first silent call must therefore start the clock, not declare a loss.
    a = _Probe()
    a.device_lost_after_s = 0.05
    assert a.note_device_silent("nothing yet") is False
    assert a.device_lost is False
    time.sleep(0.06)
    assert a.note_device_silent("still nothing") is True
    print("ok  device-lost: an adapter never seen alive starts its clock")


def test_the_engine_guards_can_read_it_off_any_adapter():
    # core/engine.py reads these with getattr(..., False) off whatever adapter
    # is loaded. The promotion means every adapter answers, so the guards are
    # live rather than Soapy-only.
    a = _Probe()
    assert getattr(a, "device_lost", None) is False
    assert getattr(a, "device_lost_reason", None) == ""
    print("ok  device-lost: the engine's guards read it off any adapter")


def main():
    for fn in (test_a_fresh_adapter_is_not_lost,
               test_silence_before_the_threshold_is_not_a_loss,
               test_sustained_silence_declares_the_device_lost_once,
               test_the_clock_runs_from_the_last_evidence_of_life,
               test_an_adapter_never_seen_alive_starts_its_clock_instead_of_firing,
               test_the_engine_guards_can_read_it_off_any_adapter):
        fn()
    print("\nall device-lost tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
