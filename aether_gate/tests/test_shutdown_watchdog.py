#
# Aether-gate — a wedged driver must not be able to hold the exit.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""SIGTERM must stop the gate even when adapter.close() never returns.

Measured 2026-08-31 on an RSPdx that had left the USB bus: SIGTERM was
delivered and "bye" was logged, then the process sat in adapter.close() for over
three minutes, because SoapySDRPlay3's stream teardown does not return for a
device that is no longer there. Two further SIGTERMs did nothing — the main
thread was blocked inside a C call, and a Python signal handler only runs
between bytecodes. It took SIGKILL, which skips ReleaseDevice and leaves the
SDRplay API service holding a stale device.

This runs the real entry point in a subprocess, because the fix ends in
os._exit() and there is no honest way to assert that in-process.

Run:  python -m pytest aether_gate/tests/test_shutdown_watchdog.py
"""
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A gate whose adapter.close() blocks forever, with the grace cut down so the
# test is quick. Everything else is the shipping code path. Bound to loopback
# because serve() binds the radio's own IP (engine.serve), which is the LAN
# address by default -- this test has no business listening on the network.
SCRIPT = """
import sys, time
import aether_gate.__main__ as M
from aether_gate.adapters.sim import SimAdapter
M.SHUTDOWN_GRACE_S = {grace}
class Hanging(SimAdapter):
    def close(self):
        while True:
            time.sleep(3600)
M.build_adapter = lambda name, args: Hanging()
sys.exit(M.main(["--adapter", "sim", "--ip", "127.0.0.1",
                 "--port", "{port}", "--ctl-port", "0"]))
"""



def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _await_listening(port, proc, timeout_s=20.0):
    """Block until the gate actually accepts a connection on `port`.

    This is the readiness signal that matters: __main__ installs the SIGTERM
    handler BEFORE it opens the adapter and serves, so a successful connect
    proves the signal will reach _graceful instead of the default disposition.

    The first version of this test slept a flat 3s instead, and flaked roughly
    one run in seven under a full-suite load: SIGTERM landed before the handler
    existed, the process died at -SIGTERM, and none of the output asserted on
    below was ever produced.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail("gate exited before it served: " + proc.communicate()[0])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    proc.kill()
    pytest.fail(f"gate never listened on port {port} within {timeout_s:.0f}s")


# Windows has the SIGTERM *name* but never delivers the signal: Popen.send_signal
# maps it to TerminateProcess(), which ends the child without running _graceful,
# the finally, or the watchdog. hasattr(signal, "SIGTERM") is true there and
# guards nothing (found by running this on Windows, 2026-09-01).
@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows cannot deliver SIGTERM; send_signal is TerminateProcess")
def test_sigterm_wins_over_a_driver_that_never_returns(tmp_path):
    grace = 2.0
    port = _free_port()
    script = tmp_path / "hang.py"
    script.write_text(SCRIPT.format(grace=grace, port=port))
    env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1")
    p = subprocess.Popen([sys.executable, str(script)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    try:
        _await_listening(port, p)
        p.send_signal(signal.SIGTERM)                      # exactly ONE, as a supervisor sends
        # Generous ceiling: the point is that it terminates at all, unassisted.
        out = p.communicate(timeout=grace + 15.0)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        pytest.fail("adapter.close() held the exit - the watchdog did not fire")
    assert p.returncode == 0, (
        f"forced stop returned {p.returncode}; it must be 0 so a supervisor "
        f"running Restart=on-failure does not bounce straight back into the "
        f"same wedged driver")
    assert "cleanup did not finish" in out, out
