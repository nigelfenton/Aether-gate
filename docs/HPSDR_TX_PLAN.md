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

### ⛔ Why the bare-carrier guard is NOT in the HPSDR adapter today (2026-07-16)
Asked to port the 9700's bare-carrier guard (`ddc164b`) here. **Deliberately not done — it would be
actively harmful.** `HpsdrAdapter` has **no `key_tx`, no `arm_tx`, no TX surface at all**, and the
engine keys purely on `hasattr`:

```python
# engine.py:1377 (xmit) and :1403 (MOX)
if self.adapter is not None and hasattr(self.adapter, "key_tx"):
    if key: self.adapter.key_tx()
```

So **defining `key_tx` on the HPSDR adapter is exactly what wires AE's MOX to the Radioberry.**
Adding a "safety guard" today would *create* the keying path it purports to protect — the guard can
only refuse *some* keys, whereas the current absence refuses *all* of them. Today `hasattr` is the
strongest guard in the system, and it is free.

**The guard belongs in Phase 2, added in the same commit as `key_tx` — never before it.** When it is
written, note the HPSDR case is STRICTER than the 9700's:
- The 9700 has a **mic**, so voice modes legitimately need no dax_tx and the guard skips them.
- The Radioberry has **no mic and no modulator** — *every* mode's TX audio comes from AE. So there is
  no voice exemption: **no dax_tx stream (or empty ring) == no TX, in every mode, no exceptions.**
- And unlike the 9700 (whose radio makes the RF), a starved ring here doesn't just mean silence — it
  means we are feeding the DAC nothing while MOX is asserted. Underrun policy is a TX-correctness
  problem, not just an audio one (see Phase 3).

### Phase 1c — telemetry decoder (RX-only, NO RF) ⬅ DO THIS WHILE THE HL2 SHIPS
Decode the EP6 C&C response bytes we already receive: temperature, fwd, rev, current
(HL2 response regs `0x01`/`0x02`; the Radioberry mirrors the same layout). Verified today that our
`parse_ep6`/`iq_samples` ignore these bytes entirely — the transport works and the slots alternate
correctly, the Radioberry just reports zeros because it has no MAX11613.

Build it now against the Radioberry (proves the parse path, RX-only, zero RF risk), surface it to AE
as an SWR/power meter, and it lights up the moment the HL2 is plugged in. **This is the prerequisite
for every TX guard that follows** — a guard cannot act on a number we do not decode.

### The filter board's own VSWR sensor — Nigel's question, 2026-07-16
*"there is a vswr meter on the output of the HL2 filter board! im not sure how we use it unless its
part of the band selection i2c data output?"*

**Good instinct, and the answer is better than the guess: it is NOT part of the band-select write —
Protocol-1 has a general I2C READ path.** From the HL2 wiki *Protocol*:

- To read from an I2C bus, set the **RQST bit, `C0[7]`**.
- Second byte `0x07` = read; then the device I2C address and register number.
- The HL2 "requests four bytes of data from the I2C device and returns the data in **C1, C2, C3, C4**".
- The response comes back with **ACK=1**, `RDATA` carrying the 4 I2C bytes, `RADDR` matching the
  original ADDR.

So the band-select I2C write (`0x20`) is one direction; reading a sensor on the same bus is a
*separate request*, not a side-effect of the filter write. **We would poll it explicitly.**

**Two independent SWR sources on an HL2, then — and they are not redundant:**
1. **HL2's native fwd/rev** (response regs `0x01`/`0x02`) — measured at the **HL2's own PA output**.
   Already decoded by Phase 1c (`a1fd077`); free, no extra requests.
2. **The filter board's VSWR sensor** — measured at the **filter output**, i.e. after the LPF, which
   is closer to what the antenna actually sees. Reachable via the RQST/`0x07` I2C read.

(1) protects the PA; (2) better reflects the antenna/feedline. Start with (1) — it is already
working and costs nothing. Add (2) only if the two disagree in a way that matters.

**✅ ANSWERED — and (2) does not exist as a separate source. Nigel called it from the parts list.**
He checked his board: the only I2C device on it is an **MCP23008T-E/SS**, and inferred the detectors
must be diodes. That is decisive:

- The **MCP23008 is an 8-bit I/O expander — GPIO only, NO ADC.** It cannot digitise anything. It is
  there to switch the filter relays, nothing more.
- The Radioberry firmware confirms the usage: it only ever `write()`s to the N2ADR at `0x20`, and
  `ldata[0] = 0x09` is the MCP23008's **OLAT (output latch)** register. Pure output.
- So **the filter board's SWR bridge cannot be read over I2C** — there is no device on that bus
  capable of it. The RQST/`0x07` I2C read path is real, but there is nothing there to read.

**Where the SWR bridge actually goes:** the N2ADR filter board "contains filters to clean up the
transmitter output but also **an SWR bridge and power sensor**", and it mates directly to the HL2
mainboard. The diode detectors feed **analog lines through the board-to-board connector into the
HL2's own ADC** — which is exactly what surfaces as the HL2's native fwd/rev in response registers
`0x01`/`0x02`.

**So (1) and (2) are the SAME sensor.** The HL2's "native" fwd/rev *is* the filter board's SWR bridge,
read by the HL2's ADC. There is no second source to poll and nothing extra to build:
**Phase 1c (`a1fd077`) already decodes it.** It reads zero on the Radioberry because that board has
neither the HL2's ADC path nor the N2ADR bridge.

⚠ Consequence worth noting: **the HL2's fwd/rev therefore measures AFTER the low-pass filter**, at
the filter board — i.e. what the antenna sees, which is the more useful place for an SWR guard. Good
news for Phase 2.

⚠ Still to confirm on real hardware: that Nigel's incoming HL2 is fitted with the N2ADR filter board
(the bridge lives on it, not on the HL2 mainboard). Without that board there may be no fwd/rev at all
— the same "sensor not fitted" trap as the Radioberry, and Phase 1c's `has_sensors` will say so.

### Phase 2 — guarded PTT, DUMMY LOAD ONLY ⚠ FIRST RF — **ON THE HL2, NOT THE RADIOBERRY**
Port the 9700's four-layer model verbatim in shape:
- ⛔ **TARGET: the HL2.** It has native fwd/rev/temp/current; the Radioberry PA hat has none. Do not
  do first-RF on the board that cannot tell you when it is in trouble.
- **SWR + thermal guards, using the Phase-1c decoder:** refuse to key above an SWR threshold, and
  unkey on rising reverse power or over-temperature. This is the guard the 9700 never had, and the
  reason to wait for the HL2 rather than rush the Radioberry.
- `_tx_armed=False` default, `arm_tx()`/`disarm_tx()`/`tx_ready()`/`key_tx()`/`unkey_tx()`.
- **The bare-carrier guard, in the SAME commit as `key_tx` (see the section above).** Port
  `set_tx_audio_ready_probe` + the `engine.tx_audio_ready()` probe from `ddc164b`, but with **no
  voice exemption** — the Radioberry has no mic, so refuse in EVERY mode when no dax_tx stream is
  registered. Keep the `force=True` escape for a deliberate tuning carrier, and keep the fail-safe
  (missing/throwing probe -> assume ready) so an unwired engine can't wedge it.
- ⚠ **Note the engine auto-arms on connect** (`engine.py:1060`: "AUTO-ARM TX on connect (per Nigel:
  arm defaults on)"). So on HPSDR, `arm_tx()` existing means AE's MOX reaches the rig with only the
  band-check + bare-carrier guard between it and RF. Decide deliberately whether HPSDR should
  opt out of auto-arm for its first RF phases.
- `TX_MAX_KEY_S = 10.0` watchdog Timer.
- `TX_BANDS_MHZ` — **Nigel's licensed HF segments only**, and start with ONE band (20m).
- ⛔ **NO-SPLIT GUARD (see the LPF section): refuse to key unless the TX frequency == RX1.** The
  Radioberry's firmware picks the LPF from **RX1 only** (`currentfreq` is set solely on `C0=0x04`;
  `0x02`/TX1 is never read), so a split TX would radiate through the wrong filter. Since Phase 1 is
  single-slice anyway this costs nothing, and it converts a hardware hazard into a guard.
