# HPSDR / Radioberry TX — design plan

**Status:** Draft for Nigel. Nothing built. Written 2026-07-16 against `feat/hpsdr-adapter` @ `57d6c67`.

**Scope:** Give `HpsdrAdapter` a real, guarded transmit path over HPSDR Protocol-1 (Metis),
so AE can key and modulate the Radioberry. Today the adapter is RX-only by construction
(`tx_capable=False`; "never sets the MOX bit").

---

## 0. Read this first — the honest blocker

**The hard part is not the HPSDR side. It's that AE doesn't send us TX audio.**

The IC-9700 already has a complete, working, guarded PTT path (`arm_tx`/`key_tx`/watchdog/
band-check) and a TX-audio drain. It has been **⛔ blocked since 2026-07-11** on exactly one
thing: *AE keys the rig but sends ~no audio to the gate* (1 UDP pkt/15 s, **0 dax_tx frames
decoded**). Current hypothesis: connect-mode keying makes AE's KISS-TX queue see
`isTransmitting()==true` → `maybeStartNextKissTx` defers the frame → audio never sent.

If we build HPSDR TX today we will key the Radioberry and **transmit a bare carrier**, hitting
the identical wall from a second direction — with the added cost that a bare carrier on HF into
an antenna is worse than one on 2m into a dummy load.

**Therefore: Phase 0 is not HPSDR work at all.** Resolve the dax_tx blocker on the 9700 first
(it is instrumented, guarded, and already there). Only then does HPSDR TX become a wire problem
worth solving. Doing it in the other order builds the second half of a bridge to nowhere.

**If FT8 TX on 20m is the actual goal**, note this plan does not deliver it quickly. The gate is
not the short path — a rig that already transmits is.

---

## 1. What exists today

**RX (works, proven 2026-07-16):** FT8 decodes end-to-end through AE + WSJT-X on 14.074 off the
Radioberry. `50c6f7a`'s IQ conjugate confirmed against WWV 15 MHz off-centre.

**Protocol facts already in the tree** (`hpsdr_proto.py`), deliberately fenced:

```
C0 byte:  bits [6:1] = register ADDR[5:0], bit [0] = MOX (1=TX).
          Keep C0 EVEN -> MOX=0 -> never keys.
C0_TX1_FREQ = 0x02   # addr 0x01, "avoid - transmit"
ep2_packet(): frame = SYNC + C0..C4 + bytes(504)   # <- 504 B of TX IQ, currently ZERO
```

So the three things TX needs are all *addressable* but none are wired:
1. **MOX** = `C0` bit 0 — every `C0` constant is even by construction.
2. **TX1 NCO** = `C0_TX1_FREQ` (0x02) — defined, never sent.
3. **TX IQ** = the 504-byte EP2 frame payload — currently `bytes(504)`.

**The safety model to copy, not reinvent** (`icom9700.py:828+`) — four layers, all mandatory:
1. **DISARMED by default** — `key_tx()` refuses until `arm_tx()`.
2. **Band check** — refuse unless the freq is in a legal TX segment.
3. **Watchdog** — `threading.Timer` force-unkeys after `TX_MAX_KEY_S` (10 s).
4. **Auto-unkey + disarm on close/disconnect.**

Plus the deliberate choice worth preserving: the 9700 kept `tx_capable=False` *even after real
PTT was wired*, so AE could not key it — the human had to call `key_tx()`. That is the right
default here too.

---

## 2. Why HPSDR TX is harder than the 9700's

The 9700 has a **transceiver** on the other end: CI-V `1C 00 01` = "you transmit", and the radio
does modulation, filtering, ALC, and power control itself. PTT is one command.

The Radioberry has **no transmitter** in that sense — it is an SDR front end. Protocol-1 TX means
*we* generate the modulated RF as **IQ samples in the EP2 payload**, continuously, in real time,
at the sample rate, while MOX is held. Every failure mode is ours: wrong level → splatter; a gap
in the stream → a hole in the transmitted signal; a stuck MOX → continuous carrier.

That is why this plan is phased with a dummy load and a spectrum check before anything reaches
an antenna, and why the RX-side lesson applies directly: **measure the thing, not your
instrument** — verify what is actually radiated, don't infer it from code that looks right.

---

## 3. Phases

### Phase 0 — unblock dax_tx (NOT HPSDR work) ⬅ START HERE
Resolve why AE sends no TX audio, using the 9700's existing instrumentation:
- Enable AE's AX.25 VERBOSE/diagnostics logging (dialog debug toggle) → why `maybeStartNextKissTx`
  defers.
- AE log (linux): `~/.config/AetherSDR/logs/aethersdr-*.log`, grep `dax|ax25|route=|KISS|txStreamId`.
- Gate: `[dax-tx] rx` heartbeat in `_decode_dax_tx`; `tcpdump src .112 dst .128 greater 200 | wc -l`.

**Exit:** the gate decodes a nonzero, sustained dax_tx frame rate while AE transmits. Outcome is
either an AE bug (report to Jeremy) or a state we can satisfy.

**Until Phase 0 passes, do not start Phase 2.** A TX path with no audio source is a carrier generator.

