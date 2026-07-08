#
# Aether-gate - Yaesu adapter: hamlib control + IF-tap SDR spectrum.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Bridge a Yaesu CAT rig into AetherSDR as a Flex.

Yaesu splits the two axes exactly as Kenwood does (see RADIO_SUPPORT.md):
  * CONTROL  = hamlib `rigctld` — freq / mode / PTT / S-meter (vendor-neutral).
  * SPECTRUM = a SoapySDR dongle (IF-tap / off-air), CAT-steered to the rig's freq.

Because the whole control+spectrum+CAT-steer mechanism is vendor-neutral, this
adapter is a THIN subclass of KenwoodAdapter: it changes only the registry it
reads (Yaesu's), the default model, and a couple of serial defaults that suit
Yaesu's older CAT (4800 8N2, and NOT the TS-450's RTS/DTR handshake fix — that
was Kenwood-specific and can confuse a Yaesu). All freq/mode/PTT/steer logic is
inherited unchanged.

⚠ STATUS: scaffold. hamlib model number confirmed (FT-847 = 1001 on the Pi5),
end-to-end untested. TX is NOT wired (gate is receive+control only) — see README.
"""
from ..kenwood.adapter import KenwoodAdapter
from .radios import get as get_yaesu


class YaesuAdapter(KenwoodAdapter):
    """hamlib-controlled Yaesu CAT rig + IF-tap SoapySDR spectrum, presented as a Flex."""

    # Yaesu-friendly serial defaults: 4800 8N2 is the classic Yaesu CAT rate
    # (FT-847/817/857/897). The parent's default serial_conf carries the TS-450's
    # RTS/DTR-asserted handshake fix, which is Kenwood-specific — clear it here so
    # rigctld drives the Yaesu with hamlib's own per-model defaults.
    def __init__(self, model="FT-847",
                 serial_baud=4800, serial_conf="", station="Yaesu-CAT",
                 serial="GATEYAES",
                 # TX intent, OFF by default — same contract as the Kenwood parent:
                 # no PTT is wired, so a Yaesu is RX+control-only (matches this
                 # module's docstring). Named explicitly (though **kwargs would carry
                 # it) so the RX-only default is visible at the Yaesu layer too.
                 enable_tx=False, **kwargs):
        # Resolve the Yaesu row and inject its hamlib_model / advertise / bands by
        # temporarily swapping the registry the parent consults. The parent's
        # __init__ calls kenwood.radios.get(); we override that lookup by passing
        # the already-resolved row's fields through explicit kwargs instead.
        row = get_yaesu(model)
        if row is not None:
            # Fill from the registry when the caller didn't supply a value. Treat an
            # explicit None (the CLI passes --hamlib-model's None default through)
            # as "not supplied" — setdefault alone won't override a present None.
            if kwargs.get("hamlib_model") is None:
                kwargs["hamlib_model"] = row.hamlib_model
            if kwargs.get("advertise") is None:
                kwargs["advertise"] = row.advertise
        super().__init__(model=model, serial_baud=serial_baud,
                         serial_conf=serial_conf, station=station,
                         serial=serial, enable_tx=enable_tx, **kwargs)
        # Parent looked the model up in the Kenwood registry (miss -> None) and
        # defaulted bands to (); re-apply the Yaesu row's bands + advertise so AE
        # gets the right radio-declared bands and Flex identity.
        if row is not None:
            self._row = row
            bands = tuple(b.name for b in row.bands)
            self.capabilities.bands = bands
            self.capabilities.model = row.advertise

    def diagnostics(self):
        d = super().diagnostics()
        # relabel so the diagnostics/README don't say "Kenwood"
        d["radio"] = self.model + " (Yaesu hamlib CAT + IF-tap SDR)"
        return d
