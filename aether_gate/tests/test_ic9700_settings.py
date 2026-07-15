#
# Aether-gate — IC-9700 CI-V SET-menu settings facility tests (no hardware).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The 1A 05 menu read/write facility: BCD value coding, the _dispatch capture
of a 1A 05 reply into _menu_replies, and the adapter-level read_setting decode
(enum label + level percent). Addresses are from the IC-9700 CI-V Reference.

This is the tooling behind diagnosing the TX-audio bare-carrier bug (e.g. is
LAN MOD Level 0?) and, later, auto-configuring the rig.

Run:  python3 -m aether_gate.tests.test_ic9700_settings
Exits non-zero on first failure.
"""
import sys


def test_bcd_roundtrip():
    from aether_gate.adapters.icom9700 import _bcd2, _unbcd
    # 2-byte level values: 0..255 as BCD, MSB pair first.
    for n in (0, 1, 5, 99, 100, 128, 254, 255):
        b = _bcd2(n)
        assert len(b) == 2, (n, b.hex())
        assert _unbcd(b) == n, (n, b.hex(), _unbcd(b))
    assert _bcd2(100).hex() == "0100", _bcd2(100).hex()
    assert _bcd2(255).hex() == "0255", _bcd2(255).hex()
    # single enum byte
    assert _unbcd(bytes([0x05])) == 5
    assert _unbcd(b"") is None
    print("ok  settings: BCD encode/decode roundtrip")


def test_settings_table():
    from aether_gate.adapters.icom9700 import IC9700_SETTINGS
    # The addresses that matter for the TX-audio path (from the CI-V reference).
    assert IC9700_SETTINGS["data_mod"]["subaddr"] == 0x0116
    assert IC9700_SETTINGS["lan_mod_level"]["subaddr"] == 0x0114
    assert IC9700_SETTINGS["usb_mod_level"]["subaddr"] == 0x0113
    assert IC9700_SETTINGS["data_mod"]["choices"][5] == "LAN"
    print("ok  settings: address table matches the CI-V reference")


def _bare_stream():
    """An _Ic9700Stream with just the fields the menu path touches."""
    import threading
    from aether_gate.adapters.icom9700 import _Ic9700Stream
    s = _Ic9700Stream.__new__(_Ic9700Stream)
    # fields _dispatch reads/writes on the 1A05 branch + scope guard
    s.n_fa = 0
    s.n_fb = 0
    s.freq_hz = None; s.mode = None
    s.rx2_freq_hz = None; s.rx2_mode = None
    s.other_freq_hz = None; s.other_mode = None
    s.dualwatch = False; s.smeter_raw = None; s._reading_rx2 = False
    s._menu_replies = {}
    s._menu_evt = threading.Event()
    s._menu_lock = threading.Lock()
    s._on_civ = lambda d: None                     # stub scope extraction
    return s


def _civ_frame(cmd_and_data):
    """Wrap CI-V payload as a radio->controller frame FE FE E0 A2 <...> FD."""
    return b"\xfe\xfe\xe0\xa2" + bytes(cmd_and_data) + b"\xfd"


def test_dispatch_captures_1a05_reply():
    s = _bare_stream()
    # Radio replies to a LAN MOD Level (0114) read with value 100 (BCD 01 00).
    frame = _civ_frame([0x1A, 0x05, 0x01, 0x14, 0x01, 0x00])
    s._dispatch(frame)
    assert 0x0114 in s._menu_replies, s._menu_replies
    assert s._menu_replies[0x0114] == b"\x01\x00", s._menu_replies[0x0114].hex()
    assert s._menu_evt.is_set()
    # A DATA MOD (0116) reply = single enum byte 05 (LAN).
    s._dispatch(_civ_frame([0x1A, 0x05, 0x01, 0x16, 0x05]))
    assert s._menu_replies[0x0116] == b"\x05"
    print("ok  settings: _dispatch captures 1A 05 replies by sub-address")


def test_read_setting_decode():
    # Drive the adapter-level decode without a radio: fake a _civ whose read_menu
    # returns canned bytes, and confirm read_setting maps them to value+label.
    from aether_gate.adapters.icom9700 import Icom9700Adapter

    class FakeCiv:
        def __init__(self, table): self.table = table
        def read_menu(self, subaddr, timeout=1.5): return self.table.get(subaddr)

    a = Icom9700Adapter.__new__(Icom9700Adapter)
    a._civ = FakeCiv({
        0x0114: b"\x00\x00",   # LAN MOD Level = 0  -> the bare-carrier smoking gun
        0x0116: b"\x05",       # DATA MOD = LAN
        0x0113: b"\x01\x28",   # USB MOD Level = 128 -> 50%
    })
    lan = a.read_setting("lan_mod_level")
    assert lan["value"] == 0 and lan["label"] == "0%", lan
    dm = a.read_setting("data_mod")
    assert dm["value"] == 5 and dm["label"] == "LAN", dm
    usb = a.read_setting("usb_mod_level")
    assert usb["value"] == 128 and usb["label"] == "50%", usb
    assert a.read_setting("nonesuch") is None
    print("ok  settings: read_setting decodes level% + enum label")


def test_auto_set_lan_mod_on_connect():
    # _ensure_lan_mod_ready(): raise LAN MOD Level if below lan_mod_min, leave a
    # deliberately-higher level alone, and skip entirely when disabled (min=0).
    from aether_gate.adapters.icom9700 import Icom9700Adapter, _bcd2, _unbcd

    class RecCiv:
        """Fake CI-V: read_menu returns the stored value; write_menu records +
        applies it, so a subsequent read reflects the write (readback works)."""
        def __init__(self, level_bytes):
            self.store = {0x0114: level_bytes}
            self.writes = []
        def read_menu(self, subaddr, timeout=1.5):
            return self.store.get(subaddr)
        def write_menu(self, subaddr, value_bytes, settle=0.25):
            self.writes.append((subaddr, _unbcd(value_bytes)))
            self.store[subaddr] = bytes(value_bytes)
            return True

    def mk(level, minv=128):
        a = Icom9700Adapter.__new__(Icom9700Adapter)
        a.lan_mod_min = minv
        a._civ = RecCiv(_bcd2(level))
        return a

    # 1. level 0 -> writes lan_mod_min (128)
    a = mk(0); a._ensure_lan_mod_ready()
    assert a._civ.writes == [(0x0114, 128)], a._civ.writes
    assert _unbcd(a._civ.store[0x0114]) == 128     # readback reflects the fix

    # 2. level already 200 (>=min) -> NO write (respect a deliberate value)
    a = mk(200); a._ensure_lan_mod_ready()
    assert a._civ.writes == [], a._civ.writes

    # 3. disabled (lan_mod_min=0) -> no read, no write
    a = mk(0, minv=0); a._ensure_lan_mod_ready()
    assert a._civ.writes == []
    print("ok  settings: auto-set LAN MOD on connect (fix-if-low, leave-if-set, disable)")


def main():
    tests = [test_bcd_roundtrip, test_settings_table,
             test_dispatch_captures_1a05_reply, test_read_setting_decode,
             test_auto_set_lan_mod_on_connect]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} settings-facility tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
