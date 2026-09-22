#
# Aether-gate — build the Windows program folder with PyInstaller.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""python packaging/windows/build.py  ->  dist/AetherGate/AetherGate.exe (+ its files)

Run from the repository root, on Windows, with numpy and pyinstaller installed:

    python -m pip install numpy pyinstaller
    python packaging/windows/build.py

The same script runs in .github/workflows/windows-installer.yml; the Inno Setup
script (installer.iss) then wraps dist/AetherGate into Aether-gate-Setup-<ver>.exe.

A folder build ("onedir"), not one-file: a one-file exe unpacks itself to %TEMP%
on every start (slow, and antivirus products dislike it), and the Setup page
starts a SECOND copy of the program for each gate, which would unpack twice.

WHAT IS IN IT (phase 1): the Setup page, the Icom LAN adapters and the sim --
everything that needs only Python and numpy. Hamlib CAT radios need rigctld.exe
and dongles need SoapySDR; neither is bundled yet, and the Setup page's Known
Info check says so rather than failing mysteriously.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def version():
    with open(os.path.join(ROOT, "aether_gate", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
    return m.group(1) if m else "0.0.0"


def main():
    if os.name != "nt":
        print("build.py makes the Windows program; run it on Windows.", file=sys.stderr)
        return 2
    sep = ";"   # PyInstaller --add-data separator on Windows
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "AetherGate",
        "--onedir",
        "--console",            # the gate prints its status; closing the window stops it
        "--collect-submodules", "aether_gate",
        # vendored radio dossiers: dossiers.py looks for <package-parent>/dossiers
        "--add-data", f"{os.path.join(ROOT, 'dossiers')}{sep}dossiers",
        "--add-data", f"{os.path.join(ROOT, 'LICENSE')}{sep}.",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build", "pyinstaller"),
        "--specpath", os.path.join(ROOT, "build"),
        os.path.join(ROOT, "packaging", "windows", "launcher.py"),
    ]
    print("Aether-gate", version(), "->", " ".join(args[3:6]))
    subprocess.check_call(args, cwd=ROOT)
    exe = os.path.join(ROOT, "dist", "AetherGate", "AetherGate.exe")
    print("built", exe, os.path.getsize(exe), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
