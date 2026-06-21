"""
kiwi_source.py — KiwiSDR IQ bridge for flex-sim
================================================
Connects to a public KiwiSDR WebSocket, receives 12 kHz IQ, runs FFT,
and produces dBm bin arrays in the same format as flex-sim's existing
pattern engine.  Designed to be dropped in as a new pattern type:

    --pattern kiwi --kiwi-host sdr.example.com --kiwi-freq 14074

Architecture (see DESIGN.md §12):
  KiwiSDR WS → IQ samples → numpy FFT → dBm bins → VITA-49 framing (existing)
  VFO retune in AE → flex-sim pan set → KiwiSource.retune()

Dependencies (not bundled — install when building):
  pip install kiwiclient numpy scipy

Status: STUB — interfaces defined, implementation pending post-FD
"""

import asyncio
import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# KiwiSDR native sample rate (fixed by hardware)
KIWI_SAMPLE_RATE = 12000   # Hz
KIWI_IQ_CHANNELS = 2       # I + Q

# FFT parameters — match flex-sim's pan resolution
FFT_SIZE      = 4096        # bins; governs frequency resolution
FFT_OVERLAP   = 0.5        # 50% overlap for smoother waterfall
WINDOW        = "hann"      # scipy.signal window type

# Noise floor to synthesise outside the 12 kHz KiwiSDR window
SYNTH_FLOOR_DBM = -120.0


@dataclass
class KiwiConfig:
    host: str          = "localhost"
    port: int          = 8073
    freq_khz: float    = 14074.0    # initial centre frequency (kHz)
    agc: bool          = True
    password: str      = ""         # most public servers need no password


class KiwiSource:
    """
    Consumes a KiwiSDR IQ stream and produces per-frame dBm bin arrays
    compatible with flex-sim's existing pattern engine.

    Usage (once implemented):
        src = KiwiSource(KiwiConfig(host="sdr.k3cal.example.com", freq_khz=14074))
        await src.connect()
        bins_dbm = await src.next_frame(n_bins=1024, span_hz=192000, centre_hz=14074000)
        await src.close()
    """

    def __init__(self, config: KiwiConfig):
        self.config  = config
        self._client = None          # kiwiclient.KiwiSDRStream instance (when connected)
        self._iq_buf: list           = []
        self._lock   = asyncio.Lock()
        self._connected = False

    async def connect(self) -> None:
        """
        Open WebSocket to KiwiSDR and start IQ stream.

        TODO (post-FD):
          - import kiwiclient; create KiwiSDRStream
          - send SET auth, SET mod=iq, SET low_cut/high_cut, SET freq
          - wire _on_iq_samples callback into self._iq_buf
          - handle reconnect on drop
        """
        log.info("KiwiSource.connect: %s:%d @ %.3f kHz [STUB]",
                 self.config.host, self.config.port, self.config.freq_khz)
        self._connected = False
        raise NotImplementedError("KiwiSource.connect — implement post-FD")

    async def retune(self, freq_hz: float) -> None:
        """
        Retune the KiwiSDR centre to freq_hz.
        Called by flex-sim when AE sends 'display pan set center=<freq>'.

        TODO (post-FD):
          - send SET freq=<freq_khz> over the existing WebSocket
          - update self.config.freq_khz
          - flush self._iq_buf (stale samples from old frequency)
        """
        freq_khz = freq_hz / 1000.0
        log.info("KiwiSource.retune → %.3f kHz [STUB]", freq_khz)
        self.config.freq_khz = freq_khz
        raise NotImplementedError("KiwiSource.retune — implement post-FD")

    async def next_frame(self, n_bins: int, span_hz: float, centre_hz: float) -> np.ndarray:
        """
        Return a dBm array of shape (n_bins,) for one pan frame.

        The 12 kHz KiwiSDR window is mapped into the centre of the pan;
        bins outside that window are filled with SYNTH_FLOOR_DBM.

        Args:
            n_bins:     number of FFT output bins (matches pan x_pixels)
            span_hz:    total panadapter span in Hz (e.g. 192000)
            centre_hz:  pan centre frequency in Hz

        Returns:
            np.ndarray, dtype float32, shape (n_bins,), values in dBm

        TODO (post-FD):
          1. Pull FFT_SIZE IQ samples from self._iq_buf (block until available)
          2. Apply Hann window
          3. np.fft.fft → magnitude → dBm  (ref: 0 dBFS = 0 dBm, calibrate offset)
          4. Map FFT bins → pan bins accounting for:
               - KiwiSDR window width (12 kHz) vs pan span (up to 192 kHz)
               - KiwiSDR centre (self.config.freq_khz) vs pan centre (centre_hz)
          5. Fill bins outside the 12 kHz window with SYNTH_FLOOR_DBM
          6. Return array
        """
        log.debug("KiwiSource.next_frame: n_bins=%d span=%.0f Hz [STUB returning synthetic floor]",
                  n_bins, span_hz)
        # Stub: return pure synthetic floor so flex-sim runs without a live KiwiSDR
        return np.full(n_bins, SYNTH_FLOOR_DBM, dtype=np.float32)

    async def close(self) -> None:
        """Disconnect from KiwiSDR and release resources."""
        log.info("KiwiSource.close [STUB]")
        self._connected = False
        # TODO: cancel kiwiclient tasks, close WebSocket