### Phase 1 — TX plumbing, INERT (no RF)
No MOX, nothing keys. Pure offline work, unit-testable:
- `hpsdr_proto.py`: `cc_tx1_freq(hz)` (C0 0x02); `ep2_packet_tx(seq, cc_a, cc_b, iq_a, iq_b)` taking
  504-B payloads; a `mox` flag that ORs bit 0 into C0 — **defaulting to 0**, with an assertion that
  it cannot be set unless explicitly passed.
- Keep `ep2_packet()` as-is (MOX-free) so every existing RX call site is provably unkeyable.
- Tests: byte-exact framing; MOX bit is 0 unless explicitly requested; TX1 freq encodes big-endian.

**Exit:** `python -m pytest aether_gate/tests/test_hpsdr.py` green; no behaviour change on air.

### Phase 2 — guarded PTT, DUMMY LOAD ONLY ⚠ FIRST RF
Port the 9700's four-layer model verbatim in shape:
- `_tx_armed=False` default, `arm_tx()`/`disarm_tx()`/`tx_ready()`/`key_tx()`/`unkey_tx()`.
- `TX_MAX_KEY_S = 10.0` watchdog Timer.
- `TX_BANDS_MHZ` — **Nigel's licensed HF segments only**, and start with ONE band (20m).
- `tx_capable` **stays False** — AE must not be able to key it. Human calls `key_tx()`.
- Auto-unkey + disarm in `close()` and on AE disconnect.
- MOX held by the `_cc_loop` sender thread (it already owns EP2 egress at 20 Hz — but see §4:
  TX IQ needs a much faster cadence, so keying likely needs its own pacing).

**Testing:** ⚠ **dummy load. Lowest achievable drive. Watch the Radioberry's PA temperature.**
First test is `key_tx()` → 1 s → `unkey_tx()`, verifying the watchdog fires if we don't.

**Exit:** MOX asserts and releases cleanly; watchdog proven by *deliberately* not unkeying;
disconnect mid-key auto-unkeys. Confirmed on a dummy load with a power meter.

### Phase 3 — TX IQ (modulation), DUMMY LOAD
- Feed the 504-B EP2 payloads from `tx_pcm_ring` (AE's dax_tx, 24 kHz int16 mono).
- Upsample 24 k → sample rate; SSB-modulate to IQ; **apply the RX-side conjugate convention in
  reverse** — if RX IQ needed `complex(i, -q)`, TX almost certainly needs the mirror, and getting
  it wrong transmits on the **wrong sideband**. Verify on a receiver, don't reason about it.
- Level/scaling: define a hard `TX_MAX_AMPLITUDE` well below full scale; no AGC on TX.
- Underrun policy: if the ring starves, **send zeros, not stale audio** — and log it. (The RX path's
  `get_audio()`-returns-None → silence pattern is the precedent.)

**Exit:** a second receiver (the 9700 on a different band, or a friend) confirms the signal is on
the **right sideband, right frequency, intelligible, and clean**. Check the spectrum for splatter.

### Phase 4 — arm UX + AE wiring (only if wanted)
- Expose `tx_ready()`/`arm_tx()` on the control panel — explicit arm button, live armed/in-band/
  keyed state, and a visible watchdog countdown.
- Only after all the above: consider `tx_capable=True` so AE can key it. **This is the last step,
  not the first** — and it is a separate decision, not a formality.

---

## 4. Known unknowns (things I have NOT verified)

Flagged honestly rather than assumed:

- **EP2 TX cadence.** RX needs EP2 only to keep registers latched (20 Hz suffices). TX IQ must be
  *continuous at the sample rate* — 48 kHz needs ~63 packets/s, and any gap is a hole in the
  transmitted signal. The current `_cc_loop` 20 Hz sender is **not** a suitable TX pump. Whether
  Python can sustain it reliably on the Pi is **unmeasured** and is the main technical risk.
- **Radioberry PA.** I do not know its output power, duty-cycle limits, or thermal behaviour, or
  whether Nigel's board has a PA/filters fitted at all. **Establish this from the hardware before
  any RF.** FT8 is 100% duty cycle for 13 s — unforgiving.
- **Does the Radioberry TX at all in this setup?** Board id `0x06` says "HL2 on the wire" for RX;
  it does not prove a working TX chain, filters, or T/R switching.
- **`CONFIG_DUPLEX`** is already set on in `cc_config` (pihpsdr does it unconditionally). Its exact
  TX-side meaning here is unverified.
- **Sideband convention on TX** — see Phase 3. Unknown until measured.
- **Legal/licensing** — TX band limits are Nigel's call, not mine to encode from memory.

---

## 5. Recommendation

1. **Do Phase 0.** It unblocks the 9700 *and* is a prerequisite here. Highest value, no RF risk.
2. **Then decide** whether HPSDR TX is worth it. If the goal is "work FT8 on 20m", a rig that
   already transmits is the short path; this plan is a project, not an afternoon.
3. If yes: Phases 1→2→3 in order, **dummy load throughout**, `tx_capable=False` until the end.
4. Answer the §4 unknowns *before* Phase 2 — especially the Radioberry's PA reality.

The RX work this session earned its result by measuring rather than reasoning. TX is where that
discipline stops being a matter of correctness and starts being a matter of not damaging hardware
or radiating something illegal.
