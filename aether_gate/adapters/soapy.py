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
import threading
import time

from .base import RadioAdapter, AdapterCaps, Meters


class SoapyAdapter(RadioAdapter):
    """Live IQ from a SoapySDR device. The core runs the FFT (provides='iq')."""

    provides = "iq"

    def __init__(self, driver="rtlsdr", device_args="", samp_rate=2_048_000,
                 gain_db=40.0, center_hz=14_100_000.0, model="FLEX-6700",
                 serial="GATE0001", direct_samp=None, agc=False):
        self.driver = driver
        self.device_args = device_args
        self.samp_rate = float(samp_rate)
        self.gain_db = float(gain_db)
        self.center_hz = float(center_hz)
        self.direct_samp = direct_samp      # RTL direct-sampling mode (Q=2 for HF on non-V4); None=auto
        self.agc = agc
        self.capabilities = AdapterCaps(model=model, serial=serial, tx_capable=False,
                                        min_span_hz=48_000.0, max_span_hz=samp_rate)
        self._sdr = None
        self._stream = None
        self._lock = threading.Lock()
        self._latest = None                 # most recent complex block (numpy array)
        self._run = False
        self._reader = None
        self._retune_to = None              # pending centre change (applied in the reader thread)
        self._np = None

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
        while self._run:
            # apply any pending retune on this thread (avoid racing readStream)
            if self._retune_to is not None:
                try:
                    self._sdr.setFrequency(self._SOAPY_SDR_RX, 0, float(self._retune_to))
                    self.center_hz = float(self._retune_to)
                except Exception:
                    pass
                self._retune_to = None
            sr = self._sdr.readStream(self._stream, [buf], CHUNK, timeoutUs=200000)
            n = sr.ret if hasattr(sr, "ret") else (sr[0] if isinstance(sr, tuple) else 0)
            if n > 0:
                with self._lock:
                    self._latest = buf[:n].copy()
            elif n < 0:
                time.sleep(0.001)           # overflow/timeout — keep the stream alive, don't spin hot

    # --- control --------------------------------------------------------
    def retune(self, center_hz):
        self._retune_to = float(center_hz)  # picked up by the reader thread

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

    def read_meters(self):
        return Meters()
