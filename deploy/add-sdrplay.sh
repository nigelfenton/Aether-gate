#!/bin/bash
# add-sdrplay.sh — add SDRplay (RSP1a/RSP2/RSPdx/RSPduo) support to a running
# Aether-gate appliance.
#
# WHY THIS IS A SEPARATE STEP, not baked into the image:
# SDRplay's API is proprietary. Its EULA grants only "publicly display, publicly
# perform the Software in Object form" — no distribution right — reserves
# everything not expressly granted (clause 3), and bars disclosure to third
# parties (clause 2). So we cannot ship it inside a published image. Fetching it
# onto YOUR OWN Pi is fine: you accept their licence yourself, which is exactly
# what this script does.
#
# Usage, on the appliance:
#     sudo /home/aethergate/gate/deploy/add-sdrplay.sh
#
# Idempotent: safe to re-run. If SDRplay is already working it says so and exits.
set -euo pipefail

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "needs root — run with sudo"

# The installer does the real work; find it wherever this script lives.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$HERE/install-pi.sh"
[ -f "$INSTALLER" ] || die "install-pi.sh not found next to this script ($HERE)"

# ---- already done? -----------------------------------------------------------
if SoapySDRUtil --info 2>/dev/null | grep -qi sdrplay; then
  say "SDRplay is already installed and visible to SoapySDR — nothing to do."
  SoapySDRUtil --find 2>/dev/null | grep -iA2 sdrplay | head -6 || true
  exit 0
fi

# ---- apt lists ---------------------------------------------------------------
# The appliance image strips /var/lib/apt/lists to save space, so the first
# apt-get install on a fresh card fails with "Unable to locate package" unless
# we refresh first. This is the single most common cause of "the SDRplay
# install didn't work" on a flashed card.
if [ -z "$(ls -A /var/lib/apt/lists 2>/dev/null)" ]; then
  say "Refreshing apt lists (the image ships without them)"
  apt-get update -qq || die "apt-get update failed — check the Pi's network/DNS"
fi

# ---- hand off to the real installer -----------------------------------------
# --with-sdrplay runs only SDR stages 4-5 in practice: stages 1-3 detect what is
# already present on the appliance and skip themselves.
say "Installing the SDRplay API + SoapySDRPlay3 (fetched from sdrplay.com)"
info "This accepts SDRplay's licence on THIS machine. Takes a few minutes on a Pi 3."
bash "$INSTALLER" --with-sdrplay

# ---- verify ------------------------------------------------------------------
say "Verifying"
FAIL=0
if [ -x /opt/sdrplay_api/sdrplay_apiService ]; then
  info "[ok]   API daemon present"
else
  info "[--]   API daemon MISSING at /opt/sdrplay_api/sdrplay_apiService"; FAIL=1
fi

if systemctl is-active --quiet sdrplay 2>/dev/null; then
  info "[ok]   sdrplay.service running"
else
  # The daemon is socket/udev driven on some installs; not fatal on its own.
  info "[..]   sdrplay.service not active — starting it"
  systemctl enable --now sdrplay 2>/dev/null || true
  systemctl is-active --quiet sdrplay 2>/dev/null \
    && info "[ok]   sdrplay.service now running" \
    || info "[..]   still not active (may be socket-activated — check --find below)"
fi

if SoapySDRUtil --info 2>/dev/null | grep -qi sdrplay; then
  info "[ok]   SoapySDR sees the sdrplay factory"
else
  info "[--]   SoapySDR does NOT list an sdrplay factory"; FAIL=1
fi

say "Devices SoapySDR can find now"
SoapySDRUtil --find 2>/dev/null | tail -12 || true

if [ "$FAIL" = 0 ]; then
  say "DONE — in the Setup UI, type 'sdrplay' as the driver."
  info "TUNING NOTE: sample rates below 2 MHz carry an uncompensated Low-IF"
  info "offset (about -13 kHz at 500k, -16 kHz at 1M). Use 2 MHz or higher and"
  info "the tuning is true."
else
  die "SDRplay did not come up cleanly — see the [--] lines above."
fi
