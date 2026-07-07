#
# Aether-gate — IC-7300 offline tests (no hardware).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Run:  python3 -m aether_gate.tests.test_icom7300"""
import sys


def test_registry():
    from aether_gate.adapters import available, get_adapter
    from aether_gate.adapters.icom7300 import Icom7300Adapter
    assert "icom7300" in available(), available()
    assert get_adapter("icom7300") is Icom7300Adapter
    print("ok  registry: icom7300 present")


def test_bcd_frequency_roundtrip():
    from aether_gate.adapters.icom7300 import encode_bcd_freq, decode_bcd_freq
    for hz in (1_840_000, 7_030_500, 14_074_000, 50_313_000):
        enc = encode_bcd_freq(hz)
        assert len(enc) == 5
        assert decode_bcd_freq(enc) == hz, (hz, enc.hex())
    print("ok  bcd: 5-byte frequency roundtrip")


def test_bcd_level_roundtrip():
    from aether_gate.adapters.icom7300 import encode_bcd_level, decode_bcd_level
    assert encode_bcd_level(0) == b"\x00\x00"
    assert encode_bcd_level(255) == b"\x02\x55"
    for val in (0, 1, 99, 120, 255):
        assert decode_bcd_level(encode_bcd_level(val)) == val
    assert decode_bcd_level(encode_bcd_level(999)) == 255
    print("ok  bcd: analog level roundtrip")


def test_civ_frame_build_parse_extract():
    from aether_gate.adapters.icom7300 import build_civ_frame, parse_civ_frame, extract_civ_frames
    frame = build_civ_frame(b"\x25\x00", radio=0x94, controller=0xE0)
    assert frame == b"\xFE\xFE\x94\xE0\x25\x00\xFD", frame.hex()
    frames, tail = extract_civ_frames(b"junk" + frame + b"\xfe")
    assert frames == [frame]
    assert tail == b"\xfe"
    assert parse_civ_frame(frame) == (0x94, 0xE0, b"\x25\x00")
    print("ok  civ: frame build/parse/extract")


