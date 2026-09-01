# AGENTS.md — working on Aether-gate

Operational notes for anyone (human or AI agent) making changes here. Principles
live in [`CONSTITUTION.md`](CONSTITUTION.md); this file is how to build, test and
verify without breaking something invisible.

**Read [`CONSTITUTION.md`](CONSTITUTION.md) §I before touching anything that
could key a radio.** The gate does not transmit, and that is deliberate.

**Everything AE shows about a bridged radio comes through this code.** AE cannot
independently check what the gate reports — see CONSTITUTION §II. Where this file
cites AetherSDR canon, it is because the gate feeds AE's input and may become an
AE backend.

---

## Which repository is canonical

- **`nigelfenton/Aether-gate` is upstream.** Branch from it, PR to it.
- `aethersdr/Aether-gate` is a **mirror**, and it is **stale** — its sync fails
  because `MIRROR_SYNC_TOKEN` lacks the `workflow` scope, so it has not moved
  since 2026-07-15 (tracked in aethersdr/AetherSDR#5302). Do not read it for
  current state; do not open PRs against it.

## Layout

```
aether_gate/
  __main__.py        CLI entry, argument surface (--ctl-port, --serial, …)
  core/engine.py     Flex emulation, control HTTP server, frame loop
  core/fft.py        transforms, dBm conversion
  adapters/base.py   RadioAdapter ABC + AdapterCaps + Meters  ← the seam
  adapters/…         icom/ hamlib/ kenwood/ yaesu/ hpsdr/ soapy.py sim.py
  tests/             stdlib and numpy suites (see below)
```

`adapters/base.py` is the contract every radio family passes through. Changes
there are reviewed harder than changes inside one adapter — CONSTITUTION §VI.

## Running it

```bash
python -m aether_gate --help
```

The control panel defaults to `http://<ip>:8731/` (`--ctl-port`, `0` disables
it). A gate started with `--ctl-port 0` has **no control surface at all** — it is
indistinguishable from "no gate here" to anything probing that port.

No hardware? `adapters/sim.py` gives a synthetic source; the README's *Try it
with no hardware* section is the quickest path to a running gate.

## Tests — and the CI boundary that is easy to trip

Run everything locally:

```bash
pytest aether_gate/tests/
```

Run one module the way CI does:

```bash
python -m aether_gate.tests.test_smoke
```

**CI does not run the whole suite.** `.github/workflows/tests.yml` runs an
explicit hand-maintained list via `python -m`, on a job with **no pip install
step** ("nothing to pip-install"). It byte-compiles the package, then runs that
list on Linux (3.11/3.12/3.13), macOS 3.13 and Windows 3.13.

Currently outside that list: `test_env_config`, `test_fft`, `test_fm_demod`,
`test_soapy_audio_ratio`, `test_span_contract`, `test_wf_packet`.

Two things follow, and the second surprises people:

1. **A module needing `numpy` or `pytest` cannot join that job as it stands.**
   Adding one silently would break the stdlib-only contract on five platforms. To
   cover such a module, add a **second job** with `pip install numpy pytest`
   rather than changing this one — and say so in the PR.
2. **Being stdlib-only is not sufficient to be in CI.** `test_env_config` and
   `test_wf_packet` are pure stdlib and still outside the list. The list is
   hand-maintained, so a new stdlib test is *not* picked up automatically. Add it
   explicitly, or it never runs.

**A test that CI does not run is not regression coverage.** State plainly in the
PR which new tests actually run in CI and which only run locally.

### Tests must not assume one platform's constants

Platform-derived constants genuinely differ: `udp_maxdgram()` is 9216 on macOS
and 65507 on Linux/Windows, so `bins_per_packet()` differs by roughly 7×, and
inequalities between such constants can invert across platforms. A test asserting
a relationship between them must **skip or parameterise**, never assume. The CI
matrix spans all three, so an assumption here fails on two of them.

Likewise `signal.SIGTERM`: on Windows `Popen.send_signal(SIGTERM)` maps to
`TerminateProcess` and does **not** deliver a catchable signal to the child, so a
handler-based shutdown test cannot pass there. `hasattr(signal, "SIGTERM")` is
true on Windows and is therefore *not* a sufficient skip guard.

### Where an assertion belongs

Adopted from AetherSDR's `AGENTS.md` "Test-layer boundary", because the gate may
become an AE backend and would inherit it:

| The assertion proves | It lives in |
|---|---|
| Wire encoding, parsing, capability tables, scheduling, DSP, level policy | a socket-free test |
| A refusal, a non-event, a dropped/malformed input, a TX guard | a socket-free test that injects the transport — feed the handler or state machine directly |
| The gate converges with real firmware | live hardware, reported with the radio named |

**Do not add a synthetic peer standing in for third-party radio firmware.** A
fake radio proves the gate agrees with our *model* of the radio, not with the
radio; the model freezes while firmware moves, so such a test fails on correct
changes or stays green on real divergence.

Tests where **the gate's own server is the subject** (the control HTTP server,
the Flex emulation surface) are legitimate — the code under test is real and the
socket is how you reach it. Any socket-owning test is disclosed in the PR body,
and must fail fast or skip when it cannot bind rather than consuming its timeout.

## Verifying a change

- **Prefer a test that would have failed before.** CONSTITUTION §IV: prove the
  test by breaking it, not by re-running it green.
- **Level and DSP changes need a stated reference.** What did you measure
  against, how many independent paths agreed, on what hardware? CONSTITUTION §V
  before touching a calibration constant.
- **Report what the radio said, not what you asked for** — CONSTITUTION §II.
- **Name the radio.** "Verified on an IC-9700 over LAN" is worth something;
  "tested" is not. Name what you could not test, too.
- **Do not key a radio to verify anything.** If a change can only be confirmed
  with RF, leave it unverified and say so.

## Changes that need more than a normal review

- Anything that asserts PTT, or moves `wants_tx()` off its `None` default —
  CONSTITUTION §I and AE Constitution VI.
- Anything altering `adapters/base.py`'s contract — it lands in every adapter.
- Global calibration constants (`DBFS_TO_DBM` and friends): one bench, every
  device.
- Anything touching `.github/workflows/` — the stdlib-only contract is
  load-bearing and its breakage is silent.
- Files carrying SDR9700 licence headers (`adapters/icom/**`): GPL obligations,
  CONSTITUTION §VIII.

## House style

- Comments explain **why**, especially where a value was measured or a trap
  found. Several of this codebase's best comments exist because someone got a
  plausible wrong answer first; keep writing those.
- Retract in place rather than rewriting history — leave the wrong attempt and
  its correction both visible when the measurement is the useful part.
- Match the surrounding file's idiom rather than importing a new one.

## When reviewing a PR here

Read `CONSTITUTION.md` first, then the diff. The failure mode this project
actually suffers is not ugly code — it is **a plausible number that is wrong**,
or an invariant (no TX; report what the radio said; declare real capability)
quietly weakened at a seam. Check those before style.

---

*Descriptive as of 2026-09-01, verified against `main` (`b8ad65b`). If something
here stops being true, fix this file in the same PR.*
