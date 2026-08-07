# Aether-gate — Raspberry Pi appliance

Turn a Raspberry Pi into a flash-and-go Aether-gate box: power it on, browse to
**`http://aethergate.local:8730`**, pick your radio, hit **Start**, and it
appears in AetherSDR. This is the recommended way to run the gate unattended.

See also: [`deploy/install-pi.sh`](deploy/install-pi.sh) (the installer),
[`deploy/systemd/README.md`](deploy/systemd/README.md) (running a radio as a
service), [`ONBOARDING.md`](ONBOARDING.md) (the day-one design).

---

## Which Pi?

| | Works? | Notes |
|---|---|---|
| **Pi 5** | ✅ best | Proven appliance. Best USB power/bandwidth (drove the V4 dongle + an FTDI CAT cable together, no brownout). |
| **Pi 4** (2GB+) | ✅ fine | Runs the gate comfortably. Source build is a bit slower (~15–20 min vs ~10–15). USB power is more marginal — use a **powered hub** if a dongle misbehaves. |
| **Pi 3 / Zero 2** | ⚠️ maybe | The **prebuilt image boots** on a Pi 3 (verified: first boot → Setup UI, 2026-07-31). Gate workloads untested there — Icom-LAN-only is the realistic use; a source build will be slow. |
| **Pi 1 / 2 / Zero** | ❌ no | ARMv6/v7 — cannot run a 64-bit image at all. Symptom: ACT LED flickers (firmware reads the card) but it never boots or joins the network. |

**The one thing that matters more than the model: the OS.** Flash **current
64-bit Raspberry Pi OS (Debian 13 / trixie, Python 3.13)** — the exact stack the
installer is pinned against. An older release (Bookworm/Bullseye) shifts apt
names and Python paths; the installer detects this and warns, but flashing
current Pi OS is the smooth path. **Always 64-bit** (aarch64) — a 32-bit OS
fights the source builds.

---

## The easy way: flash the prebuilt image

Skip the install entirely — flash a ready-made appliance image:

