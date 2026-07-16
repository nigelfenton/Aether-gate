#
# Aether-gate — HPSDR Protocol 1 (Metis) primitives.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
# Ported from the AetherSDR HL2 data-plane spike (aethersdr/AetherSDR PR #4171,
# prototypes/hl2/hpsdr.py, GPL-3.0), verified live against a real Hermes-Lite 2
# AND Nigel's Radioberry (10.0.0.224, board 0x06): WWV 10 MHz lands exactly at
# baseband DC, 0 dropped. Wire-protocol FACTS are clean-room from the HL2 wiki +
# pihpsdr (GPL-3.0) — see THIRD_PARTY_LICENSES. This is the data-plane engine the
# HpsdrAdapter drives; kept as a standalone module so it's independently testable.
#
"""HPSDR Protocol 1 (Metis) primitives for the HL2 spike — grounded, not guessed.

Register map sourced from the Hermes-Lite 2 wiki "Protocol" page and cross-checked
against the pihpsdr reference client's C&C construction (src/old_protocol.c),
consulted clean-room for wire-protocol facts only — see ../../THIRD_PARTY_LICENSES
(Principle I). Verified live against a real HL2 (WWV 10 MHz carrier lands exactly
at baseband DC; see README.md phase 0.3/0.4).

  C0 byte:  bits [6:1] = register ADDR[5:0], bit [0] = MOX (1=TX). So the C0 byte
            for a register = (addr << 1) | mox. Keep C0 EVEN → MOX=0 → never keys.
  addr 0x00 → C0 0x00 : config. C1 = speed(bits[1:0]) | CONFIG_MERCURY(0x40);
                        C4 = duplex(0x04) | ((#RX-1) & 0x7) << 3.
                        *** C1 bit6 (CONFIG_MERCURY) selects the ADC as the RX
                        source — WITHOUT it the DDC gets no input and the stream
                        is dead ADC-floor noise. This is the non-obvious must-set. ***
  addr 0x01 → C0 0x02 : TX1 NCO frequency (Hz, 32-bit)  ***DO NOT SET for RX***
  addr 0x02 → C0 0x04 : RX1 NCO frequency (Hz, 32-bit, big-endian in C1..C4)
  addr 0x0a → C0 0x14 : ADC gain. HL2 extended-range LNA: C4 = 0x40 | gain,
                        gain 0..60 = -12..+48 dB (i.e. code = dB + 12).

Register value is 32-bit; C1=bits[31:24], C2=[23:16], C3=[15:8], C4=[7:0].

Wire framing:
  metis command : EF FE 04 <cmd>  (pad 64)   cmd bit0 = IQ on/off
  EP2 (→radio)  : EF FE 01 02 | seq[4] | frame512 | frame512
  EP6 (←radio)  : EF FE 01 06 | seq[4] | frame512 | frame512
  each 512-B frame: 7F 7F 7F | C0 C1 C2 C3 C4 | 504 B payload
  RX sample (1 RX): I[3] Q[3] mic[2] = 8 B; I/Q are 24-bit signed big-endian.

Minimal working RX = round-robin three registers: config (with CONFIG_MERCURY),
RX1 freq, and ADC gain. Sending only freq (no Mercury bit) yields flat noise.
"""

import struct

METIS_PORT = 1024
SYNC = b"\x7f\x7f\x7f"
FULL_SCALE = 1 << 23  # 24-bit signed full scale

# C0 register-address bytes (address << 1, MOX=0).
C0_CONFIG = 0x00
C0_TX1_FREQ = 0x02   # avoid — transmit
C0_RX1_FREQ = 0x04
C0_ADC_GAIN = 0x14   # register 0x0a

CONFIG_MERCURY = 0x40   # C1 bit6: select ADC as RX source (mandatory for signal)
CONFIG_DUPLEX = 0x04    # C4 bit2: pihpsdr sets this on unconditionally


def metis_command(cmd: int) -> bytes:
    """EF FE 04 <cmd> padded to 64 B. cmd 0x01 = start IQ, 0x00 = stop."""
    return bytes([0xEF, 0xFE, 0x04, cmd]) + bytes(60)


def cc_config(speed: int = 0, n_rx: int = 1) -> bytes:
    """5-byte C&C for register 0x00: sample rate + receiver count + ADC select.
    C1 carries the speed AND CONFIG_MERCURY (without which there is no RX signal).
    C4 carries the duplex bit and the receiver count. MOX stays 0."""
    c1 = (speed & 0x3) | CONFIG_MERCURY
    c4 = CONFIG_DUPLEX | (((n_rx - 1) & 0x7) << 3)
    return bytes([C0_CONFIG, c1, 0x00, 0x00, c4])


