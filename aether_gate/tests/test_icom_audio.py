#
# Aether-gate — IC-9700 LAN RX-audio parse tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Pin the RS-BA1 audio-packet parse + the 48k->24k decimation get_audio uses.

Run:  python -m aether_gate.tests.test_icom_audio
"""
import struct
import sys


def _audio_pkt(pcm_bytes, seq=1):
    """Build a 0x18-header RS-BA1 audio datagram carrying pcm_bytes (int16 LE)."""
    hdr = bytearray(0x18)
    struct.pack_into("<I", hdr, 0x00, 0x18 + len(pcm_bytes))  # len
    struct.pack_into("<H", hdr, 0x04, 0x0000)                 # type
    struct.pack_into("<H", hdr, 0x06, seq)                    # seq
    struct.pack_into(">H", hdr, 0x16, len(pcm_bytes))         # datalen (BE)
    return bytes(hdr) + pcm_bytes


def _fresh_audio():
    from aether_gate.adapters.icom.audio import Ic9700Audio
    a = Ic9700Audio.__new__(Ic9700Audio)      # skip socket adoption
    import threading
    a._ring = bytearray()
    a._ring_lock = threading.Lock()
    a.audio_frames = 0
    a.audio_bytes = 0
    a.dropped = 0
    return a


def test_parse_and_read_samples():
    a = _fresh_audio()
    # 4 int16 samples: 0, 16384(=0.5), -16384(-0.5), 32767(~1.0)
    pcm = struct.pack("<4h", 0, 16384, -16384, 32767)
    a._on_audio(_audio_pkt(pcm))
    assert a.audio_frames == 1
    assert a.ring_samples == 4, a.ring_samples
    out = a.read_samples(4)
    assert len(out) == 4
    assert abs(out[0] - 0.0) < 1e-6
    assert abs(out[1] - 0.5) < 1e-3
    assert abs(out[2] + 0.5) < 1e-3
    assert out[3] > 0.99
    assert a.ring_samples == 0                # drained
    print("ok  audio: 0x18 header parsed, LPCM16 -> float, ring drains")


def test_short_read_returns_available():
    a = _fresh_audio()
    a._on_audio(_audio_pkt(struct.pack("<2h", 100, 200)))
    out = a.read_samples(10)                   # ask more than we have
    assert len(out) == 2, out
    assert a.read_samples(1) == []             # empty now
    print("ok  audio: short ring returns what's there, then empty")


def test_odd_payload_trimmed():
    a = _fresh_audio()
    # 5 bytes = 2 whole int16 + 1 stray byte -> stray dropped
    a._on_audio(_audio_pkt(b"\x01\x00\x02\x00\x7f"))
    assert a.ring_samples == 2, a.ring_samples
    print("ok  audio: odd trailing byte trimmed to whole samples")


def test_get_audio_decimates_48k_to_24k():
    from aether_gate.adapters.icom9700 import Icom9700Adapter
    ad = Icom9700Adapter.__new__(Icom9700Adapter)
    ad._audio = _fresh_audio()
    # feed 256 samples @48k -> get_audio(128) should return 128 @24k (every 2nd)
    pcm = struct.pack("<256h", *[(i % 100) * 100 for i in range(256)])
    ad._audio._on_audio(_audio_pkt(pcm))
    got = ad.get_audio(128)
    assert got is not None and len(got) == 128, (got is None, len(got) if got else 0)
    # decimated: got[0]==src[0], got[1]==src[2] ...
    assert abs(got[0] - 0.0) < 1e-6
    assert abs(got[1] - (200 / 32768.0)) < 1e-4     # src[2] = (2%100)*100 = 200
    print("ok  audio: get_audio decimates 48k->24k, returns 128 samples")


def test_get_audio_none_without_stream():
    from aether_gate.adapters.icom9700 import Icom9700Adapter
    ad = Icom9700Adapter.__new__(Icom9700Adapter)
    ad._audio = None
    assert ad.get_audio(128) is None
    print("ok  audio: get_audio None when no audio session")


def main():
    tests = [test_parse_and_read_samples, test_short_read_returns_available,
             test_odd_payload_trimmed, test_get_audio_decimates_48k_to_24k,
             test_get_audio_none_without_stream]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} audio tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
