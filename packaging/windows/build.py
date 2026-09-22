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

WHAT IS IN IT: the Setup page, the Icom LAN adapters and the sim -- everything
that needs only Python and numpy -- plus, when packaging/windows/build_sdr.ps1
has been run first, the SDR stack (SoapySDR + its Python binding, the
rtl-sdr-blog V4 driver, libusb) so RTL dongles work straight from the installer.
Point AG_SDR_PREFIX at that build if it is not in the default build/sdr-prefix.
Hamlib CAT radios (rigctld.exe) are still not bundled.
"""
import glob
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def version():
    with open(os.path.join(ROOT, "aether_gate", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
    return m.group(1) if m else "0.0.0"


def sdr_args(sep):
    """PyInstaller arguments for the SDR stack, if build_sdr.ps1 has produced it.

    Absent, the program still builds and runs -- it simply has no dongle support,
    and the Setup page's Known Info says SoapySDR is missing. Present, three
    things have to travel together: the DLLs (SoapySDR, rtlsdr, libusb), the
    Python binding (SoapySDR.py + _SoapySDR.pyd), and the driver MODULE, which
    lives in its own directory that SOAPY_SDR_PLUGIN_PATH points at (see
    launcher.py) -- a module beside the DLLs would never be found.
    """
    prefix = os.environ.get("AG_SDR_PREFIX") or os.path.join(ROOT, "build", "sdr-prefix")
    if not os.path.isdir(prefix):
        print("no SDR stack at", prefix, "- building without dongle support")
        return []
    args, py = [], os.path.join(prefix, "python")
    for dll in glob.glob(os.path.join(prefix, "bin", "*.dll")):
        args += ["--add-binary", f"{dll}{sep}."]
    for mod in glob.glob(os.path.join(prefix, "lib", "SoapySDR", "modules*", "*.dll")):
        args += ["--add-binary", f"{mod}{sep}soapy-modules"]
    for pyd in glob.glob(os.path.join(py, "_SoapySDR*.pyd")):
        args += ["--add-binary", f"{pyd}{sep}."]
    binding = os.path.join(py, "SoapySDR.py")
    if os.path.exists(binding):
        args += ["--add-data", f"{binding}{sep}."]
    args += ["--paths", py, "--hidden-import", "SoapySDR"]
    dlls = len([a for a in args if ".dll" in a])
    mods = len([a for a in args if "soapy-modules" in a])
    print(f"SDR stack: {dlls} DLLs ({mods} driver module(s)) from {prefix}")
    if not mods:
        print("  WARNING: no Soapy driver module - SoapySDR will import but find no devices")
    return args


def main():
    if os.name != "nt":
        print("build.py makes the Windows program; run it on Windows.", file=sys.stderr)
        return 2
    sep = ";"   # PyInstaller --add-data separator on Windows
    sdr = sdr_args(sep)
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
    ] + sdr + [
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
