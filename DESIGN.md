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
