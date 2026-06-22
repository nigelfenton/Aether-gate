# Aether-gate — Design Notes
**Status:** concept / pre-implementation · 2026-06-21 · Nigel Fenton (G0JKN)
**Private repo — do not publish design details until working PoC exists**

---

## The idea

AetherSDR is an exceptional SDR application — best-in-class DSP, waterfall, slice
management, TCI output. But it speaks one radio protocol: FlexRadio.

Aether-gate is a universal radio abstraction layer that sits between any radio and
AetherSDR. Every radio — hardware SDR, legacy transceiver, remote WebSDR — presents
itself to AetherSDR as a Flex 6000. AetherSDR never needs to know what is behind it.

```
Kenwood / Icom / Yaesu  (CAT / CI-V)  ─┐
RTL-SDR / Airspy / SDRplay (SoapySDR)  ─┼──→  Aether-gate  ──→  AetherSDR
Any SoapySDR hardware                  ─┤         │
Future radios                          ─┘         └── speaks Flex 6000
                                                       VITA-49 + FlexLib
```
(KiwiSDR is intentionally absent — AE supports it natively; see Scope below.)

## Why this matters

- AetherSDR's developers stay focused on DSP and UI — not 47 radio drivers
- Every radio gets AetherSDR's full feature set: waterfall, filters, TCI, slices
- No more CAT wrangling, Omni-Rig, DAX plumbing for non-Flex users
- "AetherSDR for any radio" — genuine onboarding path for new users

## Rationale — why a gate when AE can already absorb sources natively

AE has shown (with native KiwiSDR, merged upstream June 2026) that it *can* take an
external source in-process. So the gate has to justify itself against the obvious
question: "why not just keep adding sources inside AE?"

**The architecture argument (the durable one).**
Every source AE absorbs natively adds a *parallel* ingest + control path inside AE:
its own connection handling, its own quirks, its own framing, its own share of the
processing budget. N sources → N code paths inside the hot core. That surface only
grows, and each addition is a place for source-specific bugs to compete with AE's
DSP for attention and CPU.

Aether-gate inverts this: every source is normalised to **one** stream type — a Flex
VITA-49 stream — *before* AE sees it. AE then maintains exactly **one** ingest
boundary: the FlexRadio path it is already most optimised for and tests hardest. The
zoo of source-specific handling lives outside AE's core, in a sidecar that can fail,
restart, and be debugged without touching the application. This is the "narrow waist"
principle — many things converge to one well-defined interface, and the complexity
lives on the outside of that waist, not threaded through the renderer.

That principle holds **whether or not any single source is faster through the gate.**
It is an architecture claim, not a benchmark claim, and it is the load-bearing
justification for the project.

**The efficiency argument (real, but be precise about it).**
"The gate makes AE more efficient" is true in a specific, important sense and false in
a careless one — the design must not overclaim:

- *True for AE as a node.* AE's CPU only ever runs the Flex ingest path, no matter how
  many radios are behind the gate. If the bottleneck is AE's own headroom — one
  machine driving several sources, or a constrained portable head — moving the
  per-source work to a sidecar is a clean win for that node.
- *True for AE's maintainers.* One ingest path to optimise and regression-test, not N.
- *NOT automatically true for total system compute.* A Flex VITA-49 stream is
  pre-FFT panadapter data plus (near-)raw audio — AE **still** runs its DSP on it.
  So for an IQ source, the gate must do demod/filtering itself and *then* AE does its
  own processing on the result. That can raise **total** compute (work relocated, even
  duplicated), while still lowering **AE's** compute. The two are different ledgers.

**Where the win is largest.** When the gate runs on *separate* hardware (a Pi, the
shack hub) and AE is the constrained node — exactly the multi-radio / portable-Flex-
head case. There, offloading per-source ingest off AE's box is unambiguously good,
and the architecture cleanliness comes for free on top.

