#!/usr/bin/env bash
#
# Aether-gate — Raspberry Pi appliance installer.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
# Turns a fresh 64-bit Raspberry Pi OS (Debian 13 / trixie, Python 3.13) into an
# Aether-gate appliance: browse http://aethergate.local:8730 -> pick a radio ->
# Start. Idempotent — safe to re-run.
#
# WHAT IT INSTALLS
#   * apt: numpy, hamlib (rigctld), avahi, build tools           [always]
#   * source-built into /usr/local (the SDR spectrum path):      [--with-sdr, default ON]
#       - rtl-sdr-blog fork  (V4 dongle + HF direct-sampling; the apt librtlsdr
#         2.0.2 does NOT drive the RTL-SDR V4 well — hence the fork)
#       - SoapySDR core      (+ python3 bindings)
#       - SoapyRTLSDR module
#     Icom-LAN rigs (IC-9700) need ONLY numpy — skip the SDR stack with --no-sdr
#     for a fast LAN-only install.
#   * the aether_gate package copied to /home/pi/gate
#   * systemd: aether-gate-setup.service (boot -> Setup UI on :8730)
#
# USAGE
#   sudo ./install-pi.sh                 # full appliance (with SDR stack)
#   sudo ./install-pi.sh --no-sdr        # Icom-LAN only (numpy) — fast
#   ./install-pi.sh --check              # report what's present/missing; no changes
#   sudo ./install-pi.sh --dry-run       # print every step; make NO changes
#
# The pinned commits below are the exact versions proven on the Pi5 appliance
# (2026-07-03). Pinning keeps a rebuild reproducible instead of tracking moving
# upstream HEADs.

set -euo pipefail

# --- pinned upstream versions (proven on the Pi5) ------------------------------
RTLSDR_REPO="https://github.com/rtlsdrblog/rtl-sdr-blog.git"
RTLSDR_COMMIT="aed0ea1"                    # "fix declaration warning"
SOAPY_REPO="https://github.com/pothosware/SoapySDR.git"
SOAPY_COMMIT="1551ea0"                     # "Fix SWIG parallel Device::make() overloads (#474)"
SOAPYRTL_REPO="https://github.com/pothosware/SoapyRTLSDR.git"
SOAPYRTL_COMMIT="b1f568d"                  # "Update Github Action"
# SDRplay (RSP1a etc.): proprietary API daemon + the Soapy module built against it.
# The .run is fetched from SDRplay's own site; installing it implies accepting
# their licence (a copy lands in /opt/sdrplay_api/sdrplay_license.txt).
SDRPLAY_API_URL="https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run"
SDRPLAY_API_SHA256="3a97ca764263bbe76fb0f2220e6408942357e8864c19e1408a6d6987af382fe3"
SOAPYSDRPLAY_REPO="https://github.com/pothosware/SoapySDRPlay3.git"
SOAPYSDRPLAY_COMMIT="6cc3131"              # merge PR #104 (2026-06-12)

# AG_USER lets an image build (chroot, no sudo lineage) name the service user.
#
# AN EXISTING INSTALL OWNS ITS OWN USER. Without this, re-running the installer
# on a flashed appliance — which is exactly what add-sdrplay.sh does — adopts
# whoever typed sudo. The gate got re-homed from /home/aethergate/gate to
# /home/<caller>/gate and the unit was rewritten to User=<caller>, defeating the
# dedicated service user that makes the image work whatever username Raspberry
# Pi Imager created. Observed on the appliance 2026-08-07 (became User=nigel).
# An explicit AG_USER still wins, so image builds are unaffected.
INSTALLED_USER=""
if [ -z "${AG_USER:-}" ] && [ -r /etc/systemd/system/aether-gate-setup.service ]; then
  INSTALLED_USER="$(sed -n 's/^User=//p' /etc/systemd/system/aether-gate-setup.service | head -1)"
fi
GATE_USER="${AG_USER:-${INSTALLED_USER:-${SUDO_USER:-pi}}}"
GATE_HOME="$(getent passwd "$GATE_USER" | cut -d: -f6)"
GATE_DIR="$GATE_HOME/gate"
SRC_DIR="$GATE_HOME/gate-build"            # where the SDR sources are cloned/built
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # the checkout this script lives in

