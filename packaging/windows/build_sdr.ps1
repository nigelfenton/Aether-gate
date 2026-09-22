# Aether-gate — build the SDR stack for the Windows program (phase 2).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
#   pwsh packaging\windows\build_sdr.ps1 [-Prefix <dir>] [-PythonExe <python.exe>]
#
# Produces, under -Prefix (default build\sdr-prefix):
#   bin\rtlsdr.dll, bin\libusb-1.0.dll, bin\SoapySDR.dll
#   lib\SoapySDR\modules0.8-3\librtlsdrSupport.dll      (the RTL driver module)
#   python\SoapySDR.py + python\_SoapySDR.pyd           (the Python binding)
# packaging\windows\build.py bundles those into dist\AetherGate.
#
# WHY FROM SOURCE, pinned to the same commits as deploy/install-pi.sh: the
# Windows build then drives a dongle the same way the Pi appliance does, from
# versions already proven there -- in particular the rtl-sdr-blog fork, because
# stock librtlsdr does not drive the RTL-SDR V4 (see install-pi.sh).
#
# NOT the same thing as a USB driver: Windows also needs WinUSB bound to the
# dongle (the Zadig step). That is handled separately; this is only the
# userspace stack.
[CmdletBinding()]
param(
  [string]$Prefix = (Join-Path (Resolve-Path "$PSScriptRoot\..\..").Path "build\sdr-prefix"),
  [string]$PythonExe = "python",
  [string]$Work = (Join-Path (Resolve-Path "$PSScriptRoot\..\..").Path "build\sdr-src")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# --- pinned upstream (mirrors deploy/install-pi.sh) --------------------------
$RTLSDR_REPO   = "https://github.com/rtlsdrblog/rtl-sdr-blog.git"
$RTLSDR_COMMIT = "aed0ea1"
$SOAPY_REPO    = "https://github.com/pothosware/SoapySDR.git"
$SOAPY_COMMIT  = "1551ea0"
$SOAPYRTL_REPO = "https://github.com/pothosware/SoapyRTLSDR.git"
$SOAPYRTL_COMMIT = "b1f568d"
# libusb ships prebuilt Windows binaries; rtl-sdr needs them to talk to the dongle.
$LIBUSB_VER = "1.0.27"
$LIBUSB_URL = "https://github.com/libusb/libusb/releases/download/v$LIBUSB_VER/libusb-$LIBUSB_VER.7z"
$LIBUSB_SHA256 = "19835e290f46fab6bd8ce4be6ab7dc5209f1c04bad177065df485e51dc4118c8"

function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }

function Clone-Pinned($repo, $commit, $dir) {
  if (Test-Path (Join-Path $dir ".git")) {
    git -C $dir fetch --depth 50 origin | Out-Null
  } else {
    git clone $repo $dir | Out-Null
  }
  git -C $dir checkout -q $commit
}

function Build-CMake($src, $extra) {
  $b = Join-Path $src "build"
  New-Item -ItemType Directory -Force $b | Out-Null
  $args = @("-S", $src, "-B", $b, "-A", "x64",
            "-DCMAKE_INSTALL_PREFIX=$Prefix", "-DCMAKE_PREFIX_PATH=$Prefix",
            "-DCMAKE_BUILD_TYPE=Release") + $extra
  cmake @args
  if ($LASTEXITCODE -ne 0) { throw "cmake configure failed for $src" }
  cmake --build $b --config Release --target install
  if ($LASTEXITCODE -ne 0) { throw "cmake build failed for $src" }
}

New-Item -ItemType Directory -Force $Prefix, $Work | Out-Null

# --- libusb (prebuilt) -------------------------------------------------------
Say "libusb $LIBUSB_VER"
$lu = Join-Path $Work "libusb"
if (-not (Test-Path (Join-Path $lu "VS2022\MS64\dll\libusb-1.0.dll"))) {
  New-Item -ItemType Directory -Force $lu | Out-Null
  $arc = Join-Path $Work "libusb.7z"
  Invoke-WebRequest -Uri $LIBUSB_URL -OutFile $arc
  $got = (Get-FileHash $arc -Algorithm SHA256).Hash.ToLower()
  if ($got -ne $LIBUSB_SHA256) { throw "libusb download failed its pinned sha256 (got $got)" }
  & 7z x -y -o"$lu" $arc | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "could not unpack libusb" }
}
$luDll = Join-Path $lu "VS2022\MS64\dll\libusb-1.0.dll"
$luLib = Join-Path $lu "VS2022\MS64\dll\libusb-1.0.lib"
$luInc = Join-Path $lu "include"
New-Item -ItemType Directory -Force (Join-Path $Prefix "bin") | Out-Null
Copy-Item $luDll (Join-Path $Prefix "bin") -Force