1. Download the latest `aether-gate-pi-<ver>.img.xz` (+ `.sha256`) from the
   [releases page](https://github.com/nigelfenton/Aether-gate/releases).
2. Flash it with **Raspberry Pi Imager** → *Use custom image*. Imager's OS
   customisation (⚙ — your username, WiFi, SSH) **works on this image exactly
   as on stock Pi OS** — set your WiFi there if the Pi won't be on Ethernet.
3. Boot, give it a minute or two (first boot expands the filesystem), then
   browse **`http://aethergate.local:8730`** — pick your radio, hit **Start**.

The image is official 64-bit Pi OS Lite with the full install below already
baked in (SDR stack included). The gate runs as its own `aethergate` system
user, so it works whatever username you pick in Imager — and works even if you
skip Imager customisation entirely. Provenance is stamped in
`/etc/aether-gate-image-release`.

**Which Pi?** One image covers **Pi 3, 3B+, 4, 5 and Zero 2 W** — it is stock
64-bit Pi OS, so any board Pi OS calls 64-bit-capable will boot it. A **Pi 4 or
5 is the recommendation**: the Pi 3 shares one USB 2 bus between Ethernet and
USB, which shows up as audio stutter at high sample rates. ❌ **Pi 1, Pi 2 and
the original Pi Zero cannot run it** — they are 32-bit (ARMv6/v7) and there is
no 64-bit kernel for them. The failure looks alive but is not: solid red LED,
green activity flickering, and the Pi never appears on the network. If that is
what you see, check the board — the Ethernet MAC prefix `b8:27:eb` is shared by
Pi 1 through Pi 3 and cannot tell them apart, so read the silkscreen.

### SDRplay (RSP1a, RSP2, RSPdx…) — one extra command

The published image does **not** include SDRplay support. Their API is
proprietary and its licence does not permit us to redistribute it inside an
image. It installs fine on your own Pi, where you accept their licence
yourself:

On the appliance itself (the script is already on the card):

```sh
sudo /home/aethergate/gate/deploy/add-sdrplay.sh
```

It is safe to re-run, refreshes the package lists the image ships without, and
verifies the daemon and the SoapySDR driver rather than just assuming they came
up. Then pick `sdrplay` as the driver in the Setup UI.

**All RSP models are covered by one driver.** SDRplay's API claims the whole
family — RSP1 (`2500`), RSP1a (`3000`), RSP1B (`3010`), RSP2/2pro (`3020`),
RSPduo (`3030`), RSPdx (`3050`) and RSPdx-R2 (`3060`) — so the same install
serves any of them. Only the RSP1a has been tested here; the RSPduo's dual-tuner
modes in particular need extra Soapy device arguments that the Setup UI does not
expose yet.

Everything else — RTL-SDR, HPSDR/Hermes-Lite 2/Radioberry, and network Icoms —
works straight off the flashed card with nothing extra to install.

### ⚠ If 2 m and 70 cm never appear in AetherSDR

Pass **`--model FLEX-6700`**. AE decides which bands to offer from the radio
model the gate advertises, and a **FLEX-6600 is HF + 6 m only** — so 2 m simply
never shows up, no matter what your SDR can actually tune. The 6700 has native
2 m, and advertising it also raises the slice cap from 4 to 8.

This bites because the command-line default is `FLEX-6600`, and it *overrides*
the SDR adapter's own `FLEX-6700` default. It has to be stated explicitly.

### Making it start at boot

The Setup UI runs the gate as a child process, so it stops when the launcher
does and does not come back after a power cut. For an always-on appliance,
install the service instead:

```sh
sudo cp /home/aethergate/gate/deploy/systemd/aether-gate-sdr.service /etc/systemd/system/
sudoedit /etc/systemd/system/aether-gate-sdr.service   # set --soapy-driver, --ae, --model
sudo systemctl daemon-reload
sudo systemctl enable --now aether-gate-sdr
journalctl -u aether-gate-sdr -f
```

Verified on a Pi 3B+ with an RSP1a: after a reboot the Pi is back in about 40
seconds with the gate already running and AetherSDR reconnecting on its own.

> **RSP tuning note:** sample rates below 2 MHz carry an uncompensated Low-IF
> offset (≈13 kHz low at 500 k, ≈16 kHz at 1 M). Use **2 MHz or higher** and
> the tuning is true. Measured on an RSP1a — it is a property of the Low-IF
> plan, so expect it family-wide, but the exact figures are not characterised
> for other models.

### Building the image yourself

`deploy/build-image.sh` reproduces it from any 64-bit Pi (4/5) or other
aarch64 Debian host — it customises the official image in a chroot and never
boots it, so all of Pi OS's first-boot machinery stays stock:

```sh
git clone https://github.com/nigelfenton/Aether-gate.git
cd Aether-gate
sudo ./deploy/build-image.sh          # -> out/aether-gate-pi-<ver>.img.xz
```

Add `--with-sdrplay` to bake SDRplay support into your own image. That output
is named `…-sdrplay-DO-NOT-REDISTRIBUTE.img.xz`, because it is fine for your
own hardware but must not be published or passed on — see the SDRplay note
above.

---

## Install by hand (the original way)

On a fresh Pi OS Lite (SSH enabled, on your LAN):

```sh
git clone https://github.com/nigelfenton/Aether-gate.git
cd Aether-gate
sudo ./deploy/install-pi.sh              # full appliance (with the SDR spectrum stack)
```

Variants:

```sh
sudo ./deploy/install-pi.sh --no-sdr     # IC-9700 / Icom-LAN only (numpy) — fast, no long build
./deploy/install-pi.sh --check           # report what's present/missing; changes nothing
sudo ./deploy/install-pi.sh --dry-run    # print every step; changes nothing
```

The installer is **idempotent** — safe to re-run (it skips builds already done).

### What it installs

- **apt:** `python3-numpy`, `libhamlib-utils` (rigctld), `avahi-daemon`, build tools.
- **Source-built into `/usr/local`** (only with the SDR stack — the default):
  - **rtl-sdr-blog fork** — the V4 dongle + HF direct-sampling. *The apt
    `librtlsdr` (2.0.2) does not drive the RTL-SDR V4 well — that's why we build
    the fork.*
  - **SoapySDR** core + its Python 3 bindings.
  - **SoapyRTLSDR** module.
  - The exact upstream commits are **pinned** in `install-pi.sh` (the versions
    proven on the Pi5), so a rebuild is reproducible.
- The `aether_gate` package copied to `~/gate`.
- **systemd `aether-gate-setup.service`** — boots straight to the Setup UI on `:8730`.

### The dependency split (why `--no-sdr` exists)

| Radio path | Needs |
|---|---|
| **IC-9700 / Icom LAN** | **numpy only** — no native libs. The easy path. |
| Kenwood / Yaesu (CAT) | hamlib (apt) + the SoapySDR stack (IF-tap spectrum) |
| RTL dongle | the SoapySDR stack |
| **SDRplay (RSP1a etc.)** | the SoapySDR stack + the **SDRplay API daemon** (proprietary, fetched from sdrplay.com during install — installing implies accepting their licence) + SoapySDRPlay3. In the Setup UI, set the SoapySDR driver to `sdrplay`. |

If you only run an Icom-LAN rig, `--no-sdr` skips the ~15-minute source build
entirely.

---

## First boot

1. Power on. Give it a minute (first boot + any build finishing if you just installed).
2. Browse **`http://aethergate.local:8730`** (or `http://<pi-ip>:8730`).
3. Pick your radio family, fill the connection fields, **Start**.
4. Save it as a **profile** and tick **"connect on launch"** so it comes up on its own next boot.

## Always-on radio (recommended for a dedicated box)

The Setup UI starts the gate as a *child* — fine for interactive use, but a
crashed launcher can't shut the gate down cleanly (for an IC-9700 that can leave
a phantom session). For an unattended box, run the radio as its **own systemd
service** so `systemctl stop` shuts it down gracefully:

