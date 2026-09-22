#
# Aether-gate — Windows program entry point (frozen with PyInstaller).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""What AetherGate.exe runs.

Bare (the Start-menu icon): the Setup page, with the browser opened on it --
exactly `python -m aether_gate`. With arguments: the gate itself, exactly
`python -m aether_gate <args>`.

WHY THE SHIM: the Setup page launches each gate as a child process with
`[sys.executable, "-u", "-m", "aether_gate", ...]`. In a frozen program
sys.executable is AetherGate.exe, which has no Python `-u`/`-m` options, so
those three leading arguments are dropped here and the rest handed to the
normal entry point. setup.py needs no Windows-specific branch.
"""
import sys


def _strip_python_options(argv):
    """['-u', '-m', 'aether_gate', '--adapter', 'sim'] -> ['--adapter', 'sim']."""
    out = list(argv)
    while out and out[0] == "-u":
        out.pop(0)
    if out[:2] == ["-m", "aether_gate"]:
        out = out[2:]
    return out


_INSTALLER_MSG = ("This is the installed Windows program: to update, download the new "
                  "installer (Aether-gate-Setup .exe) from the release page and run it. "
                  "Your saved radios and setup PIN are kept.")


def _guard_updater():
    """Never let the in-page updater swap files inside the installed program.

    Newer gate code refuses by itself (updater.frozen_program). A release built
    BEFORE that guard -- e.g. the v0.5.1 back-fill, whose package is taken from
    the tag unchanged -- would otherwise download a source tree and swap it in
    under PyInstaller's _internal/, half-updating the program. The packaging
    closes that here, whatever gate version is inside.
    """
    from aether_gate import updater
    if hasattr(updater, "frozen_program"):
        return
    real_status = updater.status

    def status(current_version, *a, **kw):
        st = real_status(current_version, *a, **kw)
        if st.get("available"):
            st["message"] = st.get("message", "") + " -- " + _INSTALLER_MSG
        return st

    def install(*a, **kw):
        return {"ok": False, "message": _INSTALLER_MSG}

    updater.status, updater.install = status, install


def main():
    sys.argv = [sys.argv[0]] + _strip_python_options(sys.argv[1:])
    if getattr(sys, "frozen", False):
        _guard_updater()
    from aether_gate.__main__ import main as gate_main
    return gate_main()


if __name__ == "__main__":
    # multiprocessing-safe entry for a frozen Windows program
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main() or 0)
