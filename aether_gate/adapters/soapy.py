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
import math
import os as _os
import threading
import time

from .base import RadioAdapter, AdapterCaps, Meters

# How long a rate request must sit still before the reader thread acts on it.
# AE's pan zoom is a DRAG: it delivers a stream of bandwidth= commands, and each
# one applied would be a stop/set/rebuild/start cycle on the device. Trailing
# edge (not leading, as Hl2Backend uses) because the value the operator wants is
# the one they let go on, and a restart costs ~1 s here.
RATE_DEBOUNCE_S = 0.40

AUDIO_RATE = 24000          # AE remote_audio_rx rate (must match core AUDIO_RATE)

# How many consecutive readStream errors count as "transient" before backing
# off, and how many mean the device is gone for good. 20 fast retries is ~20 ms,
# comfortably longer than any real overflow; 2000 ends a hopeless loop rather
# than spinning at ~1 kHz forever when the SDR has been unplugged.
# HOW MANY readStream BLOCKS THE PANADAPTER FFT MAY SPAN.
#
# The reader hands back 4096 samples at a time, and get_iq used to return
# exactly one of them however many bins the pan asked for. That made the
# advertised bin width a fiction above 4096 bins: the true resolution was
# always samp_rate/4096 (30.5 Hz at 125 kHz) and iq_to_dbm merely interpolated
# up to the requested width. Found 2026-08-31 chasing a noise floor that did
# not move when the bin width supposedly changed 8x.
#
# Consecutive blocks come from one uninterrupted stream, so concatenating them
# is a real longer transform, not a stitch. Eight covers the 16384-bin ceiling
# with room to spare and costs 256 kB of complex64.
_PAN_RING_BLOCKS = 8

# ⚠ THE DEMODULATOR MUST NOT BE ALLOWED TO FALL BEHIND THE ANTENNA.
#
# _audio_q hands IQ blocks from the reader thread to the demodulator, which
# consumes them at exactly playback pace and never faster. So every block that
# queues up while the reader is stalled — an antenna switch, a rate change, a
# USB hiccup — stays queued for good: the audio simply runs that much late,
# permanently, and each further stall adds to it. The cap used to be 64 blocks
# of 4096 samples, a figure that is 131 ms at an RTL's 2.04 MS/s and 2.1 s at
# the 125 kS/s an SDRplay runs for fine bins. Measured 2026-09-01 on an RSPduo:
# audio trailing the panadapter by half a second, the panadapter itself prompt
# (it always takes the newest block). Bounded in TIME, so the rate cannot
# change what it means; anything older is dropped and logged.
_AUDIO_BACKLOG_S = 0.15

_ERR_FAST = 20
# ⚠ DECLARE THE DEVICE LOST ON ELAPSED TIME, NOT ERROR COUNT.
#
# Counting errors couples detection speed to the backoff schedule, and the two
# want opposite things: backing off hard saves CPU, but it also means fewer
# errors per second, so a count-based threshold arrives LATER the better the
# backoff works. Measured on a Pi 4: 40 errors took 15.3 s, not the ~5 s
# intended, because the sleep hits its 1 s ceiling by error 28 and the last
# dozen errors cost a second each. Nigel spotted it as "takes 14 seconds".
#
# 3 s of unbroken failure is comfortably longer than any real overflow and
# quick enough that an operator sees AE react rather than sit frozen.
_DEVICE_LOST_AFTER_S = 3.0
# ⚠ A DEVICE CAN FAIL WITHOUT EVER RETURNING AN ERROR.
#
# When an RSP re-enumerates on the USB bus (seen live 2026-08-11: kernel logs
# "USB disconnect" then a new device number with the SAME serial, while the
# SDRplay API logs "Device has been removed. Stopping."), readStream carries on
# returning SUCCESS at full rate - 2534 blocks/s, err=0 - handing back buffers
# whose contents never change. The engine loop stayed at 19.96 Hz and the
# freshness counter fell to 1/100: AE was fed a FROZEN frame at full frame
# rate, which is a worse failure than an error because nothing reports it.
#
# So staleness is its own liveness test, independent of return codes.
_STALE_AFTER_S = 3.0
# Try to REOPEN a dropped device before declaring it lost. This has to fire
# before _DEVICE_LOST_AFTER_S, or AE gets dropped for a fault we can fix in
# about a second.
_RECOVER_AFTER_S = 1.0
# Cooldown between attempts. A reopen costs ~1-2 s on an RSP, and hammering a
# device that really is unplugged is how you find new driver bugs.
_RECOVER_RETRY_S = 5.0
# ...escalating to this once several attempts in a row have failed, so an
# unplugged radio costs a line of log every half minute rather than every five
# seconds. (A previous spin-forever bug put 185,927 lines in a Pi 4's /tmp.)
_RECOVER_RETRY_MAX_S = 30.0
_ERR_GIVE_UP = 2000
from ..core.fft import dbm_offset_for, dbfs_to_dbm_for

# Bin powers in a noise-only FFT are exponentially distributed; their median is
# ln(2) times their mean. read_meters divides by this to turn a robust median
# into the mean power the noise actually carries.
_LN2 = math.log(2.0)

