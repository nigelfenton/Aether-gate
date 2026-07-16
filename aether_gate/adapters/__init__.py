#
# Aether-gate - adapter registry.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Adapter registry: name -> RadioAdapter subclass.

A plain dict for now (DESIGN.md defers formal entry-point plugin discovery).
New adapters register here; the CLI resolves `--adapter <name>` through get_adapter.
"""
from .base import RadioAdapter, AdapterCaps, Meters
from .sim import SimAdapter
from .soapy import SoapyAdapter   # SoapySDR import is deferred to .open(), safe to import here
from .icom9700 import Icom9700Adapter   # icom/ LAN transport imports are stdlib-only, safe here
from .icom7300 import Icom7300Adapter   # USB CI-V; pyserial import is deferred/optional
from .kenwood import KenwoodAdapter     # hamlib(rigctld TCP) + soapy; imports stdlib-only here
from .yaesu import YaesuAdapter         # thin subclass of KenwoodAdapter; Yaesu registry + defaults
from .hpsdr import HpsdrAdapter         # HPSDR Protocol-1 SDR (Radioberry/HL2); numpy import deferred to open()

_REGISTRY = {
    "sim": SimAdapter,
    "soapy": SoapyAdapter,
    "icom9700": Icom9700Adapter,
    "icom7300": Icom7300Adapter,
    "kenwood": KenwoodAdapter,
    "yaesu": YaesuAdapter,
    "hpsdr": HpsdrAdapter,
}


def register(name, cls):
    """Register an adapter class under `name`."""
    if not issubclass(cls, RadioAdapter):
        raise TypeError(f"{cls!r} is not a RadioAdapter")
    _REGISTRY[name] = cls


def get_adapter(name):
    """Return the adapter class registered under `name` (KeyError if unknown)."""
    return _REGISTRY[name]


def available():
    """List registered adapter names."""
    return sorted(_REGISTRY)


__all__ = ["RadioAdapter", "AdapterCaps", "Meters", "SimAdapter", "SoapyAdapter",
           "Icom9700Adapter", "Icom7300Adapter", "KenwoodAdapter", "YaesuAdapter", "register",
           "get_adapter", "available"]
