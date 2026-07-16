#
# Aether-gate — HPSDR adapter: live IQ from an HPSDR Protocol-1 (Metis) SDR.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""HPSDR Protocol-1 IQ adapter — a `provides="iq"` source.

Talks HPSDR/Metis (UDP :1024) to a Hermes-Lite 2 / Radioberry / original Hermes
/ Red Pitaya, so any HPSDR-1 SDR presents to AetherSDR as a Flex 6000. Like the
soapy adapter it opens the device once and runs a persistent background reader
(here a UDP EP6 loop), so `get_iq()` just hands the core the latest complex block
to FFT. RX-only for now (never sets the MOX bit); TX over HPSDR-1 is future work.

The wire protocol lives in hpsdr/hpsdr_proto.py (ported from the AE #4171 spike,
verified live vs a real HL2 and Nigel's Radioberry — WWV 10 MHz at baseband DC).

Bring-up recipe (the non-obvious bits, all in hpsdr_proto):
  discover (EF FE 02) -> start (EF FE 04 01) -> round-robin EP2 C&C:
  config (C1 CONFIG_MERCURY + C4 DUPLEX — MANDATORY or flat noise), RX1 freq,
  ADC/LNA gain -> ingest EP6 24-bit I/Q -> stop (EF FE 04 00) on close.

Dependency: numpy (only in open(), like soapy). Stdlib socket otherwise.
"""
import socket
import struct
import threading
import time

from ..base import RadioAdapter, AdapterCaps
from . import hpsdr_proto as hp

# HPSDR-1 sample rates (the `speed` code -> Hz). Metis: 00=48k 01=96k 02=192k 03=384k.
SPEED_HZ = {0: 48_000, 1: 96_000, 2: 192_000, 3: 384_000}
HZ_SPEED = {v: k for k, v in SPEED_HZ.items()}


class HpsdrAdapter(RadioAdapter):
    """Live IQ from an HPSDR Protocol-1 SDR. The core runs the FFT (provides='iq')."""

    provides = "iq"

    def __init__(self, radio_ip=None, local_ip=None, samp_rate=48_000,
                 gain_db=20, center_hz=14_100_000.0, model="FLEX-6700",
                 serial="GATEHPSD", station="Radioberry-HPSDR"):
        self.radio_ip = radio_ip           # None -> discover on the LAN
        self.local_ip = local_ip
        self.samp_rate = int(samp_rate) if int(samp_rate) in HZ_SPEED else 48_000
        self.gain_db = int(gain_db)
        self.center_hz = float(center_hz)
        # HPSDR span = the full sample rate (complex IQ). Min a sensible zoom floor.
        # native_centered_scope: the HPSDR NCO tune means the IQ is ALWAYS centered
        # on the tuned freq (WWV @ 10 MHz lands at baseband DC), so the pan must
        # re-centre on the VFO as AE tunes — else the cursor drifts within a fixed
        # frame and the pan sits off the receive freq.
        self.capabilities = AdapterCaps(model=model, serial=serial, station=station,
                                        tx_capable=False, native_centered_scope=True,
                                        min_span_hz=6_000.0, max_span_hz=self.samp_rate)
        self._sock = None
        self._dst = None                   # (radio_ip, 1024)
        self._np = None
        self._run = False
        self._reader = None
        self._lock = threading.Lock()
        self._latest = None                # most recent complex block for the panadapter FFT
        self._ep2_seq = 0
        self._retune_to = None             # pending centre change (applied in the reader)
        self._gain_dirty = False           # AE moved the RF-gain slider (rebuild gain reg)
        self._board_id = None

    # --- lifecycle -------------------------------------------------------
    def _discover(self, sock):
        """Broadcast/unicast HPSDR discovery; return the responding radio's IP, or None."""
        pkt = bytes([0xEF, 0xFE, 0x02]) + bytes(60)
        targets = [self.radio_ip] if self.radio_ip else ["255.255.255.255"]
        for t in targets:
            try:
                sock.sendto(pkt, (t, hp.METIS_PORT))
            except OSError:
                pass
        sock.settimeout(2.0)
        end = time.monotonic() + 2.5
        while time.monotonic() < end:
            try:
                d, a = sock.recvfrom(128)
            except socket.timeout:
                break
            # a discovery reply is EF FE 02/03 with a non-zero MAC at [3:9]
            if len(d) >= 11 and d[0] == 0xEF and d[1] == 0xFE and any(d[3:9]):
                self._board_id = d[10]
                return a[0]
        return None

    def open(self):
        import numpy as np                 # hard dep only when really running hardware
        self._np = np
        lip = self.local_ip or _local_ip()
        self.local_ip = lip
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind((lip, hp.METIS_PORT))

        ip = self.radio_ip or self._discover(s)
        if not ip:
            s.close()
            raise RuntimeError("no HPSDR device found on the LAN (is it powered on?)")
        self.radio_ip = ip
        self._dst = (ip, hp.METIS_PORT)
        # confirm it's up (single read-only discovery does NOT steal the stream)
        if self._board_id is None:
            self._discover(s)
        print(f"[hpsdr] {ip} board=0x{(self._board_id or 0):02x} "
              f"@ {self.samp_rate/1000:.0f} kHz, RX1={self.center_hz/1e6:.4f} MHz, "
              f"gain +{self.gain_db} dB", flush=True)

        self._sock = s
        s.settimeout(0.5)
        # START IQ, then PRIME: latch all three registers (config w/ Mercury+duplex,
        # gain, RX1 freq) twice up front via the round-robin, exactly like the proven
        # spike — a single config+freq without the gain register / full round-robin
        # left the tune not landing at DC.
        s.sendto(hp.metis_command(0x01), self._dst)
        speed = HZ_SPEED[self.samp_rate]
        regs = [hp.cc_config(speed), hp.cc_rx_gain(self.gain_db),
                hp.cc_rx1_freq(int(self.center_hz))]
        for k in range(6):
            self._send_cc(regs[k % 3], regs[(k + 1) % 3])

        self._run = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="hpsdr-rx")
        self._reader.start()

    def close(self):
        self._run = False
        if self._reader:
            self._reader.join(timeout=2)
        if self._sock is not None:
            try:
                self._sock.sendto(hp.metis_command(0x00), self._dst)   # STOP stream
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    # --- EP2 command send: round-robin two C&C registers per packet ------
    def _send_cc(self, cc_a, cc_b):
        try:
            self._sock.sendto(hp.ep2_packet(self._ep2_seq, cc_a, cc_b), self._dst)
        except OSError:
            pass
        self._ep2_seq = (self._ep2_seq + 1) & 0xFFFFFFFF

    # --- the persistent reader: ingest EP6 IQ + keep the C&C registers fresh --
    def _read_loop(self):
        np = self._np
        speed = HZ_SPEED[self.samp_rate]
        # Round-robin the three config registers so config (Mercury/duplex), RX1
        # freq and gain all stay applied. One register per EP2 frame; two per pkt.
        # Three registers a working RX needs, refreshed by round-robin: config
        # (Mercury+duplex), gain, RX1 freq. The freq entry [2] is rebuilt on retune.
        cc_cycle = [hp.cc_config(speed), hp.cc_rx_gain(self.gain_db),
                    hp.cc_rx1_freq(int(self.center_hz))]
        ci = 0
        buf = []                            # accumulate IQ into ~one FFT block
        BLOCK = 4096
        SETTLE_S = 0.4                      # discard early samples: let the NCO/AGC settle
        settle_from = time.monotonic()      # reset on each retune
        while self._run:
            # apply a pending retune (rebuild the freq register + re-settle)
            if self._retune_to is not None:
                self.center_hz = float(self._retune_to)
                self._retune_to = None
                cc_cycle[2] = hp.cc_rx1_freq(int(self.center_hz))
                buf = []; settle_from = time.monotonic()
            if self._gain_dirty:
                self._gain_dirty = False
                cc_cycle[1] = hp.cc_rx_gain(self.gain_db)   # AE slider -> LNA reg
            # SEND EP2 then RECEIVE (the spike's ordering) — round-robin config/
            # gain/freq so all three stay latched, paced 1:1 with the EP6 stream.
            self._send_cc(cc_cycle[ci % 3], cc_cycle[(ci + 1) % 3]); ci += 1
            try:
                d, _ = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if hp.ep6_seq(d) is None:
                continue
            if time.monotonic() - settle_from < SETTLE_S:
                continue                    # NCO/AGC still settling — drop these
            for i, q in hp.iq_samples(d):
                buf.append(complex(i, q))
            if len(buf) >= BLOCK:
                # normalise 24-bit -> ~[-1,1] float32 complex for the core FFT
                blk = np.array(buf[:BLOCK], dtype=np.complex64) / hp.FULL_SCALE
                with self._lock:
                    self._latest = blk
                buf = buf[BLOCK:]

    # --- control (AE -> radio) ------------------------------------------
    def retune(self, center_hz):
        self._retune_to = float(center_hz)

    def set_gain(self, rfgain):
        """Map AE's RF-gain slider (0..100) to the HPSDR LNA range (-12..+48 dB)
        and apply it live (the reader's round-robin re-latches the gain register).
        Takes effect on the next frame; no restart needed."""
        rfgain = max(0.0, min(100.0, float(rfgain)))
        self.gain_db = int(round(-12 + rfgain / 100.0 * 60))   # 0->-12dB, 100->+48dB
        self._gain_dirty = True

    def set_span(self, span_hz):
        """Follow AE's pan zoom onto the nearest HPSDR sample rate. Returns the
        effective full span (= the sample rate) so the engine advertises a
        bandwidth that matches the IQ width. Changing rate needs a restart, so
        for now we only report; live rate-switching is future work."""
        return float(self.samp_rate)

    def current_span_hz(self):
        """The IQ width the gate should advertise to AE (= the sample rate). AE
        never sends a bandwidth itself, so without this the gate advertises its
        default span while the data is 48 kHz — AE's frequency axis is then ~5x
        too wide and signals land at the wrong freq (FT8 shifts left)."""
        return float(self.samp_rate)

    # --- the IQ source --------------------------------------------------
    def get_iq(self, n, center_hz, span_hz):
        if abs(center_hz - self.center_hz) > 1.0 and self._retune_to is None:
            self._retune_to = float(center_hz)
        with self._lock:
            return self._latest            # core/fft.iq_to_dbm resamples to n bins


def _local_ip():
    """Source IP of the default route (the interface that reaches the LAN)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()