SSB_BW_HZ = 2700.0          # SSB audio passband width
# What the demodulator actually passes, as offsets from the slice frequency.
# These MUST track the filters built in _init_demod: the SSB path is a complex
# one-sided bandpass (lowpass taps of half-width 1500 Hz shifted to +1500 Hz ->
# 0..3 kHz above the carrier, conjugated to mirror it below for LSB), and the FM
# path is a +/-8 kHz channel filter. read_meters measures power over exactly
# this band so the S-meter reports what the operator is listening to.
SSB_PASS_HZ = 3000.0
FM_PASS_HZ = 8000.0


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
        self._latest = None                 # most recent complex block (meters, demod priming)
        # Recent blocks in arrival order, so the pan FFT can span more than one.
        self._pan_ring = collections.deque(maxlen=_PAN_RING_BLOCKS)
        self._run = False
        self._reader = None
        self._retune_to = None              # pending centre change (applied in the reader thread)
        self._gain_to = None                # pending RF gain dB (ditto — see set_gain)
        self._rate_to = None                # pending sample rate (ditto — see set_samp_rate)
        self._rate_req_at = 0.0             # monotonic stamp of the newest rate request
        self._setting_to = {}               # pending Soapy settings (ditto — see set_device_setting)
        # LNA state the dBm calibration belongs to, so a change can be called
        # out. Soapy cannot tell us what a state is worth in dB (see below).
        self._lna_state = "0"
        self._lna_cal_state = "0"
        self._antenna_to = None             # pending antenna port (ditto)
        self._gain_lo = 0.0                 # device gain range, filled in by _open_hw
        self._gain_hi = 50.0
        self._ae_center_hz = None           # last centre AE asked for, offset-free (see get_iq)
        self._pan_shift_phase = 0.0         # NCO phase for the panadapter's offset-undo mix
        self._np = None
        # --- demod / audio state (SSB first) ---
        self._slice_hz = center_hz          # where to demodulate (the slice freq; core updates it)
        self._mode = "USB"                  # USB/LSB (others -> default to USB for now)
        self.dbm_trim = 0.0                 # operator calibration, dB (see core.fft)
        self.dbm_base = dbfs_to_dbm_for(driver)  # this front end's dBFS->dBm anchor (ditto)
        self._audio_q = collections.deque()  # raw IQ blocks for the demodulator; see _queue_audio
        self._audio_dropped = 0             # blocks discarded to keep the demod current
        self._audio_drop_logged = 0.0       # monotonic stamp of the last drop log line
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

        self._open_hw()

        self._init_demod()

        self._run = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _open_hw(self):
        """Open the device and start its stream. Safe to call again after a loss.

        Everything from enumerate() to activateStream() lives here and only
        here, so the recovery path in _read_loop re-runs exactly the sequence
        that worked at startup instead of a hand-copied approximation of it.
        """
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
        args = dict(driver=self.driver)
        if self.device_args:
            for kv in self.device_args.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1); args[k] = v
        # PASS THE ENUMERATE RESULT THROUGH UNCHANGED. Rebuilding an
        # identical-looking dict from its keys() does NOT work: measured on an
        # RSPdx-R2, Device(enumerate()[0]) opens while Device({driver,label,
        # serial}) with the very same visible keys raises "no match" — the
        # object carries matching state keys() does not expose. And a near miss
        # is not a clean failure: SoapySDRPlay3 throws from its no-match path
        # while still holding sdrplay_api_LockDeviceApi() (Settings.cpp ~2051),
        # deadlocking the SDRplay API service for every later process until it
        # is restarted.
        wanted = {k: v for k, v in args.items() if k != "driver"}
        found = matched = None
        try:
            found = list(SoapySDR.Device.enumerate(dict(driver=self.driver)))
            for cand in found:
                have = {k: cand[k] for k in cand.keys()}
                if all(have.get(k) == v for k, v in wanted.items()):
                    args = cand          # the object itself, not a copy
                    matched = True
                    break
        except Exception:
            found = None    # enumerate itself failed — fall through as before
        # ⚠ NEVER HAND Device() ARGS THAT CANNOT MATCH.
        #
        # Same landmine as above, from the other side: on a no-match
        # SoapySDRPlay3 throws while still holding sdrplay_api_LockDeviceApi(),
        # wedging the API service for every process on the machine until it is
        # restarted. "The radio is unplugged" must therefore fail HERE, cleanly,
        # rather than one line later inside the driver. This matters most on the
        # recovery path below, which runs precisely when the device may be gone.
        if found is not None and not matched:
            raise RuntimeError(
                f"no {self.driver} device matches {wanted or 'driver=' + self.driver} "
                f"({len(found)} enumerated) — refusing to call Device(), which "
                f"would deadlock the SDRplay API service")
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
        # ⚠ SAY WHAT THE GAIN ACTUALLY ENDED UP AS, and never swallow a failure.
        #
        # Hardware AGC on an RSP swings the level by ~14 dB peak-to-peak on a
        # DEAD-STEADY sig-gen carrier (measured 2026-08-12 on an RSP1a: 13.99 dB
        # with AGC on vs 0.52 dB with it off, on the raw IQ before any of our
        # DSP). That is audible as a warble and it makes the S-meter meaningless,
        # so whether it is on is not a detail worth hiding behind `except: pass`.
        try:
            self._sdr.setGainMode(SOAPY_SDR_RX, 0, bool(self.agc))   # AGC on/off
        except Exception as e:
            print(f"[soapy] could NOT set AGC mode: {e!r} — the device keeps its "
                  f"default, which for SDRplay is AGC ON", flush=True)
        if not self.agc:
            self._sdr.setGain(SOAPY_SDR_RX, 0, self.gain_db)
        # ASK THE DEVICE ITS RANGE — do not guess one. AE sizes its RF Gain
        # slider from whatever we report to `display pan rfgain_info`, and an
        # RSPdx, an RTL dongle and an Airspy share no gain scale at all.
        # Span limits = the rates this device offers, now that AE's zoom can
        # change the rate (see set_span). Pinning max_span to the rate we happen
        # to be running would mean zooming IN stranded you there: the band-zoom
        # button reads max_span_hz, so it would only ever offer the width you
        # already had.
        _rates = self.supported_rates()
        if _rates:
            self.capabilities.min_span_hz = min(_rates)
            # Capped well below the device's ceiling (an RSPdx offers 10 MS/s).
            # A zoom-out is one click and the decimation chain grows with the
            # rate — _init_demod documents a single FIR at 2.048 MS/s already
            # being ~13x too slow on a Pi5. Explicit /resolution requests are
            # not bound by this; an accidental zoom-out should not be able to
            # ask for 10 MS/s of DSP.
            self.capabilities.max_span_hz = min(max(_rates), 2_000_000.0)
            print(f"[soapy] rates {min(_rates):.0f}..{max(_rates):.0f} S/s; "
                  f"zoom span capped at {self.capabilities.max_span_hz:.0f}", flush=True)
        try:
            _gr = self._sdr.getGainRange(SOAPY_SDR_RX, 0)
            self._gain_lo, self._gain_hi = float(_gr.minimum()), float(_gr.maximum())
        except Exception as e:
            print(f"[soapy] no gain range from the driver ({e!r}) — advertising "
                  f"{self._gain_lo:.0f}..{self._gain_hi:.0f} dB", flush=True)
        try:
            _agc_now = self._sdr.getGainMode(SOAPY_SDR_RX, 0)
            _g_now = self._sdr.getGain(SOAPY_SDR_RX, 0)
            print(f"[soapy] gain: AGC={_agc_now} overall={_g_now:.1f} dB "
                  f"(requested AGC={bool(self.agc)} gain={self.gain_db:.1f})", flush=True)
            if _agc_now and not self.agc:
                print("[soapy] ⚠ AGC is ON despite being disabled — expect a "
                      "warbling level on steady carriers", flush=True)
        except Exception:
            pass
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
        self._start_stream()

    def _start_stream(self):
        """setupStream + activateStream on the already-open device."""
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
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

    def _stop_stream(self):
        """Deactivate and close the stream, tolerating a driver that is upset."""
        if self._stream is not None:
            for fn in ("deactivateStream", "closeStream"):
                try:
                    getattr(self._sdr, fn)(self._stream)
                except Exception:
                    pass
            self._stream = None

    def _verify_stream(self, timeout_s=2.0):
        """Prove the stream is alive by actually reading IQ out of it.

        ⚠ A FAILED activateStream() IS NOT AN EXCEPTION ON THIS DRIVER.
        Measured live 2026-08-31: with the API service restarted underneath a
        running gate, SoapySDRPlay3 logged

            error in activateStream() - Init() failed: sdrplay_api_AlreadyInitialised

        and then RETURNED NORMALLY. The recovery path believed it, announced
        "back on the air", and looped seventeen times over ~50 s on a stream
        that never produced a single sample. Same lesson as setSampleRate and
        setFrequency elsewhere in this file: the only trustworthy answer this
        driver gives is data.
        """
        np = self._np
        buf = np.empty(4096, dtype=np.complex64)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                sr = self._sdr.readStream(self._stream, [buf], 4096, timeoutUs=200000)
            except Exception:
                return False
            n = sr.ret if hasattr(sr, "ret") else (sr[0] if isinstance(sr, tuple) else 0)
            if n > 0:
                return True
        return False

    def _recover_device(self):
        """Tear the stream down and start it again, WITHOUT touching the device.

        A marginal USB link drops the occasional bulk-IN transfer. Measured on
        this RSPdx-R2 (2026-08-31): the kernel logged ten `endpoint 0x81 ...
        transaction error | timeout` completions in fifteen minutes, under
        SDRconnect and SDR++ every bit as much as under the gate. The vendor
        applications ride those out. SoapySDRPlay3 does not — it prints
        "Device has been removed. Stopping." and every readStream after that
        fails forever, so a hiccup that costs SDRconnect a few milliseconds took
        the whole bridge off the air for the rest of the session.

        ⚠ DO NOT DROP THE Device REFERENCE TO REOPEN IT. That was the obvious
        fix and it is strictly worse than the bug. SoapySDRPlay3's destructor
        calls sdrplay_api_ReleaseDevice() and THROWS std::runtime_error when it
        fails; a C++ destructor is implicitly noexcept, so the throw does not
        become a Python exception, it calls std::terminate. Measured live
        2026-08-31 — `self._sdr = None` during recovery killed the whole gate:

            [ERROR] ReleaseDevice Error: sdrplay_api_ServiceNotResponding
            libc++abi: terminating due to uncaught exception of type
            std::runtime_error: ReleaseDevice() failed

        No try/except anywhere in Python can catch that, and the one moment we
        would ever want to reopen is exactly the moment ReleaseDevice is most
        likely to fail. So recovery stops at the stream: activateStream re-runs
        sdrplay_api_Init() on the device we already hold, which is what a USB
        hiccup actually needs, and every failure it can raise arrives as a
        catchable Python exception.
        """
        self._stop_stream()
        time.sleep(0.25)
        try:
            self._start_stream()
        except Exception as e:
            print(f"[soapy] stream restart raised: {e!r}", flush=True)
            return False
        return self._verify_stream()

    def _apply_samp_rate(self, want):
        """Change the sample rate. READER THREAD ONLY — it touches the stream.

        setSampleRate on a LIVE stream is ignored by SoapySDRPlay3, and the
        rate defines both the panadapter span (see set_span) and the whole
        audio decimation chain, so the order is forced: stop the stream, set,
        READ BACK (this driver's setters lie — see _verify_stream), rebuild
        the demod chain for the rate we actually got, then restart.

        Deliberately does NOT reopen the device — see _recover_device for why
        dropping the Soapy handle is a std::terminate, not an exception.
        """
        prev = self.samp_rate
        self._stop_stream()
        try:
            self._sdr.setSampleRate(self._SOAPY_SDR_RX, 0, want)
            got = float(self._sdr.getSampleRate(self._SOAPY_SDR_RX, 0))
        except Exception as e:
            print(f"[soapy] SET RATE FAILED at {want:.0f} S/s: {e!r} "
                  f"(still {prev:.0f} S/s)", flush=True)
            got = prev
        self.samp_rate = got if got > 0 else prev
        self._init_demod()                  # decimation chain is a function of the rate
        self._iq_resid = None               # leftovers are at the OLD rate: they would click
        self._audio_q.clear()               # ditto for anything already queued
        with self._lock:
            self._pan_ring.clear()          # concatenating across a rate change is a splice
        try:
            self._start_stream()
        except Exception as e:
            print(f"[soapy] stream restart after rate change raised: {e!r}", flush=True)
            return False
        ok = self._verify_stream()
        print(f"[soapy] sample rate -> {self.samp_rate:.0f} S/s (asked {want:.0f}, "
              f"was {prev:.0f}); stream {'ok' if ok else 'DEAD'}", flush=True)
        return ok

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

    def _meter_band_hz(self):
        """Offsets from the slice frequency that the demodulator passes.

        Deliberately mirrors the branch _init_demod/demod use to pick taps —
        `startswith("LSB")` for the lower sideband, everything else upper — so
        the meter measures the band the operator is actually hearing. That means
        DIGL meters as upper sideband, because the demodulator demodulates it as
        upper sideband; the two staying wrong together is better than the meter
        silently disagreeing with the audio.
        """
        if self._is_fm_mode(self._mode):
            return (-FM_PASS_HZ, FM_PASS_HZ)
        if (self._mode or "").upper().startswith("LSB"):
            return (-SSB_PASS_HZ, 0.0)
        return (0.0, SSB_PASS_HZ)

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
        consec_err = 0                      # consecutive readStream failures
        need_recover = False                # a reopen is owed (see below)
        last_recover = 0.0                  # monotonic stamp of the last attempt
        recover_n = 0                       # attempts so far, for the log
        recover_fail = 0                    # consecutive failed attempts
        err_since = 0.0                     # monotonic stamp of the first of them
        last_sig = None                     # fingerprint of the previous block
        fresh_at = _time.monotonic()        # when the samples last actually CHANGED
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
            # apply any pending gain change on this thread, for the same reason
            if self._gain_to is not None:
                want = float(self._gain_to)
                self._gain_to = None
                try:
                    self._sdr.setGain(self._SOAPY_SDR_RX, 0, want)
                    # Read it back — this driver's setters lie (see _verify_stream).
                    got = float(self._sdr.getGain(self._SOAPY_SDR_RX, 0))
                    self.gain_db = got          # keeps read_meters' gain term honest
                    if self.agc:
                        print(f"[soapy] gain -> {got:.1f} dB, but AGC IS ON so the "
                              f"hardware will override it", flush=True)
                    else:
                        print(f"[soapy] gain -> {got:.1f} dB (asked {want:.1f})",
                              flush=True)
                except Exception as e:
                    print(f"[soapy] SET GAIN FAILED at {want:.1f} dB: {e!r} "
                          f"(still {self.gain_db:.1f} dB)", flush=True)
            # apply any pending sample-rate change on this thread, for the same
            # reason — and because it has to bracket setupStream/activateStream.
            if (self._rate_to is not None
                    and _time.monotonic() - self._rate_req_at >= RATE_DEBOUNCE_S):
                want = float(self._rate_to)
                if abs(want - self.samp_rate) > 1.0:
                    self._apply_samp_rate(want)
                self._rate_to = None            # cleared LAST: it is set_samp_rate's done-signal
            # Device settings + antenna port, same thread for the same reason.
            # No debounce: these are discrete toggles, not a drag, and none of
            # them restarts the stream.
            if self._antenna_to is not None:
                want = self._antenna_to
                self._antenna_to = None
                try:
                    self._sdr.setAntenna(self._SOAPY_SDR_RX, 0, want)
                    got = str(self._sdr.getAntenna(self._SOAPY_SDR_RX, 0))
                    print(f"[soapy] antenna -> {got} (asked {want})", flush=True)
                except Exception as e:
                    print(f"[soapy] SET ANTENNA FAILED to {want}: {e!r}", flush=True)
            while self._setting_to:
                key, want = self._setting_to.popitem()
                try:
                    self._sdr.writeSetting(key, want)
                    # Read it back: this driver's setters lie (see _verify_stream).
                    got = str(self._sdr.readSetting(key))
                    if got != str(want):
                        print(f"[soapy] setting {key}: asked {want!r}, device "
                              f"reports {got!r}", flush=True)
                    else:
                        print(f"[soapy] setting {key} -> {got}", flush=True)
                    # A SETTING CAN MOVE THE GAIN, AND THIS DRIVER CANNOT SAY BY
                    # HOW MUCH.
                    #
                    # rfgain_sel is the LNA state: on an RSPdx, 28 steps of
                    # front-end attenuation worth tens of dB, written through
                    # here rather than through set_gain. So self.gain_db keeps
                    # whatever the operator last asked for while the real front
                    # end moves underneath it, and since dbm_offset_for backs
                    # gain out of BOTH scales, every dBm figure shifts with it.
                    #
                    # Reading getGain() back after the write -- which is what the
                    # set_gain path does, and what was tried here first -- makes
                    # it WORSE. Swept live on an RSPdx-R2 at 3.7 MHz
                    # (2026-08-31), rfgain_sel 0 -> 10:
                    #
                    #   uncompensated  floor slid -86.0 -> -111.6 dBm  (25.6 dB)
                    #   getGain said   12.0 -> 22.0 dB, i.e. gain went UP
                    #   compensated    floor slid -86.0 -> -121.6 dBm  (35.4 dB)
                    #
                    # More attenuation cannot be more gain, and 10 dB is not
                    # 25.6 dB either: this driver reports LNA-state gain with the
                    # wrong sign AND the wrong magnitude, so "correcting" by it
                    # added its 10 dB error on top of the real slide. The setters
                    # lie (see _verify_stream) and so does this getter.
                    #
                    # Compensating honestly needs the per-band LNA-state dB table
                    # from the SDRplay API, which Soapy does not expose. Until
                    # then, say so and leave the number alone: the dBm scale is
                    # calibrated for the LNA state it was calibrated at.
                    if str(key) == "rfgain_sel" and str(got) != str(self._lna_state):
                        print(f"[soapy] rfgain_sel {self._lna_state} -> {got}: the "
                              f"dBm scale is calibrated for LNA state "
                              f"{self._lna_cal_state} and does NOT track this. "
                              f"Re-trim, or compare levels only within one state.",
                              flush=True)
                        self._lna_state = str(got)
                except Exception as e:
                    print(f"[soapy] SET {key}={want!r} FAILED: {e!r}", flush=True)
            # ── REOPEN A DROPPED DEVICE INSTEAD OF GOING OFF THE AIR ──────
            # Set by either liveness test below. See _recover_device for why
            # reopening is the only way back once the driver has stopped.
            if need_recover:
                need_recover = False
                # Back off once attempts start failing: a radio that is really
                # unplugged should cost one log line every half minute, not one
                # every five seconds, and it costs nothing to keep trying — so
                # plugging it back in is enough on its own, with no restart.
                _wait = min(_RECOVER_RETRY_S * max(1, recover_fail),
                            _RECOVER_RETRY_MAX_S)
                if _time.monotonic() - last_recover >= _wait:
                    last_recover = _time.monotonic()
                    recover_n += 1
                    if recover_fail == 0 or recover_n % 10 == 0:
                        print(f"[soapy] stream is dead — restarting it "
                              f"(attempt {recover_n})", flush=True)
                    try:
                        ok = self._recover_device()
                    except Exception as e:
                        ok = False
                        print(f"[soapy] stream restart raised: {e!r}", flush=True)
                    if ok:
                        print(f"[soapy] stream restarted after {recover_n} "
                              f"attempt(s) — back on the air (verified by a live "
                              f"block, not by activateStream's word)", flush=True)
                        consec_err = recover_fail = 0
                        err_since = 0.0
                        last_sig = None
                        fresh_at = _time.monotonic()
                        self.device_lost = False
                        self.device_lost_reason = ""
                        continue
                    if not self.device_lost:
                        print("[soapy] the restarted stream produced no samples — "
                              "the radio is not there. Still retrying.", flush=True)
                    recover_fail += 1
                    self.device_lost = True
                    self.device_lost_reason = (
                        "the SDR stopped responding and its stream could not be "
                        "restarted")
            _t0 = _time.perf_counter() if _prof else 0.0
            sr = self._sdr.readStream(self._stream, [buf], CHUNK, timeoutUs=200000)
            n = sr.ret if hasattr(sr, "ret") else (sr[0] if isinstance(sr, tuple) else 0)
            if _prof:
                _t_read += _time.perf_counter() - _t0
                if n > 0: _n_data += 1
                elif n == 0: _n_none += 1
                else: _n_err += 1
            if n > 0:
                consec_err = 0              # a good read clears the backoff
                err_since = 0.0
                # LIVENESS BY CONTENT, NOT BY RETURN CODE. Two samples are
                # enough to tell one block of live IQ from another and cost
                # nothing per block; comparing the whole buffer would not be
                # affordable at ~500 blocks/s. Identical consecutive blocks mean
                # the hardware has stopped feeding us even though the driver
                # says otherwise.
                _sig = float(abs(buf[0])) + float(abs(buf[n // 2]))
                _now = _time.monotonic()
                if _sig != last_sig:
                    last_sig = _sig
                    fresh_at = _now
                elif (not self.device_lost) and (_now - fresh_at) >= _STALE_AFTER_S:
                    # Same fault as a hard read error, just wearing a success
                    # code — so it gets the same treatment: try to reopen before
                    # telling AE the radio is gone.
                    need_recover = True
                    print(f"[soapy] IQ has not changed for {_now - fresh_at:.1f}s "
                          f"while readStream still reports success — restarting "
                          f"the stream",
                          flush=True)
                    fresh_at = _now         # don't re-fire on every block
                block = buf[:n].copy()
                with self._lock:
                    self._latest = block        # for the meters (latest is fine)
                    self._pan_ring.append(block)
                self._queue_audio(block)        # for the demod (continuous — every block consumed)
            elif n < 0:
                # BACK OFF ON A PERSISTENT ERROR, AND GIVE UP ON A DEAD DEVICE.
                #
                # A 1 ms retry is right for a transient overflow/timeout, which
                # is what this branch was written for. It is badly wrong when
                # the device has GONE - unplugged, or reset by the host - because
                # that never recovers, and the loop then spins at ~1 kHz forever
                # printing the driver's error each time. Measured twice on
                # 2026-08-11: 42,291 lines filled a Pi 5's 2 GB /tmp, and an
                # RSP swap left 185,927 on a Pi 4. Meanwhile AE keeps painting
                # the last frame it received, so the operator sees a FROZEN
                # display rather than an error.
                consec_err += 1
                if err_since == 0.0:
                    err_since = _time.monotonic()
                if consec_err <= _ERR_FAST:
                    time.sleep(0.001)       # transient: retry immediately
                else:
                    # Escalate 10 ms -> 1 s so a long outage costs almost
                    # nothing, while a brief one still recovers quickly.
                    time.sleep(min(0.01 * (2 ** min(consec_err - _ERR_FAST, 7)), 1.0))
                    if consec_err == _ERR_FAST + 1 or consec_err % 200 == 0:
                        print(f"[soapy] read error x{consec_err} — backing off "
                              f"(device unplugged or reset?)", flush=True)
                    # Tell the core EARLY. Waiting for the give-up would leave
                    # AE staring at a frozen waterfall for half an hour; a few
                    # seconds of solid failure is already enough to say the
                    # radio is not there.
                if err_since and _time.monotonic() - err_since >= _RECOVER_AFTER_S:
                    need_recover = True
                if (not self.device_lost and err_since
                        and _time.monotonic() - err_since >= _DEVICE_LOST_AFTER_S):
                    self.device_lost = True
                    self.device_lost_reason = (
                        "the SDR stopped responding (unplugged, or reset by the host)")
                if consec_err >= _ERR_GIVE_UP:
                    # Stop rather than spin forever. The gate stays up and AE
                    # sees the stream end, which is honest; a restart (or a
                    # reconnect from the setup page) re-opens the device.
                    print(f"[soapy] giving up after {consec_err} consecutive read "
                          f"errors — the device is gone. Restart the gate once it "
                          f"is plugged back in.", flush=True)
                    self.device_lost = True
                    self.device_lost_reason = (
                        f"the SDR stopped responding after {consec_err} read errors "
                        f"(unplugged, or reset by the host)")
                    self._run = False
                continue
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

    def gain_range(self):
        """(low_db, high_db, step_db) for AE's `display pan rfgain_info`.

        AE asks this once per panadapter and uses the answer as the RF Gain
        slider's travel. Left unanswered it keeps the Flex 6000 default of
        -8..32 in steps of 8 (AetherSDR's PanadapterModel) — five positions, on
        a scale that has nothing to do with an SDR front end, and every value it
        then sends lands outside what the device will accept.
        """
        return (int(math.floor(self._gain_lo)), int(math.ceil(self._gain_hi)), 1)

    def set_gain(self, gain_db):
        """AE's RF Gain slider. The value is dB, in the range gain_range() gave.

        ⚠ dB, NOT 0..100. AetherSDR sends the operator's value in the range the
        backend advertised (IRadioBackend::setPanRfGain -> RadioModel's
        `display pan set %1 rfgain=%2`), so treating it as a percentage silently
        rescales every setting.

        ⚠ APPLIED ON THE READER THREAD, not here. This runs on the TCP command
        thread, and SoapySDRPlay3 is not safe against a setter racing an
        in-flight readStream — the same reason retune() defers, and the retune
        storm that knocked the device off the bus was found the same way. Hand
        the reader a value and let it land between reads.
        """
        lo, hi, _ = self.gain_range()
        self._gain_to = max(float(lo), min(float(hi), float(gain_db)))

    def device_controls(self):
        """What THIS device offers beyond the Flex protocol's vocabulary.

        Antenna port, bias-T, the MW/FM/DAB notches, IF mode — an RSPdx has all
        of these and "display pan set" has no verb for any of them, so they can
        only ever reach the operator through the gate's own surface.

        Asked of the driver, never assumed: this same file already paid for
        guessing a device's sample rates instead of calling listSampleRates.
        Every value is read back from the device, so the panel shows what is
        actually set rather than what we last sent.
        """
        if self._sdr is None:
            return {}
        rx = self._SOAPY_SDR_RX
        out = {}
        try:
            ants = [str(a) for a in self._sdr.listAntennas(rx, 0)]
            if ants:
                out["antenna"] = {"value": str(self._sdr.getAntenna(rx, 0)),
                                  "options": ants}
        except Exception:
            pass
        settings = []
        try:
            for info in self._sdr.getSettingInfo():
                key = str(info.key)
                item = {"key": key, "name": str(info.name) or key,
                        "type": str(info.type)}
                opts = [str(o) for o in info.options] if info.options else []
                if opts:
                    item["options"] = opts
                # The driver's own bounds for a numeric setting. Without them
                # a panel has to guess a range, and a guess clamps in both
                # directions: a write outside it is capped and a read-back
                # outside it is displayed wrong. Only sent when the driver
                # actually bounded it (Soapy's default range is 0..0).
                try:
                    r = info.range
                    lo, hi, st = float(r.minimum()), float(r.maximum()), float(r.step())
                    if hi > lo:
                        item["range"] = {"min": lo, "max": hi, "step": st}
                except Exception:
                    pass
                try:
                    item["value"] = str(self._sdr.readSetting(key))
                except Exception:
                    continue          # write-only or unreadable: not a control
                settings.append(item)
        except Exception:
            pass
        if settings:
            out["settings"] = settings
        return out

    def set_antenna(self, name):
        """Queue an antenna-port change; the reader thread applies it.

        Not blocking and not verified here — the caller re-reads
        device_controls() to see what the device took, which is the same
        read-back-don't-trust rule the rest of this adapter runs on.
        """
        if self._sdr is None or not name:
            return False
        self._antenna_to = str(name)
        return True

    def set_device_setting(self, key, value):
        """Queue a Soapy setting write (bias-T, notches, HDR, AGC setpoint...).

        Values go as strings — Soapy's setting ABI is stringly typed, and
        booleans must be "true"/"false" rather than Python's "True"/"False",
        which the driver does not parse.
        """
        if self._sdr is None or not key:
            return False
        if isinstance(value, bool):
            value = "true" if value else "false"
        self._setting_to[str(key)] = str(value)
        return True

    def diagnostics(self):
        """'What the gate sees from the radio' for the diagnostics panel."""
        if self._sdr is None:
            return {"radio": f"soapy:{self.driver}",
                    "link": {"transport": "soapy", "state": "closed"}}
        rates = self.supported_rates()
        d = {
            "radio": f"soapy:{self.driver}",
            "link": {"transport": "soapy", "host": self.device_args or self.driver,
                     "state": "open" if self._stream is not None else "no stream"},
            "vfos": [{"name": "Tuner", "freq_hz": self.center_hz,
                      "mode": self._mode, "selected": True}],
            "scope": {"bins": None, "span_hz": self.samp_rate,
                      "samp_rate": self.samp_rate,
                      "rates": [int(round(r)) for r in rates]},
            "rx_controls": {"rf_gain_db": self.gain_db, "agc": self.agc,
                            "gain_range_db": [self._gain_lo, self._gain_hi]},
            "audio": {"decim": self._decim, "post_decim_rate": self._pd_rate},
        }
        try:
            d["link"]["detail"] = str(self._sdr.getHardwareKey())
        except Exception:
            pass
        controls = self.device_controls()
        if controls:
            d["device"] = controls
        return d

    def set_mode(self, mode):
        self._mode = (mode or "USB").upper()

    def current_span_hz(self):
        """The span our IQ actually covers — the device sample rate.

        The core seeds the pan from this when a slice is created, because AE
        NEVER SENDS A BANDWIDTH of its own (see engine.py "[radio-wins] pan span
        seeded from adapter"). Without it the pan keeps AE's 0.25 MHz default
        while the data covers the full sample rate, so the frequency axis is
        wrong: signals paint too narrow and a click on the pan tunes short.

        ⚠ This hook existed and this adapter simply did not implement it. At
        2.04 MS/s the error was easy to miss; at 0.768 MS/s the pan read 0.25
        MHz over 0.768 MHz of data — a 3x axis error — which is what exposed it
        (2026-08-12). `set_span` alone is not enough: it is only called when the
        operator zooms, and AE does not zoom on connect.
        """
        return float(self.samp_rate)

    def set_span(self, span_hz):
        """AE zoomed the panadapter — retune the device to match.

        The pan window IS the device sample rate. get_iq hands the core
        full-rate blocks, so the engine must label the bins with the width the
        data actually covers. Before that was honoured, AE's default 250 kHz
        label sat on 2.04 MHz of spectrum: every signal painted ~8x too narrow
        and a click on the pan tuned ~8x short of the signal — off-tuned SSB =
        robotic 'Dalek' audio (found with a sig gen on 2 m, 2026-08-01).

        The corollary went unimplemented until 2026-08-31: if the span is the
        rate, then a zoom IS a rate change, and this returned the current rate
        while discarding the request. AE's zoom reached the gate and died here,
        one call short of the radio, so the only way to change resolution was
        to restart the gate.

        RETURNS THE RATE RUNNING RIGHT NOW, NOT THE REQUEST. The change is
        applied by the reader thread after a debounce, so the core labels the
        bins with the width the IQ currently has and re-advertises when the new
        rate actually lands (see the span sync in stream_loop). This is the
        contract AetherSDR already documents for the seam — IRadioBackend.h:
        "hz is a REQUEST: a backend whose hardware offers a fixed set of rates
        snaps to the nearest one it can actually run".

        Deliberately NON-BLOCKING: this runs on the TCP command thread, and
        waiting here for a ~1 s stream restart would stall every other command
        AE has in flight.
        """
        if span_hz and self._sdr is not None:
            snapped = self._request_rate(span_hz)
            if snapped is not None and abs(snapped - self.samp_rate) > 1.0:
                print(f"[soapy] AE zoom -> {snapped:.0f} S/s "
                      f"(asked {float(span_hz):.0f}, now {self.samp_rate:.0f})",
                      flush=True)
        return float(self.samp_rate)

    def supported_rates(self):
        """Sample rates this device will actually accept, ascending.

        Asked of the driver, never hardcoded: SoapySDRPlay3 takes decimations
        of 2 MS/s, an RTL stick takes an entirely different set.
        """
        if self._sdr is None:
            return []
        try:
            return sorted(float(r) for r in
                          self._sdr.listSampleRates(self._SOAPY_SDR_RX, 0)
                          if float(r) > 0)
        except Exception:
            return []

    def _request_rate(self, rate_hz):
        """Snap a rate request onto the device's grid and queue it.

        Shared by the operator's explicit control (set_samp_rate) and AE's pan
        zoom (set_span). Stamping the request is what makes the reader thread's
        debounce work: a zoom DRAG delivers ~30 of these a second and each one
        would otherwise be a full stream restart.

        Returns the snapped rate, or None if no device is open.
        """
        if self._sdr is None:
            return None
        want = float(rate_hz)
        rates = self.supported_rates()
        snapped = min(rates, key=lambda r: abs(r - want)) if rates else want
        if rates and abs(snapped - want) > 1.0:
            print(f"[soapy] sample rate {want:.0f} unsupported — snapping to "
                  f"{snapped:.0f} (device offers: "
                  f"{', '.join(f'{r:.0f}' for r in rates)})", flush=True)
        self._rate_to = snapped
        self._rate_req_at = time.monotonic()
        return snapped

    def set_samp_rate(self, rate_hz, wait_s=5.0):
        """Ask for a new sample rate; the reader thread applies it.

        The rate IS the panadapter span here (see set_span), so this is the
        operator's resolution knob: bin width = rate / bins.

        ⚠ SNAPS TO A SUPPORTED RATE — never passes the request through. An
        unsupported rate is not an error on this driver: setSampleRate logs
        "[WARNING] invalid sample rate. Sample rate unchanged." and returns,
        leaving the device where it was. Asking an RSPdx for 256 kS/s (a
        plausible-looking number that is not a 2 MS/s decimation) left it at
        2 MS/s — the operator asked for finer bins and silently got bins 4x
        COARSER, with only a driver warning to say so (2026-08-31).

        Blocks until the reader thread has applied it, then returns the rate
        the device ACTUALLY reports, or None if no device is open.
        """
        snapped = self._request_rate(rate_hz)
        if snapped is None:
            return None
        # Wait for the reader thread to land it. The core re-advertises the pan
        # geometry from our samp_rate the moment we return, and labelling new
        # data with the old span is precisely the axis error current_span_hz
        # exists to prevent — so return truth, not the request.
        deadline = time.monotonic() + float(wait_s)
        while self._rate_to is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._rate_to is not None:
            print(f"[soapy] sample-rate change to {snapped:.0f} still pending after "
                  f"{wait_s:.0f}s — reader thread is not consuming", flush=True)
        return self.samp_rate

    # --- the IQ source (core FFTs this) ---------------------------------
    def get_iq(self, n, center_hz, span_hz):
        # If AE's centre moved, schedule the hardware to follow — through
        # retune(), which applies the DC offset. Assigning _retune_to directly
        # here was the third way the centre could land back on the slice, and
        # the one AE drives on every frame: the pan centre and the slice are the
        # same frequency whenever the operator has not scrolled the panadapter.
        #
        # COMPARE AE-TO-AE, NOT AE-TO-HARDWARE. self.center_hz is the *hardware*
        # centre, which offset-tunes a quarter sample rate away from the slice to
        # keep the DC spike off it. Comparing AE's offset-free request against it
        # left a permanent samp_rate/4 gap that this test could never close, so
        # every frame scheduled another retune to the frequency the tuner was
        # already on: 1419 setFrequency calls in 85 s, all to 3.922500 MHz, which
        # destabilised the SDRplay API until the device dropped off the bus
        # (found live 2026-08-31 on an RSPdx-R2 at 250 kHz). Remembering what AE
        # last asked for is what "AE's centre moved" actually means.
        if (self._ae_center_hz is None
                or abs(center_hz - self._ae_center_hz) > 1.0) \
                and self._retune_to is None:
            self._ae_center_hz = center_hz
            self.retune(center_hz)
        # Serve the FFT the length it asked for, newest samples last, so the
        # bin width the pan advertises is the bin width it actually has. Short
        # of that (right after a start or a rate change) hand back what exists
        # and let iq_to_dbm interpolate — less resolution, never a wrong scale.
        with self._lock:
            blocks = list(self._pan_ring)
        if not blocks:
            return None
        np_ = self._np
        want = max(1, int(n))
        if np_ is None or len(blocks) == 1:
            blk = blocks[-1]
        else:
            take, have = [], 0
            for b in reversed(blocks):
                take.append(b)
                have += len(b)
                if have >= want:
                    break
            blk = np_.concatenate(list(reversed(take)))
        if len(blk) > want:
            blk = blk[-want:]

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

    def _queue_audio(self, block):
        """Hand one IQ block to the demodulator, keeping the queue no deeper
        than _AUDIO_BACKLOG_S of signal (see the note at the constant)."""
        n = max(1, len(block))
        keep = max(2, -(-int(_AUDIO_BACKLOG_S * self.samp_rate) // n))   # ceil, blocks
        dropped = 0
        with self._lock:
            self._audio_q.append(block)
            while len(self._audio_q) > keep:
                self._audio_q.popleft()
                dropped += 1
        if dropped:
            self._audio_dropped += dropped
            now = time.monotonic()
            if now - self._audio_drop_logged > 5.0:
                self._audio_drop_logged = now
                print(f"[soapy] audio had fallen {1000.0 * dropped * n / self.samp_rate:.0f} ms "
                      f"behind the antenna — dropped {dropped} IQ block(s) to catch up "
                      f"({self._audio_dropped} total)", flush=True)

    def audio_backlog_ms(self):
        """How far behind the antenna the demodulator's input currently is."""
        with self._lock:
            queued = sum(len(b) for b in self._audio_q)
        return 1000.0 * queued / self.samp_rate if self.samp_rate else 0.0

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
        while len(self._ar_buf) < need_r:
            with self._lock:
                blk = self._audio_q.popleft() if self._audio_q else None
            if blk is None:
                break
            self._ar_buf = np.concatenate([self._ar_buf, self._demod_block(blk)])
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
        # DIAGNOSTIC: AETHER_GATE_NO_AGC=1 freezes the AGC at a fixed gain so a
        # steady carrier can be judged without the level tracker modulating it.
        if _os.environ.get("AETHER_GATE_NO_AGC") == "1":
            audio = audio * (self._agc_target / max(self._agc_level, 1e-4))
            np.clip(audio, -1.0, 1.0, out=audio)
            return audio.tolist()
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
        # Over the DEMODULATOR'S PASSBAND (minus the noise floor's share of it,
        # see below), not a single bin at the slice
        # frequency. The previous version mixed the slice to DC and took
        # |mean()| over the block, which is a Goertzel bin: 8192 samples at
        # 250 kS/s is a 33 ms window, so it measured a ~30 Hz sliver centred
        # exactly on the slice frequency. On SSB that point is the SUPPRESSED
        # CARRIER — there is no energy there, the voice sits 300-2700 Hz to one
        # side — so the meter tracked noise in an empty 30 Hz gap and barely
        # responded to signal. It only ever worked for a carrier sitting dead on
        # the slice frequency (CW, or an FM centre).
        f_off = self._slice_hz - self.center_hz
        lo_off, hi_off = self._meter_band_hz()
        n = min(len(blk), 8192)
        x = blk[-n:]
        win = np.hanning(n)
        spec = np.fft.fft(x * win)
        freqs = np.fft.fftfreq(n, 1.0 / self.samp_rate)
        sel = (freqs >= f_off + lo_off) & (freqs <= f_off + hi_off)
        if not sel.any():
            # Passband fell outside the digitised window — a slice parked beyond
            # the span. Report nothing rather than a number read off the edge.
            return Meters()
        # SIGNAL POWER, NOT TOTAL POWER. Reporting the whole passband's power is
        # the honest measurement and it makes the meter useless: a 3 kHz slice of
        # band noise IS about -85 dBm, so the needle sat at S8 on dead static and
        # had nowhere left to go for an actual signal. Every reference instrument
        # an operator owns — a rig's meter, SDRconnect — is AGC/detector derived
        # and reads far below the true noise power for the same reason. Measured
        # against SDRconnect on the same antenna: its readout sits 8-14 dB under
        # its OWN spectrum integrated across the same filter (2026-08-31).
        #
        # So subtract the noise floor's share of the passband and report what is
        # left. On static that lands at the bottom of the scale; on a signal it
        # is that signal's strength, which is the number the meter exists to
        # show. The absolute scale is untouched — this is a different quantity,
        # not a fudged one.
        psd = np.abs(spec) ** 2
        # wg is needed for the noise figure below as well as the signal, so it
        # is hoisted above the early return.
        wg = float(np.mean(win * win))
        # Median, not mean: signals occupy a handful of the window's bins and
        # would drag a mean estimate up toward whatever we are trying to measure.
        # Noise-only bin powers are exponentially distributed, whose median is
        # ln(2) of the mean — without that factor the floor reads 1.6 dB light
        # and every weak signal is over-reported by the same amount.
        noise_per_bin = float(np.median(psd)) / _LN2
        excess = float(np.sum(psd[sel])) - noise_per_bin * float(np.count_nonzero(sel))
        noise_dbm = (10.0 * np.log10(max(noise_per_bin * float(np.count_nonzero(sel)),
                                          1e-30) / (n * n * wg))
                     + dbm_offset_for(self.gain_db, self.dbm_trim, self.dbm_base))
        if excess <= 0.0:
            # Nothing above the floor. Still report the floor: on a quiet band
            # that is the only real measurement there is, and it is the half of
            # SNR an operator tunes an antenna against.
            return Meters(s_meter_dbm=-140.0, noise_dbm=float(noise_dbm))
        # Normalised by the window's power gain so the reading is a property of
        # the signal, not of the window we chose.
        rms = float(np.sqrt(excess / (n * n * wg))) + 1e-12
        # ONE calibration, shared with the panadapter (core.fft). Backing the
        # RF gain out here is what stops our own front-end setting masquerading
        # as signal strength; the panadapter does the same, so the two scales
        # agree instead of drifting apart as the gain moves.
        dbm = 20.0 * np.log10(rms) + dbm_offset_for(self.gain_db, self.dbm_trim, self.dbm_base)
        return Meters(s_meter_dbm=max(-140.0, min(0.0, dbm)),
                      noise_dbm=float(noise_dbm))
