#
# Aether-gate — AE MOX -> real PTT wiring tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""AE's 'transmit set mox=1/0' must drive the adapter's GUARDED key_tx/unkey_tx
(before this, mox was only tracked + echoed to AE, so AE showed TX but the rig
never keyed — only the hand mic could TX). These tests drive the real
on_line('transmit set ...') path with a stub adapter that records key/unkey.

Run:  python -m aether_gate.tests.test_mox_ptt
"""
import sys
import threading


class _Conn:
    def __init__(self): self.out = bytearray()
    def sendall(self, b): self.out += b


class _TxAdapter:
    """Records arm/key/unkey. key_tx honours 'armed' like the real one."""
    def __init__(self):
        self.armed = False
        self.keyed = False
        self.calls = []                 # ("arm"|"key"|"unkey"|"disarm")

    def arm_tx(self):
        self.armed = True; self.calls.append("arm")

    def disarm_tx(self):
        self.keyed = False; self.armed = False; self.calls.append("disarm")

    def key_tx(self):
        self.calls.append("key")
        if not self.armed:
            return False
        self.keyed = True
        return True

    def unkey_tx(self):
        self.calls.append("unkey"); self.keyed = False


def _radio(adapter):
    from aether_gate.core.engine import Radio
    r = Radio.__new__(Radio)
    r.adapter = adapter
    r.send_lock = threading.Lock()
    r.handle_hex = "0000AAAA"
    r.tx_mox = False
    r.tx_tune = False
    r.conn = None
    r.emit_transmit_status = lambda: None       # not under test
    return r


def _tx(r, conn, mox, seq="1"):
    r.on_line(conn, f"C{seq}|transmit set mox={mox}")


def _xmit(r, conn, n, seq="1"):
    r.on_line(conn, f"C{seq}|xmit {n}")


def test_xmit_1_keys_the_rig():
    # THE REAL KEY PATH: AE sends 'xmit 1' to key (FlexBackend send("xmit %1")).
    a = _TxAdapter(); a.arm_tx()
    r = _radio(a)
    _xmit(r, _Conn(), "1")
    assert a.keyed is True, a.calls
    assert r.tx_mox is True
    print("ok  xmit: 'xmit 1' keys the rig (armed)")


def test_xmit_0_unkeys():
    a = _TxAdapter(); a.arm_tx()
    r = _radio(a)
    _xmit(r, _Conn(), "1")
    _xmit(r, _Conn(), "0", seq="2")
    assert a.keyed is False, a.calls
    assert a.calls[-1] == "unkey"
    assert r.tx_mox is False
    print("ok  xmit: 'xmit 0' unkeys the rig")


def test_xmit_refused_when_disarmed():
    a = _TxAdapter()                            # not armed
    r = _radio(a)
    _xmit(r, _Conn(), "1")
    assert a.keyed is False                     # key_tx refused
    print("ok  xmit: disarmed -> 'xmit 1' attempted but refused")


def test_mox_on_keys_when_armed():
    a = _TxAdapter(); a.arm_tx()                # armed (as auto-arm-on-connect would)
    r = _radio(a)
    _tx(r, _Conn(), "1")
    assert a.keyed is True, a.calls
    assert "key" in a.calls
    assert r.tx_mox is True
    print("ok  mox: mox=1 keys the rig (armed)")


def test_mox_off_unkeys():
    a = _TxAdapter(); a.arm_tx()
    r = _radio(a)
    _tx(r, _Conn(), "1")
    _tx(r, _Conn(), "0", seq="2")
    assert a.keyed is False, a.calls
    assert a.calls[-1] == "unkey"
    assert r.tx_mox is False
    print("ok  mox: mox=0 unkeys the rig")


def test_mox_refused_when_disarmed():
    a = _TxAdapter()                            # NOT armed
    r = _radio(a)
    _tx(r, _Conn(), "1")
    assert a.keyed is False                     # key_tx refused
    assert "key" in a.calls                     # it was attempted...
    assert r.tx_mox is True                     # AE still shows MOX (echoed)
    print("ok  mox: disarmed -> key attempted but refused, rig stays RX")


def test_adapter_without_ptt_is_safe():
    # An adapter with no key_tx (sim / 7300) must not error on a MOX command.
    class _NoTx: pass
    r = _radio(_NoTx())
    _tx(r, _Conn(), "1")                        # must not raise
    assert r.tx_mox is True
    print("ok  mox: adapter without PTT -> MOX tracked, no crash")


class _Caps:
    def __init__(self, tx): self.tx_capable = tx


def _radio_for_slice(tx_capable):
    from aether_gate.core.engine import Radio
    r = Radio.__new__(Radio)
    class _A: pass
    a = _A(); a.capabilities = _Caps(tx_capable)
    r.adapter = a
    r.send_lock = threading.Lock()
    r.handle_hex = "0000AAAA"
    r.slices = {0: {"freq": 145.07, "mode": "FM", "active": True, "pan": 0x40000000}}
    r.pans = {0x40000000: {"center": 145.07, "slice": 0, "wf_id": 0x42000000}}
    r._primary_pan = lambda: 0x40000000
    return r


def test_tx_slice_advertised_when_tx_capable():
    # AE greys the TX button until a slice reports tx=1. A TX-capable radio must
    # mark its active slice tx=1 (+ a TX antenna) so AE un-greys it.
    r = _radio_for_slice(True)
    conn = _Conn()
    r.emit_slice_status(conn, 0)
    s = conn.out.decode()
    assert "tx=1" in s, s
    assert "tx_ant=" in s, s
    print("ok  slice: TX-capable radio advertises tx=1 (+tx_ant) on active slice")


def test_no_tx_slice_when_rx_only():
    # An RX-only radio must NOT claim a TX slice (button stays correctly greyed).
    r = _radio_for_slice(False)
    conn = _Conn()
    r.emit_slice_status(conn, 0)
    s = conn.out.decode()
    assert "tx=1" not in s, s
    print("ok  slice: RX-only radio sends no tx=1 (TX button stays greyed)")


def main():
    tests = [test_xmit_1_keys_the_rig, test_xmit_0_unkeys,
             test_xmit_refused_when_disarmed,
             test_mox_on_keys_when_armed, test_mox_off_unkeys,
             test_mox_refused_when_disarmed, test_adapter_without_ptt_is_safe,
             test_tx_slice_advertised_when_tx_capable, test_no_tx_slice_when_rx_only]
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
    print(f"\nall {len(tests)} mox-ptt tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