def _iq_to_dbm(iq: np.ndarray, ref_dbm: float = 0.0) -> np.ndarray:
    """
    Convert complex IQ block to dBm magnitude spectrum.

    Args:
        iq:      complex64 array, length FFT_SIZE
        ref_dbm: calibration offset — adjust so S9 = -73 dBm matches AE's S-meter

    Returns:
        float32 array of dBm values, length FFT_SIZE // 2 (positive frequencies only)

    TODO (post-FD): validate calibration offset against flex-sim S-meter reading.
    A known-level signal (e.g. a WSPR beacon at a known EIRP) is the best reference.
    """
    window  = np.hanning(len(iq))
    fft_out = np.fft.fft(iq * window)
    mag_sq  = np.abs(fft_out[:len(iq) // 2]) ** 2
    # Avoid log(0)
    mag_sq  = np.maximum(mag_sq, 1e-30)
    dbm     = 10.0 * np.log10(mag_sq) + ref_dbm
    return dbm.astype(np.float32)


def _map_fft_to_pan(fft_dbm: np.ndarray,
                    kiwi_centre_hz: float,
                    pan_centre_hz: float,
                    pan_span_hz: float,
                    n_pan_bins: int) -> np.ndarray:
    """
    Map a KiwiSDR FFT result (12 kHz wide) into a flex-sim pan bin array.

    Bins that fall outside the KiwiSDR window are filled with SYNTH_FLOOR_DBM.

    Args:
        fft_dbm:        dBm array from _iq_to_dbm(), length FFT_SIZE // 2
        kiwi_centre_hz: KiwiSDR tuned centre in Hz
        pan_centre_hz:  flex-sim pan centre in Hz
        pan_span_hz:    flex-sim pan span in Hz
        n_pan_bins:     number of pan output bins (x_pixels)

    Returns:
        float32 array of shape (n_pan_bins,) in dBm

    TODO (post-FD): implement bin interpolation / overlap handling.
    """
    result = np.full(n_pan_bins, SYNTH_FLOOR_DBM, dtype=np.float32)

    kiwi_span_hz   = KIWI_SAMPLE_RATE           # 12 kHz
    kiwi_low_hz    = kiwi_centre_hz - kiwi_span_hz / 2
    kiwi_high_hz   = kiwi_centre_hz + kiwi_span_hz / 2
    pan_low_hz     = pan_centre_hz  - pan_span_hz  / 2

    hz_per_pan_bin = pan_span_hz / n_pan_bins
    hz_per_fft_bin = kiwi_span_hz / len(fft_dbm)

    for pan_bin in range(n_pan_bins):
        bin_hz = pan_low_hz + pan_bin * hz_per_pan_bin
        if kiwi_low_hz <= bin_hz < kiwi_high_hz:
            fft_idx = int((bin_hz - kiwi_low_hz) / hz_per_fft_bin)
            fft_idx = min(fft_idx, len(fft_dbm) - 1)
            result[pan_bin] = fft_dbm[fft_idx]

    return result


# ---------------------------------------------------------------------------
# Integration note for flex-sim (flex_sim.py)
# ---------------------------------------------------------------------------
# When implementing post-FD, add to flex_sim.py:
#
#   from kiwi.kiwi_source import KiwiSource, KiwiConfig
#
#   In SimRadio.__init__:
#     self.kiwi: Optional[KiwiSource] = None
#
#   In SimRadio.generate_frame() pattern dispatch:
#     elif self.pattern == "kiwi":
#         if self.kiwi:
#             dbm_bins = await self.kiwi.next_frame(self.x_pixels, self.bandwidth, self.center_hz)
#         else:
#             dbm_bins = np.full(self.x_pixels, -120.0)
#
#   In SimRadio.on_line() for 'display pan set center=':
#     if self.kiwi and self.pattern == "kiwi":
#         asyncio.create_task(self.kiwi.retune(new_center_hz))
#
#   Web panel: add "kiwi" to pattern list, add KiwiSDR host/freq fields.
#   Requirements: pip install kiwiclient numpy scipy
# ---------------------------------------------------------------------------
