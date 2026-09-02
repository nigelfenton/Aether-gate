#
# Aether-gate — the dBFS->dBm anchor is per device, not a global (no hardware).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The dBm anchor is keyed by driver, and an operator can replace it.

Review of the calibration work, 2026-09-01: -41.0 was measured on one RSPdx-R2
on one bench, and as a module constant it moved every other device's numbers
too, including hardware nobody could check it against. So the anchor is now a
per-driver table holding the measured devices, an unkeyed fallback for the
rest, and --dbm-base for a front end the operator has measured themselves.

Run:  python -m aether_gate.tests.test_dbm_base
"""
import argparse

from aether_gate.core.fft import (DBFS_TO_DBM, DBFS_TO_DBM_BY_DRIVER, GAIN_REF_DB,
                                  dbfs_to_dbm_for, dbm_offset_for)


def test_measured_drivers_get_their_own_anchor():
    assert dbfs_to_dbm_for("sdrplay") == -41.0
    assert dbfs_to_dbm_for("SDRplay") == -41.0      # driver names are not case-sensitive


def test_unmeasured_drivers_get_the_fallback_not_someone_elses_number():
    for drv in ("rtlsdr", "airspy", "none", "", None):
        assert dbfs_to_dbm_for(drv) == DBFS_TO_DBM, drv
    assert "rtlsdr" not in DBFS_TO_DBM_BY_DRIVER, "no reference measurement exists for it yet"


def test_the_offset_is_the_anchor_at_reference_gain_and_zero_trim():
    assert dbm_offset_for(GAIN_REF_DB, 0.0) == DBFS_TO_DBM
    assert dbm_offset_for(GAIN_REF_DB, 0.0, None) == DBFS_TO_DBM
    assert dbm_offset_for(GAIN_REF_DB, 0.0, -41.0) == -41.0


def test_soapy_adapter_carries_the_anchor_for_its_driver():
    from aether_gate.adapters.soapy import SoapyAdapter
    assert SoapyAdapter(driver="sdrplay").dbm_base == -41.0
    assert SoapyAdapter(driver="none").dbm_base == DBFS_TO_DBM


def _soapy_args(**over):
    ns = dict(soapy_driver="none", soapy_args="", samp_rate=250_000.0, gain=12.0,
              model="FLEX-6600", serial="GATE0001", station="aether-gate 1",
              direct_samp=None, agc=False, dbm_trim=0.0, dbm_base=None)
    ns.update(over)
    return argparse.Namespace(**ns)


def test_dbm_base_flag_replaces_the_driver_default():
    from aether_gate.__main__ import build_adapter
    assert build_adapter("soapy", _soapy_args(soapy_driver="sdrplay")).dbm_base == -41.0
    assert build_adapter("soapy", _soapy_args(soapy_driver="sdrplay", dbm_base=-37.5)).dbm_base == -37.5
    # Zero is a value, not "unset".
    assert build_adapter("soapy", _soapy_args(dbm_base=0.0)).dbm_base == 0.0


def test_kenwood_pan_uses_the_anchor_of_the_dongle_doing_its_spectrum():
    # The engine reads dbm_base off the OUTER adapter; a rig-plus-dongle pair
    # must hand it the dongle's, or its pan silently falls back to the guess.
    from aether_gate.adapters.kenwood.adapter import KenwoodAdapter
    assert KenwoodAdapter(model="TS-2000", soapy_driver="sdrplay").dbm_base == -41.0
    assert KenwoodAdapter(model="TS-2000", soapy_driver="rtlsdr").dbm_base == DBFS_TO_DBM


def main():
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:                       # noqa: BLE001 - report and continue
                fails += 1
                print(f"FAIL {name}: {e!r}")
    print("ALL PASS" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
