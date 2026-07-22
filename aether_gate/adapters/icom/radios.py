#
# Aether-gate - Icom radio registry (data-driven capability table).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Per-radio capability data for the Icom family the gate can bridge.

Design (2026-06-30): the *gate* owns radio knowledge, not AetherSDR. AE stays a
transparent Flex client; each supported rig is one DATA ROW here, so adding a radio
is a table entry, not code. The adapter reads this to decide:
  * transport   - "lan" (RS-BA1 UDP; implemented in handler/udpbase/civ) vs
                  "usb" (serial CI-V + USB-audio soundcard; NOT yet implemented),
  * civ_addr    - default CI-V address (user-changeable on the radio),
  * advertise   - which Flex model to present so AE offers the right native bands
                  (FLEX-6700 has built-in 2m; 6600/6300 = HF+6m only),
  * bands       - each band's span, and whether it needs a TRANSVERTER (XVTR):
                  a band beyond the advertised Flex model's native reach is driven
                  via AE's native XVTR (IF the radio tunes <-> real RF), the gate
                  mapping IF<->RF over CI-V. That also fixes AE's >3-digit-MHz VFO.

Only IC-9700 is hardware-VERIFIED (this session: civ 0xA2, LAN, 27h scope, 2m
tuned live from AE as a FLEX-6700). Other rows are a documented starter set:
**verify civ_addr / band edges / scope / transport against the CI-V Reference
Guide before relying on them** (band edges here are indicative and region-neutral;
the exact per-region segments live in AE's resources/bandplans/*.json).
"""
from dataclasses import dataclass, field
from typing import List, Optional

# Transverter-band map is delegated to AE's XVTR subsystem; the IF is a suggestion
# (a common HF/6m IF the radio-behind can't reach anyway, so no clash). 0 = pick later.


@dataclass
class Band:
    name: str
    low_mhz: float
    high_mhz: float
    needs_xvtr: bool = False       # beyond advertised Flex model -> use AE XVTR
    xvtr_if_mhz: float = 0.0       # suggested transverter IF (MHz); 0 = decide later


@dataclass
class IcomRadio:
    model: str                     # Icom model, e.g. "IC-9700"
    civ_addr: int                  # default CI-V address (hex in source; user-changeable)
    transport: str                 # "lan" (implemented) | "usb" (TODO: serial CI-V + soundcard)
    advertise: str                 # Flex model presented to AE (drives native band caps)
    bands: List[Band] = field(default_factory=list)
    has_scope: bool = False        # CI-V 27h band-scope waveform output
    verified: bool = False         # civ_addr/bands/scope confirmed on real hardware
    notes: str = ""

    def native_bands(self) -> List[Band]:
        return [b for b in self.bands if not b.needs_xvtr]

    def xvtr_bands(self) -> List[Band]:
        return [b for b in self.bands if b.needs_xvtr]


# Common band spans (indicative; region-neutral widest ham allocation).
_HF = [Band("160m", 1.8, 2.0), Band("80m", 3.5, 4.0), Band("60m", 5.25, 5.45),
       Band("40m", 7.0, 7.3), Band("30m", 10.1, 10.15), Band("20m", 14.0, 14.35),
       Band("17m", 18.068, 18.168), Band("15m", 21.0, 21.45), Band("12m", 24.89, 24.99),
       Band("10m", 28.0, 29.7)]
_6M = [Band("6m", 50.0, 54.0)]
_2M = [Band("2m", 144.0, 148.0)]                              # FLEX-6700 native
_70CM = [Band("440", 420.0, 450.0, needs_xvtr=True)]         # via XVTR; wire name "440" = AE BandDefs vocab (AE has no "70cm")
_23CM = [Band("23cm", 1240.0, 1300.0, needs_xvtr=True)]      # via XVTR


# --- the registry ---------------------------------------------------------
# advertise=FLEX-6700 when the rig has 2m (unlocks AE's native 2m band);
# advertise=FLEX-6600 for HF+6m-only rigs. Bands past that -> needs_xvtr.
REGISTRY = {
    "IC-9700": IcomRadio(
        model="IC-9700", civ_addr=0xA2, transport="lan", advertise="FLEX-6700",
        bands=_2M + _70CM + _23CM, has_scope=True, verified=True,
        notes="VERIFIED 2026-06-30: LAN RS-BA1, 27h scope, 2m tuned live from AE."),

    "IC-705": IcomRadio(
        model="IC-705", civ_addr=0xA4, transport="lan", advertise="FLEX-6700",
        bands=_HF + _6M + _2M + _70CM, has_scope=True, verified=True,
        notes="VERIFIED 2026-07-21 on hardware (K6OZY lab): RS-BA1 over WLAN, civ 0xA4, "
              "27h scope 30.0 fps / 475 bins, LAN RX audio, HF+2m tuned live from AE."),

    "IC-7610": IcomRadio(
        model="IC-7610", civ_addr=0x98, transport="lan", advertise="FLEX-6600",
        bands=_HF + _6M, has_scope=True, verified=False,
        notes="HF+6m, LAN RS-BA1, dual scope. VERIFY civ/scope."),

    "IC-R8600": IcomRadio(
        model="IC-R8600", civ_addr=0x96, transport="lan", advertise="FLEX-6700",
        bands=_HF + _6M + _2M + _70CM + _23CM, has_scope=True, verified=False,
        notes="Wideband RX-ONLY 10kHz-3GHz; represent via XVTR chain. VERIFY."),

    "IC-905": IcomRadio(
        model="IC-905", civ_addr=0xAC, transport="lan", advertise="FLEX-6700",
        bands=_2M + _70CM + _23CM
              + [Band("13cm", 2300.0, 2450.0, needs_xvtr=True),
                 # 5.7 GHz: wire name "5cm" = AE BandDefs vocabulary (AE has no "6cm").
                 # The band is variously called 6cm/5cm/5.6GHz; AE names it "5cm".
                 Band("5cm", 5650.0, 5925.0, needs_xvtr=True),
                 Band("3cm", 10000.0, 10500.0, needs_xvtr=True)],
        has_scope=True, verified=False,
        notes="VHF->microwave (2m/70cm/5.7G/23cm/13cm/+10GHz w/ CX-10G). All but 2m via XVTR. VERIFY."),

    "IC-7300": IcomRadio(
        model="IC-7300", civ_addr=0x94, transport="usb", advertise="FLEX-6600",
        bands=_HF + _6M, has_scope=True, verified=False,
        notes="USB CI-V + USB-audio ONLY (no LAN) -> needs the usb transport (TODO). Scope 27h yes."),

    "IC-7100": IcomRadio(
        model="IC-7100", civ_addr=0x88, transport="usb", advertise="FLEX-6700",
        bands=_HF + _6M + _2M + _70CM, has_scope=False, verified=False,
        notes="USB CI-V. HF/6m/2m/70cm. Limited/no 27h scope -> audio-FFT fallback. VERIFY."),

    "IC-9100": IcomRadio(
        model="IC-9100", civ_addr=0x7C, transport="usb", advertise="FLEX-6700",
        bands=_HF + _6M + _2M + _70CM + _23CM, has_scope=False, verified=False,
        notes="USB CI-V. HF/6m/2m/70cm (+23cm option). No modern scope output. VERIFY."),
}


def get(model: str) -> Optional[IcomRadio]:
    """Look up a radio by model (case-insensitive, tolerant of spaces/underscores)."""
    if not model:
        return None
    key = model.strip().upper().replace("_", "-").replace(" ", "")
    for m, r in REGISTRY.items():
        if m.upper().replace("-", "").replace(" ", "") == key.replace("-", ""):
            return r
    return REGISTRY.get(model)


def supported() -> List[str]:
    return sorted(REGISTRY)


def lan_radios() -> List[str]:
    """Models the current LAN transport already handles."""
    return sorted(m for m, r in REGISTRY.items() if r.transport == "lan")
