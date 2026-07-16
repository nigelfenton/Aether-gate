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
AUDIO_RATE = 24_000          # AE remote_audio_rx rate (must match core AUDIO_RATE)
CC_INTERVAL_S = 0.05         # EP2 C&C round-robin period (20 Hz) — see _cc_loop.
                             # Decoupled from EP6 on purpose: the radio free-runs
                             # the IQ stream, so sends must never pace reads.
RCVBUF_BYTES = 1 << 20       # 1 MB EP6 socket buffer: absorb scheduling jitter so
                             # a late drain loses nothing (default is far too small
                             # for 48-384 kHz of 1032-byte packets)


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
        self._retune_to = None             # pending centre change (applied in _cc_loop)
        self._gain_dirty = False           # AE moved the RF-gain slider (rebuild gain reg)
        self._sender = None                # EP2 C&C thread (decoupled from EP6)
        self._resettle = False             # _cc_loop -> reader: retuned, drop partial IQ
        self._board_id = None
        # --- audio / SSB demod state (mirrors the soapy adapter) ---
        import collections
        self._slice_hz = center_hz         # demod target (the slice freq)
        self._mode = "USB"
        self._audio_q = collections.deque(maxlen=64)   # IQ blocks queued for the demodulator
        self._nco_phase = 0.0              # persistent mixer phase (continuity across blocks)
        self._decim = None                 # samp_rate / AUDIO_RATE (48k/24k = 2)
        self._stage_firs = []              # [taps, overlap_state, M] per decimation stage
        self._iq_resid = None              # leftover IQ between audio calls
        self._audio_gain = 4.0             # post-demod gain (IQ already ~[-1,1] normalised)
        self._agc_level = 0.05
        self._agc_target = 0.25

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
        # Big RX buffer BEFORE bind: EP6 free-runs, so anything we don't drain in
        # time is dropped by the kernel, silently and unrecoverably.
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF_BYTES)
        except OSError:
            pass                            # best-effort; kernel may cap it
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

        # --- demod setup: staged anti-alias + decimate samp_rate -> 24 kHz ---
        # At 48 kHz this is just /2 (one cheap stage); wider rates factor into a
        # few small stages so the taps only run at progressively lower rates.
        self._decim = max(1, int(round(self.samp_rate / AUDIO_RATE)))    # 48k/24k = 2
        stages = self._factor_decim(self._decim)
        self._stage_firs = []
        for M in stages:
            ntaps = 4 * M + 1
            cutoff = 0.45 / M
            idx = np.arange(ntaps) - (ntaps - 1) / 2.0
            h = np.sinc(2 * cutoff * idx) * np.hamming(ntaps)
            h = (h / h.sum()).astype(np.float64)
            self._stage_firs.append([h, np.zeros(ntaps - 1, dtype=np.complex128), M])
        self._iq_resid = np.zeros(0, dtype=np.complex64)

        self._run = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="hpsdr-rx")
        self._reader.start()
        # EP2 egress runs on its OWN thread: the radio free-runs EP6, so the
        # reader must never be paced by our sends (see _cc_loop).
        self._sender = threading.Thread(target=self._cc_loop, daemon=True,
                                        name="hpsdr-cc")
        self._sender.start()

    def close(self):
        self._run = False
        if self._sender:
            self._sender.join(timeout=2)
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

    # --- EP2 egress: keep the C&C registers latched, INDEPENDENT of EP6 ------
    def _cc_loop(self):
        """Round-robin the three C&C registers on our own clock.

        This MUST NOT be driven by EP6 arrivals. HPSDR EP6 is a free-running
        stream — the radio emits it at the sample rate whether or not we send
        anything — so pacing sends 1:1 with reads (as the original loop did)
        throttles the reader to the send rate, overflows the socket buffer, and
        silently discards most of the stream. Measured on a Radioberry: a
        coupled loop drained ~4.8 kHz of a nominal 48 kHz (~10x slow), which
        time-compressed the IQ (WWV landed ~9x off frequency), broke FT8 decode,
        and starved audio into ~0.5 s on/off bursts. 20 Hz is ample to hold the
        registers latched; the reader is then free to drain at line rate.
        """
        speed = HZ_SPEED[self.samp_rate]
        # The three registers a working RX needs: config (Mercury+duplex), gain,
        # RX1 freq. Entry [2] is rebuilt on retune, [1] when the gain slider moves.
        cc_cycle = [hp.cc_config(speed), hp.cc_rx_gain(self.gain_db),
                    hp.cc_rx1_freq(int(self.center_hz))]
        ci = 0
        while self._run:
            if self._retune_to is not None:
                self.center_hz = float(self._retune_to)
                self._retune_to = None
                cc_cycle[2] = hp.cc_rx1_freq(int(self.center_hz))
                # tell the reader to re-settle + drop the partial block
                self._resettle = True
            if self._gain_dirty:
                self._gain_dirty = False
                cc_cycle[1] = hp.cc_rx_gain(self.gain_db)   # AE slider -> LNA reg
            self._send_cc(cc_cycle[ci % 3], cc_cycle[(ci + 1) % 3]); ci += 1
            time.sleep(CC_INTERVAL_S)

    # --- the persistent reader: drain EP6 IQ as fast as it arrives ----------
    def _read_loop(self):
        np = self._np
        buf = []                            # accumulate IQ into ~one FFT block
        BLOCK = 4096
        SETTLE_S = 0.4                      # discard early samples: let the NCO/AGC settle
        settle_from = time.monotonic()      # reset on each retune
        while self._run:
            # a retune happened (applied by _cc_loop) — drop partial IQ, re-settle
            if self._resettle:
                self._resettle = False
                buf = []; settle_from = time.monotonic()
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
                buf.append(complex(i, -q))     # conjugate: HPSDR IQ sideband is inverted
                                               # vs AE's convention (mirrors the spectrum;
                                               # fixes waterfall alignment + FT8 decode)
            if len(buf) >= BLOCK:
                # normalise 24-bit -> ~[-1,1] float32 complex for the core FFT
                blk = np.array(buf[:BLOCK], dtype=np.complex64) / hp.FULL_SCALE
                with self._lock:
                    self._latest = blk          # latest block -> panadapter FFT
                self._audio_q.append(blk)       # every block -> demod (continuous)
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

    # --- the AUDIO source (SSB demod; numpy only) -----------------------
    @staticmethod
    def _factor_decim(D):
        """Factor a decimation D into a few small stages (largest-first)."""
        factors = []
        for p in (5, 4, 3, 2):
            while D % p == 0 and D // p >= 1:
                factors.append(p); D //= p
        if D > 1:
            factors.append(D)
        return factors or [1]

    def set_slice(self, slice_hz):
        """Set the demod target. On HPSDR the NCO retunes the hardware itself, so
        the slice essentially IS the centre; if AE ever asks for a slice far off
        the centre, retune the NCO onto it."""
        self._slice_hz = float(slice_hz)
        if abs(self._slice_hz - self.center_hz) > 0.40 * self.samp_rate:
            self._retune_to = self._slice_hz

    def set_mode(self, mode):
        if mode:
            self._mode = mode.upper()

    def get_audio(self, n_samples, slice_hz=None, mode=None):
        """Return n_samples of 24 kHz mono audio demodulated from the live IQ.
        None until enough IQ is buffered. Mirrors the soapy SSB demod: mix the
        slice to baseband, staged-decimate to 24 kHz, take the real part (USB) /
        conj-real (LSB), then a light AGC."""
        np = self._np
        if np is None or not self._stage_firs:
            return None
        if slice_hz is not None:
            self.set_slice(slice_hz)
        if mode is not None:
            self._mode = mode.upper()

        need_in = n_samples * self._decim
        while len(self._iq_resid) < need_in and self._audio_q:
            self._iq_resid = np.concatenate([self._iq_resid, self._audio_q.popleft()])
        if len(self._iq_resid) < need_in:
            return None
        iq = self._iq_resid[:need_in].astype(np.complex128)
        self._iq_resid = self._iq_resid[need_in:]

        # 1) mix slice -> baseband (near-zero on HPSDR: NCO already centred on it)
        f_off = self._slice_hz - self.center_hz
        k = np.arange(len(iq))
        ph = self._nco_phase + 2.0 * np.pi * (-f_off) / self.samp_rate * k
        iq = iq * np.exp(1j * ph)
        self._nco_phase = (ph[-1] + 2.0 * np.pi * (-f_off) / self.samp_rate) % (2.0 * np.pi)

        # 2) staged anti-alias + decimate to 24 kHz
        sig = iq
        for fir in self._stage_firs:
            taps, state, M = fir
            x = np.concatenate([state, sig])
            y = np.convolve(x, taps, mode="valid")
            fir[1] = sig[-(len(taps) - 1):]
            sig = y[::M]
        base = sig[:n_samples]
        if len(base) < n_samples:
            base = np.concatenate([base, np.zeros(n_samples - len(base), dtype=base.dtype)])

        # 3) SSB demod
        if self._mode.startswith("LSB"):
            audio = np.real(np.conj(base))
        else:                                # USB / DIGU / default
            audio = np.real(base)

        audio = audio * self._audio_gain
        rms = float(np.sqrt(np.mean(audio * audio)) + 1e-9)
        a = 0.3 if rms > self._agc_level else 0.02
        self._agc_level = (1 - a) * self._agc_level + a * rms
        audio = audio * (self._agc_target / max(self._agc_level, 1e-4))
        np.clip(audio, -1.0, 1.0, out=audio)
        return audio.tolist()


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
