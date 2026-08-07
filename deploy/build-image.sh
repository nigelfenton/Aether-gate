#!/usr/bin/env bash
#
# Aether-gate — flashable Pi appliance image builder.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
# Bakes official 64-bit Raspberry Pi OS Lite into an Aether-gate appliance
# image: flash it with Raspberry Pi Imager, boot, browse
# http://aethergate.local:8730, pick a radio, Start.
#
# HOW IT WORKS
#   The official image is customised in a chroot and NEVER BOOTED, so all of
#   Pi OS's stock first-boot machinery stays armed: Raspberry Pi Imager's
#   customisation (your user, WiFi, SSH) still applies, the filesystem still
#   auto-expands, SSH host keys are still generated per-card. The gate itself
#   runs as its own baked-in system user (aethergate), independent of whatever
#   username the operator chooses in Imager.
#
# REQUIREMENTS
#   * an aarch64 Debian-ish host (a Pi 4/5 is ideal) — the chroot runs natively
#   * root
#   * ~10 GB free in --workdir
#
# USAGE (from a checkout of this repo)
#   sudo ./deploy/build-image.sh                     # full build -> ./out
#   sudo ./deploy/build-image.sh --no-sdr            # slim Icom-LAN-only image
#   sudo ./deploy/build-image.sh --workdir /big/dir --out /big/dir/out
#
# The base image is cached in $WORKDIR/cache — later builds skip the download.

set -euo pipefail

BASE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64_latest"
GROW_GB=3                  # extra room for build tools + source builds
AG_SVC_USER="aethergate"   # baked-in service user (independent of Imager's user)
IMG_HOSTNAME="aethergate"  # -> http://aethergate.local:8730

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$REPO_ROOT/imagework"
OUTDIR="$REPO_ROOT/out"
SDR_ARG="--with-sdr"
# Default OFF: this builder's output is meant to be publishable, and SDRplay's
# EULA grants no distribution right (see WITH_SDRPLAY in install-pi.sh). Opt in
# with --with-sdrplay for a private image for your own hardware.
SDRPLAY_ARG="--no-sdrplay"

for a in "$@"; do
  case "$a" in
    --no-sdr)       SDR_ARG="--no-sdr" ;;
    --with-sdrplay) SDRPLAY_ARG="--with-sdrplay" ;;
    --no-sdrplay)   SDRPLAY_ARG="--no-sdrplay" ;;
    --workdir=*)    WORKDIR="${a#*=}" ;;
    --out=*)        OUTDIR="${a#*=}" ;;
    --base-url=*)   BASE_URL="${a#*=}" ;;
    --grow-gb=*)    GROW_GB="${a#*=}" ;;
    -h|--help)      sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a (try --help)"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[fail] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ]        || die "needs root (loop devices + chroot)"
[ "$(uname -m)" = aarch64 ] || die "needs an aarch64 host — the chroot runs natively (a Pi 4/5 is ideal)"
for t in losetup parted e2fsck resize2fs xz curl rsync sha256sum; do
  command -v "$t" >/dev/null || PATH="$PATH:/usr/sbin:/sbin" command -v "$t" >/dev/null \
    || die "missing tool: $t"
done
export PATH="$PATH:/usr/sbin:/sbin"

mkdir -p "$WORKDIR/cache" "$OUTDIR"

# ------------------------------------------------------------------------------
# cleanup — runs on any exit; unwinds whatever got set up
# ------------------------------------------------------------------------------
LOOP=""
ROOT=""
RESOLV_WAS_LINK=""
cleanup() {
  set +e
  if [ -n "$ROOT" ] && [ -d "$ROOT" ]; then
    # restore the image's stock resolv.conf symlink before the fs goes away
    if [ -n "$RESOLV_WAS_LINK" ]; then
      rm -f "$ROOT/etc/resolv.conf"
      ln -s "$RESOLV_WAS_LINK" "$ROOT/etc/resolv.conf"
    fi
    rm -f "$ROOT/usr/sbin/policy-rc.d" "$ROOT/tmp/image-stage.sh"
    for m in dev/pts dev proc sys run boot/firmware; do
      mountpoint -q "$ROOT/$m" && umount -l "$ROOT/$m"
    done
    mountpoint -q "$ROOT" && umount -l "$ROOT"
  fi
  [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
}
trap cleanup EXIT

# ------------------------------------------------------------------------------
# 1) fetch + unpack the base image (cached)
# ------------------------------------------------------------------------------
say "Base image"
FINAL_URL="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$BASE_URL")" \
  || die "cannot resolve $BASE_URL"
BASE_XZ="$WORKDIR/cache/$(basename "$FINAL_URL")"
case "$BASE_XZ" in *.img.xz) : ;; *) die "unexpected base image name: $BASE_XZ" ;; esac
if [ -s "$BASE_XZ" ]; then
  echo "    cached: $BASE_XZ"
else
  echo "    downloading: $FINAL_URL"
  curl -fSL -o "$BASE_XZ.part" "$FINAL_URL" && mv "$BASE_XZ.part" "$BASE_XZ"
  # verify against the publisher's checksum when available
  if curl -fsSL -o "$BASE_XZ.sha256" "$FINAL_URL.sha256" 2>/dev/null; then
    (cd "$WORKDIR/cache" && sha256sum -c "$(basename "$BASE_XZ.sha256")") \
      || die "base image failed its published sha256"
  else
    echo "    [warn] no published .sha256 alongside the base image — skipping verify"
  fi
fi

IMG="$WORKDIR/aether-gate-build.img"
say "Unpacking -> $IMG"
xz -dkc "$BASE_XZ" > "$IMG"

