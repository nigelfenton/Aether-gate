#
# Aether-gate — Windows packaged-program tests (no hardware, no network, any OS).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Two seams the Windows installer build depends on, pinned without building it:

  * packaging/windows/launcher.py turns the Setup page's child command
    [exe, "-u", "-m", "aether_gate", <gate args>] into plain <gate args>. The
    frozen AetherGate.exe has no Python -u/-m options; if the shim stopped
    stripping them, every Start from the Setup page would fail with an argparse
    error, while a bare double-click still worked -- easy to miss by hand.
  * the one-click updater refuses to swap files inside the installed program
    (sys.frozen), and says to run the new installer instead. Swapping a source
    tree under PyInstaller's _internal/ would leave a half-updated program.

The frozen build itself is exercised end to end in
.github/workflows/windows-installer.yml (build + start a sim gate from the exe).

Run:  python -m aether_gate.tests.test_windows_program
"""
import importlib.util
import os
import sys

from aether_gate import updater

_LAUNCHER = os.path.join(os.path.dirname(__file__), "..", "..", "packaging", "windows", "launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("ag_win_launcher", os.path.abspath(_LAUNCHER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shim_drops_the_python_options():
    strip = _launcher()._strip_python_options
    assert strip(["-u", "-m", "aether_gate", "--adapter", "sim", "--port", "5992"]) == \
        ["--adapter", "sim", "--port", "5992"]
    assert strip(["--adapter", "sim"]) == ["--adapter", "sim"], "plain gate args pass through"
    assert strip([]) == [], "bare launch stays bare (-> Setup page)"
    assert strip(["--setup", "--no-browser"]) == ["--setup", "--no-browser"]
    print("ok  launcher: '-u -m aether_gate' is dropped; everything else passes through")


def test_frozen_program_refuses_the_file_swap():
    had = hasattr(sys, "frozen")
    old = getattr(sys, "frozen", None)
    sys.frozen = True
    try:
        assert updater.frozen_program()
        r = updater.install(None, os.path.join(os.path.dirname(updater.__file__)))
        assert r["ok"] is False and "installer" in r["message"].lower(), r
    finally:
        if had:
            sys.frozen = old
        else:
            del sys.frozen
    assert not updater.frozen_program()
    print("ok  updater: the installed program is told to run the new installer, nothing swapped")


def test_launcher_guards_an_older_updater():
    """A gate built before updater.frozen_program existed (the v0.5.1 back-fill)
    must still refuse the file swap inside the installed program."""
    saved = (updater.install, updater.status, getattr(updater, "frozen_program", None))
    del updater.frozen_program                      # look like the old updater
    try:
        _launcher()._guard_updater()
        r = updater.install(None, "anywhere")
        assert r["ok"] is False and "installer" in r["message"].lower(), r
    finally:
        updater.install, updater.status, updater.frozen_program = saved
    print("ok  launcher: an updater without the guard is guarded by the packaging")


def main():
    tests = [test_shim_drops_the_python_options, test_frozen_program_refuses_the_file_swap,
             test_launcher_guards_an_older_updater]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} windows-program tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