def test_scope_segment_combiner():
    from aether_gate.adapters.icom7300 import Icom7300ScopeAssembler, encode_bcd_freq
    asm = Icom7300ScopeAssembler()
    # Sequence 1: mode=center, center=14.100 MHz, half-span=250 kHz.
    seq1 = b"\x27\x00\x00\x01\x11\x00" + encode_bcd_freq(14_100_000) + encode_bcd_freq(250_000) + b"\x00"
    assert asm.feed_payload(seq1) is None
    expected = bytearray()
    for seq in range(2, 11):
        chunk = bytes([(seq * 10 + i) % 161 for i in range(50)])
        expected.extend(chunk)
        payload = b"\x27\x00\x00" + bytes([(seq // 10 << 4) | (seq % 10), 0x11]) + chunk
        assert asm.feed_payload(payload) is None
    tail = bytes([(110 + i) % 161 for i in range(25)])
    expected.extend(tail)
    final = b"\x27\x00\x00\x11\x11" + tail
    row = asm.feed_payload(final)
    assert row == bytes(expected)
    assert len(row) == 475
    assert asm.frames == 1
    assert asm.start_hz == 13_850_000
    assert asm.end_hz == 14_350_000
    print("ok  scope: segmented 11-part row -> 475 bytes")


def test_vfo_and_mode_filter_parsing():
    from aether_gate.adapters.icom7300 import Icom7300SerialCiv, encode_bcd_freq, encode_bcd_level
    c = Icom7300SerialCiv("/dev/test")
    c._handle_frame(0xE0, 0x94, b"\x25\x00" + encode_bcd_freq(7_188_870))
    c._handle_frame(0xE0, 0x94, b"\x25\x01" + encode_bcd_freq(14_225_000))
    c._handle_frame(0xE0, 0x94, b"\x04\x01\x02")
    c._handle_frame(0xE0, 0x94, b"\x26\x01\x00\x03")
    c._handle_frame(0xE0, 0x94, b"\x14\x02" + encode_bcd_level(201))
    assert c.freq_hz == 7_188_870
    assert c.other_freq_hz == 14_225_000
    assert c.mode == "USB"
    assert c.filter == 2
    assert c.other_mode == "LSB"
    assert c.other_filter == 3
    assert c.levels[0x02] == 201
    print("ok  civ: VFO A/B, mode/filter, levels parsed")


def test_scope_span_and_filter_width_helpers():
    from aether_gate.adapters.icom7300 import (
        choose_scope_half_span, filter_width_to_index, filter_index_to_width,
        encode_bcd_freq, Icom7300SerialCiv,
    )
    assert choose_scope_half_span(4800) == 2500
    assert choose_scope_half_span(120000) == 50000
    assert choose_scope_half_span(900000) == 250000
    idx = filter_width_to_index(2400, "USB")
    assert filter_index_to_width(idx, "USB") == 2400
    c = Icom7300SerialCiv("/dev/test")
    c._handle_frame(0xE0, 0x94, b"\x27\x15\x00" + encode_bcd_freq(50_000, 3))
    assert c.scope_half_span_hz == 50_000
    print("ok  scope/filter: span selection and width encoding")


def test_adapter_caps():
    from aether_gate.adapters.icom7300 import Icom7300Adapter
    a = Icom7300Adapter(usb_civ_port="/dev/null")
    assert a.provides == "spectrum"
    assert a.capabilities.model == "FLEX-6600"
    assert a.capabilities.tx_capable is False
    assert a.capabilities.max_slices == 1
    assert "20m" in a.capabilities.bands
    assert "6m" in a.capabilities.bands
    print("ok  adapter: caps are HF/6m RX/control-safe, one live receiver")


def test_receivers_exposes_only_selected_vfo():
    from aether_gate.adapters.icom7300 import Icom7300Adapter
    a = Icom7300Adapter(usb_civ_port="/dev/null")
    class FakeCiv:
        freq_hz = 14_225_000
        mode = "USB"
        filter = 1
        other_freq_hz = 3_504_790
        other_mode = "CW"
        other_filter = 1
        smeter_raw = 0
        levels = {}
        preamp = None
        attenuator_db = None
        split = False
        tuner = None
        latest_dbm = []
        scope_half_span_hz = None
        n_fb = 0
        n_fa = 0
        last_err = None
        @property
        def scope_bounds(self):
            return None, None
        @property
        def scope_frames(self):
            return 0
    a._civ = FakeCiv()
    assert a.receivers() == [{"freq_hz": 14_225_000, "mode": "USB"}]
    d = a.diagnostics()
    assert d["vfos"][1]["freq_hz"] == 3_504_790
    print("ok  receivers: VFO B diagnostics without live slice B")


def test_control_methods_queue_without_serial_write():
    from aether_gate.adapters.icom7300 import Icom7300Adapter

    class FakeCiv:
        def __init__(self):
            self.freq_hz = 7_100_000
            self.mode = "USB"
            self.filter = 1
            self.filter_width_hz = 2200
            self.scope_half_span_hz = 10000
            self.calls = []

        def set_freq(self, hz):
            self.calls.append(("freq", hz))
            self.freq_hz = hz
            return True

        def set_mode(self, mode, filt=None):
            self.calls.append(("mode", mode, filt))
            self.mode = mode
            self.filter = filt
            return True

        def set_filter_width(self, hz):
            self.calls.append(("width", hz))
            self.filter_width_hz = hz
            return True

        def set_scope_span(self, span):
            self.calls.append(("span", span))
            self.scope_half_span_hz = 25000
            return True

    a = Icom7300Adapter(usb_civ_port="/dev/null")
    fake = FakeCiv()
    a._civ = fake
    a.retune(7_200_000)
    a.set_mode("USB")
    a.set_filter(2)
    a.set_filter_width_hz(2400)
    a.set_span(50_000)
    assert fake.calls == []
    a._apply_control_targets()
    assert ("freq", 7_200_000) in fake.calls
    assert ("mode", "USB", 2) in fake.calls
    assert ("width", 2400) in fake.calls
    assert ("span", 50_000.0) in fake.calls
    print("ok  control: tune/mode/span queue off command path")


def test_adapter_audio_fake_pcm():
    from aether_gate.adapters import icom7300

    class FakeStdout:
        def __init__(self):
            self.data = b"\x00\x00\x00\x40\x00\xc0\xff\x7f"

        def read(self, n):
            out = self.data[:n]
            self.data = self.data[n:]
            return out

    class FakeProc:
        def __init__(self, *args, **kwargs):
            self.stdout = FakeStdout()
            self.stderr = FakeStdout()

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

    old_popen, old_which = icom7300.subprocess.Popen, icom7300.shutil.which
    try:
        icom7300.subprocess.Popen = FakeProc
        icom7300.shutil.which = lambda name: "/usr/bin/arecord"
        cap = icom7300.AlsaPcmCapture(device="hw:FAKE", frames_per_read=4)
        got = cap.read(4)
    finally:
        icom7300.subprocess.Popen = old_popen
        icom7300.shutil.which = old_which
    assert len(got) == 4
    assert got[0] == 0.0
    assert 0.49 < got[1] < 0.51
    assert -0.51 < got[2] < -0.49
    assert 0.99 < got[3] <= 1.0
    print("ok  audio: fake ALSA PCM -> mono floats")


def test_setup_argv_builder_includes_icom7300():
    from aether_gate.setup import _build_argv
    cfg = {"adapter": "icom7300", "usb_civ_port": "/dev/ttyUSB7300",
           "usb_civ_baud": "115200", "civ_addr": "0x94", "ae": "10.0.1.10"}
    argv = _build_argv(cfg)
    joined = " ".join(argv)
    assert "--adapter icom7300" in joined
    assert "--usb-civ-port /dev/ttyUSB7300" in joined
    assert "--usb-civ-baud 115200" in joined
    assert "--civ-addr 0x94" in joined
    print("ok  setup: icom7300 argv built")


def test_serial_open_holds_rts_dtr_low():
    from aether_gate.adapters import icom7300

    class FakeSerial:
        instances = []

        def __init__(self):
            self.port = None
            self.baudrate = None
            self.timeout = None
            self.rtscts = None
            self.dsrdtr = None
            self.rts = None
            self.dtr = None
            self.opened = False
            self.rts_calls = []
            self.dtr_calls = []
            FakeSerial.instances.append(self)

        def open(self):
            assert self.rts is False
            assert self.dtr is False
            assert self.rtscts is False
            assert self.dsrdtr is False
            self.opened = True

        def setRTS(self, val):
            self.rts_calls.append(val)
            self.rts = val

        def setDTR(self, val):
            self.dtr_calls.append(val)
            self.dtr = val

        def send_break(self, val=False):
            self.break_val = val

        def read(self, n):
            return b""

        def write(self, data):
            self.last_write = data
            return len(data)

        def close(self):
            self.opened = False

    class FakeSerialModule:
        Serial = FakeSerial

    old = icom7300.serial
    try:
        icom7300.serial = FakeSerialModule
        c = icom7300.Icom7300SerialCiv("/dev/test", 115200)
        c.open()
        c.close()
    finally:
        icom7300.serial = old

    s = FakeSerial.instances[0]
    assert s.rts_calls and all(v is False for v in s.rts_calls)
    assert s.dtr_calls and all(v is False for v in s.dtr_calls)
    print("ok  serial: RTS/DTR held low on open/close")


def test_native_scope_slice_tune_recenters_pan_status():
    from aether_gate.adapters.base import AdapterCaps
    from aether_gate.core import Radio

    class FakeAdapter:
        capabilities = AdapterCaps(model="FLEX-6600", max_slices=1, native_centered_scope=True)
        provides = "spectrum"

        def __init__(self):
            self.tunes = []

        def initial_center_hz(self):
            return 7_029_500

        def initial_mode(self):
            return "CW"

        def receivers(self):
            return [{"freq_hz": 7_029_500, "mode": "CW"}]

        def retune(self, hz):
            self.tunes.append(int(hz))

    class FakeConn:
        def __init__(self):
            self.lines = []

        def sendall(self, data):
            self.lines.extend(data.decode("utf-8").splitlines())

    adapter = FakeAdapter()
    radio = Radio("127.0.0.1", None, adapter=adapter, port=5992)
    conn = FakeConn()
    radio.streaming = True
    pid = radio._new_pan()
    radio.slices[0] = {"freq": 7.0295, "mode": "CW", "active": True, "pan": pid}
    radio.pans[pid]["slice"] = 0
    radio.pans[pid]["center"] = 7.0345
    radio.active_slice = 0

    radio.on_line(conn, "C1|slice tune 0 7.029500 autopan=0")

    assert adapter.tunes[-1] == 7_029_500
    pan_lines = [line for line in conn.lines if "display pan 0x40000000" in line]
    assert pan_lines, conn.lines
    assert "center=7.029500" in pan_lines[-1], pan_lines[-1]
    assert radio.pans[pid]["center"] == 7.0295
    print("ok  native scope: slice tune recenters pan status")


def test_pan_segment_and_band_zoom_drive_adapter_span():
    from aether_gate.adapters.base import AdapterCaps
    from aether_gate.core import Radio

    class FakeAdapter:
        capabilities = AdapterCaps(model="FLEX-6600", max_slices=1,
                                   native_centered_scope=True,
                                   min_span_hz=5_000.0, max_span_hz=500_000.0)
        provides = "spectrum"

        def __init__(self):
            self.spans = []

        def initial_center_hz(self):
            return 7_029_500

        def initial_mode(self):
            return "CW"

        def receivers(self):
            return [{"freq_hz": 7_029_500, "mode": "CW"}]

        def set_span(self, hz):
            self.spans.append(int(hz))
            return float(hz)

    class FakeConn:
        def __init__(self):
            self.lines = []

        def sendall(self, data):
            self.lines.extend(data.decode("utf-8").splitlines())

    adapter = FakeAdapter()
    radio = Radio("127.0.0.1", None, adapter=adapter, port=5992)
    conn = FakeConn()
    radio.streaming = True
    pid = radio._new_pan()
    original_span_hz = int(radio.span_mhz * 1e6)

    radio.on_line(conn, "C1|display pan set 0x40000000 band_zoom=1")
    assert adapter.spans[-1] == 500_000
    assert "bandwidth=0.500000" in [l for l in conn.lines if "display pan" in l][-1]
    radio.on_line(conn, "C2|display pan set 0x40000000 band_zoom=0")
    assert adapter.spans[-1] == original_span_hz
    radio.on_line(conn, "C3|display pan set 0x40000000 segment_zoom=1")
    assert adapter.spans[-1] == 50_000
    assert radio.pans[pid]["center"] == 7.0295
    print("ok  pan zoom: S/B toggles drive adapter span")


def main():
    tests = [test_registry, test_bcd_frequency_roundtrip, test_bcd_level_roundtrip,
             test_civ_frame_build_parse_extract, test_scope_segment_combiner,
             test_vfo_and_mode_filter_parsing, test_scope_span_and_filter_width_helpers,
             test_adapter_caps, test_receivers_exposes_only_selected_vfo,
             test_control_methods_queue_without_serial_write,
             test_adapter_audio_fake_pcm, test_setup_argv_builder_includes_icom7300,
             test_serial_open_holds_rts_dtr_low,
             test_native_scope_slice_tune_recenters_pan_status,
             test_pan_segment_and_band_zoom_drive_adapter_span]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} icom7300 tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
