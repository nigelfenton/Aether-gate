# Kenwood bring-up runbook (TS-2000 / TS-590SG / TS-890S)

**Status:** scaffold ready, untested end-to-end. Follow this when the hardware is on the bench.
The gate presents the Kenwood to AetherSDR as a Flex; **control = hamlib, spectrum = an IF-tap
SoapySDR dongle CAT-steered to follow the rig**. (See RADIO_SUPPORT.md for the design.)

## What you need
1. **The Kenwood** with a CAT connection to a host (USB/serial cable, or the TS-2000's COM port).
2. **hamlib `rigctld`** running on that host. ✅ **INSTALLED on linux-aether** (`libhamlib-utils`
   4.5.5, `/usr/bin/rigctld`). Model ids CONFIRMED vs that version: TS-2000=2014, TS-590S=2031,
   TS-590SG=2037, TS-890S=2041. On another host: `apt install libhamlib-utils`.
3. **An HF-capable SDR dongle** for the spectrum (RTL-SDR Blog V4 / upconverter / RX888 / Airspy
   HF+) on the same host that runs the gate — Kenwood HF needs HF coverage a plain R820T can't do.
   Tap it off-air (own antenna, no TX risk — the default), or antenna-coupler / IF-tap for more
   fidelity later.

## Step 1 — start rigctld  (OR let the gate spawn it — see Step 3)
Find the model id: `rigctl -l | grep -i kenwood`. Ids confirmed in the registry:
- **TS-450S = 2003** (HF-only), TS-2000 = 2014, TS-590SG = 2037, TS-590S = 2031, TS-890S = 2041.

**⭐ CRITICAL for the TS-450 (and likely other older rigs):** it needs
`--set-conf="serial_handshake=None,rts_state=ON,dtr_state=ON"` or hamlib TIMES OUT (retval -5,
"no response") even though the radio IS answering — RTS+DTR asserted enables the cable's
level-shifter. (Proven 2026-07-02: without it hamlib fails; with it, full read+set works.)
```
rigctld -m 2003 -r COM10 -s 4800 -t 4532 \
    --set-conf="serial_handshake=None,rts_state=ON,dtr_state=ON"
# TS-450 = 4800 baud 8N1. Test it:
rigctl -m 2 127.0.0.1:4532 f                    # should print the rig's current freq (e.g. 14074000)
```
(`-m 2` = NET rigctl client talking to the daemon. On Windows, hamlib binaries live at
`C:\Users\nigel\Documents\Claude\tools\hamlib\hamlib-w64-4.7.2\bin\`.)

## Step 2 — confirm the dongle enumerates
On the gate host: `SoapySDRUtil --find` should list the dongle (driver=rtlsdr, your V4).
The SoapySDR python binding must import: `python3 -c "import SoapySDR"` (on the Pi5 it's built;
on a fresh host see the soapy-install notes in the aurora13 memory).

## Step 3 — run the gate
**Easiest — let the gate spawn rigctld itself** (bakes in the serial-config fix; just give it the
port). TS-450 example:
```
python -m aether_gate --adapter kenwood \
    --kw-model TS-450S --rig-serial-port COM10 --rig-baud 4800 \
    --rigctld-bin "C:/Users/nigel/Documents/Claude/tools/hamlib/hamlib-w64-4.7.2/bin/rigctld.exe" \
    --soapy-driver rtlsdr --gain 40 \
    --ip <this-host-ip> --ae <AE-host-ip> \
    --serial GATEKENW --station "aether-gate kenwood" --ctl-port 8733
```
(On the Pi, `--rig-serial-port /dev/ttyUSB0` and drop `--rigctld-bin` — it's on PATH.)

**Or point at an already-running rigctld** (Step 1): drop `--rig-serial-port` and use
`--rigctld-host 127.0.0.1 --rigctld-port 4532`.

⚠ **No HF dongle on the host = control works but no waterfall.** The TS-450 is HF; the panadapter
needs an HF-capable SDR (V4/upconverter). Control (tune both ways, mode, S-meter) works without it.
Notes:
- `--samp-rate` defaults 2.040 MS/s (integer audio decimation). RTL span is ~2 MHz — fine for a band view.
- For an HF dongle that needs direct sampling (non-V4): add `--direct-samp 2`.
- Pick a `--ctl-port` not clashing with other gates (9700 gate uses 8732).
- Advertises **FLEX-6700** (TS-2000 has 2m) so AE offers the right bands; `bands=` declares the
  rig's true set (needs the radio-declared-bands AE build; older AE falls back to the model).

## Step 4 — verify (the "does it work" checklist)
- AE's chooser shows **"aether-gate kenwood"**; connect to it.
- The gate's diagnostics page `http://<host>:8733/radio` shows: link "connected", hamlib model,
  the rig's current freq/mode, S-meter.
- **Tune the rig's dial** → AE's slice should follow (CAT-steer, ~1 s).
- **Tune in AE** → the rig's dial should move (hamlib set_freq).
- The **panadapter/waterfall** should show the dongle's spectrum around the rig's freq. (If the
  dongle is off-air on its own antenna, you'll see whatever it hears near that freq — not
  necessarily what the rig hears, until you IF/antenna-tap.)
- **Mode changes** both ways (rig ↔ AE).

## Known unknowns to check on HW (update the registry rows after)
- hamlib model id correct for your firmware/hamlib version (`rigctl -l`).
- S-meter scaling: hamlib `STRENGTH` is dB rel S9; the adapter maps S9 = −73 dBm (HF). Sanity-check
  against the rig's meter; VHF S9 is −93 dBm if you're on 2m via the TS-2000.
- CAT baud/flow-control quirks (TS-2000 is picky; try 9600 first).
- CAT-steer cadence (currently ~3 Hz): if the rig's CAT chokes, slow it (the IC-9700 taught us to
  be gentle). If AE↔rig tuning fights (both driving freq), add an echo-guard like the icom path.

## After it works
- Fill in `verified=True` + real notes on the registry row in `adapters/kenwood/radios.py`.
- This is the 2nd vendor for the upstream `feat/radio-declared-bands` PR ("Icom + Kenwood + …").
- Yaesu is the same pattern — a `yaesu/` registry + reuse KenwoodAdapter (rename to a generic
  `HamlibSoapyAdapter`? — it's already vendor-neutral, only the registry differs).