# ------------------------------------------------------------------------------
# 2) grow the image so the SDR source builds have room
# ------------------------------------------------------------------------------
say "Growing image by ${GROW_GB}G"
truncate -s "+${GROW_GB}G" "$IMG"
parted -s "$IMG" resizepart 2 100%

LOOP="$(losetup -fP --show "$IMG")"
e2fsck -pf "${LOOP}p2" >/dev/null || true
resize2fs "${LOOP}p2"

# ------------------------------------------------------------------------------
# 3) mount + enter
# ------------------------------------------------------------------------------
ROOT="$WORKDIR/root"
mkdir -p "$ROOT"
mount "${LOOP}p2" "$ROOT"
mount "${LOOP}p1" "$ROOT/boot/firmware"
mount -t proc  proc  "$ROOT/proc"
mount -t sysfs sys   "$ROOT/sys"
mount --bind  /dev    "$ROOT/dev"
mount --bind  /dev/pts "$ROOT/dev/pts"
mount -t tmpfs tmpfs "$ROOT/run"

# working DNS inside the chroot (the image's resolv.conf is a dead symlink here)
if [ -L "$ROOT/etc/resolv.conf" ]; then
  RESOLV_WAS_LINK="$(readlink "$ROOT/etc/resolv.conf")"
  rm -f "$ROOT/etc/resolv.conf"
fi
cat /etc/resolv.conf > "$ROOT/etc/resolv.conf"

# stop apt postinsts from trying to start daemons in the chroot
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"
chmod +x "$ROOT/usr/sbin/policy-rc.d"

# the repo goes in for the installer to run from
rsync -a --exclude .git --exclude attic --exclude imagework --exclude out \
  "$REPO_ROOT/" "$ROOT/opt/aether-gate-src/"

# ------------------------------------------------------------------------------
# 4) the in-chroot stage: service user, installer, hostname, cleanup
# ------------------------------------------------------------------------------
say "Chroot stage (installer $SDR_ARG $SDRPLAY_ARG) — the SDR builds take a while"
cat > "$ROOT/tmp/image-stage.sh" <<STAGE
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# dedicated service user — survives whatever username Imager creates
if ! id -u $AG_SVC_USER >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/$AG_SVC_USER \
          --shell /usr/sbin/nologin $AG_SVC_USER
fi
usermod -aG dialout,plugdev $AG_SVC_USER   # CAT serial + USB dongles

AG_USER=$AG_SVC_USER bash /opt/aether-gate-src/deploy/install-pi.sh $SDR_ARG $SDRPLAY_ARG

# appliance identity: http://aethergate.local
echo $IMG_HOSTNAME > /etc/hostname
sed -i "s/\braspberrypi\b/$IMG_HOSTNAME/g" /etc/hosts

# slim down: build sources + installer checkout + apt droppings
rm -rf /home/$AG_SVC_USER/gate-build /opt/aether-gate-src
apt-get clean
rm -rf /var/lib/apt/lists/*
STAGE
chmod +x "$ROOT/tmp/image-stage.sh"
chroot "$ROOT" /tmp/image-stage.sh

# image provenance stamp
VER="$(git -C "$REPO_ROOT" describe --tags --always 2>/dev/null || echo unknown)"
{
  echo "aether-gate image"
  echo "version=$VER"
  echo "built=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "sdr=$SDR_ARG"
  # Record the SDRplay decision on the card itself. A ham running --check wants
  # to know whether SDRplay is missing because of a broken build or by design,
  # and anyone handed an image needs to see at a glance whether it is one of the
  # private --with-sdrplay builds that must not be passed on.
  echo "sdrplay=$SDRPLAY_ARG"
  if [ "$SDRPLAY_ARG" = "--with-sdrplay" ]; then
    echo "redistributable=no  # contains SDRplay's proprietary API - do not publish or pass on"
  fi
} > "$ROOT/etc/aether-gate-image-release"

# ------------------------------------------------------------------------------
# 5) zero free space so xz crushes it, then unwind
# ------------------------------------------------------------------------------
say "Zero-filling free space (helps compression)"
dd if=/dev/zero of="$ROOT/zero.fill" bs=4M status=none || true
rm -f "$ROOT/zero.fill"
sync

cleanup
trap - EXIT
LOOP=""; ROOT=""

# ------------------------------------------------------------------------------
# 6) name, compress, checksum
# ------------------------------------------------------------------------------
SUFFIX=""; [ "$SDR_ARG" = "--no-sdr" ] && SUFFIX="-lite"
# NAME THE PROPRIETARY BUILD SO IT CANNOT BE UPLOADED BY ACCIDENT. An image with
# the SDRplay API baked in is for the builder's own hardware only — the EULA
# grants no right to distribute it. The filename is the last line of defence
# between "built it for my own Pi" and "attached it to a public release".
[ "$SDRPLAY_ARG" = "--with-sdrplay" ] && SUFFIX="${SUFFIX}-sdrplay-DO-NOT-REDISTRIBUTE"
OUT="$OUTDIR/aether-gate-pi${SUFFIX}-${VER}.img"
say "Compressing -> $OUT.xz"
mv "$IMG" "$OUT"
xz -T0 -6 -f "$OUT"
(cd "$OUTDIR" && sha256sum "$(basename "$OUT").xz" > "$(basename "$OUT").xz.sha256")

say "Done."
ls -lh "$OUT.xz" "$OUT.xz.sha256"
cat <<EOF

    Flash with Raspberry Pi Imager (Use custom image). Imager's OS
    customisation (your user, WiFi, SSH) works on this image exactly as on
    stock Pi OS. First boot: give it a minute, then browse
    http://$IMG_HOSTNAME.local:8730/
EOF
