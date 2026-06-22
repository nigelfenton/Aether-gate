# KiwiSDR bridge — design
_Relocated from flex-sim DESIGN.md section 12 (2026-06-21). NOTE: source had a DUPLICATED section 12 — both copies preserved below; dedup when building out._

## 12. KiwiSDR bridge (PRIVATE — do not move to public repo)

**Concept (2026-06-21):** feed real HF signals from a public KiwiSDR into flex-sim so
AetherSDR sees live RF through its normal Flex radio path — full DSP stack, filters,
S-meter, slice tuning — with zero hardware.

### Why inside flex-sim (not a separate bridge)

flex-sim already IS the Flex as far as AetherSDR is concerned. If KiwiSDR IQ is
pre-processed inside flex-sim before VITA-49 framing, AetherSDR gets real HF signals
through its normal radio path — every AE feature works (DSP, filters, modes, CAT).
Alternatives (VAC audio pipe, standalone client) lose the AE DSP stack entirely.

### Signal path

```
KiwiSDR (WebSocket :8073)
  → kiwiclient (Python, existing lib)
  → 12 kHz IQ @ 16-bit PCM stereo
  → numpy FFT (windowed, overlap-add)
  → dBm bin array (same format as existing patterns)
  → VITA-49 pan/waterfall framing  ← flex-sim already does this
  → AetherSDR
```

Audio path (bonus — AE gets demodulated audio too):
```
Same IQ → software demod (USB/LSB/CW per active slice mode)
         → 24 kHz mono WAV frames
         → existing flex-sim VITA-49 audio stream path
```

### Closed-loop tuning

When AE sends `display pan set center=14.225MHz`, flex-sim forwards the retune to the
KiwiSDR WebSocket (`SET freq=14225`). VFO tuning in AE actually moves the KiwiSDR
centre. This closes the loop completely — AE behaves as if it owns real hardware.

### The 12 kHz window

KiwiSDR delivers 12 kHz of IQ. A Flex panadapter shows up to 192 kHz. Solution:
- Real signal fills the 12 kHz KiwiSDR window (centred on the VFO)
- Outside that window: synthesised noise floor at the configured dBm level
- Visually honest and functional — the user can see exactly what the KiwiSDR covers

### What flex-sim needs (new code only)

1. `KiwiSource` class — WebSocket client using `kiwiclient` lib; tunes on VFO change
2. FFT engine — numpy windowed FFT on IQ blocks → dBm bin array at pan resolution
3. Pattern integration — `kiwi` as a new pattern type in the existing pattern engine
4. Retune hook — on `display pan set center=` command, call `KiwiSource.retune()`
5. Audio demod — optional but valuable; LSB/USB/CW filter → 24 kHz mono → audio path
6. Public KiwiSDR selector — simple list of reliable public servers in the web panel

### Public KiwiSDR sources for testing

Directory: kiwisdr.com/public — 300+ servers worldwide, no auth required.
Good test targets: W4 (20m FT8 pileup), European 40m evening, 14.074 MHz FT8 constant.

### What this enables (the demo story)

Fire up AetherSDR, pick a public KiwiSDR from the list, see real HF signals — no

## 12. KiwiSDR bridge (PRIVATE — do not move to public repo)

**Concept (2026-06-21):** feed real HF signals from a public KiwiSDR into flex-sim so
AetherSDR sees live RF through its normal Flex radio path — full DSP stack, filters,
S-meter, slice tuning — with zero hardware.

### Why inside flex-sim (not a separate bridge)

flex-sim already IS the Flex as far as AetherSDR is concerned. If KiwiSDR IQ is
pre-processed inside flex-sim before VITA-49 framing, AetherSDR gets real HF signals
through its normal radio path — every AE feature works (DSP, filters, modes, CAT).
Alternatives (VAC audio pipe, standalone client) lose the AE DSP stack entirely.

### Signal path

```
KiwiSDR (WebSocket :8073)
  → kiwiclient (Python, existing lib)
  → 12 kHz IQ @ 16-bit PCM stereo
  → numpy FFT (windowed, overlap-add)
  → dBm bin array (same format as existing patterns)
  → VITA-49 pan/waterfall framing  ← flex-sim already does this
  → AetherSDR
```

Audio path (bonus — AE gets demodulated audio too):
```
Same IQ → software demod (USB/LSB/CW per active slice mode)
         → 24 kHz mono WAV frames
         → existing flex-sim VITA-49 audio stream path
```

### Closed-loop tuning

When AE sends `display pan set center=14.225MHz`, flex-sim forwards the retune to the
KiwiSDR WebSocket (`SET freq=14225`). VFO tuning in AE actually moves the KiwiSDR
centre. This closes the loop completely — AE behaves as if it owns real hardware.

### The 12 kHz window

KiwiSDR delivers 12 kHz of IQ. A Flex panadapter shows up to 192 kHz. Solution:
- Real signal fills the 12 kHz KiwiSDR window (centred on the VFO)
- Outside that window: synthesised noise floor at the configured dBm level
- Visually honest and functional — the user can see exactly what the KiwiSDR covers

### What flex-sim needs (new code only)

1. `KiwiSource` class — WebSocket client using `kiwiclient` lib; tunes on VFO change
2. FFT engine — numpy windowed FFT on IQ blocks → dBm bin array at pan resolution
3. Pattern integration — `kiwi` as a new pattern type in the existing pattern engine
4. Retune hook — on `display pan set center=` command, call `KiwiSource.retune()`
5. Audio demod — optional but valuable; LSB/USB/CW filter → 24 kHz mono → audio path
6. Public KiwiSDR selector — simple list of reliable public servers in the web panel

### Public KiwiSDR sources for testing

Directory: kiwisdr.com/public — 300+ servers worldwide, no auth required.
Good test targets: 14.074 MHz (FT8 constant activity), 40m European evening, 7.074 MHz.

### What this enables (the demo story)

"Fire up AetherSDR, pick a public KiwiSDR from the list, see real HF signals — no
Flex, no antenna, no hardware." That is a genuine onboarding path for new AetherSDR
users and a compelling demo at club nights / hamfests.

Also directly addresses Jeremy [KK7GWY]'s ask (2026-06-21 #ai-lab): demo mode where
non-Flex users can tune a Kiwi station and exercise the full AetherSDR DSP stack.

### Effort estimate

- Proof of concept (FFT + VITA framing, no audio, fixed KiwiSDR): ~1 day
- Closed-loop tuning (retune on VFO change): +0.5 day
- Audio demod: +1 day
- Web panel integration (server picker, status): +0.5 day
- Total PoC: ~3 days post-FD

### Strategic sensitivity

This idea is not published anywhere. No existing project bridges KiwiSDR to the
FlexRadio protocol. Keep design notes in shack-experiments only. The public flex-sim
repo gets the feature once it is working, framed as "KiwiSDR source" with no
design history visible.