def cc_rx1_freq(hz: int) -> bytes:
    """5-byte C&C for register 0x02: RX1 NCO frequency in Hz (32-bit BE). MOX 0."""
    return bytes([C0_RX1_FREQ]) + struct.pack(">I", hz & 0xFFFFFFFF)


def cc_rx_gain(db: int = 20) -> bytes:
    """5-byte C&C for register 0x0a: HL2 extended-range LNA gain, -12..+48 dB.
    C4 = 0x40 (enable direct AD9866 gain) | code, code = clamp(dB+12, 0, 60)."""
    code = max(0, min(60, db + 12))
    return bytes([C0_ADC_GAIN, 0x00, 0x00, 0x00, 0x40 | code])


def ep2_packet(seq: int, cc_a: bytes, cc_b: bytes) -> bytes:
    """EP2 host→radio packet: two frames, each carrying one C&C register.
    TX payload is zero (RX-only). cc_* must be exactly 5 bytes (C0..C4)."""
    assert len(cc_a) == 5 and len(cc_b) == 5
    frame_a = SYNC + cc_a + bytes(504)
    frame_b = SYNC + cc_b + bytes(504)
    return bytes([0xEF, 0xFE, 0x01, 0x02]) + struct.pack(">I", seq) + frame_a + frame_b


def parse_ep6(pkt: bytes):
    """Return (seq, n_samples, peak_abs, sumsq, sync_ok) or None if not an EP6 packet.
    Accumulates level stats rather than materializing all samples."""
    if len(pkt) < 1032 or pkt[0] != 0xEF or pkt[1] != 0xFE or pkt[2] != 0x01 or pkt[3] != 0x06:
        return None
    seq = struct.unpack(">I", pkt[4:8])[0]
    n, peak, sumsq, sync_ok = 0, 0, 0.0, True
    for fstart in (8, 520):
        frame = pkt[fstart:fstart + 512]
        if frame[0:3] != SYNC:
            sync_ok = False
            continue
        payload = frame[8:512]
        for k in range(0, 504, 8):
            i = int.from_bytes(payload[k:k + 3], "big", signed=True)
            q = int.from_bytes(payload[k + 3:k + 6], "big", signed=True)
            a = abs(i) if abs(i) > abs(q) else abs(q)
            if a > peak:
                peak = a
            sumsq += float(i) * i + float(q) * q
            n += 1
    return seq, n, peak, sumsq, sync_ok


# --- EP6 response telemetry (C&C bytes, the radio -> host direction) --------
#
# Every EP6 frame carries C0..C4 just like EP2 does, but INBOUND they are the
# radio's response registers, not our commands. HL2 (and the Radioberry, which
# mirrors the layout) alternate two register slots across successive frames:
#
#   C0 & 0xF8 == 0x08 :  C1:C2 = temperature      C3:C4 = forward power
#   C0 & 0xF8 == 0x10 :  C1:C2 = reverse power    C3:C4 = PA current
#
# C0's low 3 bits are the Radioberry's rb_control status word:
#   bit2 = pa_temp_ok   bit1 = CWX   bit0 = running
# (HL2 uses the same C0 slot ids; its status bits are its own — treat the low
# bits as informational, and key only off the 0x08/0x10 slot id.)
#
# Source: openHPSDR Protocol-1 / Hermes-Lite2 wiki "Protocol" (ACK==0 base memory
# map: response reg 0x01 = [31:16] temp, [15:0] fwd; reg 0x02 = [31:16] rev,
# [15:0] current), cross-checked against the Radioberry firmware's packing
# (radioberry.c, hpsdrdata[11..15] + coarse_pointer). Clean-room: wire FACTS only.
#
# ⚠ ZEROS ARE MEANINGFUL. A Radioberry WITHOUT the preAmp board has no MAX11613
# ADC, so its firmware never populates fwd/rev/current and they stay 0 forever,
# while temperature falls back to the RPi's own CPU temp. Verified live on
# Nigel's board 2026-07-16: both slots alternate correctly, temp ~1100 (=RPi
# CPU), fwd/rev/current all 0, pa_temp_ok=0 on every packet. So `has_sensors`
# below reports whether the numbers mean anything — do NOT drive a TX guard off
# a reading without checking it.

TELEM_SLOT_TEMP_FWD = 0x08     # C0 & 0xF8 -> C1:C2 temp,    C3:C4 fwd
TELEM_SLOT_REV_CUR = 0x10      # C0 & 0xF8 -> C1:C2 rev,     C3:C4 current

