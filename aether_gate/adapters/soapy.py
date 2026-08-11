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


def rtl_bufflen(samp_rate, target_s=0.030):
    """USB transfer size (BYTES) giving ~target_s of signal per transfer.

    CS8 on the wire = 2 bytes per complex sample, so bytes = 2*rate*target.
    librtlsdr wants the length in 16384-byte granules (URB constraint), and
    16384 is also the practical floor. Examples at the 30 ms default:
      250 kS/s -> 16384 B  (32.8 ms/lump, ~30 updates/s)
      2.04 MS/s -> 114688 B (28.1 ms/lump) — vs the driver default 262144 B,
      which is 64 ms at 2.04M and a display-freezing 524 ms at 250k.
    """
    bl = int(2 * float(samp_rate) * target_s)
    return max(16384, (bl // 16384) * 16384)


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
        self._pan_shift_phase = 0.0         # NCO phase for the panadapter's offset-undo mix
        self._np = None
        # --- demod / audio state (SSB first) ---
        self._slice_hz = center_hz          # where to demodulate (the slice freq; core updates it)
        self._mode = "USB"                  # USB/LSB (others -> default to USB for now)
        self._audio_q = collections.deque(maxlen=64)  # raw IQ blocks queued for the demodulator
        self._nco_phase = 0.0               # persistent mixer phase (continuity across blocks)
        self._nco_ramp = None               # cached exp(1j*step*k); see _demod_block
        self._nco_ramp_n = 0                # block length the cached ramp was built for
        self._nco_ramp_step = None          # phase step the cached ramp was built for
        self._decim = None                  # samp_rate / AUDIO_RATE (integer-ish); set in open()
        self._stages = []                   # decimation factors per stage
        self._stage_firs = []               # [taps, overlap_state, M, stride_offs] per stage
        self._iq_resid = None               # leftover IQ samples between audio calls
        self._audio_gain = 60.0             # post-demod fixed gain (SSB baseband is small)
        self._agc_level = 0.05              # AGC running estimate of audio level
        self._agc_target = 0.25             # desired RMS-ish output level
        self._agc_gain = None               # last applied gain (per-sample ramp continuity)

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
        # Never trust the requested rate: drivers snap to their own rate table
        # (SDRplay honours only its discrete rates; a mismatch here plays audio
        # pitch-shifted by actual/assumed and mis-scales every spectrum bin).
        try:
            actual = float(self._sdr.getSampleRate(SOAPY_SDR_RX, 0))
        except Exception:
            actual = 0.0
        if actual > 0 and abs(actual - self.samp_rate) > 1.0:
            print(f"[soapy] device runs {actual:.0f} S/s (requested {self.samp_rate:.0f}) — using actual",
                  flush=True)
            self.samp_rate = actual
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

        # --- stream setup: size the USB transfer to the SAMPLE RATE ---------
        # librtlsdr hands data up in fixed-size USB transfers — 262144 bytes =
        # 131072 complex samples per lump by default, REGARDLESS of sample rate.
        # At 2.04 MS/s that is a 64 ms lump; at 250 kS/s it is a 524 ms lump: the
        # panadapter/audio can only be as fresh as the lumps, so the display
        # "ticks" every half-second while every layer above measures healthy.
        # (Measured 2026-07-16: reader avg 56 blocks/s but BURSTY — p50 gap
        # 0.01 ms, max 524.06 ms; the 20 Hz engine loop saw 2.0 fresh blocks/s.
        # With bufflen=16384 the gaps flatten to p50=32.6 max=33.1 ms.)
        # ⚠ bufflen must go in the STREAM args (setupStream) — SoapyRTLSDR
        # ignores it in the Device args, which is how this hid from an earlier
        # test. Honour an explicit bufflen/buffers from --soapy-args either way.
        stream_args = {}
        if self.driver == "rtlsdr":
            ua = {}
            for kv in (self.device_args or "").split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    ua[k.strip()] = v.strip()
            stream_args["bufflen"] = ua.get("bufflen") or str(rtl_bufflen(self.samp_rate))
            if "buffers" in ua:
                stream_args["buffers"] = ua["buffers"]
        self._stream = self._sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [], stream_args)
        self._sdr.activateStream(self._stream)

        self._init_demod()

        self._run = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _init_demod(self):
        """Build the staged-decimation + fractional-resampler audio chain.

        STAGED decimation (samp_rate -> ~AUDIO_RATE): a single huge FIR at
        2.048 MS/s is ~13x too slow on a Pi5 (audio starves -> popping), so
        decimate in cheap stages — each a short FIR then [::M], the expensive
        taps running at ever-lower rates.

        FLOOR (never round) the decimation so the post-decimation rate R is
        >= AUDIO_RATE, then a phase-continuous linear resampler maps R exactly
        onto the 24 kHz grid. round() bred a starvation clock: at 500 kS/s it
        picked 21, consuming 504 k/s from a 500 k/s tap — a 0.8% deficit that
        clicked every ~1.3 s regardless of band, mode or signal (found live
        with a sig gen on 2 m, 2026-08-01). At the 2.040 MS/s sweet spot
        (85 * 24 kHz exactly) the resampler ratio is 1.0 = a pass-through.
        """
        np = self._np
        self._decim = max(1, int(self.samp_rate // AUDIO_RATE))
        self._stages = self._factor_decim(self._decim)                  # e.g. 85 -> [5, 17]
        self._stage_firs = []          # per stage: [taps, overlap state, M, stride offset]
        for M in self._stages:
            # short anti-alias FIR for this stage: cutoff at the post-decimation Nyquist
            ntaps = 4 * M + 1
            cutoff = 0.45 / M                              # normalised to this stage's input rate
            idx = np.arange(ntaps) - (ntaps - 1) / 2.0
            h = (np.sinc(2 * cutoff * idx) * np.hamming(ntaps))
            h = (h / h.sum()).astype(np.float64)
            self._stage_firs.append([h, np.zeros(ntaps - 1, dtype=np.complex128), M, 0])
        self._pd_rate = self.samp_rate / self._decim       # post-decimation rate, >= AUDIO_RATE
        self._rs_ratio = self._pd_rate / AUDIO_RATE        # input samples per output sample (>= 1)
        self._rs_phase = 0.0                               # fractional read position carry-over
        self._ar_buf = np.zeros(0, dtype=np.float64)       # demodulated audio at _pd_rate
        # SSB sideband selection: a complex one-sided bandpass (lowpass taps
        # shifted to +1500 Hz -> passband ~0..3 kHz above the carrier for USB;
        # conjugate taps mirror it below for LSB). The previous 'demod' took
        # real(z) — and real(conj(z)) == real(z), so USB and LSB were byte-
        # identical and both sidebands folded together. Found by ear against a
        # sig gen (2026-08-01): "strangely in usb and lsb ... no difference".
        ssb_ntaps = 63
        k = np.arange(ssb_ntaps) - (ssb_ntaps - 1) / 2.0
        f_half = 1500.0 / self._pd_rate                    # half-width, normalised
        lp = np.sinc(2 * f_half * k) * np.hamming(ssb_ntaps)
        lp = lp / lp.sum()
        self._ssb_usb = (lp * np.exp(2j * np.pi * (1500.0 / self._pd_rate) * k)).astype(np.complex128)
        self._ssb_lsb = np.conj(self._ssb_usb)
        self._ssb_state = np.zeros(ssb_ntaps - 1, dtype=np.complex128)

        # --- NBFM: a REAL discriminator, not the SSB path ------------------
        # Everything that was not LSB used to fall through to the USB taps, so
        # asking for FM got an SSB product detector. That sounds plausible to
        # the ear — which is exactly why it survived — but it destroys the
        # 1200/2200 Hz Bell 202 tone pair AX.25 rides on, because those tones
        # live in the FM DEVIATION and slope-detecting them mangles their
        # relative amplitude and phase. Packet never decoded on 2 m for that
        # reason (found live 2026-08-07: clean-sounding audio, zero decodes).
        #
        # Channel filter BEFORE the discriminator. FM is not linear, so any
        # adjacent-channel energy reaching it intermodulates with the wanted
        # signal and cannot be filtered out afterwards. ~+/-8 kHz passband
        # covers Carson for 2.5-5 kHz deviation NBFM without clipping the
        # sidebands that carry the tones.
        fm_ntaps = 63
        kf = np.arange(fm_ntaps) - (fm_ntaps - 1) / 2.0
        fm_half = min(8000.0 / self._pd_rate, 0.45)        # normalised half-width
        fh = np.sinc(2 * fm_half * kf) * np.hamming(fm_ntaps)
        self._fm_taps = (fh / fh.sum()).astype(np.float64)
        self._fm_state = np.zeros(fm_ntaps - 1, dtype=np.complex128)
        # Discriminator continuity: the last sample of the previous block, so
        # angle(x[n] * conj(x[n-1])) is unbroken across block boundaries. A
        # reset here would inject a phase glitch every block — an audible tick
        # at the block rate, and a bit error in the middle of a packet.
        self._fm_prev = np.complex128(0)
        # De-emphasis is DELIBERATELY OFF for data. Broadcast/voice FM applies
        # 75 us (or 50 us) de-emphasis to undo transmit pre-emphasis, but AFSK
        # packet is not pre-emphasised: rolling off 2200 Hz relative to 1200 Hz
        # would skew the very tone ratio the demodulator downstream measures.
        self._fm_deemph = None

    def _demod_block(self, block):
        """One raw IQ block -> demodulated audio at _pd_rate (NCO + stages + SSB).
        Stride offsets carried per stage keep the [::M] comb aligned across
        arbitrary block boundaries."""
        np = self._np
        iq = block.astype(np.complex128)
        f_off = self._slice_hz - self.center_hz
        step = 2.0 * np.pi * (-f_off) / self.samp_rate
        # NCO BY CACHED RAMP, NOT PER-SAMPLE TRANSCENDENTAL. The mixer runs at
        # the FULL sample rate - 4096 samples per block, ~500 blocks/s - and
        # np.exp(1j*ph) over that measured 1.15 ms/block on a Pi 4: the single
        # largest item in get_audio, ~3 ms of a 5.33 ms real-time budget.
        #
        # For a FIXED offset the phase ramp is arithmetic:
        #   exp(j*(p0 + k*step)) == exp(j*p0) * exp(j*step)**k
        # so cache the unit-step ramp and rotate it by the start phase of the
        # block: one exp() per block instead of 4096, plus one multiply. The
        # ramp is rebuilt only when the offset or block length changes (i.e. on
        # retune), so a steady slice pays nothing. Measured 4.0x on the NCO.
        #
        # Phase continuity is UNCHANGED: _nco_phase still advances by exactly
        # len(iq)*step, so consecutive blocks still join seamlessly.
        n_iq = len(iq)
        if (self._nco_ramp is None or self._nco_ramp_n != n_iq
                or self._nco_ramp_step != step):
            self._nco_ramp = np.exp(1j * step * np.arange(n_iq))
            self._nco_ramp_n = n_iq
            self._nco_ramp_step = step
        iq = iq * (np.exp(1j * self._nco_phase) * self._nco_ramp)
        self._nco_phase = (self._nco_phase + step * n_iq) % (2.0 * np.pi)
        sig = iq
        for fir in self._stage_firs:
            taps, state, M, offs = fir
            x = np.concatenate([state, sig])
            # DECIMATE IN PLACE: compute ONLY the outputs that survive [::M].
            #
            # The previous form convolved the whole block and then discarded
            # (M-1)/M of the result. With a decimation that factors badly that is
            # ruinous - see _factor_decim: at 2.000 MS/s the decimation is
            # 2000000//24000 = 83, which is PRIME, so the "cheap stages" design
            # collapses to one full-rate FIR and a block costs 38.9 ms against a
            # 5.33 ms budget. Evaluating only the kept samples is 6.1x there.
            #
            # Identical output: same taps, same overlap-save state, same comb
            # phase. y[k] of the full 'valid' convolution is
            # dot(x[k:k+ntaps], taps[::-1]), and the survivors are k = offs,
            # offs+M, offs+2M, ... - so build that stride as a window matrix and
            # do one matmul.
            n_out = 0 if len(x) < len(taps) else len(x) - len(taps) + 1
            n_keep = 0 if n_out <= offs else (n_out - offs + M - 1) // M
            if n_keep > 0:
                starts = offs + np.arange(n_keep) * M
                win = x[starts[:, None] + np.arange(len(taps))]
                sig_next = win @ taps[::-1]
            else:
                sig_next = np.zeros(0, dtype=x.dtype)
            fir[1] = x[len(x) - (len(taps) - 1):]          # overlap-save (block-size safe)
            fir[3] = (offs - n_out) % M                    # comb phase into the next block
            sig = sig_next
        if self._is_fm_mode(self._mode):
            return self._demod_fm(sig)
        taps = self._ssb_lsb if self._mode.startswith("LSB") else self._ssb_usb
        x = np.concatenate([self._ssb_state, sig])
        y = np.convolve(x, taps, mode="valid")
        self._ssb_state = x[len(x) - (len(taps) - 1):]
        return 2.0 * np.real(y)                            # x2: real() halves the one-sided energy

    @staticmethod
    def _is_fm_mode(mode):
        """True for every mode AE may send that means 'frequency modulation'.

        AE sends the Flex data-mode variants too: DFM is FM-with-data-filters,
        NFM is narrow FM. All three want the discriminator. Anything else
        (LSB/USB/DIGU/DIGL/CW/AM/...) stays on the SSB path, which is also the
        safe fallback for a mode we do not model.
        """
        return (mode or "").upper() in ("FM", "FM-N", "NFM", "DFM")

    def _demod_fm(self, sig):
        """NBFM quadrature discriminator: angle(x[n] * conj(x[n-1])).

        The instantaneous frequency IS the phase advance between consecutive
        samples, so the product with the previous sample's conjugate gives the
        deviation directly. Carrying _fm_prev across blocks keeps that
        difference unbroken — see _init_demod for why that matters.
        """
        np = self._np
        # channel filter first (see _init_demod: FM is non-linear)
        x = np.concatenate([self._fm_state, sig])
        z = np.convolve(x, self._fm_taps, mode="valid")
        self._fm_state = x[len(x) - (len(self._fm_taps) - 1):]
        if len(z) == 0:
            return np.zeros(0, dtype=np.float64)
        prev = np.concatenate([[self._fm_prev], z[:-1]])
        self._fm_prev = z[-1]
        disc = np.angle(z * np.conj(prev))                 # radians/sample = deviation
        # radians/sample -> a normalised audio swing. Full scale is +/-pi, but
        # NBFM at 5 kHz deviation on a ~24 kHz grid only reaches ~pi*5/12, so
        # scale by pd_rate/(2*pi*peak_dev) to land near +/-1 rather than leaving
        # packet audio 4x too quiet for the AGC to sort out.
        # SCALE AGAINST THE FULL DISCRIMINATOR RANGE, NOT AGAINST PEAK DEVIATION.
        # angle() returns +/-pi, so dividing by pi maps the whole possible output
        # onto +/-1 and NOTHING can clip. The previous scaling (pd_rate /
        # (2*pi*peak_dev) = 0.76) was calibrated so a 5 kHz-deviation tone hit
        # full scale — but noise, whose phase steps are uniform over +/-pi, has
        # an RMS of pi/sqrt(3) = 1.81 rad/sample and therefore came out at 1.39,
        # i.e. HARD CLIPPED, while a real 3 kHz-deviation signal only reached
        # 0.79. The clipper ate the signal and passed the noise: measured RMS
        # 0.65 with 24% clipping on a quiet channel, identical at 6, 20 and
        # 40 dB of RF gain (found 2026-08-07 — RF gain having no effect at all
        # was the clue that the saturation was ours, not the front end's).
        disc = disc * (1.0 / np.pi)
        # DC block: any residual tuning offset shows up as a constant frequency
        # error, i.e. a DC term after the discriminator. Left in, it walks the
        # AFSK slicer's decision threshold off centre and costs bits. One-pole
        # highpass, ~10 Hz, well below the 1200 Hz mark tone.
        # VECTORISED, not a per-sample loop. A Python loop here would run at the
        # post-decimation rate on every block — the same shape of mistake that
        # starved the audio clock before (see _init_demod). Subtracting a
        # block-mean that is itself smoothed across blocks gives the same ~10 Hz
        # highpass behaviour with one numpy op.
        blk_mean = float(np.mean(disc))
        if getattr(self, "_fm_dc", None) is None:
            self._fm_dc = blk_mean
        # per-block one-pole toward the block mean: a = 1-exp(-2*pi*fc*N/fs)
        a = 1.0 - np.exp(-2.0 * np.pi * 10.0 * len(disc) / self._pd_rate)
        self._fm_dc += a * (blk_mean - self._fm_dc)
        return disc - self._fm_dc

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
                want = float(self._retune_to)
                try:
                    self._sdr.setFrequency(self._SOAPY_SDR_RX, 0, want)
                    # READ IT BACK. A silent 'except: pass' here left the tuner
                    # wherever it was while every layer above believed the retune
                    # had happened — the panadapter, the slice and AE all showed
                    # the new frequency and the receiver was still on the old one.
                    # Same lesson as setSampleRate: never trust a setter on this
                    # driver, and never swallow its failure.
                    try:
                        got = float(self._sdr.getFrequency(self._SOAPY_SDR_RX, 0))
                    except Exception:
                        got = want
                    self.center_hz = got
                    if abs(got - want) > 1000.0:
                        print(f"[soapy] RETUNE MISMATCH: asked {want/1e6:.6f} MHz, "
                              f"tuner reports {got/1e6:.6f} MHz", flush=True)
                    else:
                        print(f"[soapy] tuned to {got/1e6:.6f} MHz", flush=True)
                except Exception as e:
                    print(f"[soapy] RETUNE FAILED to {want/1e6:.6f} MHz: {e!r} "
                          f"(still on {self.center_hz/1e6:.6f} MHz)", flush=True)
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
            # OFFSET-TUNE: recentre NEAR the slice, never ON it.
            #
            # Every direct-conversion SDR has a DC spike at the centre of its
            # IQ — LO leakage and ADC offset, an artifact of the receiver and
            # not a signal. Retuning the hardware exactly onto the slice put
            # the demodulator on top of that spike: the S-meter read S9+20 (it
            # measures the artifact), the waterfall showed a bright line at the
            # cursor, and the audio contained nothing but the artifact. A real
            # S9+20 transmission six times over produced no measurable change
            # in the demodulated audio (found live 2026-08-07 on an RSP1a).
            #
            # Placing the centre a quarter-window away keeps the slice well
            # inside the usable passband while moving DC off it entirely.
            self._retune_to = slice_hz + self._dc_offset_hz()

    def _dc_offset_hz(self):
        """How far to put the hardware centre from the slice.

        A quarter of the sample rate: far enough that the DC spike is nowhere
        near the demodulated channel, close enough that the slice stays inside
        the 80% usable window even after the tuner rounds our request.
        """
        return 0.25 * self.samp_rate

    def retune(self, center_hz):
        # Legacy/explicit hardware recentre (e.g. a band-change pan set).
        #
        # OFFSET HERE TOO. Fixing only set_slice() left this path putting the
        # centre back exactly on the slice: the log showed a correct offset tune
        # to 145.510 immediately undone by a retune to 145.070, and the
        # demodulator was on the DC spike again. Any route that moves the
        # hardware has to respect the offset.
        center_hz = float(center_hz)
        if abs(center_hz - self._slice_hz) < 0.05 * self.samp_rate:
            center_hz = self._slice_hz + self._dc_offset_hz()
        self._retune_to = center_hz

    def set_mode(self, mode):
        self._mode = (mode or "USB").upper()

    def set_span(self, span_hz):
        """The pan window IS the device sample rate. get_iq hands the core
        full-rate blocks, so the engine must label the bins with the width the
        data actually covers. Before this, AE's default 250 kHz label sat on
        2.04 MHz of spectrum: every signal painted ~8x too narrow and a click
        on the pan tuned ~8x short of the signal — off-tuned SSB = robotic
        'Dalek' audio (found with a sig gen on 2 m, 2026-08-01)."""
        return float(self.samp_rate)

    # --- the IQ source (core FFTs this) ---------------------------------
    def get_iq(self, n, center_hz, span_hz):
        # If AE's centre moved, schedule the hardware to follow — through
        # retune(), which applies the DC offset. Assigning _retune_to directly
        # here was the third way the centre could land back on the slice, and
        # the one AE drives on every frame: the pan centre and the slice are the
        # same frequency whenever the operator has not scrolled the panadapter.
        if abs(center_hz - self.center_hz) > 1.0 and self._retune_to is None:
            self.retune(center_hz)
        with self._lock:
            blk = self._latest
        if blk is None:
            return None

        # UNDO THE DC OFFSET FOR THE PANADAPTER. The core FFTs this block and
        # labels the bins with AE's pan centre, so the samples must actually BE
        # centred there. Since offset tuning moved the hardware a quarter rate
        # away from the slice, handing the raw block over painted the waterfall
        # 510 kHz off: the signal appeared well away from the slice cursor while
        # the demodulator — which does its own NCO shift — heard it correctly.
        # Nigel spotted it as "the waterfall and signal are not in the same
        # place" (2026-08-07).
        #
        # Mixing by (center_hz - hardware centre) puts AE's requested centre at
        # DC, which is what the FFT assumes. The DC spike moves off-centre in
        # the display, which is correct and honest: that is where it really is.
        np = self._np
        if np is None:
            return blk
        delta = float(center_hz) - float(self.center_hz)
        if abs(delta) < 1.0:
            return blk
        n = len(blk)
        ph = self._pan_shift_phase + 2.0 * np.pi * (-delta) / self.samp_rate * np.arange(n)
        self._pan_shift_phase = float((ph[-1] if n else self._pan_shift_phase)
                                      % (2.0 * np.pi))
        return blk * np.exp(1j * ph)

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

        # rate-R audio needed in the buffer to interpolate n_samples on the 24 k grid
        need_r = int(np.ceil(self._rs_phase + n_samples * self._rs_ratio)) + 2
        while len(self._ar_buf) < need_r and self._audio_q:
            self._ar_buf = np.concatenate(
                [self._ar_buf, self._demod_block(self._audio_q.popleft())])
        if len(self._ar_buf) < need_r:
            return None                      # not enough IQ yet (stream still filling)

        # fractional resample _pd_rate -> AUDIO_RATE, phase-continuous across calls.
        # At the 2.040 MS/s sweet spot the ratio is exactly 1.0 -> pure pass-through.
        idx = self._rs_phase + np.arange(n_samples) * self._rs_ratio
        audio = np.interp(idx, np.arange(len(self._ar_buf)), self._ar_buf)
        nxt = self._rs_phase + n_samples * self._rs_ratio
        k = int(np.floor(nxt))
        self._ar_buf = self._ar_buf[k:]
        self._rs_phase = nxt - k

        if self._is_fm_mode(self._mode):
            # FM IS ALREADY LEVEL. The discriminator output depends on deviation,
            # not on received amplitude — that is the whole point of FM — so it
            # arrives near full scale and needs neither the x60 SSB baseband
            # gain (which would just clip it) nor an AGC.
            #
            # The AGC is actively HARMFUL here: AFSK slices on the relative
            # amplitude of the 1200/2200 Hz tones, and a gain that chases the
            # envelope across a packet moves that decision threshold mid-frame.
            # A fixed trim only.
            #
            # NO TRIM. _demod_fm normalises against the discriminator's full
            # +/-pi range, which already puts the output in the right place:
            # broadband noise lands at 1/sqrt(3) = 0.58 RMS and a 3 kHz-deviation
            # signal at 0.18. Those numbers look "quiet", and the temptation is
            # to multiply them up — a x3 trim did exactly that and put noise at
            # 1.73, i.e. 40% of samples clipped, undoing the scaling fix in the
            # same commit that made it.
            #
            # A narrowband signal being quieter than full-band noise is CORRECT
            # for FM: noise power grows with bandwidth, and the signal only wins
            # once it captures the discriminator. Preserving that ratio is the
            # whole point — the AFSK slicer needs the relative levels, not a
            # loud output.
            np.clip(audio, -1.0, 1.0, out=audio)
            return audio.tolist()

        audio = audio * self._audio_gain
        # simple AGC: track signal level, scale toward target (fast attack, slow release).
        # Apply the gain as a per-sample RAMP from the previous chunk's gain — a
        # stepped per-chunk gain modulates a steady carrier at the chunk rate
        # (20 ms chunks = 50 Hz flutter, heard on a sig gen 2026-08-01).
        rms = float(np.sqrt(np.mean(audio * audio)) + 1e-9)
        a = 0.3 if rms > self._agc_level else 0.02
        self._agc_level = (1 - a) * self._agc_level + a * rms
        g_new = self._agc_target / max(self._agc_level, 1e-4)
        g_old = self._agc_gain if self._agc_gain is not None else g_new
        audio = audio * np.linspace(g_old, g_new, len(audio))
        self._agc_gain = g_new
        np.clip(audio, -1.0, 1.0, out=audio)
        return audio.tolist()

    def read_meters(self):
        """S-meter from the demodulated slice, not the whole IQ block.

        This adapter reported nothing at all before, so AE's S-meter sat dead on
        an SDR gate. Measuring the FULL block (as the HPSDR adapter does) would
        read total power across the entire 2 MHz window, so a strong signal
        anywhere on the band would peg the meter while the slice was on a quiet
        channel — worse than useless for tuning.

        _ar_buf holds the audio already demodulated at the slice frequency, so
        its level tracks what the operator is actually listening to.

        UNCALIBRATED. There is no dBm reference for a Soapy front end whose gain
        we set ourselves, so this is a relative indication: the offset below
        merely places a typical signal in a plausible S-unit range. Do not treat
        it as an absolute measurement.
        """
        np = self._np
        if np is None:
            return Meters()
        with self._lock:
            blk = self._latest
        if blk is None or not len(blk):
            return Meters()

        # MEASURE RF POWER IN THE SLICE, NOT THE DEMODULATED AUDIO. Reading the
        # discriminator output backwards: full-band noise produces MORE audio
        # than a narrowband signal does (FM noise power grows with bandwidth),
        # so a quiet channel metered STRONGER than a real carrier — measured
        # -47 dBm on noise against -55 dBm on a clean FM signal.
        #
        # Goertzel-style: mix the slice to DC, low-pass by averaging over the
        # block, and take the power there. That is the energy in roughly the
        # audio bandwidth around the slice, which is what the operator is
        # listening to.
        f_off = self._slice_hz - self.center_hz
        n = min(len(blk), 8192)
        x = blk[-n:]
        k = np.exp(-2j * np.pi * f_off * np.arange(n) / self.samp_rate)
        # coarse channel power: |mean| picks the DC term after the mix
        p = abs(np.mean(x * k))
        rms = float(p) + 1e-12
        # UNCALIBRATED — no dBm reference exists for a front end whose gain we
        # set ourselves. The offset merely places a typical signal in a
        # plausible S-unit range; treat it as relative.
        dbm = 20.0 * np.log10(rms) - 10.0
        # Do not let our own RF gain masquerade as signal strength.
        dbm -= float(self.gain_db) - 20.0
        return Meters(s_meter_dbm=max(-140.0, min(0.0, dbm)))
