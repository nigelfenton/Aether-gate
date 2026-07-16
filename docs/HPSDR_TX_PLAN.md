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

### Phase 2 — guarded PTT, DUMMY LOAD ONLY ⚠ FIRST RF
Port the 9700's four-layer model verbatim in shape:
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
  - ⚠ **But `currentfreq` is the firmware's own idea of frequency.** It is declared in `filters.h`
    and set elsewhere in the driver — **VERIFY it tracks the TX NCO on TX, not just RX1.** If it
    only follows RX1, then transmitting on a different frequency than we're listening on would
    select the WRONG filter. This is the remaining question, and it is narrow.
  - ⚠ `handleALEX` also has `currentMox`/`currentCW` state — T/R switching is firmware-side too.

  **⛔ HARD GATE STANDS anyway: first key into a DUMMY LOAD with a scope/analyser, and confirm
  harmonic suppression empirically.** Source-reading says the filter should be selected for us;
  today's lesson says measure it rather than trust a code path we have not seen run.

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