# Radioberry firmware's ADC encoding, from radioberry.c's own comment:
#   temperature == (((T*.01)+.5)/3.26)*4096   -> PA off above 50 C (raw 1256)
_TEMP_SCALE = 4096.0 / 3.26
TEMP_TRIP_RAW = 1256           # raw counts at 50 C (firmware disables the PA)


def temp_raw_to_c(raw: int) -> float:
    """Raw 12-bit ADC counts -> degrees C (inverse of the firmware's encoding).

    ⚠ The SAME encoding is used for the PA sensor and for the RPi CPU fallback,
    so a plausible-looking temperature does NOT tell you which one you are
    reading. Check has_sensors / fwd-rev-current instead.
    """
    return ((raw / _TEMP_SCALE) - 0.5) * 100.0


def parse_ep6_telemetry(pkt: bytes):
    """Decode the response C&C telemetry from an EP6 packet.

    Returns a dict of the fields present in THIS packet's two frames (a single
    packet may carry one slot, both, or neither), or None if not EP6:

        {"temp_raw": int, "temp_c": float, "fwd": int,      # from the 0x08 slot
         "rev": int, "current": int,                        # from the 0x10 slot
         "pa_temp_ok": bool, "cwx": bool, "running": bool,  # C0 low bits
         "slots": [0x08, 0x10]}

    Absent fields are simply missing — callers should accumulate across packets.
    Cheap: reads 5 bytes per frame, no IQ decode.
    """
    if (len(pkt) < 1032 or pkt[0] != 0xEF or pkt[1] != 0xFE
            or pkt[2] != 0x01 or pkt[3] != 0x06):
        return None
    out = {"slots": []}
    for fstart in (8, 520):
        if pkt[fstart:fstart + 3] != SYNC:
            continue
        c0 = pkt[fstart + 3]
        a = (pkt[fstart + 4] << 8) | pkt[fstart + 5]     # C1:C2
        b = (pkt[fstart + 6] << 8) | pkt[fstart + 7]     # C3:C4
        slot = c0 & 0xF8
        out["pa_temp_ok"] = bool(c0 & 0x04)
        out["cwx"] = bool(c0 & 0x02)
        out["running"] = bool(c0 & 0x01)
        if slot == TELEM_SLOT_TEMP_FWD:
            out["slots"].append(slot)
            out["temp_raw"] = a
            out["temp_c"] = temp_raw_to_c(a)
            out["fwd"] = b
        elif slot == TELEM_SLOT_REV_CUR:
            out["slots"].append(slot)
            out["rev"] = a
            out["current"] = b
    return out if out["slots"] else None


def swr_from_fwd_rev(fwd: int, rev: int):
    """SWR from raw forward/reverse readings, or None if it can't be computed.

    Returns None when fwd is 0 (not transmitting, or no sensor) — a caller must
    treat None as "unknown", NEVER as "good". rev >= fwd would be an infinite
    SWR; clamp to a large finite number so a UI can render it.
    """
    if fwd <= 0 or rev < 0:
        return None
    if rev >= fwd:
        return 99.9
    g = (rev / fwd) ** 0.5          # reflection coefficient (power ratio -> voltage)
    if g >= 1.0:
        return 99.9
    return min(99.9, (1.0 + g) / (1.0 - g))


def ep6_seq(pkt: bytes):
    """Sequence number of an EP6 packet, or None if it isn't one. Cheap — reads
    only the header, no per-sample decode (use when you want seq + iq_samples()
    without paying for parse_ep6's discarded level stats)."""
    if len(pkt) < 8 or pkt[0] != 0xEF or pkt[1] != 0xFE or pkt[2] != 0x01 or pkt[3] != 0x06:
        return None
    return struct.unpack(">I", pkt[4:8])[0]


def iq_samples(pkt: bytes):
    """Yield (I, Q) tuples from an EP6 packet — for FFT/spectrum use."""
    if len(pkt) < 1032 or pkt[0] != 0xEF or pkt[1] != 0xFE or pkt[2] != 0x01 or pkt[3] != 0x06:
        return
    for fstart in (8, 520):
        frame = pkt[fstart:fstart + 512]
        if frame[0:3] != SYNC:
            continue
        payload = frame[8:512]
        for k in range(0, 504, 8):
            i = int.from_bytes(payload[k:k + 3], "big", signed=True)
            q = int.from_bytes(payload[k + 3:k + 6], "big", signed=True)
            yield i, q
