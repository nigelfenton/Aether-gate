# Changelog

All notable changes to Aether-gate. Newest first.

## [Unreleased]

### Fixed
- **The 0.5 s waterfall tick — librtlsdr's USB lump.** The driver delivers fixed
  262,144-byte transfers (131,072 samples) regardless of sample rate: 64 ms of
  signal per lump at 2.04 MS/s, but **524 ms at 250 kS/s** — the panadapter and
  audio can only be as fresh as the lumps, so the display ticked at ~2 Hz while
  every layer above measured healthy (engine loop 19.97 Hz, reader 56 blocks/s
  *average* — but bursty: p50 gap 0.01 ms, max 524.06 ms). Fix: size `bufflen` to
  ~30 ms of signal at the configured rate and pass it as **stream args**
  (SoapyRTLSDR ignores it in device args). Measured: engine freshness went from
  2.0 to 20.0 new blocks/s — every frame now carries new IQ. Also improves the
  2.04 MS/s default (64→28 ms) and steadies the audio demod feed.
- **`kenwood` `set_span()` advertised a span it does not deliver** (bare `pass` →
  the engine kept AE's requested span while the dongle delivered its own). At the
  2.04 MHz default the ratio happened to be ~1 so it hid; at 250 kHz AE painted a
  2.04 MHz axis with 250 kHz of data — the "fewer signals" report. Now returns the
  dongle's real sample rate, exactly as the HPSDR adapter always did.
- Both "known limitations" flagged in 0.3.0 for 250 kHz operation are hereby
  resolved; `--samp-rate 250000` is now the better HF setting for the kenwood
  gate (156 Hz/px vs 1275).

### Added
- `AETHER_GATE_PROFILE=1` — stream-loop + soapy read-loop instrumentation
  (loop Hz, per-phase ms, IQ freshness, read-gap stats). Off by default, ~free
  when off. It is how both bugs above were found.

## [0.3.0] — 2026-07-16

Adds the **HPSDR / Protocol-1 adapter** (Radioberry, Hermes-Lite 2), radio-reported
**telemetry + SWR**, a **bare-carrier TX guard** for the IC-9700, and a real fix to the
core FFT that every IQ adapter shares.

### Added
- **HPSDR Protocol-1 (Metis) adapter** — `--adapter hpsdr`. Raw IQ over UDP:1024 from a
  Radioberry or Hermes-Lite 2, presented to AetherSDR as a Flex. Discovery, EP2 C&C
  round-robin, EP6 IQ ingest, live RF-gain, SSB demod audio.
  **Proven on air: FT8 decodes end-to-end** through AE + WSJT-X on 14.074 (3 then 8
  stations, 2026-07-16), and separately via headless `jt9` off the gate's own DSP.
- **EP6 response telemetry** (`parse_ep6_telemetry`) — PA temperature, forward/reverse
  power and PA current, decoded from C&C bytes we already receive. Plus
  `swr_from_fwd_rev()` and a `telemetry()` / `diagnostics()` surface.
  **`swr` is `None` when it cannot be measured — never a fake 1.0.** A board with no
  sensors reports `has_sensors=False`, and the engine then **omits AE's SWR meter
  entirely** rather than showing a "perfect match" that is really "no idea".
- **Bare-carrier guard (IC-9700)** — `key_tx()` now refuses in a digital mode when AE has
  registered no `dax_tx` stream, because no audio can ever arrive and keying would
  radiate an unmodulated carrier for the full watchdog. Measured: **127 of 261 keys on
  2026-07-15 ran exactly like that.** Voice modes unaffected (the rig's mic is the
  source); `key_tx(force=True)` allows a deliberate tuning carrier; a missing probe
  fails safe.
- **`aether_gate/tests/test_fft.py`** — first tests for the core transform (19 tests).
- **Docs**: `HPSDR_TX_PLAN.md` (phased, dummy-load-first TX design),
  `SHARED_SDR_DESIGN.md` and `SDR_SOURCES_SKETCH.md` (naming dongles as shared sources).

### Fixed
- **`core/fft.py`: `iq_to_dbm` subsampled before the FFT.** `x = x[idx]` (every Nth
  sample) is aliasing, not decimation — it discarded ~61% of each 4096-sample block and
  folded its energy back onto the survivors. Now windows and transforms the **whole**
  block, then reduces to pan columns by **peak** (not mean, so a narrow carrier inside
  one column survives). Measured gain: **+1.4 to +1.9 dB** of dynamic range.
  Affects every IQ adapter (soapy/RTL, HPSDR, kenwood, yaesu).
- **HPSDR IQ sideband inverted** vs AE's convention (`complex(i, -q)`). Confirmed on air:
  WWV 15 MHz lands on the correct side of centre at NCO offsets −3000/−1000/+2000, with a
  constant −359.5 Hz error at 33–38 dB SNR.
- **HPSDR EP2/EP6 decoupling** — C&C egress moved to its own 20 Hz thread with a 1 MB
  `SO_RCVBUF`, so a send can never gate the free-running IQ stream. ⚠ **Defensive, not a
  repair**: both loop shapes measure the same (~49.1 kHz of a nominal 48 kHz). An earlier
  claim that this fixed a ~10× starvation was **retracted** — that number came from a
  throwaway probe script, not this adapter.

### Known limitations
- **HPSDR is RX-only by construction.** The adapter defines no `key_tx`, so the engine's
  `hasattr` gate means AE's MOX cannot reach the radio. TX is planned and targets the
  **HL2** (which has native fwd/rev/temp/current); see `docs/HPSDR_TX_PLAN.md`.
- **`set_span()` is a no-op** on the HPSDR and kenwood adapters — AE's zoom does not
  change the dongle/radio sample rate, so the span is fixed. Root cause of the
  "zoom does nothing" report is **not** understood beyond this; unresolved.
- **Radioberry PA hats without the preAmp board report no sensors** (no MAX11613): temp
  falls back to the host CPU's, and fwd/rev/current are permanently 0. `has_sensors`
  reports this honestly. On such a board there is **no thermal or SWR protection**.
- **`--samp-rate 250000` on the kenwood adapter measured WORSE** (fewer signals, ~0.5 s
  updates) than the 2.04 MHz default, and that is **unexplained**. Reverted; do not
  assume a narrower span is better until it is understood.
- AE does not always send `stream create type=dax_tx` before keying. Root cause unknown
  (per-AE-connection; slice `tx=1`, mode, and reconnect were each tested and refuted).
  The bare-carrier guard makes the consequence non-radiating.

## [0.2.0] and earlier
See git history.
