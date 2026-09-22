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


def main():
    sys.argv = [sys.argv[0]] + _strip_python_options(sys.argv[1:])
    from aether_gate.__main__ import main as gate_main
    return gate_main()


if __name__ == "__main__":
    # multiprocessing-safe entry for a frozen Windows program
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main() or 0)