**Bottom line.** Sell the gate as an **architecture** play — "AE should have exactly
one ingest boundary; the zoo of sources lives outside it" — with node-level offload as
a situational bonus. Do **not** sell it as a universal compute reduction; that claim
won't survive scrutiny for IQ sources, and overclaiming it would undercut the much
stronger structural argument.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Aether-gate                       │
│                                                      │
│  ┌──────────────┐    ┌──────────────────────────┐   │
│  │ Radio adapter│    │       Core engine         │   │
│  │  (plugin)    │───▶│  - IQ → FFT → VITA-49    │   │
│  │              │◀───│  - VFO retune dispatch    │   │
│  └──────────────┘    │  - S-meter / meter plane  │   │
│                      │  - Multi-slice management  │   │
│                      │  - Discovery responder     │   │
│                      │  - FlexLib control server  │   │
│                      └──────────────┬─────────────┘  │
└─────────────────────────────────────┼─────────────────┘
                                      │ Flex 6000 protocol
                                      ▼
                                 AetherSDR
```

The core engine is derived from flex-sim (nigelfenton/flex-sim, GPL-3.0) — already
live-validated against AetherSDR v26.6.3. The adapter is a plugin interface.
Each new radio is a new adapter. The core never changes.

## What each adapter provides

1. **IQ / spectrum data** — raw samples or pre-computed FFT at hardware rate;
   Aether-gate resamples and packages as VITA-49 pan/waterfall frames
2. **Frequency/mode control** — VFO retune and mode change translated to the
   radio's native command (CAT, CI-V, WebSocket, SoapySDR API)
3. **Status readback** — S-meter, TX state, band, mode fed into VITA-49 meter plane

## Scope — the long tail AE will not adopt natively

AetherSDR now has **native KiwiSDR support** (merged upstream June 2026, rfoust).
KiwiSDR is therefore **out of scope** — AE reaches it directly, inside its own DSP,
with no re-FFT round-trip. Aether-gate would only be a slower, lossier path to a
destination AE already owns.

Aether-gate's value is the radios AE's maintainers chose **not** to build in: the
unglamorous long tail of legacy CAT transceivers and generic SDR dongles. AE went
"KiwiSDR in"; it is not going to absorb Icom CI-V, Kenwood/Yaesu CAT, and every
SoapySDR device. That uncontested space is where an external bridge earns its place.

## Planned adapters

| Radio / Source    | Protocol       | IQ source        | Priority |
|-------------------|----------------|------------------|----------|
| RTL-SDR           | SoapySDR       | wideband IQ      | First    |
| SDRplay / Airspy  | SoapySDR       | wideband IQ      | First    |
| Icom CI-V radios  | CI-V / USB     | audio/VAC        | Second   |
| Kenwood CAT       | serial CAT     | audio/VAC        | Second   |
| Yaesu CAT         | serial CAT     | audio/VAC        | Second   |
| Any SoapySDR HW   | SoapySDR       | wideband IQ      | Ongoing  |

SoapySDR covers the majority of SDR hardware in a single adapter.

## Development sequence

1. **SoapySDR adapter** — RTL-SDR / Airspy / SDRplay in one shot; proves the
   IQ → VITA-49 path with cheap, ubiquitous hardware
2. **Icom CI-V adapter** — TX-capable real transceiver
3. **Plugin interface formalised** — community adds adapters
4. **Announce** — working code first, then public discussion

## Relationship to other projects

- **flex-sim** (public) — stays as the AetherSDR waterfall test bench / QA fixture;
  Aether-gate is a separate evolution of the same core
- **tci-probe** — TCI observer on AetherSDR's output side; Aether-gate is on the
  input side; together they bracket AetherSDR completely
- **AetherSDR** — companion project, not a PR; the AE team benefits from it
  existing without needing to maintain it

## Name

**Aether-gate** — implies both a gateway and a gatekeeper. Signals the AetherSDR
connection without describing the implementation. Right name.

---

*Notes kept here are design thinking, not implementation commitments.*
*Show working code before any public discussion.*
