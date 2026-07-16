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

### Phase 0 — ✅ ALREADY PASSES (checked 2026-07-16). The blocker was stale.
**AE DOES send dax_tx audio.** `journalctl -u aether-gate-9700` over 7 days shows **304 `[dax-tx] rx`
heartbeats on Jul 15** (15:14:48 → 21:43:06), up to `frames=3801 ring=5184B peak=0.350` — real audio at a
healthy level, arriving *while keyed* (`[tx] KEYED @ 145.07 MHz` → `[dax-tx] rx` → `[tx] UNKEYED`).
Something between 07-11 and 07-15 fixed it; the `maybeStartNextKissTx`-defers hypothesis is dead.

**Phase 0's exit criterion — "the gate decodes a sustained dax_tx frame rate while AE transmits" — is
already met.** No work needed. The TX-audio *source* exists.

### Phase 0b — ROOT CAUSE FOUND (2026-07-16): AE keys WITHOUT creating the dax_tx stream
**It is not a drain race, and `_tx_audio_loop` is not at fault.** The drain works correctly whenever
audio actually arrives. Measured across Jul 15: **127 keys with `0 real audio`, 134 with ~30 real** —
not per-process (every PID sees both), so it is per-key.

Diffing a failing cycle against a working one shows the whole story in the AE command stream:

```
WORKING 15:14:47          FAILING 15:59:19
  C2063|stream create type=dax_tx     (absent!)
  C2064|transmit set dax=1            C4361|transmit set dax=1
  C2065|xmit 1                        C4362|xmit 1
  -> [dax-tx] rx frames=1..101        -> no [dax-tx] rx AT ALL
  -> drain END (30 real audio)        -> drain END (0 real audio, seen_real=False)
```

**In the failing cycles AE never sends `stream create type=dax_tx`, so `dax_tx_stream_id` is None,
so the prime loop's guard (`engine.py:965`) drops every inbound VITA packet before
`_decode_dax_tx` — there is no `[dax-tx] rx` at all.** AE keys and sets `dax=1` but never registers
the stream. The gate then keys the rig and, correctly, has nothing to send.

**It is PER-AE-CONNECTION, and it is NOT simply "the reconnect clears our id".** Timeline for one
gate process (881710), which saw three AE connections:

```
15:58:31  AE connected  -> 15 keys, ALL 0 real     (dead session)
16:27:26  AE connected  -> 25 keys, ALL 0 real     (dead session)
16:37:43  AE connected  -> 5,2,1,2,2,1,8,2,1...    ALL 30-35 real  (working session)
```

So a *later* connection works fine — the gate is not permanently poisoned, and the
`dax_tx_stream_id = None` on disconnect (`engine.py:1050`) is not by itself the bug. Whatever AE
does differently on the working connection is the key. Note this process logged **zero**
`stream create type=dax_tx` across its whole lifetime yet still had 80 `[dax-tx] rx` heartbeats and
35 good drains — meaning **the id was learned some other way, or the stream survived a reconnect at
AE's end**. That contradiction is unresolved and is the thread to pull.

**Next step (unresolved — do NOT guess):** for the working 16:37:43 session vs the dead 16:27:26
one, diff the FULL AE command stream from connect to first key (not just `stream create`) — what
does AE send in one and not the other? Candidates: a `stream create` under a different spelling,
`transmit set dax=`, a slice/mode precondition, or a client-handle/binding difference. Then decide
whether the gate should (a) re-advertise/re-register the dax_tx stream itself on reconnect, or
(b) this is an AE-side bug worth reporting.

**Exit:** every keyed transmission is preceded by a live `dax_tx_stream_id`, and `drain END` reports
nonzero `real audio`.

**This is 9700 work, and it is the honest prerequisite.** It is RF-free to diagnose (log reading).
**Until Phase 0b passes, do not start Phase 2.** A TX path that forwards silence is a carrier generator.

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
- **⚠⚠ THE LPF BAND-SELECT QUESTION — THE SINGLE BIGGEST TX RISK. NOT SETTLED.**
  Nigel's board has a **PA hat with a T/R switch and an I2C-controlled low-pass filter bank**
  (https://github.com/pa3gsb/Radioberry-2.x). TX chain, T/R switching and band filtering **exist** —
  Phase 2/3 are not blocked on "does it transmit at all".

  **But how the filter gets selected is the thing that can radiate illegal harmonics or kill the PA,
  and the sources CONFLICT.** Nigel pointed at the HL2 companions
  (https://github.com/softerhardware/Hermes-Lite2/tree/master/hardware/companions — the N2ADR filter
  board lives there). The Radioberry borrows the **N2ADR I2C filter design**, but *drives it
  differently*, and that difference is exactly what matters:

  > "The control of the I2C devices is **not done by the gateware as done in the HL-2** but in the
  > **firmware by use of the I2C module running on the RPI**." — Radioberry group

  …versus a later account that with the preamp work "the I2C logic must be built into the gateware…
  with N2ADR filter selection all controlled by the gateware." **Both cannot be true of one board.**
  This is version-dependent (gateware rev + which hat), and Nigel's board runs **gateware 7.3**
  (measured). PA3GSB: *"the gateware is verilog as HDL but the filters can be programmed in good old
  C language"* — which points at host/RPi-side control, i.e. the dangerous case.

  **Why it matters:** if filter selection is host/firmware-driven, then a bare Protocol-1 client (us)
  that keys MOX **without** selecting the LPF transmits **unfiltered** — harmonics into the antenna.
  Note our gate talks Protocol-1 **over the network to `10.0.0.224:1024`**; it is NOT the RPi
  firmware, so *any* RPi-side I2C filter logic is not something we invoke. Whether the Radioberry's
  own firmware does it for us on TX is **UNVERIFIED**.

  **⚠ STRONG EVIDENCE FOR THE HOST-DRIVEN (DANGEROUS) CASE (Nigel, 2026-07-16):** HPSDR client
  software (pihpsdr/Thetis-class) exposes a **SETTING for whether the filter board is present** —
  and whether it has the band-pass filter. **A setting that the operator declares is not
  auto-detection.** If the filter were selected autonomously by the gateware from the TX NCO, the
  host would have no reason to know or care that the board exists. This points hard at: the HOST is
  expected to participate in filter selection, and a client that doesn't know about the board simply
  will not drive it. **Our gate has no such setting and no such code — so on current evidence,
  keying from the gate would transmit with the LPF bank unselected/unswitched.**

  **⛔ HARD GATE: resolve this from the gateware source / PA3GSB directly BEFORE any Phase 2 RF, and
  do the first key into a DUMMY LOAD with a scope/analyser on the output — verify harmonic
  suppression empirically rather than trusting either account.** Measure the thing, don't reason
  about it. Phase 2 likely also needs a `filter_board=` config + the I2C/IO select path, mirroring
  whatever pihpsdr does — i.e. the filter is *our* responsibility, exactly like the TX IQ.

  Still unknown besides: output power, duty-cycle limits, thermal behaviour. FT8 is 100% duty for
  13 s — unforgiving.
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
