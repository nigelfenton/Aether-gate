# Sketch — naming SDR dongles as sources, and declaring what they're connected to

**Status:** Sketch for discussion. Nothing built. 2026-07-16.
Follows [SHARED_SDR_DESIGN.md](SHARED_SDR_DESIGN.md), from Nigel's idea:

> *"maybe in the setup screen selecting the common name for antenna and delegated name if attached
> to IF (maybe the name of the radio)"*

## The idea, restated

A dongle's name should follow from **what it is physically connected to** — because that is the fact
that actually determines how it may be used:

| Connection | Name | Who may use it | Steering |
|---|---|---|---|
| **Antenna** (own aerial) | a **common name** the operator picks — `"Wideband"`, `"Attic dipole"` | **any** gate | tunable anywhere; contention is arbitration |
| **IF tap** (a rig's IF out) | **delegated** — the rig's name, `"TS-450S IF"` | **only that rig's** gate | pinned to that rig's IF; not shareable, ever |
| **Antenna via upconverter** | common name + the converter, `"Dipole via Ham It Up"` | **any** gate | tunable, but every frequency needs an **offset** |

### ⚠ A source's capability is set by its RF CHAIN, not its chip (2026-07-16)

Nigel has a **Ham It Up v1.3** upconverter (RF-only — it sits in the coax between antenna and dongle;
it has no USB and **can never be seen in software**). That exposes a flaw in my own naming today:

I flashed the generic dongle as **`R820VHF1`** and pinned it to the Yaesu, reasoning "plain R820T, no
upconverter, tuner floors out ~24 MHz -> it is a VHF/UHF dongle". True of a **bare** dongle. Put the
Ham It Up in front of it and **it becomes an HF receiver** — the converter shifts HF up by 125 MHz,
the dongle tunes at `signal + 125 MHz`, and 20m works fine.

**So the name encodes an assumption about the RF chain, and the assumption can be changed with a
coax lead.** If the Ham It Up is ever fitted in front of `R820VHF1`, the name is a lie.

Consequences for this design:
- A source needs an **`upconverter_offset_hz`** field (0 for direct, e.g. `+125_000_000` for a Ham It
  Up). The gate must add it to every tune request and subtract it from every displayed frequency.
  Without it the panadapter is simply 125 MHz wrong.
- The **serial identifies the DONGLE; the source identifies the CHAIN.** Those are not the same thing —
  one dongle moved to a different feed is a different source. Naming the EEPROM serial after an
  intended role (as I did) bakes today's cabling into the hardware.
- Better: keep serials **neutral and identity-only** (`NESDR01`, `V4BLOG01`), and let the *source
  name* in `profiles.json` carry the role and the chain. Then re-cabling is a config edit, not an
  `rtl_eeprom` reflash.
  ⚠ **`V4HF0001` / `R820VHF1` as flashed are already role-named — worth re-flashing to neutral names
  if this design proceeds.**

This is the right cut. It encodes the physical truth once, in the place a human knows it (the setup
screen), instead of leaving it implicit in three service files that each say `--soapy-driver rtlsdr`
and hope.

**Today's bug in one line:** three services claim "any rtlsdr", there is one dongle, and nothing in
the system knows either fact. Whoever starts first wins; the loser dies with
`usb_claim_interface error -6`.

## What "connected to" buys us (the part worth building)

The connection type is not decoration — **it decides the sharing rule**:

- **Antenna-fed → shareable.** No rig owns it. Two gates *could* both consume it (a broker), because
  the only conflict is which centre frequency it parks on. That is a policy question.
- **IF-fed → exclusive, by physics.** It sees one rig's IF and nothing else. A second gate consuming
  it would get the *wrong rig's* spectrum — worse than no spectrum, because it looks plausible.
  So the delegated name is not a label; it is a **lock**.

That asymmetry is why Nigel's two-name scheme is better than a flat "give every dongle a name". The
name carries the rule.

## Shape

### 1. Declare sources, once
A new top-level section in `~/.aether-gate/profiles.json` (which already holds saved radio profiles,
so the setup screen already has the pattern):

```json
{
  "sdr_sources": {
    "Wideband": {
      "driver": "rtlsdr",
      "device_args": "serial=00000001",
      "connected_to": "antenna",
      "note": "attic dipole"
    },
    "TS-450S IF": {
      "driver": "rtlsdr",
      "device_args": "serial=00000002",
      "connected_to": "if",
      "rig": "TS-450S",
      "if_freq_hz": 8830000
    }
  },
  "profiles": { "...": "unchanged" }
}
```

`device_args` is **already** plumbed end-to-end (`--soapy-args` → `SoapyAdapter.device_args` → parsed
into the SoapySDR args dict), so `serial=` selection needs no new code — only a UI and a name.
⚠ **CHECKED 2026-07-16 — and it is the stock serial, so step 3 needs `rtl_eeprom` first:**

```
iManufacturer  RTLSDRBlog
iProduct       Blog V4
iSerial        00000001          <-- factory default
soapy enumerate: {'label': 'Generic RTL2832U OEM :: 00000001', 'serial': '00000001',
                  'tuner': 'unavailable'}     <-- 'unavailable' = kenwood-gate holds it
```

`serial=00000001` is the **factory default every RTL-SDR Blog V4 ships with**. So `device_args`
selection works *today* only because there is one dongle — plug in a second and both answer to the
same serial, and "any rtlsdr" roulette returns wearing a name badge. **Before a 2nd dongle:
`rtl_eeprom -s <unique>` on each** (needs the dongle free — stop whichever gate holds it). That is a
one-time flash per dongle, and it is the prerequisite that makes named sources actually mean
something.

### 2. Radios reference a source by name
In a radio profile, replace the raw `soapy_driver` field with:

```json
{ "adapter": "kenwood", "kw_model": "TS-450S", "sdr_source": "Wideband" }
```

Setup screen: a **dropdown of source names**, not a driver string. Options are filtered by the rule —
an IF source delegated to another rig simply is not offered.

### 3. The adapter stops owning the dongle
Today `KenwoodAdapter.__init__` does `self._sdr = SoapyAdapter(...)` and `YaesuAdapter` inherits it,
so each gate constructs a private handle. Instead: resolve the named source and *acquire* it.

Minimum viable version — **no broker, just an honest claim** (option B from the design note):

```python
self._sdr = SdrSources.acquire(name)      # None if already claimed / absent
...
if self._sdr is None:
    log(f"[sdr] '{name}' unavailable (in use or not present) — CAT-only, no panadapter")
    self.spectrum_available = False       # gate still serves freq/mode/PTT
```

**Do not die.** The gate runs CAT-only and says so — same principle as the HPSDR adapter's
`has_sensors=False`: report "I can't see" rather than dying or lying. `diagnostics()` and the control
panel surface it.

### 4. Later, if wanted: a broker for antenna sources only
One owner streams IQ; several gates subscribe. **Only legal for `connected_to: antenna`** — an IF
source must stay exclusive. The unsolved bit is centre-frequency arbitration, since CAT-steer means
each gate wants the dongle parked on *its* rig. A first cut: one gate is the **steerer**, the rest are
passive consumers of the same IQ, and the UI says who is steering.

## Phasing
1. **Sources section + named acquire + honest CAT-only degrade.** Kills the reboot roulette and the
   opaque `-6`. No broker. (Real fix for today's pain.)
2. **Setup UI**: define sources, dropdown in the radio profile, `connected_to` picker.
3. **`serial=` per dongle** (needs `rtl_eeprom` if the serials are all `00000001`) → a 2nd dongle
   then Just Works, and the whole conflict evaporates in hardware.
4. **Broker for antenna sources** — only if a genuine two-gates-one-antenna-dongle need appears.

## Open questions for Nigel
- ~~Does the RTL have a unique serial?~~ **Answered: no — `00000001`, the factory default.** Step 3
  needs `rtl_eeprom -s` per dongle first, done while no gate holds it.
- **Is CAT-steer even wanted on an antenna-fed dongle?** It follows the rig's freq — but the dongle is
  a *different antenna*, so its spectrum is not the rig's receiver. Maybe a free-running "just show me
  20m" mode is more useful, and steering should be per-source rather than assumed.
- **Which is the intended default owner today?** `yaesu-gate` is enabled (returns on reboot),
  `kenwood-gate` is running but disabled (does not). After a reboot the Yaesu takes the dongle and the
  Kenwood is gone — currently decided by start order, not by anyone.
