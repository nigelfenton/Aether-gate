# Aether-gate — Constitution

The rules this project has been operating under, written down, **aligned with
AetherSDR's canon** because the gate feeds AE's input.

This is mostly a **descriptive** document — little here is new. It exists so a
contributor (or an AI agent) can check work against the standard *before*
review. Where a rule came from a specific mistake, the mistake is named; a
principle without its failure story gets argued away.

## Why this cites AetherSDR

Aether-gate is not a standalone toy. It presents itself to AetherSDR as a
FlexRadio, and **everything AE displays about a bridged radio arrives through
this code.** If the gate reports a frequency, a level or a TX state, AE has no
independent way to check it — the gate *is* AE's radio.

That makes AE's constitution partly binding here, and the relevant principles are
cited by number below. Should the gate ever be adopted as an AE backend, these
stop being borrowed good practice and become the actual review standard.

Companion: [`AGENTS.md`](AGENTS.md) for build, test and verification mechanics.

---

## I. The gate does not transmit — and if it ever does, intent must be unambiguous

**Aether-gate does frequency/mode control and receive. It does not key a radio.**

Stated in the README, and reflected in the adapter contract:
`RadioAdapter.wants_tx()` returns `None` by default — "leave TX alone."

What makes this subtle: **AE's UI lights up as though transmitting.** The gate
acknowledges the transmit command and reports `transmitting` back to AE, but
never asserts PTT. The absence of RF is invisible from the app.

Today that safety rests on a *convention* — no adapter overrides `wants_tx()` —
not on an enforced invariant. Treat that as a known weakness, not a design.

**AE Constitution VI (*Never Transmits Without Operator Intent*) is the standard
any PTT work must meet.** Its wording matters: never key on a timer, as a side
effect of a status update or model change, to recover or resync state, or as an
automatic retry; **any path that can transmit fails closed.** For a gate that
translates between two protocols, "fails closed" means: if the operator's intent
cannot be established unambiguously *through the translation*, do not key.

So a PR that makes any adapter assert PTT:

- is not a normal feature PR — it changes what this software can do to a licensed
  transmitter;
- must carry its arm / tx-band safety story **in the same PR**, not as follow-up;
- must say what happens when the translation is ambiguous, and prove it fails
  closed.

Never key a radio to test something. If a change needs a keyed radio to verify,
say so as a review limit and leave it unverified.

## II. The radio is authoritative — the gate must not become a second source of truth

**AE Constitution II** says the radio holds live state, the client mirrors it,
and reconciliation flows one way: radio status updates the client; the client
never writes its remembered value back over the radio's.

The gate sits **inside that path**, which makes it the one component that can
break the rule invisibly. Rules that follow:

- **Report what the radio said, not what the gate asked for.** A value the gate
  commanded but has not read back is not a status. Where a device offers no
  read-back, say so rather than echoing the request as truth.
- **A refused or failed command must not read as a successful one.** If the
  radio declines, that is the truth and it must reach AE.
- **Never let the gate's own model and the radio's state form a feedback loop.**
  Command path gate → radio; truth path radio → gate → AE.
- **Where the gate synthesises a value the radio cannot provide** (an
  interpolated bin, a modelled meter), it is a derived value, and the fact that
  it is derived belongs in the code and the docs.

## III. Impersonation is a translation layer, not a lie

The gate presents non-Flex hardware as a FlexRadio because AE speaks exactly one
protocol. That is the design. But impersonation has a boundary, and this project
has consistently chosen **truth over convenience** at it:

- **Declare the real bands** — `AdapterCaps.bands` exists so an IC-9700 shows
  2m/440/23cm, not a full HF menu it cannot tune.
- **Declare real capability** — `tx_capable`, `max_slices`, `min_span_hz`,
  `native_centered_scope` describe the *source*, not the impersonated model.
- **When the Flex protocol has no verb for something, do not invent one.**
  Settings with no Flex equivalent (antenna port, bias-T, notches, HDR mode, AGC
  set-point) belong on the gate's own control surface, never smuggled through a
  Flex verb that means something else.

The test: if AE shows the operator a number or control, it must correspond to
something the real radio actually has.

## IV. A measurement is not a measurement until you know what would falsify it

This is a DSP and level-reporting pipeline. Its characteristic bug is **a
plausible number that is wrong**, and those do not announce themselves. Compare
**AE Constitution VIII (*Evidence Over Assertion*)** and **XI (*Fixes Are
Demonstrated*)**.

- **A steady tone cannot test a modulator.** Use a varying envelope; a constant
  input passes almost any broken transform.
