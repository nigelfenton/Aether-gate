# Icom LAN transport — systematic audit vs SDR9700 (2026-07-08)

Our Icom RS-BA1/LAN transport (`aether_gate/adapters/icom/`) is a Python port of
[w5jwp/SDR9700](https://github.com/w5jwp/SDR9700)'s C++ transport. The port was
done piecemeal, and three protocol pieces were discovered missing the hard way —
each after days of live symptoms:

1. **The RX-audio session** was never brought up at all (→ no audio, ever).
2. **Session token renewal** (`sendToken(0x05)` every 60 s) was never sent — the
   radio's token expires after ~90 s and it then silently stops the privileged
   streams (scope + audio) while ping/idle keep flowing. That was the infamous
   "deaf scope": the "~2650-frame cap" was ~90 s × 29.5 fps. Time, not frames.
3. **Stream/session teardown hygiene** (below) — the phantom-session wedge.

So we audited the whole transport line-by-line against the reference
(`UdpBase`, `UdpHandler`, `UdpCivData`, `UdpAudio`, `PacketTypes.h`) and closed
every meaningful gap at once. This file is the record.

## Fixed in this audit

| # | Find | Reference | Fix |
|---|------|-----------|-----|
| 1 | **Stream CLOSE never sent on teardown** — we only fired the 0x05 disconnect; the radio kept data-stream state half-open (phantom-session food) | `UdpCivData::closeStream` → `sendOpenClose(true)` | `Ic9700Civ.stop()` sends `openclose` magic 0x00 before `super().stop()` |
| 2 | **Token REMOVAL never sent on shutdown** — the login slot lingered until the radio aged it out (the `authed=True` reconnect wedge) | `UdpHandler` shutdown → `sendToken(0x01)` + wait for ack | `Ic9700Handler.stop()` sends token-removal 0x01 (+0.3 s flush) before 0x05 |
| 3 | **Token renewal REJECTION ignored** — we logged and waited to die | "Radio rejected token renewal, performing login" → re-login in place | On `response=0xFFFFFFFF`: adopt ids from the packet, clear auth, `_send_login()` |
| 4 | **Radio-initiated disconnect ignored** (status 0x50 with `disc=1`) — we sat orphaned with stale streams | `UdpHandler` closes streams on disc | Handler sets `radio_disconnected`, clears events; adapter watchdog recycles |
| 5 | **Are-you-there sent exactly once** — a lost 0x03 datagram = connection hangs forever | `areYouThereTimer` retries at 500 ms until i-am-here | `UdpBase._timer_loop` retries at 500 ms; after 20 tries, logs + keeps probing at 2 s (headless service ≠ GUI, don't give up) |
| 6 | **Data-stream open sent once** — a lost open = silent dead scope at bring-up | `startCivDataTimer` re-sends open every 100 ms until the first CI-V frame | `Ic9700Civ._on_tick` open-retry at 100 ms until `frames > 0` |
| 7 | **tokrequest not validated** on the login reply (stale-reply hazard) | reference compares `tokrequest` | mismatching replies are ignored + logged |
| 8 | **Audio silence invisible** — a dead audio stream just sounded like a quiet band | `UdpAudio` watchdog alerts at 30 s | `Ic9700Audio._on_tick` logs once per 30 s-silent episode |
| 9 | **Loss/congestion untracked** | `packetsLost` / congestion % | `n_lost` counts radio retransmit-requests (our packets it lost) |
| 10 | **0x90 CONNINFO dropped silently** ("radio busy / in use by …") | full handler | recognized; logs the busy flag |

Plus the piece that motivated the audit (fixed just before it):
**token renewal** — `_send_token(0x05)` every 60 s once authenticated;
`response=0` restarts the clock, `0xFFFFFFFF` → re-login (#3 above).

## Deliberately NOT ported (with reasons)

| Reference behavior | Why skipped |
|---|---|
| Ping time-sync / latency estimation (`pingLatenessMs`, baselines) | Feeds SDR9700's Qt audio jitter buffer; our audio uses a drop-oldest realtime ring — nothing consumes latency estimates. Revisit only if audio quality degrades on high-latency links. |
| `splitWaterfall` scope fragmentation | Our spectrum consumer takes whole frames. |
| Audio `seqPrefix` 32-bit sequence extension | Only needed for seq-aware jitter analysis; the ring stores samples in arrival order. |
| Multi-radio `setCurrentRadio()` | The gate presents one radio per process by design. |
| TX audio path (`sendAudioBuffer`, PTT fade, DTMF) | The gate is deliberately RX-only until the TX arm/safety work lands. |
| Audio level metrics (RxPeak/RMS rolling buffers) | UI instrumentation. |
| Qt signal/slot plumbing, four-mutex lock split, try-lock timeouts | Language/framework idiom; our single-lock + threads are equivalent. |
| 10 s age-purge of TX history | Ours is count-bounded (500 entries) — memory already bounded; age only matters for not re-sending very stale packets, which the 500-cap effectively covers at our packet rates. |

## Corrections to the audit itself

Auditor claims we verified and **rejected**:
- "`_tx_hist` grows unbounded" — false; it's capped at `BUFSIZE=500` in
  `send_tracked` (the reference's cap, same value).
- "idle timer restart on send" — timing nuance with no protocol contract
  difference (both send idle at ≥10 Hz).

## Invariants worth keeping (learned the hard way)

- The radio wants the **ping(500ms)/idle(100ms)/retransmit(100ms)** cadence
  running continuously from before auth.
- **are-you-there is a discovery retry, not a keepalive** — the reference stops
  it on i-am-here. (A permanent 0x03 keepalive was tried; it does nothing.)
- **The session token is the master liveness** — renew at 60 s or the radio
  silently stops scope+audio at ~90 s while the transport looks healthy.
- Teardown order: **stream close (openclose 0x00) → token removal (0x01) →
  disconnect (0x05) → close the socket.** Skipping any of these leaves state
  on the radio that blocks the next connect ("phantom session").
