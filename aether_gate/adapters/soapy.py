#
# Aether-gate — SoapySDR adapter: live IQ from any SoapySDR device (RTL-SDR first).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""SoapyRTLSDR / SoapySDR IQ adapter — a `provides="iq"` source.

This is the real-hardware adapter and the fix for the PoC's ~1 fps: instead of
spawning `rtl_sdr` once per frame (re-opening USB + re-locking the tuner PLL each
time, ~0.8 s of pure overhead), it opens the device ONCE and runs a persistent
`readStream` loop on a background thread. The tuner stays locked, samples flow
continuously, and `get_iq()` just hands the core the latest block to FFT.

Covers any SoapySDR device via `--soapy-driver` (rtlsdr, airspy, sdrplay, ...);
RTL-SDR Blog V4 is the default/first target.

Dependency: the SoapySDR Python binding (`import SoapySDR`) + the device's Soapy
module (e.g. SoapyRTLSDR). Import is deferred to open() so the package stays
importable on hosts without Soapy (tests, the sim adapter).
"""
import collections
import threading
import time

from .base import RadioAdapter, AdapterCaps, Meters

AUDIO_RATE = 24000          # AE remote_audio_rx rate (must match core AUDIO_RATE)
SSB_BW_HZ = 2700.0          # SSB audio passband width


class SoapyAdapter(RadioAdapter):
    """Live IQ from a SoapySDR device. The core runs the FFT (provides='iq')."""

    provides = "iq"

    def __init__(self, driver="rtlsdr", device_args="", samp_rate=2_040_000,
                 gain_db=40.0, center_hz=14_100_000.0, model="FLEX-6700",
                 serial="GATE0001", station="aether-gate 1", direct_samp=None, agc=False):
        # NB default 2.040 MS/s (not 2.048) = EXACTLY 85 * 24 kHz, so audio decimation
        # is integer with no drift/underrun. RTL accepts it; panadapter span is fine.
        self.driver = driver
        self.device_args = device_args
        self.samp_rate = float(samp_rate)
        self.gain_db = float(gain_db)
        self.center_hz = float(center_hz)
        self.direct_samp = direct_samp      # RTL direct-sampling mode (Q=2 for HF on non-V4); None=auto
        self.agc = agc
        self.capabilities = AdapterCaps(model=model, serial=serial, station=station, tx_capable=False,
                                        min_span_hz=48_000.0, max_span_hz=samp_rate)
        self._sdr = None
        self._stream = None
        self._lock = threading.Lock()
        self._latest = None                 # most recent complex block (for the panadapter FFT)
        self._run = False
        self._reader = None
        self._retune_to = None              # pending centre change (applied in the reader thread)
        self._np = None
        # --- demod / audio state (SSB first) ---
        self._slice_hz = center_hz          # where to demodulate (the slice freq; core updates it)
        self._mode = "USB"                  # USB/LSB (others -> default to USB for now)
        self._audio_q = collections.deque(maxlen=64)  # raw IQ blocks queued for the demodulator
        self._nco_phase = 0.0               # persistent mixer phase (continuity across blocks)
        self._decim = None                  # samp_rate / AUDIO_RATE (integer-ish); set in open()
        self._stages = []                   # decimation factors per stage
        self._stage_firs = []               # [taps, overlap_state, M] per stage
        self._iq_resid = None               # leftover IQ samples between audio calls
        self._audio_gain = 60.0             # post-demod fixed gain (SSB baseband is small)
        self._agc_level = 0.05              # AGC running estimate of audio level
        self._agc_target = 0.25             # desired RMS-ish output level

    # --- lifecycle -------------------------------------------------------
    def open(self):
        import numpy as np                  # hard deps only when really running hardware
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
        self._np = np
        self._SOAPY_SDR_RX = SOAPY_SDR_RX
        self._SOAPY_SDR_CF32 = SOAPY_SDR_CF32

        args = dict(driver=self.driver)
        if self.device_args:
            for kv in self.device_args.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1); args[k] = v
        self._sdr = SoapySDR.Device(args)
        self._sdr.setSampleRate(SOAPY_SDR_RX, 0, self.samp_rate)
        self._sdr.setFrequency(SOAPY_SDR_RX, 0, self.center_hz)
        try:
            self._sdr.setGainMode(SOAPY_SDR_RX, 0, bool(self.agc))   # AGC on/off
        except Exception:
            pass
        if not self.agc:
            self._sdr.setGain(SOAPY_SDR_RX, 0, self.gain_db)
        if self.direct_samp is not None:                            # RTL HF direct-sampling (non-V4 dongles)
            try:
                self._sdr.writeSetting("direct_samp", str(self.direct_samp))
            except Exception:
                pass

        self._stream = self._sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self._sdr.activateStream(self._stream)

        # --- demod setup: STAGED decimation (samp_rate -> AUDIO_RATE) ---
        # A single huge FIR at 2.048 MS/s is ~13x too slow on a Pi5 (70ms/call vs 5.3ms
        # budget -> audio starves -> popping). Decimate in cheap stages instead: each
        # stage is a short half/quarter-band FIR then [::M], so the expensive taps only
        # ever run at progressively lower rates. 85 = 5 * 17; do 5 then 17.
        self._decim = max(1, int(round(self.samp_rate / AUDIO_RATE)))   # 85 for 2.048M/24k
        self._stages = self._factor_decim(self._decim)                  # e.g. [5, 17]
        rate = self.samp_rate
        self._stage_firs = []                                            # (taps, state) per stage
        for M in self._stages:
            # short anti-alias FIR for this stage: cutoff at the post-decimation Nyquist
            ntaps = 4 * M + 1
            cutoff = 0.45 / M                                            # normalised to this stage's input rate
            idx = np.arange(ntaps) - (ntaps - 1) / 2.0
            h = (np.sinc(2 * cutoff * idx) * np.hamming(ntaps))
            h = (h / h.sum()).astype(np.float64)
            self._stage_firs.append([h, np.zeros(ntaps - 1, dtype=np.complex128), M])
            rate /= M
        self._iq_resid = np.zeros(0, dtype=np.complex64)

        self._run = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self):
        self._run = False
        if self._reader:
            self._reader.join(timeout=2)
        try:
            if self._stream is not None:
                self._sdr.deactivateStream(self._stream)
                self._sdr.closeStream(self._stream)
        except Exception:
            pass
        self._sdr = self._stream = None

    # --- the persistent reader (this is what kills the per-frame PLL re-lock) --
    def _read_loop(self):
        np = self._np
        CHUNK = 4096
        buf = np.empty(CHUNK, dtype=np.complex64)
        # Optional read-loop instrumentation (AETHER_GATE_PROFILE=1): how often
        # does readStream actually hand us a block? The panadapter can only be as
        # fresh as this — a 20 fps engine loop re-FFTs stale IQ if this is slower.
        import os as _os, time as _time
        _prof = _os.environ.get("AETHER_GATE_PROFILE") == "1"
        _n_data = _n_none = _n_err = 0
        _t_read = 0.0
        _plast = _time.monotonic()
        while self._run:
            # apply any pending retune on this thread (avoid racing readStream)
            if self._retune_to is not None:
                try:
                    self._sdr.setFrequency(self._SOAPY_SDR_RX, 0, float(self._retune_to))
                    self.center_hz = float(self._retune_to)
                except Exception:
                    pass
                self._retune_to = None
            _t0 = _time.perf_counter() if _prof else 0.0
            sr = self._sdr.readStream(self._stream, [buf], CHUNK, timeoutUs=200000)
            n = sr.ret if hasattr(sr, "ret") else (sr[0] if isinstance(sr, tuple) else 0)
            if _prof:
                _t_read += _time.perf_counter() - _t0
                if n > 0: _n_data += 1
                elif n == 0: _n_none += 1
                else: _n_err += 1
            if n > 0:
                block = buf[:n].copy()
                with self._lock:
                    self._latest = block        # for the panadapter FFT (latest is fine)
                self._audio_q.append(block)     # for the demod (continuous — every block consumed)
            elif n < 0:
                time.sleep(0.001)           # overflow/timeout — keep the stream alive, don't spin hot
            if _prof:
                _tn = _time.monotonic()
                if _tn - _plast >= 5.0:
                    _el = _tn - _plast
                    _tot = _n_data + _n_none + _n_err
                    print(f"[prof-read] {_n_data/_el:6.1f} blocks/s "
                          f"(data={_n_data} ret0={_n_none} err={_n_err}) "
                          f"| readStream {_t_read/max(1,_tot)*1000:6.2f} ms avg "
                          f"| samples {_n_data*CHUNK/_el:9.0f}/s (rate {self.samp_rate:.0f})",
                          flush=True)
                    _n_data = _n_none = _n_err = 0
                    _t_read = 0.0
                    _plast = _tn

    @staticmethod
    def _factor_decim(D):
        """Factor a decimation D into a few small stages (largest-first ~ balanced)."""
        factors = []
        for p in (5, 4, 3, 2):
            while D % p == 0 and D // p >= 1:
                factors.append(p); D //= p
        if D > 1:
            factors.append(D)               # leftover prime (e.g. 17) as one stage
        return factors or [1]

    # --- control --------------------------------------------------------
    def set_slice(self, slice_hz):
        """Set the DEMOD target frequency. Tune in software within the existing IQ
        window; only physically retune the V4 if the slice nears the window edge
        (keeps the hardware centre stable so small tuning doesn't thrash the tuner)."""
        slice_hz = float(slice_hz)
        self._slice_hz = slice_hz
        # usable window = ~80% of the sample rate (avoid the filtered band edges)
        edge = 0.40 * self.samp_rate
        if abs(slice_hz - self.center_hz) > edge:
            # slice left the window -> recentre the hardware ON the slice
            self._retune_to = slice_hz

    def retune(self, center_hz):
        # Legacy/explicit hardware recentre (e.g. a band-change pan set).
        self._retune_to = float(center_hz)

    def set_mode(self, mode):
        self._mode = (mode or "USB").upper()

    # --- the IQ source (core FFTs this) ---------------------------------
    def get_iq(self, n, center_hz, span_hz):
        # If AE's centre moved, schedule the hardware to follow.
        if abs(center_hz - self.center_hz) > 1.0 and self._retune_to is None:
            self._retune_to = float(center_hz)
        with self._lock:
            blk = self._latest
        if blk is None:
            return None
        return blk                          # core/fft.iq_to_dbm resamples to n bins

    # --- the AUDIO source (SSB demod; numpy only) -----------------------
    def get_audio(self, n_samples, slice_hz=None, mode=None):
        """Return n_samples of 24 kHz mono audio (float, ~[-1,1]) demodulated from
        the live IQ at the slice frequency. None if not enough IQ buffered yet."""
        np = self._np
        if np is None or not self._stage_firs:
            return None
        if slice_hz is not None:
            self.set_slice(slice_hz)        # sets demod target + hardware retune if off-window
        if mode is not None:
            self._mode = mode.upper()

        # how many input samples we need for n_samples output after decimation
        need_in = n_samples * self._decim
        # drain queued IQ blocks into the residual buffer until we have enough
        while len(self._iq_resid) < need_in and self._audio_q:
            self._iq_resid = np.concatenate([self._iq_resid, self._audio_q.popleft()])
        if len(self._iq_resid) < need_in:
            return None                      # not enough IQ yet (stream still filling)

        iq = self._iq_resid[:need_in].astype(np.complex128)
        self._iq_resid = self._iq_resid[need_in:]

        # 1) mix the slice down to baseband: shift by (slice - hardware centre)
        f_off = self._slice_hz - self.center_hz
        k = np.arange(len(iq))
        ph = self._nco_phase + 2.0 * np.pi * (-f_off) / self.samp_rate * k
        iq = iq * np.exp(1j * ph)
        self._nco_phase = (ph[-1] + 2.0 * np.pi * (-f_off) / self.samp_rate) % (2.0 * np.pi)

        # 2) STAGED anti-alias + decimate (cheap: taps run at ever-lower rates)
        sig = iq
        for fir in self._stage_firs:
            taps, state, M = fir
            x = np.concatenate([state, sig])
            y = np.convolve(x, taps, mode="valid")       # len == len(sig)
            fir[1] = sig[-(len(taps) - 1):]              # save overlap state
            sig = y[::M]
        base = sig[:n_samples]
        if len(base) < n_samples:                        # pad a short tail block
            base = np.concatenate([base, np.zeros(n_samples - len(base), dtype=base.dtype)])

        # 3) SSB demod: USB = real part of the (already lowpassed) baseband; for LSB
        #    conjugate first (mirrors the sideband). Real part recovers the audio.
        if self._mode.startswith("LSB"):
            audio = np.real(np.conj(base))
        else:                                # USB / DIGU / default
            audio = np.real(base)

        audio = audio * self._audio_gain
        # simple AGC: track signal level, scale toward target (fast attack, slow release)
        rms = float(np.sqrt(np.mean(audio * audio)) + 1e-9)
        a = 0.3 if rms > self._agc_level else 0.02
        self._agc_level = (1 - a) * self._agc_level + a * rms
        audio = audio * (self._agc_target / max(self._agc_level, 1e-4))
        np.clip(audio, -1.0, 1.0, out=audio)
        return audio.tolist()

    def read_meters(self):
        return Meters()