# --- rtl-sdr-blog fork (the V4 driver) --------------------------------------
# LIBRARY ONLY. `--target install` also builds rtl_fm / rtl_tcp / rtl_power /
# rtl_adsb, and those include <pthread.h>, which MSVC does not have: the whole
# build then fails with C1083 on a command-line tool the gate never uses. So we
# build rtlsdr_shared and place its files ourselves.
Say "rtl-sdr-blog $RTLSDR_COMMIT (library only -- the CLI tools need pthreads)"
$rtl = Join-Path $Work "rtl-sdr-blog"
Clone-Pinned $RTLSDR_REPO $RTLSDR_COMMIT $rtl
$rtlBuild = Join-Path $rtl "build"
New-Item -ItemType Directory -Force $rtlBuild | Out-Null
cmake -S $rtl -B $rtlBuild -A x64 -DCMAKE_BUILD_TYPE=Release `
      -DCMAKE_INSTALL_PREFIX=$Prefix -DLIBUSB_INCLUDE_DIRS=$luInc `
      -DLIBUSB_LIBRARIES=$luLib -DDETACH_KERNEL_DRIVER=OFF
if ($LASTEXITCODE -ne 0) { throw "cmake configure failed for rtl-sdr-blog" }
cmake --build $rtlBuild --config Release --target rtlsdr_shared
if ($LASTEXITCODE -ne 0) { throw "cmake build failed for rtl-sdr-blog (rtlsdr_shared)" }

New-Item -ItemType Directory -Force (Join-Path $Prefix "lib"), (Join-Path $Prefix "include") | Out-Null
$rtlDll = Get-ChildItem -Path $rtlBuild -Recurse -Filter "rtlsdr.dll" | Select-Object -First 1
$rtlLib = Get-ChildItem -Path $rtlBuild -Recurse -Filter "rtlsdr.lib" | Select-Object -First 1
if (-not $rtlDll -or -not $rtlLib) { throw "rtlsdr.dll/.lib not found after the build" }
Copy-Item $rtlDll.FullName (Join-Path $Prefix "bin") -Force
Copy-Item $rtlLib.FullName (Join-Path $Prefix "lib") -Force
Copy-Item (Join-Path $rtl "includetl-sdr.h") (Join-Path $Prefix "include") -Force
Copy-Item (Join-Path $rtl "includetl-sdr_export.h") (Join-Path $Prefix "include") -Force

# --- SoapySDR core + Python binding -----------------------------------------
Say "SoapySDR $SOAPY_COMMIT (with the Python 3 binding)"
$soapy = Join-Path $Work "SoapySDR"
Clone-Pinned $SOAPY_REPO $SOAPY_COMMIT $soapy
$pyExe = (Get-Command $PythonExe).Source
Build-CMake $soapy @("-DENABLE_PYTHON3=ON", "-DENABLE_PYTHON=OFF",
                     "-DPYTHON3_EXECUTABLE=$pyExe", "-DPython3_EXECUTABLE=$pyExe",
                     "-DENABLE_TESTS=OFF", "-DENABLE_DOCS=OFF")

# --- the RTL module ----------------------------------------------------------
Say "SoapyRTLSDR $SOAPYRTL_COMMIT"
$srtl = Join-Path $Work "SoapyRTLSDR"
Clone-Pinned $SOAPYRTL_REPO $SOAPYRTL_COMMIT $srtl
Build-CMake $srtl @("-DRTLSDR_INCLUDE_DIR=$Prefix\include",
                    "-DRTLSDR_LIBRARY=$Prefix\lib\rtlsdr.lib")

# --- collect the Python binding where build.py expects it --------------------
Say "collecting"
$pyOut = Join-Path $Prefix "python"
New-Item -ItemType Directory -Force $pyOut | Out-Null
$binding = Get-ChildItem -Path $Prefix -Recurse -Include "SoapySDR.py", "_SoapySDR*.pyd" -ErrorAction SilentlyContinue
if (-not $binding) { throw "SoapySDR Python binding was not built (is SWIG on PATH?)" }
$binding | ForEach-Object { Copy-Item $_.FullName $pyOut -Force }

Say "done"
Get-ChildItem -Recurse -File $Prefix |
  Where-Object { $_.Extension -in ".dll", ".pyd", ".py" } |
  ForEach-Object { "    " + $_.FullName.Substring($Prefix.Length + 1) + "  {0:N0} bytes" -f $_.Length }