- **Levels cannot show shape.** The same dBFS covers a burst and a silence. If
  the question is shape, record samples, not levels.
- **Green proves one configuration.** Ask what would falsify the result, and
  whether the test would still pass with the code removed.
- **Prove the test by breaking it.** A test that has never failed has not been
  shown to test anything. Never a green re-run as evidence.
- **A test that CI does not run is not regression coverage.** See `AGENTS.md`.
- **Anchor on a reference you have justified.** Two traps found in PR #40, worth
  recording permanently: do not anchor on SDRconnect's PWR readout (not the same
  measurement), and a trimmed-mean noise estimate sits well below the true mean
  for exponentially-distributed bin powers, faking a wrong trim.
- **Follow the value to its output.** A fix is not a statement about a fix — ask
  what the *next* function does with the value before calling it fixed.

## V. A global constant is a claim about every device

Calibration constants (`DBFS_TO_DBM` and kin) are **global**, but measured on
**one device, one antenna, one gain setting, one bench.**

Before changing one, state: what it was measured against and why that reference
is trustworthy; how many independent paths agreed and how far apart; and which
devices you could **not** check.

A constant measured on one front end that moves every other device's numbers
should be per-device or config-keyed rather than a global default, unless there
is a reason it generalises.

Where the driver itself is untrustworthy — SoapySDRPlay3 reports LNA-state gain
with the wrong sign and magnitude (upstream issue #10, PRs #25/#26/#27, open
since 2021) — **warn rather than correct.** A correction built on a broken
read-back is worse than none: it moved the floor 35.4 dB across an LNA sweep
against a true 25.6 dB.

## VI. Contracts get tightened at the seam, not worked around

`RadioAdapter` is the seam every radio family passes through; loose wording there
becomes a bug in every adapter at once. Compare **AE Constitution VII
(*Untrusted Input Is Validated At The Boundary*)** — and note that moving code
*to* a boundary raises its standard rather than inheriting the interior's.

The cautionary case is `get_iq(n, …)`, documented as returning "a complex sample
block (len ~n)". The `~` was load-bearing: one adapter ignored `n` and always
returned 4096 samples, so any advertised bin width above 4096 bins was fiction —
true resolution was always `samp_rate/4096`, reached by interpolation. It took a
noise floor that refused to move under an 8× bin-width change to catch it.

When you find a vague contract, **tighten the contract** and add the test that
would have caught the violation. Do not special-case the one adapter you noticed.

## VII. Platform differences are real; do not encode one platform's truth as universal

The gate runs on Linux, macOS, Windows and a Raspberry Pi appliance, and the
platforms genuinely differ — macOS ships `net.inet.udp.maxdgram` at 9216 where
Linux and Windows allow 65507, so datagram-derived constants differ by ~7×.

Consequences: a test asserting a relationship between platform-derived constants
must **skip or parameterise**, not assume; and a feature that only engages on one
platform still needs its logic exercised on the others.

## VIII. Attribution is a licence obligation, not a courtesy

Aether-gate is **GPL-3.0-or-later**, and it is GPL because its Icom transport
derives from Justin W5JWP's [SDR9700](https://github.com/w5jwp/SDR9700). Derived
files carry SDR9700's copyright and licence headers, and they stay.

- Do not strip or reformat a licence header.
- Do not copy code from a project whose licence you have not checked. A repo with
  **no** licence is all-rights-reserved — absence is not permission.
- Contributed adapters keep their contributor's attribution (the IC-7300 USB
  adapter is s53zo's).

Compare **AE Constitution IV (*Every Contribution Is Clean-Room*)**: if the gate
becomes an AE backend, provenance of every line matters upstream too.

## IX. Hardware claims name the hardware, and report their limits

"Tested" is not a claim. "Verified on an RSPdx-R2 against SDRconnect, antenna A
vs an empty antenna B measured 25 dB apart" is — it says what was exercised and
lets the next person judge whether it covers their case.

- State which radios a change was exercised on, and which it was **not**.
- Say what you did not test, and why. One bench cannot cover this hardware range.
- **Retract in place** rather than rewriting history when a measurement turns out
  wrong — the retracted attempt is often the most useful part of the record.
- Flag anything a reviewer would otherwise have to find. A disclosed scope
  overrun is a judgement call; an undisclosed one is a problem.

---

*Descriptive as of 2026-09-01, verified against `main` (`b8ad65b`). AE principles
cited from `aethersdr/AetherSDR` `CONSTITUTION.md`. If a rule here stops matching
what the project actually does, the rule is wrong — fix it or delete it.*
