# Designating the RTL dongle as a shared resource (not a radio's property)

**Status:** Draft for Nigel. Nothing built. Written 2026-07-16 from his observation:
*"we need to maybe look at designating the rtl to a common name rather than tying it to a radio."*

## The problem, precisely

The dongle is a **shared, single-instance resource**, but the code models it as a **private member of
each CAT adapter**. `KenwoodAdapter.__init__`:

```python
self._sdr = SoapyAdapter(driver=soapy_driver, device_args=soapy_args, ...)
```

and `YaesuAdapter` is a *thin subclass of KenwoodAdapter* — so both inherit that line and each
constructs its own handle to the same USB device. `KenwoodAdapter.open()` then calls
`self._sdr.open()` **unguarded**, so the second gate to start dies:

```
usb_claim_interface error -6
[adapter] open failed: Unable to open RTL-SDR device
```

Observed live 2026-07-16: `aether-gate.service` (standalone RTL) failed exactly this way because
`yaesu-gate` held the dongle — while idle, with no AE connected. Freeing it required stopping the
Yaesu gate outright.

**The tell that the model is wrong:** the dongle is named three times, in three services, as three
different radios' property (`--soapy-driver rtlsdr` in `aether-gate.service`, `kenwood-gate.service`,
`yaesu-gate.service`), yet there is one physical device. Nothing in the system knows that.

## Why this bites now, and will get worse

- **Reboot roulette.** `yaesu-gate` is `enabled`; `kenwood-gate` is `disabled` but was hand-started
  today. After a reboot the Yaesu grabs the dongle and the Kenwood dies — or vice versa, depending on
  start order. **Whoever wins is not a decision anyone made.**
- **Idle hoarding.** The Yaesu gate held the dongle with no AE attached. A radio nobody is using
  should not deny a radio someone is.
- **The failure is opaque.** `usb_claim_interface error -6` does not say "the FT-847 has it". Today
  that cost a diagnosis cycle.
- **It scales badly.** Every new IF-tap radio adds another claimant to one device.

## Options

### A. Named devices + explicit assignment (smallest honest fix)
Give each dongle a stable name and let a service ask for it by name, not by "the rtlsdr driver".
SoapySDR already supports this — `device_args` is parsed into the args dict, so `serial=00000001`
selects a specific dongle. Today's services pass `--soapy-driver rtlsdr` with **no** `--soapy-args`,
i.e. "any rtlsdr", which is precisely the ambiguity.

- Pros: tiny change; fixes multi-dongle immediately (a 2nd dongle makes the conflict vanish); no new
  process.
- Cons: with ONE dongle it does not resolve contention at all — it just makes the collision explicit
  instead of accidental. **Necessary, not sufficient.**

### B. First-come lock + honest degrade (recommended first step)
Wrap the claim: if the dongle is already held, **do not die** — start CAT-only and tell the operator.
`KenwoodAdapter.open()` currently calls `self._sdr.open()` unguarded; catch it, set a
`spectrum_available=False`, and let the gate serve CAT (freq/mode/PTT) with no panadapter.

- Pros: no reboot roulette — both gates run, one has spectrum; the failure becomes a visible state
  rather than a dead unit. Matches how the HPSDR adapter already reports `has_sensors=False` rather
  than faking a reading. Mirrors the SWR lesson: **report "I can't see" instead of dying or lying.**
- Cons: the loser silently has no waterfall until someone looks. Mitigate by surfacing it in
  `diagnostics()` + the control panel, and logging it once at claim time.

### C. A dongle broker (one owner, many consumers)
One process owns the RTL, publishes IQ, and gates subscribe. This is the "common name" idea taken to
its conclusion.

- Pros: genuinely shared — several radios could tap one dongle; hot-swap without restarts.
- Cons: real work (IPC/transport, centre-frequency arbitration). And the fundamental problem does not
  go away: **an IF-tap dongle must be tuned to ONE rig's IF at a time.** Two rigs on different bands
  cannot share one dongle's spectrum, broker or not. So C buys orchestration, not simultaneity.

### D. Just buy a second dongle
- Pros: solves it in hardware, today. RTL-SDR V4s are cheap.
- Cons: needs A (named devices) to tell them apart — otherwise "any rtlsdr" grabs whichever enumerates
  first, and the roulette returns in a new costume.

## Recommendation

**B + A, in that order.** B stops the reboot roulette and the opaque failure (the actual pain today,
and cheap). A makes multi-dongle work and is a prerequisite for D. C only if a genuine
many-consumers-one-dongle need appears — and note it cannot make two rigs on different bands share one
IF tap.

## ✅ ANSWERED (Nigel, 2026-07-16): the RTL is on an ANTENNA, not an IF tap

*"the rtl is NOT wired to the kenwood if output - its to an antenna."*

**This makes Nigel's instinct correct and my IF-tap caveat wrong.** The dongle is not cabled to any
rig — it is an **independent wideband receiver on its own antenna**. Consequences:

- **There is no physical reason to tie it to a radio.** The whole "the dongle belongs to the rig
  whose IF it taps" premise evaporates. It is a shared resource in the fullest sense, and the current
  model (a private `SoapyAdapter` inside each CAT adapter) is simply wrong.
- **The C-broker objection dissolves.** I argued a broker "cannot make two rigs on different bands
  share one IF tap" — true for an IF tap, irrelevant here. On an antenna the dongle can be tuned
  anywhere; the only contention is *which centre frequency*, which is arbitration, not physics.
- **The adapter's own docstring already allows this:** "*a SoapySDR dongle ... tapped off-air / IF /
  antenna and STEERED to follow the rig's tuned freq*." Only the module header says "IF-tap"; the
  design was always broader. The naming is what misleads (incl. `diagnostics()` reporting
  "TS-450S (hamlib CAT + IF-tap SDR)" — inaccurate on this setup).
- **CAT-steer still makes sense** — the dongle follows the rig's frequency so AE's panadapter shows
  where the rig is tuned. That is a *choice*, not a wiring constraint, and it is why the spectrum
  looks rig-specific when the hardware is not.

**Revised recommendation: B then A, and C is now genuinely on the table** (it was not, under the
IF-tap assumption). A shared antenna-fed dongle *could* serve several gates — the open question is
centre-frequency arbitration, since CAT-steer means each gate wants it parked on its own rig.
An honest first step for that: let ONE gate own the steer and others consume the same IQ.

⚠ **Corollary worth stating plainly:** the RTL's spectrum is *not the TS-450S's receiver*. It is a
separate antenna's view of the band, steered to the rig's frequency. Signals on AE's panadapter come
from the dongle's antenna, and the S-meter comes from hamlib (the rig). **They can legitimately
disagree** — different antennas, different front ends. That is worth knowing before anyone debugs a
"mismatch" that is not one.

## Immediate state (2026-07-16)
- `yaesu-gate`: **stopped** (to free the dongle), still `enabled` -> returns on reboot.
- `kenwood-gate`: **running** on `:7992` / ctl `8739`, `--ae 10.0.0.104`, holding the dongle.
  Still `disabled` -> will NOT return on reboot.
- Net: after a reboot the Yaesu takes the dongle back and the Kenwood is gone. Decide the intended
  steady state rather than leaving it to start order.
