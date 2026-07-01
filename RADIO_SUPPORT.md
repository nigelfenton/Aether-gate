# Aether-gate — multi-vendor radio support (design)

**Status:** design · 2026-06-30 · G0JKN. Radio knowledge lives in the GATE (AE stays
a transparent Flex client). Icom IC-9700 is the only hardware-VERIFIED entry so far;
everything else is a best-known plan — **VERIFY before relying on it.**

## The two axes (this is the whole model)

A radio contributes **two independent things**; the gate sources them separately:

| Axis | Options | Notes |
|---|---|---|
| **CONTROL** (freq/mode/PTT) | `native-lan` · `hamlib` · `usb-civ` | how we set/read the rig |
| **SPECTRUM** (the panadapter) | `civ-scope` · `soapy-iftap` · `audio-fft` · `native-iq` | where the waterfall comes from |

**Key insight:** Icom LAN rigs are special — their RS-BA1 protocol bundles *control + scope +
audio* in one UDP stream (control=`native-lan`, spectrum=`civ-scope`). **Most Kenwood/Yaesu
do NOT emit scope data over CAT**, so they split: control=`hamlib`, spectrum=an **IF-tap
SoapySDR dongle steered by the CAT frequency** (`soapy-iftap`) — or `audio-fft` as a narrow
last resort. SDRs (Radioberry/RTL/Airspy) are `native-iq` and need no CAT at all.

## Control backends

- **`native-lan` (implemented):** Icom RS-BA1 UDP (handler/udpbase/civ). Uniquely also carries
  scope + audio. Use for Icom LAN rigs (9700/705/7610/R8600/905).
- **`hamlib` (RECOMMENDED for everything else, build TODO):** libhamlib / `rigctld` covers ~250
  rigs' CAT — Kenwood/Yaesu/Elecraft/Icom-USB — community-maintained, so we don't hand-port each
  vendor's protocol (as we did SDR9700's CI-V). **Control ONLY** — no spectrum/audio, so always
  pair with a spectrum source. Cloned at linux-aether `/srv/build/hamlib` but **NOT built**
  (`rigctl` missing) → TODO: build + resolve `rigctl -l` model IDs. Hamlib's own rig list *is*
  effectively the CAT registry; our table below only adds the gate bits (advertise-model,
  spectrum source, bands).
- **`usb-civ`:** Icom CI-V over USB serial (7300/7100/9100) — could ride Hamlib too.

## Spectrum sources

- **`civ-scope`:** Icom 27h band-scope → dBm (implemented in civ.py). Real, wide, radio's own scope.
- **`soapy-iftap`:** a cheap SDR dongle on the rig's IF (or antenna) tap; the gate steers/labels
  the dongle's spectrum from the CAT freq. The path for CAT rigs with no scope-over-CAT. Reuses the
  existing `soapy` adapter + the CAT-audio-IF-dongle design (see aurora13 memory).
- **`audio-fft`:** FFT the rig's USB-audio (narrow ~12 kHz window). Last resort.
- **`native-iq`:** SDR delivers raw IQ; core FFTs it (soapy adapter). Radioberry/RTL/Airspy/SDRplay.

## Bands beyond the advertised Flex model
Advertise FLEX-6700 (has built-in 2 m) for rigs with 2 m; FLEX-6600 for HF+6 m. Anything past that
(70 cm, 23 cm, microwave) rides **AE's native XVTR** — gate maps the IF AE tunes ↔ the real RF.

---

## Radio table (⚠ non-Icom = best-known, VERIFY)

### Icom (in `adapters/icom/radios.py`)
| Model | control | spectrum | advertise | bands | status |
|---|---|---|---|---|---|
| IC-9700 | native-lan | civ-scope | FLEX-6700 | 2m/70cm/23cm | ✅ VERIFIED, 2m driven from AE |
| IC-705 | native-lan | civ-scope | FLEX-6700 | HF/6m/2m/70cm | LAN family |
| IC-7610 | native-lan | civ-scope | FLEX-6600 | HF/6m | LAN family |
| IC-R8600 | native-lan | civ-scope | FLEX-6700 | 10kHz–3GHz RX | RX-only |
| IC-905 | native-lan | civ-scope | FLEX-6700 | 2m→10GHz | most bands via XVTR |
| IC-7300 | usb-civ/hamlib | civ-scope | FLEX-6600 | HF/6m | needs usb transport |
| IC-7100 | usb-civ/hamlib | audio-fft | FLEX-6700 | HF/6m/2m/70cm | no 27h scope |
| IC-9100 | usb-civ/hamlib | soapy-iftap | FLEX-6700 | HF/6m/2m/70cm(+23cm opt) | no scope |

### Kenwood (via Hamlib — no scope-over-CAT, so IF-tap dongle)
| Model | control | spectrum | advertise | bands | status |
|---|---|---|---|---|---|
| TS-590SG | hamlib | soapy-iftap | FLEX-6600 | HF/6m | GUESS/VERIFY |
| TS-890S | hamlib | soapy-iftap | FLEX-6600 | HF/6m | has scope but not over CAT |
| TS-2000 | hamlib | soapy-iftap | FLEX-6700 | HF/6m/2m/70cm(+23cm opt) | multibander |

### Yaesu (via Hamlib — IF-tap dongle for panadapter)
| Model | control | spectrum | advertise | bands | status |
|---|---|---|---|---|---|
| FT-991A | hamlib | soapy-iftap | FLEX-6700 | HF/6m/2m/70cm | GUESS/VERIFY |
| FTDX10 | hamlib | soapy-iftap | FLEX-6600 | HF/6m | scope not over CAT |
| FT-710 | hamlib | soapy-iftap | FLEX-6600 | HF/6m | GUESS/VERIFY |
| FT-817/818 | hamlib | audio-fft | FLEX-6700 | HF/6m/2m/70cm | QRP, no scope |

### SDR / other (NOT CAT — IQ source family, `soapy`/HPSDR adapter)
| Device | control | spectrum | advertise | bands | status |
|---|---|---|---|---|---|
| Radioberry | (n/a) | native-iq (HPSDR) | FLEX-6700 | HF(+VHF w/ board) | HPSDR proto or SoapyHPSDR; TX-capable; see HPSDR fork |
| RTL-SDR / Airspy / SDRplay | (n/a) | native-iq (SoapySDR) | FLEX-6x00 | per device | ✅ soapy adapter exists |

---

## Build order to realise this
1. **Hamlib** — build on linux-aether (`/srv/build/hamlib`), resolve model IDs, wrap `rigctld`
   (or python bindings) as a control backend. Unlocks Kenwood/Yaesu/Icom-USB control in one shot.
2. **`soapy-iftap`** — CAT-steered dongle spectrum (extend the soapy adapter with CAT-follow) —
   the panadapter for every non-scope CAT rig.
3. **usb-civ transport** — for Icom USB rigs without Hamlib (or just use Hamlib).
4. **XVTR IF↔RF mapping** in the adapter — 70 cm/23 cm/microwave.
5. **Radioberry** — HPSDR IQ source (SoapyHPSDR or direct), ties to the ae6ch HPSDR fork.
6. **Verify** every non-9700 row against datasheets / on hardware.

*Future: AE may absorb these natively (KiwiSDR is the precedent). Until then the gate holds the
radio knowledge — as data, so a new radio is a table row, not a new driver.*