WITH_SDR=1
# SDRplay's API is proprietary and its EULA grants only "publicly display,
# publicly perform ... in Object form" — no distribution right, with everything
# not granted expressly reserved (clause 3) and a confidentiality clause that
# bars disclosure to third parties (clause 2). Fetching it onto the operator's
# own Pi is fine: THEY accept the licence. Baking it into an image that is then
# published is redistribution, so release builds set this to 0.
WITH_SDRPLAY=1
DRY_RUN=0
CHECK_ONLY=0

for a in "$@"; do
  case "$a" in
    --no-sdr)   WITH_SDR=0 ;;
    --with-sdr) WITH_SDR=1 ;;
    --no-sdrplay)   WITH_SDRPLAY=0 ;;
    --with-sdrplay) WITH_SDRPLAY=1 ;;
    --dry-run)  DRY_RUN=1 ;;
    --check)    CHECK_ONLY=1 ;;
    -h|--help)  sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a (try --help)"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '    [dry-run] %s\n' "$*"; else eval "$@"; fi; }

need_root() {
  if [ "$CHECK_ONLY" = 0 ] && [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != 0 ]; then
    echo "This needs root for apt + /usr/local + systemd. Re-run with sudo (or use --check/--dry-run)."
    exit 1
  fi
}

# ------------------------------------------------------------------------------
# --check : report only, no changes
# ------------------------------------------------------------------------------
report() {
  say "Aether-gate Pi — environment check"
  . /etc/os-release 2>/dev/null || true
  info "OS:      ${PRETTY_NAME:-unknown}  ($(uname -m))"
  info "Model:   $(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo '?')"
  info "Python:  $(python3 --version 2>&1)"
  case "${VERSION_CODENAME:-}" in
    trixie) : ;;
    bookworm|bullseye) warn "This was proven on trixie (Debian 13 / Py3.13). On ${VERSION_CODENAME} apt names / Python paths may differ — flashing current 64-bit Pi OS is the smooth path." ;;
    *) warn "Unrecognised OS release — proceed with care." ;;
  esac
  [ "$(uname -m)" = "aarch64" ] || warn "Not aarch64 — expected 64-bit Pi OS. A 32-bit OS will fight the source builds."

  chk() { if "$@" >/dev/null 2>&1; then printf '    \033[1;32m[ok]\033[0m   %s\n' "$*"; else printf '    \033[1;31m[--]\033[0m   %s\n' "$*"; fi; }
  say "Dependencies"
  chk python3 -c 'import numpy'
  chk sh -c 'command -v rigctld'
  chk sh -c 'command -v SoapySDRUtil'
  chk python3 -c 'import SoapySDR'
  chk sh -c 'SoapySDRUtil --info 2>/dev/null | grep -q rtlsdr'
  if [ "$WITH_SDRPLAY" = 1 ]; then
    chk test -x /opt/sdrplay_api/sdrplay_apiService
    chk sh -c 'SoapySDRUtil --info 2>/dev/null | grep -qi sdrplay'
  else
    # Absent BY DESIGN on a published image — see WITH_SDRPLAY above. Reporting
    # these as [--] would read as a broken build to the first ham who runs --check.
    printf '    \033[1;33m[..]\033[0m   SDRplay not installed (--no-sdrplay; add with --with-sdrplay)\n'
  fi
  chk sh -c 'command -v avahi-daemon || test -x /usr/sbin/avahi-daemon'
  say "Aether-gate"
  # LOOK WHERE THE GATE ACTUALLY IS, not where THIS invocation would install it.
  # On an appliance the gate belongs to the `aethergate` service user, but
  # --check is run by whoever is logged in (nigel, pi, ...), so GATE_DIR points
  # at the caller's home and the test failed red on a perfectly healthy image.
  # Prefer the running service's WorkingDirectory, then this caller's dir.
  SVC_DIR="$(systemctl show -p WorkingDirectory --value aether-gate-setup.service 2>/dev/null || true)"
  if [ -n "$SVC_DIR" ] && [ -d "$SVC_DIR/aether_gate" ]; then
    chk test -d "$SVC_DIR/aether_gate"
  else
    chk test -d "$GATE_DIR/aether_gate"
  fi
  chk systemctl is-enabled aether-gate-setup.service
  chk python3 -c 'import numpy; import aether_gate' 2>/dev/null || true
}