```sh
sudo cp ~/gate/deploy/systemd/aether-gate-9700.service /etc/systemd/system/
sudoedit /etc/systemd/system/aether-gate-9700.service   # set radio IP, --pass, --ip, --ae
sudo systemctl enable --now aether-gate-9700
journalctl -u aether-gate-9700 -f
```

See [`deploy/systemd/README.md`](deploy/systemd/README.md) for why-a-service and
the graceful-stop verification.

---

## Capturing an image (once installed & configured)

After the appliance works the way you want:

1. Optionally clear machine-specific state (SSH host keys, logs, saved Wi-Fi) for
   a clean template.
2. Shut down, pull the SD card, and image it (`dd`, or Raspberry Pi Imager's
   "read" / a tool like `pishrink` to shrink the image before sharing).
3. That image is now flash-and-go for the next Pi.

> ⚠️ **GPL note:** handing that image (a built binary distribution) to *anyone*
> triggers the GPL-3.0 obligation to offer the complete corresponding source —
> which is fine, it's all in this repo. Just ship it *with* the repo URL / a copy
> of the source. (Same rule as showing anyone a running build.)

---

## Status / what's proven

- ✅ Installer written, **pinned** to the Pi5's exact working versions.
- ✅ `--check` and `--dry-run` validated on the Pi5 (all probes green; dry-run
  walks every step).
- ⚠️ **Not yet run end-to-end on a genuinely fresh flash** — the Pi5 already has
  the SDR stack, so the installer's source-build path skips there (as designed
  and idempotent). The true flash-and-go test is a clean Pi OS Lite install;
  that's the next milestone once a spare Pi/SD card is on hand.
