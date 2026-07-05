#
# Aether-gate - Yaesu radio registry (data-driven capability table).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Per-radio capability data for the Yaesu family the gate can bridge.

Like Kenwood (and unlike the Icom LAN rigs whose one RS-BA1 stream carries
control+scope+audio), Yaesu CAT rigs split the two axes (see RADIO_SUPPORT.md):
  * CONTROL  = hamlib `rigctld` (vendor-neutral; freq/mode/PTT). No scope over CAT.
  * SPECTRUM = an IF-tap / off-air SoapySDR dongle, CAT-steered so the dongle's
               spectrum follows the rig's tuned frequency ("soapy-iftap").

So a Yaesu row carries: the hamlib model number (for `rigctld -m N`), the Flex
model to advertise to AE, the band set, and the default spectrum dongle hint. The
`hamlib_model` is the ONLY vendor-specific bit — everything else (freq/mode) rides
the generic rigctld client, identical to Kenwood.

⚠ hamlib model numbers below are CONFIRMED against the hamlib on the Pi5 gate
(`rigctl -l | grep -i yaesu`, 2026-07-05): FT-847 = 1001. Bands are indicative
widest ham allocations (region-neutral). END-TO-END on hardware: NOT yet verified.
"""
from dataclasses import dataclass, field
from typing import List, Optional

# Reuse the Icom registry's Band shape (same concept: span + XVTR flag).
from ..icom.radios import Band, _HF, _6M, _2M, _70CM


@dataclass
class YaesuRadio:
    model: str                     # Yaesu model, e.g. "FT-847"
    hamlib_model: int              # `rigctld -m N` id (rigctl -l)
    advertise: str                 # Flex model presented to AE (drives native band caps)
    bands: List[Band] = field(default_factory=list)
    # Yaesu CAT gives no scope -> spectrum comes from an IF-tap dongle (RTL-SDR V4).
    # NB the gate does NOT implement audio-FFT spectrum: a SignaLink/soundcard is for
    # audio/digimode only; AE's own audio chain provides an audioscope if wanted.
    spectrum: str = "soapy-iftap"  # dongle, CAT-steered (the only spectrum path)
    hf_dongle_needed: bool = True  # HF coverage needs an HF-capable dongle (V4/upconverter/RX888)
    verified: bool = False
    notes: str = ""

    def native_bands(self) -> List[Band]:
        return [b for b in self.bands if not b.needs_xvtr]

    def xvtr_bands(self) -> List[Band]:
        return [b for b in self.bands if b.needs_xvtr]


# Yaesu CAT rigs: control via hamlib, spectrum via IF-tap dongle.
# advertise=FLEX-6700 when the rig has 2m (unlocks AE's native 2m); FLEX-6600 for HF+6m.
REGISTRY = {
    "FT-847": YaesuRadio(
        model="FT-847", hamlib_model=1001, advertise="FLEX-6700",
        # HF/6m/2m/70cm all-mode satellite base. 70cm past the 6700's native 2m -> XVTR.
        bands=_HF + _6M + _2M + _70CM,
        spectrum="soapy-iftap", hf_dongle_needed=True, verified=False,
        notes="HF/6m/2m/70cm all-mode (sat rig). hamlib -m 1001 (confirmed vs Pi5 hamlib "
              "2026-07-05). Older CAT: typ 4800/8N2, ~200ms between commands, no PTT-over-CAT "
              "guarantee on all firmware. No CAT scope -> IF-tap dongle. VERIFY end-to-end."),

    "FT-991A": YaesuRadio(
        model="FT-991A", hamlib_model=1035, advertise="FLEX-6700",
        bands=_HF + _6M + _2M + _70CM,
        spectrum="soapy-iftap", hf_dongle_needed=True, verified=False,
        notes="HF/6m/2m/70cm all-mode. hamlib -m 1035. USB CAT, typ 38400. No CAT scope. VERIFY."),

    "FTDX10": YaesuRadio(
        model="FTDX10", hamlib_model=1042, advertise="FLEX-6600",
        bands=_HF + _6M, spectrum="soapy-iftap", hf_dongle_needed=True, verified=False,
        notes="HF/6m SDR rig. Built-in scope NOT over CAT -> IF-tap dongle. hamlib -m 1042 "
              "(VERIFY the id against the target box's `rigctl -l`)."),

    "FT-710": YaesuRadio(
        model="FT-710", hamlib_model=1046, advertise="FLEX-6600",
        bands=_HF + _6M, spectrum="soapy-iftap", hf_dongle_needed=True, verified=False,
        notes="HF/6m SDR rig. hamlib -m 1046 (VERIFY id vs target box). No CAT scope."),

    "FT-817": YaesuRadio(
        model="FT-817", hamlib_model=1020, advertise="FLEX-6700",
        bands=_HF + _6M + _2M + _70CM,
        spectrum="soapy-iftap", hf_dongle_needed=True, verified=False,
        notes="QRP HF/6m/2m/70cm. hamlib -m 1020 (817/818 share this on older hamlib; "
              "818 may be 1041 on newer — VERIFY). No scope -> IF-tap dongle for the pan "
              "(the gate does NOT do audio-FFT spectrum; AE's own audio chain covers an "
              "audioscope if wanted)."),
}


def get(model: str) -> Optional[YaesuRadio]:
    """Look up by model, tolerant of case/spaces/underscores."""
    if not model:
        return None
    key = model.strip().upper().replace("_", "-").replace(" ", "")
    for m, r in REGISTRY.items():
        if m.upper().replace("-", "").replace(" ", "") == key.replace("-", ""):
            return r
    return REGISTRY.get(model)


def supported() -> List[str]:
    return sorted(REGISTRY)