if [ "$CHECK_ONLY" = 1 ]; then report; exit 0; fi

need_root
say "Aether-gate Pi installer  (user=$GATE_USER  gate=$GATE_DIR  with-sdr=$WITH_SDR  dry-run=$DRY_RUN)"

# OS sanity (non-fatal; warn like --check does)
. /etc/os-release 2>/dev/null || true
[ "${VERSION_CODENAME:-}" = "trixie" ] || warn "Proven on trixie/Py3.13; you're on '${VERSION_CODENAME:-?}'. If apt/build steps fail, reflash current 64-bit Pi OS."
[ "$(uname -m)" = "aarch64" ] || warn "Expected aarch64 (64-bit Pi OS)."

# ------------------------------------------------------------------------------
# 1) apt packages
# ------------------------------------------------------------------------------
say "apt: base + build prerequisites"
# tcpdump: an appliance you can't wiretap is one you can't diagnose (2026-08-01)
APT_PKGS=(python3 python3-numpy python3-dev libhamlib-utils avahi-daemon tcpdump)
if [ "$WITH_SDR" = 1 ]; then
  APT_PKGS+=(build-essential cmake git pkg-config libusb-1.0-0-dev swig curl)
fi
run "apt-get update -y"
run "apt-get install -y ${APT_PKGS[*]}"

# ------------------------------------------------------------------------------
# 2) SDR stack (source-built into /usr/local) — the V4/HF spectrum path
# ------------------------------------------------------------------------------
# Each build is guarded so a re-run skips work already done. cmake install into
# /usr/local, then ldconfig so the runtime linker + Soapy find the libs.
build_cmake() {  # $1=srcdir  $2..=extra cmake args
  local src="$1"; shift
  run "mkdir -p '$src/build'"
  run "cd '$src/build' && cmake -DCMAKE_INSTALL_PREFIX=/usr/local $* .. && make -j\$(nproc) && make install"
}
clone_pin() {   # $1=repo $2=commit $3=dest
  if [ -d "$3/.git" ]; then
    run "cd '$3' && git fetch --depth 50 origin && git checkout -q '$2'"
  else
    run "git clone '$1' '$3' && cd '$3' && git checkout -q '$2'"
  fi
}

