#
# Aether-gate — IC-9700 (Icom networked-radio) LAN adapter package.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Transport ported from github.com/w5jwp/SDR9700 (GPL-3.0); see file headers.
#
"""IC-9700 LAN adapter (in progress). M0 (obfuscation) done; M1 (auth) WIP.
See ../ICOM9700_PLAN.md for the build plan."""
from .obfuscation import obfuscate, deobfuscate

__all__ = ["obfuscate", "deobfuscate"]
