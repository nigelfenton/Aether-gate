# Kenwood bring-up runbook (TS-2000 / TS-590SG / TS-890S)

**Status:** scaffold ready, untested end-to-end. Follow this when the hardware is on the bench.
The gate presents the Kenwood to AetherSDR as a Flex; **control = hamlib, spectrum = an IF-tap
SoapySDR dongle CAT-steered to follow the rig**. (See RADIO_SUPPORT.md for the design.)

## What you need
1. **The Kenwood** with a CAT connection to a host (USB/serial cable, or the TS-2000's COM port).
2. **hamlib `rigctld`** running on that host (it's cloned at linux-aether `/srv/build/hamlib` but
   **not built yet** — build it, or `apt install libhamlib-utils` for a quick start).
3. **An HF-capable SDR dongle** for the spectrum (RTL-SDR Blog V4 / upconverter / RX888 / Airspy
   HF+) on the same host that runs the gate — Kenwood HF needs HF coverage a plain R820T can't do.
   Tap it off-air (own antenna, no TX risk — the default), or antenna-coupler / IF-tap for more
   fidelity later.

## Step 1 — start rigctld
Find the model id: `rigctl -l | grep -i kenwood`. Current ids baked into the registry:
- TS-2000 = **2014**, TS-590SG = **2035** (TS-590S = 2029), TS-890S = **2045**.
```
rigctld -m 2014 -r /dev/ttyUSB0 -s 9600        # TS-2000, adjust device + baud to your cable
# it listens on TCP :4532. Test it:
rigctl -m 2 127.0.0.1:4532 f                    # should print the rig's current freq
```
(`-m 2` = NET rigctl client talking to the daemon.)

## Step 2 — confirm the dongle enumerates
On the gate host: `SoapySDRUtil --find` should list the dongle (driver=rtlsdr, your V4).
The SoapySDR python binding must import: `python3 -c "import SoapySDR"` (on the Pi5 it's built;
on a fresh host see the soapy-install notes in the aurora13 memory).

## Step 3 — run the gate
```
python -m aether_gate --adapter kenwood \
    --kw-model TS-2000 \
    --rigctld-host 127.0.0.1 --rigctld-port 4532 \
    --soapy-driver rtlsdr --gain 40 \
    --ip <this-host-ip> --ae <AE-host-ip> \
    --serial GATEKENW --station "aether-gate kenwood" --ctl-port 8733
```
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