if [ "$WITH_SDR" = 1 ]; then
  run "install -d -o '$GATE_USER' -g '$GATE_USER' '$SRC_DIR'"

  if command -v SoapySDRUtil >/dev/null 2>&1 && SoapySDRUtil --info 2>/dev/null | grep -q rtlsdr; then
    info "SoapySDR + rtlsdr module already present — skipping SDR build (re-run with a wiped $SRC_DIR to force)."
  else
    say "SDR build 1/5: rtl-sdr-blog (V4 fork) -> /usr/local"
    clone_pin "$RTLSDR_REPO" "$RTLSDR_COMMIT" "$SRC_DIR/rtl-sdr-blog"
    build_cmake "$SRC_DIR/rtl-sdr-blog" "-DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=OFF"

    say "SDR build 2/5: SoapySDR core (+ python3 bindings) -> /usr/local"
    clone_pin "$SOAPY_REPO" "$SOAPY_COMMIT" "$SRC_DIR/SoapySDR"
    build_cmake "$SRC_DIR/SoapySDR" "-DENABLE_PYTHON3=ON"

    say "SDR build 3/5: SoapyRTLSDR module -> /usr/local"
    clone_pin "$SOAPYRTL_REPO" "$SOAPYRTL_COMMIT" "$SRC_DIR/SoapyRTLSDR"
    build_cmake "$SRC_DIR/SoapyRTLSDR"

    run "/sbin/ldconfig"
  fi

  # ---- SDRplay (RSP1a/RSP2/RSPdx...): proprietary API + SoapySDRPlay3 ----------
  if [ "$WITH_SDRPLAY" != 1 ]; then
    say "SDR build 4-5/5: SDRplay SKIPPED (--no-sdrplay)"
    info "RSP owners: run 'sudo ./deploy/install-pi.sh --with-sdrplay' on the Pi to add it."
    info "It is fetched from sdrplay.com so you accept their licence directly — which is"
    info "why it cannot be shipped pre-baked in a published image."
  elif [ -e /usr/local/lib/libsdrplay_api.so ] && SoapySDRUtil --info 2>/dev/null | grep -q sdrplay; then
    info "SDRplay API + Soapy module already present — skipping."
  else
    say "SDR build 4/5: SDRplay API 3.15 (proprietary) -> /usr/local + /opt/sdrplay_api"
    info "fetching from sdrplay.com — installing implies accepting their licence"
    if [ "$DRY_RUN" = 1 ]; then
      info "[dry-run] download+verify+extract $SDRPLAY_API_URL; install lib/headers/daemon/udev/service"
    else
      RUNFILE="$SRC_DIR/sdrplay-api.run"
      if ! echo "$SDRPLAY_API_SHA256  $RUNFILE" | sha256sum -c - >/dev/null 2>&1; then
        curl -fSL -o "$RUNFILE" "$SDRPLAY_API_URL"
        echo "$SDRPLAY_API_SHA256  $RUNFILE" | sha256sum -c - \
          || { echo "SDRplay API download failed its pinned sha256"; exit 1; }
      fi
      rm -rf "$SRC_DIR/sdrplay-extract"
      sh "$RUNFILE" --noexec --target "$SRC_DIR/sdrplay-extract" >/dev/null
      cd "$SRC_DIR/sdrplay-extract"
      # mirror install_lib.sh's actions for arm64, minus the interactive licence pager
      rm -f /usr/local/lib/libsdrplay_api.so*
      cp -f arm64/libsdrplay_api.so.3.15 /usr/local/lib/
      ln -s /usr/local/lib/libsdrplay_api.so.3.15 /usr/local/lib/libsdrplay_api.so.3
      ln -s /usr/local/lib/libsdrplay_api.so.3 /usr/local/lib/libsdrplay_api.so
      cp -f inc/sdrplay_api*.h /usr/local/include/
      chmod 644 /usr/local/include/sdrplay_api*.h
      install -d -m 755 /opt/sdrplay_api
      cp -f arm64/sdrplay_apiService /opt/sdrplay_api/
      chmod 755 /opt/sdrplay_api/sdrplay_apiService
      cp -f sdrplay_license.txt /opt/sdrplay_api/
      cat > /etc/udev/rules.d/66-sdrplay.rules <<'RULES'
SUBSYSTEM=="usb",ENV{DEVTYPE}=="usb_device",ATTRS{idVendor}=="1df7",ATTRS{idProduct}=="2500",MODE:="0666"
SUBSYSTEM=="usb",ENV{DEVTYPE}=="usb_device",ATTRS{idVendor}=="1df7",ATTRS{idProduct}=="3000",MODE:="0666"
SUBSYSTEM=="usb",ENV{DEVTYPE}=="usb_device",ATTRS{idVendor}=="1df7",ATTRS{idProduct}=="3010",MODE:="0666"
SUBSYSTEM=="usb",ENV{DEVTYPE}=="usb_device",ATTRS{idVendor}=="1df7",ATTRS{idProduct}=="3020",MODE:="0666"
SUBSYSTEM=="usb",ENV{DEVTYPE}=="usb_device",ATTRS{idVendor}=="1df7",ATTRS{idProduct}=="3030",MODE:="0666"
SUBSYSTEM=="usb",ENV{DEVTYPE}=="usb_device",ATTRS{idVendor}=="1df7",ATTRS{idProduct}=="3050",MODE:="0666"
SUBSYSTEM=="usb",ENV{DEVTYPE}=="usb_device",ATTRS{idVendor}=="1df7",ATTRS{idProduct}=="3060",MODE:="0666"
RULES
      chmod 644 /etc/udev/rules.d/66-sdrplay.rules
      cat > /etc/systemd/system/sdrplay.service <<'UNIT'
[Unit]
Description=SDRplay API Service
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
Restart=on-failure
RestartSec=1
User=root
ExecStart=/opt/sdrplay_api/sdrplay_apiService

