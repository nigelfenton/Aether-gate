#
# Aether-gate — /device reports the driver's own bounds for a numeric setting.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""device_controls() must pass a setting's ArgInfo range through, and only when
the driver actually bounded it.

A panel built from /device has no other way to know that an RSP's AGC
set-point runs -72..-20 dBfs, or that a frequency-correction value can be
several thousand ppm. Without the range it has to guess, and a guess clamps in
BOTH directions: a write outside it is capped before it reaches the device,
and a read-back outside it is displayed as the clamp rather than the value the
device holds (AetherSDR#5372 review, blocker 3).

Soapy's default ArgInfo range is 0..0, which means "unbounded" — that must
NOT be sent, or the panel would build a control that can only hold zero.
"""
from aether_gate.adapters.soapy import SoapyAdapter


class _Range:
    def __init__(self, lo, hi, step=0.0):
        self._lo, self._hi, self._step = lo, hi, step

    def minimum(self):
        return self._lo

    def maximum(self):
        return self._hi

    def step(self):
        return self._step


class _ArgInfo:
    def __init__(self, key, type_, options=(), rng=None, name=""):
        self.key, self.name, self.type = key, name, type_
        self.options = list(options)
        self.range = rng if rng is not None else _Range(0.0, 0.0)


class _FakeSdr:
    """Just enough of a SoapySDR.Device for device_controls()."""

    def __init__(self, infos, values):
        self._infos, self._values = infos, values

    def listAntennas(self, direction, channel):
        return ["Antenna A", "Antenna B"]

    def getAntenna(self, direction, channel):
        return "Antenna B"

    def getSettingInfo(self):
        return self._infos

    def readSetting(self, key):
        return self._values[key]


def _adapter(infos, values):
    a = SoapyAdapter.__new__(SoapyAdapter)          # no hardware needed
    a._sdr = _FakeSdr(infos, values)
    a._SOAPY_SDR_RX = 0
    return a


def _settings(out):
    return {s["key"]: s for s in out["settings"]}


def test_bounded_numeric_setting_carries_its_range():
    infos = [_ArgInfo("agc_setpoint", 1, rng=_Range(-72.0, -20.0, 1.0), name="AGC set-point")]
    out = _adapter(infos, {"agc_setpoint": "-30"}).device_controls()
    s = _settings(out)["agc_setpoint"]
    assert s["range"] == {"min": -72.0, "max": -20.0, "step": 1.0}
    assert s["value"] == "-30"
    assert s["type"] == "1"


def test_soapy_default_range_is_not_a_range():
    """0..0 is Soapy's 'no bounds given' — sending it would build a control
    that can only hold zero."""
    infos = [_ArgInfo("corr_ppm", 2)]
    s = _settings(_adapter(infos, {"corr_ppm": "0.5"}).device_controls())["corr_ppm"]
    assert "range" not in s


def test_enum_and_bool_settings_are_unchanged():
    infos = [
        _ArgInfo("biasT_ctrl", 0),
        _ArgInfo("if_mode", 3, options=["Zero-IF", "Low-IF"]),
    ]
    out = _adapter(infos, {"biasT_ctrl": "false", "if_mode": "Zero-IF"}).device_controls()
    s = _settings(out)
    assert "range" not in s["biasT_ctrl"] and "options" not in s["biasT_ctrl"]
    assert s["if_mode"]["options"] == ["Zero-IF", "Low-IF"]
    assert out["antenna"] == {"value": "Antenna B", "options": ["Antenna A", "Antenna B"]}


def test_a_driver_that_raises_on_range_still_reports_the_setting():
    class _Bad:
        key, name, type, options = "rfgain_sel", "", 1, []

        @property
        def range(self):
            raise RuntimeError("no range attribute in this binding")

    infos = [_Bad()]
    s = _settings(_adapter(infos, {"rfgain_sel": "4"}).device_controls())["rfgain_sel"]
    assert s["value"] == "4" and "range" not in s