- `tx_capable` **stays False** — AE must not be able to key it. Human calls `key_tx()`.
- Auto-unkey + disarm in `close()` and on AE disconnect.
- **MOX does NOT go in `_cc_loop`** — that is settled, not open. §4 measured the TX cadence at
  `rate/126` = **381 pkt/s at 48 kHz**, vs `_cc_loop`'s 20 Hz. TX needs its OWN sender paced at
  `rate/126`, carrying MOX + TX IQ in every packet; `_cc_loop` stays the RX register-latcher.
  Two senders on one socket needs a deliberate story (likely: TX sender takes over egress while
  keyed, `_cc_loop` pauses) — design it, don't discover it.

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

- **EP2 TX cadence — ✅ MEASURED 2026-07-16 ON THE PI. Python sustains it, IF the payload is
  vectorised.** (RF-free test: every `C0` even → MOX=0 → the radio cannot key.)

  **First, a correction to my own arithmetic:** I said "~63 packets/s at 48 kHz". Wrong — 63 is
  samples per *frame*. Each 504-B frame holds 63 IQ samples (8 B each: I[3] Q[3] mic[2]), and a
  packet carries 2 frames = 126 samples. So the real cadence is **rate / 126**:

  | rate | needed | per-sample Python loop | numpy-vectorised |
  |---|---|---|---|
  | 48 kHz | 381 pkt/s | 380.9 (100%), 23 late, build 436 µs | **380.9 (100%), 1 late, build 64 µs** |
  | 192 kHz | 1524 pkt/s | **1410 (92.6%), ALL 8463 late**, overrun median 256 ms | **1523.8 (100%), 0 late, build 89 µs** |
  | 384 kHz | 3048 pkt/s | not tested | **3047.6 (100%), 3 late, build 81 µs** |

  **The naive per-sample build collapses at 192 kHz** — 436 µs to build against a 656 µs budget, and
  it fell irrecoverably behind (median overrun 256 ms, i.e. a quarter-second hole in the signal).
  Replacing the per-sample loop with vectorised numpy byte-slicing drops the build to ~64–89 µs and
  it **holds 100% at every rate up to 384 kHz**, with 328 µs of budget to spare.

  **So this is no longer the main risk — but it is a hard design constraint:** the TX IQ payload
  MUST be built with numpy, never a per-sample Python loop. (Note the RX path's `iq_samples()` is
  exactly such a loop — do NOT mirror it for TX.) The `_cc_loop` 20 Hz sender remains unsuitable as
  the TX pump; TX needs its own paced sender at rate/126.

  Caveat: this measured **build + sendto pacing** on an idle Pi. It did not measure building IQ from
  live dax_tx audio (upsample + SSB-modulate per packet) while the RX chain is also running. That
  combined load is still unmeasured.
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

  **✅ RESOLVED FROM SOURCE 2026-07-16 — and the news is GOOD.** It is **not** the gateware and it is
  **not** the network client. It is the **Radioberry's own Pi-side firmware**, which selects the
  filter *for us*, autonomously, from the frequency it sees in the EP2 stream.

  Source: `SBC/rpi-5/device_driver/firmware/filters.h` (rpi-5 = Nigel's Pi), called from
  `radioberry.c:390` — `handleFilters(buffer, CWX)` — on the inbound EP2 buffer:

  ```c
  else {
      //firmware does determine the filter.
      uint16_t hpf = 0, lpf = 0;
      if (currentfreq < 1416000) hpf = 0x20;       /* bypass */
      ...
      if (currentfreq > 32000000) lpf = 0x10;      /* bypass */
      else if (currentfreq > 22000000) lpf = 0x20; /* 12/10 meters */
      else if (currentfreq > 15000000) lpf = 0x40; /* 17/15 meters */
      else if (currentfreq > 8000000)  lpf = 0x01; /* 30/20 meters */
      ...
      i2c_alex_data = hpf << 8 | lpf;
  }
  ```

  **Read the comment: "firmware does determine the filter."** That `else` is the fallback taken when
  the host does NOT supply manual filter data — i.e. **exactly our case**. A plain Protocol-1 client
  that never sends ALEX/manual-filter bytes gets **automatic, frequency-derived filter selection**
  from the Radioberry's firmware. We do not have to drive I2C, and we cannot: our gate talks
  Protocol-1 over the network to `:1024`; the firmware runs on the Radioberry's own Pi.

  So Nigel's "HPSDR software has a filter-present setting" is real but is about the **manual
  override path**: `handleALEX` first checks `(buffer[523] & 0xFE) == 0x12` (ALEX C0) and a manual
  bit (`buffer[525] & 0x40`), and only falls through to auto if the host didn't supply data.
  `handleN2ADRFilterBoard` is the same shape for the N2ADR board (mcp23008 @ 0x20; ALEX PCA9555 @
  0x21; VA2SAJ switcher @ 0x22).

  **Consequences for this plan — Phase 2 gets EASIER, not harder:**
  - We do **not** need a `filter_board=` config or any I2C code. Sending nothing is the correct and
    safe behaviour: it selects the auto path.
  - ⛔ **`currentfreq` TRACKS RX1 ONLY — CONFIRMED FROM SOURCE 2026-07-16. The filter follows the
    RECEIVE frequency, never the TX NCO.** `filters.h`:

    ```c
    static inline void handleFilters(char* buffer, int cw) {
        if ((buffer[11]  & 0xFE) == 0x04) { currentfreq = determine_freq(11, buffer); }
        if ((buffer[523] & 0xFE) == 0x04) { currentfreq = determine_freq(523, buffer); }
    ```

    `0x04` is `C0_RX1_FREQ`. **`0x02` (`C0_TX1_FREQ`) never appears as a register match anywhere in
    the firmware** — the only `& 0xFE` comparisons are `0x00` (N2ADR), `0x04` (RX1) and `0x12`
    (ALEX manual). Identical in the rpi-4 and rpi-5 trees, so it is not a variant quirk.

    **Consequence: split-frequency TX selects the WRONG filter**, because the firmware only ever
    saw RX1. Transmitting on a band away from where we are listening = wrong LPF = harmonics.

    **Mitigations (decide in Phase 2):**
    1. **Simplest and safest: refuse split TX entirely.** Our Phase-1 scope is one slice / one pan
       anyway, so require TX freq == RX1 freq and make `key_tx` refuse otherwise. This turns a
       hardware hazard into a guard we already know how to write (mirrors `ddc164b`).
    2. Or drive the ALEX **manual** path ourselves (`C0=0x12` + the manual bit), taking full
       responsibility for filter selection — more code, more ways to be wrong.
    3. Or set RX1 to the TX frequency before keying so the firmware picks the right filter — a hack
       that fights the firmware's model and breaks the panadapter.

    Option 1 is recommended: it is honest about what we can guarantee, and it is free.
  - ⚠ `handleALEX` also has `currentMox`/`currentCW` state — T/R switching is firmware-side too.

  **⛔ HARD GATE STANDS anyway: first key into a DUMMY LOAD with a scope/analyser, and confirm
  harmonic suppression empirically.** Source-reading says the filter should be selected for us;
  today's lesson says measure it rather than trust a code path we have not seen run.

- **✅ TELEMETRY + PA PROTECTION EXIST — CONFIRMED FROM SOURCE 2026-07-16 (Nigel asked; he was
  right).** The preamp/PA board carries a **MAX11613 4-channel I2C ADC @ `0x34`** (`measure.h`),
  read as:

  ```c
  void read_I2C_measure(int *current, int *temperature, int *fwd, int *rev);
  ```

  So there IS **PA temperature, PA current, and FORWARD + REVERSE power** (i.e. the makings of
  VSWR). Better still, two things we do not have to build:

  **1. The firmware protects the PA itself** (`radioberry.c`, `rb_measure_thread`):
  ```c
  // temperature == (((T*.01)+.5)/3.26)*4096   if pa temperature > 50C (=1256) switch pa off!
  if (pa_temp_ok && (pa_temp >= 1256)) {
      fprintf(stderr, "ALERT: temperature of PA is higher than 50C; PA will be switched off!\n");
      pa_temp_ok = 0;
  }
  ```
  …with auto-recovery once the temp is back in range for 10 s, and `pa_temp_ok` folded into the
  gateware control word (`rb_command`). It also disables the PA if temp/current **cannot be
  measured** at all — fail-safe, not fail-open. This substantially de-risks the FT8 100%-duty worry.

  **2. The telemetry comes back to us in the EP6 stream we ALREADY receive** — no extra transport,
  no I2C on our side. `radioberry.c` alternates it into the C&C bytes of each EP6 frame, keyed by
  `hpsdrdata[11]`:
  | `[11] & 0xF8` | `[12:13]` | `[14:15]` |
  |---|---|---|
  | `0x08` (even seq) | PA temp (or RPi temp if no module) | **FWD power** |
  | `0x10` (odd seq) | **REV power** | PA current |

  **Phase 3+ should decode this and surface it to AE** (fwd/rev → SWR meter, temp → a guard).
  Our `parse_ep6`/`iq_samples` currently ignore the C&C bytes entirely — they only read the IQ
  payload. This is a clean, RF-free win available *before* TX: **decode and display it during RX
  first**, so the telemetry path is proven before it is load-bearing.

  ⛔ **MEASURED ON NIGEL'S BOARD 2026-07-16 (live RX, 1903 EP6 packets): the telemetry path WORKS,
  but `i2c_measure_module_active` is almost certainly FALSE — there is NO fwd/rev/current.**

  ```
  C0=0x08 (temp/fwd) x1902     C0=0x10 (rev/current) x1904    <- both slots alternate correctly
    temp        min=1086 max=1104 avg=1099.9   -> 37.5 C
    fwd pwr     min=0 max=0 avg=0.0
    rev pwr     min=0 max=0 avg=0.0
    pa current  min=0 max=0 avg=0.0
    rb_control  pa_temp_ok=0  CWX=0  running=0   (on EVERY packet)
  ```

  **Two independent reads of the source agree on why.** The `0x10` branch is emitted
  *unconditionally*, but `rev`/`pa_current` are only ever written inside `read_I2C_measure()`, which
  `rb_measure_thread` calls **only** `if (i2c_measure_module_active)`. So permanent zeros = the
  module is not being read. Likewise `fwd` is only packed inside the `if (i2c_measure_module_active)`
  branch of the `0x08` slot — our zeros there say the same thing. The 37.5 C we see is therefore the
  **RPi CPU fallback** (`sys_temp`), not PA temperature.
  ⚠ Note 37.5 C is *plausible as either*, so temperature alone CANNOT distinguish them — both paths
  use the same `(4096/3.26)*((C/100)+0.5)` encoding. The fwd/rev/current zeros are the real tell.

  **⛔ This RETRACTS the de-risking claimed above.** `pa_temp_ok=0` on every packet, and the firmware
  comment says *"if temperature could not be measured the pa is disabled"*. So on this board, as it
  stands: **no PA thermal protection, no SWR, no PA current.** The 100%-duty FT8 worry is NOT
  de-risked — it is exactly as open as before, and the fail-safe may mean the PA is disabled outright.

  **✅ EXPLAINED (Nigel + source, 2026-07-16). His board is a PA hat WITHOUT the MAX11613.** Nigel:
  *"i think i have the pa board that does not have the max — that allows you to set the standing bias
  electronicly."* The source agrees: `bias.h` says the MCP4662 dual digital pot (@`0x2C`) "is used to
  set both bias settings for the **Radioberry preAmp**", and `measure.h` scopes the MAX11613 (@`0x34`)
  to "the radioberry **preAmp**". **The measure ADC and the electronic-bias pot both live on the
  preAmp board — which he does not have.** His PA hat has the T/R switch + I2C LPF; no ADC.

  And it is **autodetected, not configured** — which is why Nigel never saw a compile flag or setting
  for it (`measure.c`):
  ```c
  void openI2C_measure(void) {
      i2c_measure_module_active = 0;
      fd_i2c_measure = open("/dev/i2c-1", O_RDWR);
      ...
      i2c_measure_handler = ioctl(fd_i2c_measure, I2C_SLAVE, ADDR_MEAS);   // 0x34
      if (i2c_measure_handler >=0) if (config_I2C_measure()==1) i2c_measure_module_active = 1;
      else close(i2c_measure_handler);
  };
  ```
  The firmware probes `0x34`; nothing answers; the flag stays 0. Everything we measured follows.

  **So this is a permanent property of his hardware, not a config to fix:**
  - **No PA temperature, no PA current, no FWD/REV, no SWR — ever, on this board.**
  - `pa_temp_ok=0` on every packet is therefore *expected*, not a fault. ⚠ **But the firmware
    comment says "if temperature could not be measured the pa is disabled" and folds `pa_temp_ok`
    into the gateware control word — so whether his PA is INHIBITED by this is an open question and
    a Phase-2 blocker. It would explain "ive never noticed the temp when pa is disabled".**
  - The HPSDR client's **PA on/off/enable setting** Nigel remembers is the operator declaring the PA
    present — consistent with everything else here being host-declared rather than sensed.

  ⚠ Still unknown: **output power** and **duty-cycle limits** as numbers.

  **⛔ CONCLUSION FOR TX *ON THE RADIOBERRY*: no reflected-power protection, no PA thermal
  protection, no current sensing — not available at any price, because the sensors are not fitted.**
  FT8 is 100% duty for 13 s. An antenna fault would be invisible until something burns.

### ✅ THE HL2 CHANGES THIS — TARGET TX AT THE HL2, NOT THE RADIOBERRY (2026-07-16)
Nigel has **ordered a Hermes-Lite 2**. That is the right TX target, and it removes the single worst
risk in this plan, because **the HL2 has the telemetry natively — in the hardware, not on an optional
companion board** (openHPSDR Protocol-1 response registers, HL2 wiki *Protocol*, ACK==0 base map):

| Response register | `[31:16]` | `[15:0]` |
|---|---|---|
| `0x01` | **Temperature** | **Forward power** |
| `0x02` | **Reverse power** | **Current** |

Delivered in the C1–C4 response bytes of the EP6 C&C — i.e. **the exact bytes we already decoded
live on the Radioberry today**, and which came back zero there. On an HL2 they should carry real
values. So the telemetry decoder is worth building **now, against the Radioberry, RX-only** (it
proves the parse path against known-zero fields), and it lights up when the HL2 arrives.

**Revised recommendation: Phases 1–3 target the HL2.** The Radioberry stays an RX source. This is
not a detour — the HL2 *is* the board this plan was always describing (`prototypes/hl2/`, board id
`0x06`, gateware 7.x), and it is the one with fwd/rev/temp/current for the guards to act on.

### ⚠ ON THE ATU-100 — it protects the ANTENNA path, NOT the PA
Nigel: *"i also have an ATU-100 fitted to the output so it should be always ok."* **Do not build the
TX plan on that.** Being straight about the gap rather than agreeing:
- An ATU presents a **matched load once it has tuned**. It does **not** protect during the tune
  itself, and a tuning cycle is exactly when the PA sees a bad match.
- It cannot help with an **open/shorted feedline, a disconnected antenna, or a match outside its
  range** — it will hunt and fail, with the PA keyed into whatever is there.
- It does nothing for **thermal** limits. A 100%-duty FT8 sequence into a perfect 1:1 load still
  heats the PA, and this board reports **no temperature**.
- The ATU has no path to tell *us* anything — no feedback into Protocol-1. Our guards would still be
  blind.
So the ATU-100 meaningfully reduces the *usual* case, and is good to have. It is not a substitute
for reflected-power or thermal sensing, which is precisely what the HL2 provides and this PA hat
cannot.

### On "4 W already reached Europe on FT8"
Real and relevant: it proves the **TX chain, PA, LPF and antenna all work**, and that the firmware's
auto filter selection does the right thing on air — that was previously only read from source. Worth
recording as evidence the hardware path is sound. But note it proves the *hardware* works when driven
by a **known-good client** (pihpsdr/Thetis/Quisk), which is a different question from whether **our
gate** can drive it correctly. Our TX IQ, our MOX pacing, our sideband convention and our level
scaling are all still unwritten and unproven. The 4 W contact raises confidence in the board, not in
code that does not exist yet.

  ⚠ Still unknown: **output power** and **duty-cycle limits** as numbers.
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
