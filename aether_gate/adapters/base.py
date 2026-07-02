#
# Aether-gate — the RadioAdapter contract.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The adapter contract: how a radio source plugs into the Aether-gate core.

The core (a vendored flex-sim engine) handles everything Flex-shaped — discovery,
the FlexLib control handshake, VITA-49 framing, slice/pan management, audio. An
adapter supplies exactly one thing the core cannot know: where the spectrum comes
from. Each radio/SDR is one adapter; the core never changes.

Two adapter shapes (set `provides`):
  * "spectrum" — implement get_spectrum(ctx, t) -> dBm bins. For sources that are
    already (or cheaply become) a spectrum: the built-in test patterns, a KiwiSDR
    pre-FFT bridge. The core just frames the bins.
  * "iq"       — implement get_iq(n, center_hz, span_hz) -> complex samples. For
    raw-IQ SDR hardware (RTL-SDR/Airspy/SDRplay via SoapySDR). The CORE runs the
    FFT (core/fft.iq_to_dbm), so every IQ source shares one transform.

Either spectrum/IQ method may return None to signal a TX gap (RX muted this frame).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AdapterCaps:
    """Per-adapter identity + capability, surfaced to AE via discovery/radio-status.

    model must be one of core.engine.MODELS (drives the advertised slice cap);
    pick the Flex model whose capabilities best match the real source.
    """
    model: str = "FLEX-6600"       # advertised radio model (-> slice cap)
    serial: str = ""               # advertised serial (blank -> core derives one)
    station: str = ""              # station name AE displays (blank -> core default)
    tx_capable: bool = False       # source can transmit (real transceiver) vs RX-only (dongle/WebSDR)
    min_span_hz: float = 48_000.0  # narrowest span the source can render
    max_span_hz: float = 14_000_000.0


@dataclass
class Meters:
    """Optional readback an adapter can provide each frame."""
    s_meter_dbm: float = -120.0
    tx: bool = False
    fwd_power_w: float = 0.0
    swr: float = 1.0


class RadioAdapter(ABC):
    """Base class for all radio sources. Subclass and set `provides`."""

    provides = "spectrum"          # "spectrum" | "iq"
    capabilities = AdapterCaps()

    # --- lifecycle -------------------------------------------------------
    def open(self):
        """Acquire the source (open device, connect socket). Override as needed."""

    def close(self):
        """Release the source. Override as needed."""

    # --- control (AE -> radio) ------------------------------------------
    def retune(self, center_hz: float):
        """AE tuned a slice/pan; move the real source's centre. Override for HW."""

    def set_mode(self, mode: str):
        """AE changed mode (USB/LSB/CW/...). Override for HW that cares."""

    def set_span(self, span_hz: float):
        """AE's pan zoom changed (full width, Hz). Optional: adapters whose
        source has a native span (a rig's band scope) can follow it."""

    def initial_center_hz(self):
        """Where the source is tuned right now (Hz), or None. A real radio
        should answer so the engine seeds AE's pan/slice on the rig's band
        instead of the sim default."""
        return None

    def initial_mode(self):
        """The source's current mode string (USB/FM/...), or None."""
        return None

    # --- readback (radio -> AE) -----------------------------------------
    def read_meters(self) -> Meters:
        """Optional S-meter / TX state. Default: quiet RX."""
        return Meters()

    def wants_tx(self, ctx):
        """Return True/False to assert TX this frame, or None to leave TX alone."""
        return None

    # --- the ONE source method (implement exactly one per `provides`) ----
    def get_spectrum(self, ctx, t):
        """provides == 'spectrum': return list[float] of ctx.n dBm bins, or None."""
        raise NotImplementedError("spectrum adapter must implement get_spectrum()")

    def get_iq(self, n: int, center_hz: float, span_hz: float):
        """provides == 'iq': return a complex sample block (len ~n), or None."""
        raise NotImplementedError("iq adapter must implement get_iq()")