[Install]
WantedBy=multi-user.target
UNIT
      chmod 644 /etc/systemd/system/sdrplay.service
      if [ -d /run/systemd/system ]; then
        systemctl daemon-reload
        systemctl enable --now sdrplay
        udevadm control --reload-rules 2>/dev/null || true
      else
        systemctl enable sdrplay   # image-build chroot: starts on first real boot
      fi
      /sbin/ldconfig
      cd - >/dev/null
    fi

    say "SDR build 5/5: SoapySDRPlay3 module -> /usr/local"
    clone_pin "$SOAPYSDRPLAY_REPO" "$SOAPYSDRPLAY_COMMIT" "$SRC_DIR/SoapySDRPlay3"
    build_cmake "$SRC_DIR/SoapySDRPlay3"
    run "/sbin/ldconfig"
  fi

  # Blacklist the kernel DVB driver so it doesn't grab the dongle before SoapySDR.
  # (rtl-sdr-blog's INSTALL_UDEV_RULES lays down the device-perms .rules; this is
  # the module blacklist half.)
  say "Blacklist kernel DVB driver (frees the RTL dongle for SoapySDR)"
  if [ "$DRY_RUN" = 1 ]; then
    info "[dry-run] write /etc/modprobe.d/blacklist-rtlsdr.conf"
  else
    cat > /etc/modprobe.d/blacklist-rtlsdr.conf <<'BL'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
BL
  fi
else
  say "SDR stack SKIPPED (--no-sdr): Icom-LAN rigs need only numpy. Kenwood/Yaesu/dongle spectrum needs SoapySDR — re-run without --no-sdr to add it."
fi

# ------------------------------------------------------------------------------
# 3) deploy the aether_gate package
# ------------------------------------------------------------------------------
# Copy the package from this checkout to $GATE_DIR (a plain copy — no PYTHONPATH
# surprises, matches how the Pi5 runs). Excludes dev/junk. If the script is being
# run FROM $GATE_DIR already, this is a no-op.
say "Deploy aether_gate -> $GATE_DIR"
if [ "$REPO_ROOT" != "$GATE_DIR" ]; then
  run "install -d -o '$GATE_USER' -g '$GATE_USER' '$GATE_DIR'"
  run "cp -r '$REPO_ROOT/aether_gate' '$GATE_DIR/'"
  run "cp -r '$REPO_ROOT/deploy' '$GATE_DIR/'"
  run "chown -R '$GATE_USER':'$GATE_USER' '$GATE_DIR'"
  # drop __pycache__ so a stale .pyc can't shadow a fresh .py (deploy-race lesson)
  run "find '$GATE_DIR' -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true"
else
  info "running from $GATE_DIR already — skipping copy"
fi

# ------------------------------------------------------------------------------
# 4) systemd: Setup UI on boot (:8730) — the first-boot face of the appliance
# ------------------------------------------------------------------------------
say "systemd: aether-gate-setup.service (boot -> Setup UI :8730)"
UNIT_SRC="$GATE_DIR/deploy/systemd/aether-gate-setup.service"
if [ "$DRY_RUN" = 1 ]; then
  info "[dry-run] install $UNIT_SRC -> /etc/systemd/system/, enable --now"
else
  # the shipped unit assumes User=pi + /home/pi/gate; rewrite for this user/home
  sed -e "s#User=pi#User=$GATE_USER#" \
      -e "s#/home/pi/gate#$GATE_DIR#g" \
      "$UNIT_SRC" > /etc/systemd/system/aether-gate-setup.service
  if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    systemctl enable --now aether-gate-setup.service
  else
    # image-build chroot: systemd isn't running — enable is just a symlink,
    # the service starts on the appliance's first real boot.
    systemctl enable aether-gate-setup.service
  fi
fi

# ------------------------------------------------------------------------------
# done
# ------------------------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOSTN="$(hostname 2>/dev/null).local"
say "Done."
cat <<EOF
    Setup UI:   http://$HOSTN:8730/        (avahi)
                http://${IP:-<pi-ip>}:8730/
    Open it, pick a radio, hit Start. Mark a profile "connect on launch" to
    auto-start next boot.

    For an always-on radio (survives reboots, graceful stop), install a
    dedicated service instead of relying on the launcher:
        sudo cp $GATE_DIR/deploy/systemd/aether-gate-9700.service /etc/systemd/system/
        sudoedit /etc/systemd/system/aether-gate-9700.service   # set radio IP / pass / --ip / --ae
        sudo systemctl enable --now aether-gate-9700
    (see $GATE_DIR/deploy/systemd/README.md)

    Verify anytime:  ./install-pi.sh --check
EOF
