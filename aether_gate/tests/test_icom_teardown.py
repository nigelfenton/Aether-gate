#
# Aether-gate — IC-9700 teardown-on-open-failure tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Guard the phantom-session fix: whenever open() fails, the adapter MUST tear
the session down (which sends the 0x05 disconnect the radio needs to release
its RS-BA1 session). Before the fix the connect-failed path raised WITHOUT
calling close(), so the disconnect never went out and the radio was left with a
phantom session that blocked the next Start ("came up then jumped to the other
radio after 2-3 s").

These tests substitute fakes for the network layers so nothing touches a real
radio. They assert the CONTRACT (close() ran / 0x05 was sent on failure), not
the wire.

Run:  python -m aether_gate.tests.test_icom_teardown
Exits non-zero on first failure.
"""
import sys

from aether_gate.adapters import icom9700
from aether_gate.adapters.icom9700 import Icom9700Adapter


class _FakeHandler:
    """Stands in for Ic9700Handler. connect() returns self.ok; records stop()."""
    def __init__(self, *a, **k):
        self.stopped = False
        self._fail = "fake: connect refused"
        import threading
        self.authenticated = threading.Event()
        self.civ_port = 50002
        self._civ_sock = object()
        # audio bring-up is skipped when audio_port is falsy (this suite tests the
        # civ/teardown contract, not the LAN-audio session)
        self.audio_port = None
        self._audio_sock = None
    def connect(self, timeout=8.0):
        return getattr(type(self), "ok", False)
    def stop(self):
        self.stopped = True


class _FakeCiv:
    """Stands in for _Ic9700Stream. Never produces a freq (poisoned-session case)."""
    def __init__(self, *a, **k):
        self.stopped = False
        self.freq_hz = None
        self.frames = 0
    def start(self):
        pass
    def _on_iamready(self):
        pass                             # open()'s health-gate re-fires bring-up
    def stop(self):
        self.stopped = True


def _patch(monkey_ok):
    """Point the adapter at the fakes; connect() succeeds iff monkey_ok."""
    _FakeHandler.ok = monkey_ok
    icom9700.Ic9700Handler = _FakeHandler
    icom9700._Ic9700Stream = _FakeCiv


def _new_adapter():
    return Icom9700Adapter(radio_ip="10.0.0.7", username="u", password="p",
                           local_ip="10.0.0.103")


def test_connect_failure_tears_down():
    """connect() returns False -> open() raises AND close() ran on the handler."""
    _patch(monkey_ok=False)
    a = _new_adapter()
    raised = False
    try:
        a.open()
    except RuntimeError:
        raised = True
    assert raised, "open() must raise when connect() fails"
    # the handler that was built must have been stopped (stop() sends 0x05)
    assert isinstance(a._handler, _FakeHandler) and a._handler.stopped, \
        "handler.stop() (the 0x05 disconnect) was NOT called on connect failure"
    print("ok  connect failure -> handler.stop() ran (0x05 sent)")


def test_poisoned_session_tears_down_both():
    """connect() succeeds but civ never yields a freq -> open() raises AND both
    the civ stream and the handler are stopped (full clean teardown)."""
    _patch(monkey_ok=True)
    a = _new_adapter()
    # keep the health-gate wait short so the test doesn't sit for 12 s
    import time as _t
    orig_mono, orig_sleep = _t.monotonic, _t.sleep
    clock = [0.0]
    _t.monotonic = lambda: clock[0]
    def _fast_sleep(s):
        clock[0] += s                    # advance the fake clock instead of waiting
    _t.sleep = _fast_sleep
    try:
        raised = False
        try:
            a.open()
        except RuntimeError:
            raised = True
    finally:
        _t.monotonic, _t.sleep = orig_mono, orig_sleep
    assert raised, "open() must raise when the session never answers our reads"
    assert isinstance(a._civ, _FakeCiv) and a._civ.stopped, \
        "civ.stop() was NOT called on poisoned session"
    assert isinstance(a._handler, _FakeHandler) and a._handler.stopped, \
        "handler.stop() was NOT called on poisoned session"
    print("ok  poisoned session -> civ.stop() + handler.stop() both ran")


def test_success_does_not_tear_down():
    """A healthy open() (civ yields a freq) must NOT stop the streams."""
    _patch(monkey_ok=True)
    a = _new_adapter()
    # make the fake civ report a freq so the health gate passes immediately
    class _LiveCiv(_FakeCiv):
        def __init__(self, *aa, **kk):
            super().__init__(*aa, **kk)
            self.freq_hz = 146_520_000
            self.frames = 5
    icom9700._Ic9700Stream = _LiveCiv
    a.open()
    assert isinstance(a._civ, _LiveCiv) and not a._civ.stopped, \
        "healthy open() must not stop the civ stream"
    assert not a._handler.stopped, "healthy open() must not stop the handler"
    print("ok  healthy open() -> streams left running")


def main():
    tests = [test_connect_failure_tears_down,
             test_poisoned_session_tears_down_both,
             test_success_does_not_tear_down]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} teardown tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
