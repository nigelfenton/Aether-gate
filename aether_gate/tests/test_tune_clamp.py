#
# Aether-gate — tune-frequency boundary clamp tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""AE tune freqs cross the engine->adapter boundary into a physical rig, so the
engine validates them and fails CLOSED (keeps the previous freq, logs) rather
than dialing garbage. Covers issue #3.

Run:  python -m aether_gate.tests.test_tune_clamp
"""
import sys


class FakeConn:
    """Captures the bytes the Radio would write back to AE."""
    def __init__(self):
        self.out = bytearray()

    def sendall(self, b):
        self.out.extend(b)


def _radio():
    from aether_gate.core import Radio
    from aether_gate.adapters import SimAdapter
    # A sim adapter with no real hardware: retune()/set_slice() are no-ops, so an
    # accepted freq is safe and a rejected one simply never reaches the (absent) rig.
    r = Radio("127.0.0.1", None, adapter=SimAdapter(model="FLEX-6700"), port=5992)
    # Seed a known-good active slice at 14.100 MHz so we can watch it hold/move.
    r.slices[0] = {"freq": 14.100, "mode": "USB", "active": True, "pan": r._primary_pan()}
    r.active_slice = 0
    return r


def test_helper_accepts_in_envelope():
    from aether_gate.core.engine import TUNE_MIN_MHZ, TUNE_MAX_MHZ
    r = _radio()
    for good in (0.137, 3.573, 7.074, 14.074, 50.313, 145.0, 435.0, 1270.0):
        assert r._valid_tune_mhz(good, 14.1) == good, good
    # exact boundaries are inclusive
    assert r._valid_tune_mhz(TUNE_MIN_MHZ, 14.1) == TUNE_MIN_MHZ
    assert r._valid_tune_mhz(TUNE_MAX_MHZ, 14.1) == TUNE_MAX_MHZ
    print("ok  helper: in-envelope freqs accepted (incl. boundaries)")


def test_helper_fails_closed():
    r = _radio()
    keep = 14.100
    # non-numeric -> keep previous
    assert r._valid_tune_mhz("not-a-freq", keep) == keep
    assert r._valid_tune_mhz(None, keep) == keep
    # out of envelope (negative, zero, absurd, just past the top) -> keep previous
    for bad in (-1.0, 0.0, 0.001, 1301.0, 5_000.0, 1e9):
        assert r._valid_tune_mhz(bad, keep) == keep, bad
    # keep=None means "drop" (used by the pan center path)
    assert r._valid_tune_mhz("garbage", None) is None
    assert r._valid_tune_mhz(9999.0, None) is None
    print("ok  helper: malformed / out-of-envelope fail closed")


def test_slice_tune_positional_clamped():
    r, conn = _radio(), FakeConn()
    # valid positional tune moves the slice
    r.on_line(conn, "C1|slice tune 0 7.074000")
    assert abs(r.slices[0]["freq"] - 7.074) < 1e-9, r.slices[0]["freq"]
    # garbage positional tune is rejected -> slice HOLDS its last good freq
    r.on_line(conn, "C2|slice tune 0 999999")
    assert abs(r.slices[0]["freq"] - 7.074) < 1e-9, r.slices[0]["freq"]
    print("ok  slice tune: positional out-of-envelope held at last good freq")


def test_slice_set_rf_frequency_clamped():
    r, conn = _radio(), FakeConn()
    r.on_line(conn, "C1|slice set 0 RF_frequency=21.074000")
    assert abs(r.slices[0]["freq"] - 21.074) < 1e-9, r.slices[0]["freq"]
    # a non-numeric RF_frequency must not blow up the handler nor move the slice
    r.on_line(conn, "C2|slice set 0 RF_frequency=NaNaNa")
    assert abs(r.slices[0]["freq"] - 21.074) < 1e-9, r.slices[0]["freq"]
    print("ok  slice set: bad RF_frequency held, handler survives")


def test_pan_center_clamped():
    r, conn = _radio(), FakeConn()
    pid = r._primary_pan()
    before = r.pans[pid]["center"]
    # an absurd pan center= is dropped: no retune, center unchanged
    r.on_line(conn, f"C1|display pan set 0x{pid:08X} center=8000.0")
    assert r.pans[pid]["center"] == before, (r.pans[pid]["center"], before)
    # a sane center= still works
    r.on_line(conn, f"C2|display pan set 0x{pid:08X} center=28.400")
    assert abs(r.pans[pid]["center"] - 28.400) < 1e-9, r.pans[pid]["center"]
    print("ok  pan center: out-of-envelope dropped, valid accepted")


def main():
    tests = [test_helper_accepts_in_envelope, test_helper_fails_closed,
             test_slice_tune_positional_clamped, test_slice_set_rf_frequency_clamped,
             test_pan_center_clamped]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} tune-clamp tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
