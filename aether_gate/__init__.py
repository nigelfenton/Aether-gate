#
# Aether-gate — universal radio bridge: any radio presents to AetherSDR as a Flex 6000.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Aether-gate: one ingest boundary for AetherSDR.

Every source (SDR hardware, legacy transceiver, remote WebSDR) is normalised to a
Flex 6000 VITA-49 stream before AE sees it. The core speaks Flex; each radio is a
RadioAdapter. See DESIGN.md.
"""
__version__ = "0.1.0"   # Aether-gate's own version (distinct from the vendored flex-sim engine's FLEX_SIM_VERSION)

from .core import Radio, Rack
from .adapters import get_adapter, available, register, RadioAdapter

__all__ = ["__version__", "Radio", "Rack",
           "get_adapter", "available", "register", "RadioAdapter"]
