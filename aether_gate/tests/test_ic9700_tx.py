#
# Aether-gate — IC-9700 PTT safety-layer tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The 9700 PTT layer is the FIRST place the gate keys real RF, so its safety
guards are pinned here: DISARMED by default, refuse out-of-band, refuse without
a CI-V session, and a watchdog that force-unkeys. A fake CI-V records the raw
1C 00 <on/off> bytes so we can assert exactly when the rig would (not) key.

Run:  python -m aether_gate.tests.test_ic9700_tx
"""
import sys
import threading


class _FakeCiv:
    """Stands in for _Ic9700Stream: a settable freq + a log of PTT sends."""
    def __init__(self, freq_hz):
        self.freq_hz = freq_hz
        self.ptt = []                       # list of bools: True=key, False=unkey

    def _ptt_raw(self, on):
        self.ptt.append(bool(on))


def _adapter(freq_hz=145_140_000):
    from aether_gate.adapters.icom9700 import Icom9700Adapter
    a = Icom9700Adapter.__new__(Icom9700Adapter)
    a._civ = _FakeCiv(freq_hz)
    a._tx_armed = False
    a._tx_keyed = False
    a._tx_watchdog = None
    a._tx_lock = threading.Lock()
    return a


def test_disarmed_refuses_to_key():
    a = _adapter()
    assert a.key_tx() is False              # not armed
    assert a._civ.ptt == []                 # nothing sent to the rig
    assert a._tx_keyed is False
    print("ok  tx: disarmed -> key_tx refuses, no CI-V sent")


def test_armed_in_band_keys():
    a = _adapter(145_140_000)               # 2m, legal
    a.arm_tx()
    assert a.key_tx() is True
    assert a._civ.ptt == [True]             # 1C 00 01 sent once
    assert a._tx_keyed is True
    a.unkey_tx()
    assert a._civ.ptt == [True, False]      # 1C 00 00 sent
    assert a._tx_keyed is False
    a.disarm_tx()
    print("ok  tx: armed + in-band -> keys, unkey sends 1C 00 00")


def test_out_of_band_refuses_even_when_armed():
    a = _adapter(146_000_000 + 10_000_000)  # 156 MHz — outside every 9700 band
    a.arm_tx()
    assert a.key_tx() is False
    assert a._civ.ptt == []
    print("ok  tx: armed but out-of-band -> refused")


def test_70cm_in_band():
    a = _adapter(435_000_000)                # 70cm — TX allowed
    a.arm_tx()
    assert a.key_tx() is True
    a.disarm_tx()
    print("ok  tx: 70cm accepted as TX-allowed")


def test_23cm_tx_is_refused():
    # 23cm/1.2 GHz TX is DELIBERATELY disabled (Nigel's instruction): RX ok,
    # but the gate must refuse to KEY there even when armed + on-frequency.
    a = _adapter(1_296_000_000)              # 23cm
    a.arm_tx()
    assert a.key_tx() is False
    assert a._civ.ptt == []                  # never sent a key
    print("ok  tx: 23cm/1.2GHz TX refused (RX-only band for TX)")


def test_no_civ_session_refuses():
    a = _adapter()
    a._civ = None
    a.arm_tx()
    assert a.key_tx() is False
    print("ok  tx: no CI-V session -> refused")


def test_disarm_force_unkeys():
    a = _adapter(145_140_000)
    a.arm_tx(); a.key_tx()
    assert a._tx_keyed is True
    a.disarm_tx()                           # must unkey AND re-latch
    assert a._tx_keyed is False
    assert a._civ.ptt[-1] is False          # last send was unkey
    assert a._tx_armed is False
    print("ok  tx: disarm force-unkeys and re-latches the arm")


def test_watchdog_force_unkeys():
    # Shrink the cap so the test is fast; assert the watchdog unkeys on its own.
    import time
    a = _adapter(145_140_000)
    a.TX_MAX_KEY_S = 0.15
    a.arm_tx()
    assert a.key_tx() is True
    assert a._tx_keyed is True
    time.sleep(0.3)                         # let the watchdog fire
    assert a._tx_keyed is False, "watchdog did not force-unkey"
    assert a._civ.ptt[-1] is False
    print("ok  tx: watchdog force-unkeys after the hard cap")


# --- --rx-only (hard transmit disable) ---------------------------------------
# The engine AUTO-ARMS on every AE connect, so for an unattended gateway the arm
# itself has to be refused -- not just the key.

def test_rx_only_latch_survives_new():
    """REGRESSION GUARD, and the reason _rx_only is a CLASS attribute.

    _adapter() builds the adapter with __new__, so __init__ never runs. Were
    _rx_only instance-only it would simply be ABSENT here -- and a defensive
    getattr(self, "_rx_only", False) would read False, so every rx-only test
    below would pass while exercising nothing at all. Pin the class attribute."""
    from aether_gate.adapters.icom9700 import Icom9700Adapter, _Ic9700Stream
    assert "_rx_only" in vars(Icom9700Adapter), "_rx_only must be a CLASS attribute"
    assert "rx_only" in vars(_Ic9700Stream), "rx_only must be a CLASS attribute"
    a = _adapter()
    assert a._rx_only is False               # resolves with no __init__
    print("ok  rx-only: latch resolves on a __new__-built adapter (class attr)")


def test_rx_only_refuses_arm_and_key():
    a = _adapter(145_140_000)                # 2m -- would otherwise be legal
    a._rx_only = True
    a.arm_tx()                               # the engine's auto-arm-on-connect
    assert a._tx_armed is False, "rx-only must swallow the auto-arm"
    assert a.key_tx() is False
    assert a._civ.ptt == [], "nothing may reach the rig under rx-only"
    assert a._tx_keyed is False
    print("ok  rx-only: auto-arm no-ops, key_tx refuses, no CI-V sent")


def test_rx_only_blocks_ptt_at_the_civ_layer():
    """THE load-bearing guard. _ptt_raw is the only place PTT goes on the wire,
    and the engine DISCARDS key_tx()'s return value -- so a refusal further up
    cannot be relied on by itself. Unkey must still always be allowed: a latched
    transmitter has to be able to drop no matter what the flags say."""
    from aether_gate.adapters.icom9700 import _Ic9700Stream
    civ = _Ic9700Stream.__new__(_Ic9700Stream)
    sent = []
    civ._send_civ = lambda payload: sent.append(bytes(payload))
    assert civ.rx_only is False               # class default resolves
    civ.rx_only = True
    civ._ptt_raw(True)
    assert sent == [], "rx-only must not put a key-down on the wire"
    civ._ptt_raw(False)
    assert sent == [bytes([0x1C, 0x00, 0x00])], "unkey must NEVER be blocked"
    print("ok  rx-only: _ptt_raw blocks key-down, still allows unkey")


def test_rx_only_advertises_tx_capable_false():
    """AE greys its TX button off tx_capable, so an rx-only gate should not
    offer a control whose PTT it will refuse. Default must stay unchanged."""
    from aether_gate.adapters.icom9700 import Icom9700Adapter
    kw = dict(radio_ip="192.0.2.1", username="u", password="p")
    assert Icom9700Adapter(rx_only=True, **kw).capabilities.tx_capable is False
    assert Icom9700Adapter(**kw).capabilities.tx_capable is True
    print("ok  rx-only: tx_capable False; default unchanged (still True)")


def main():
    tests = [test_disarmed_refuses_to_key, test_armed_in_band_keys,
             test_out_of_band_refuses_even_when_armed, test_70cm_in_band,
             test_23cm_tx_is_refused, test_no_civ_session_refuses,
             test_disarm_force_unkeys, test_watchdog_force_unkeys,
             test_rx_only_latch_survives_new, test_rx_only_refuses_arm_and_key,
             test_rx_only_blocks_ptt_at_the_civ_layer,
             test_rx_only_advertises_tx_capable_false]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} tx-safety tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
