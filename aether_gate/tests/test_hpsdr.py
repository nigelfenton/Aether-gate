#
# Aether-gate — HPSDR Protocol-1 primitives tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The vendored hpsdr_proto encoders + EP6 IQ decode. These are the wire facts
verified live against a real HL2 and Nigel's Radioberry (WWV at DC); the tests
lock the byte layout so a refactor can't silently break the tune/decode.

Run:  python3 -m aether_gate.tests.test_hpsdr
Exits non-zero on first failure.
"""
import struct
import sys

from aether_gate.adapters.hpsdr import hpsdr_proto as hp


def test_register_constants():
    # C0 = addr<<1 (MOX=0). config=0x00, RX1 freq=0x04, ADC gain=0x14 (reg 0x0a).
    assert hp.C0_CONFIG == 0x00
    assert hp.C0_RX1_FREQ == 0x04
    assert hp.C0_ADC_GAIN == 0x14
    assert hp.CONFIG_MERCURY == 0x40      # C1 bit6 — mandatory ADC select
    assert hp.CONFIG_DUPLEX == 0x04       # C4 bit2
    assert hp.METIS_PORT == 1024
    print("ok  hpsdr: register constants")


def test_cc_config_has_mercury_and_duplex():
    cc = hp.cc_config(speed=0, n_rx=1)
    assert len(cc) == 5, cc.hex()
    assert cc[0] == hp.C0_CONFIG
    # C1 carries speed | CONFIG_MERCURY — bit6 MUST be set or the rig gives flat noise.
    assert cc[1] & hp.CONFIG_MERCURY, f"CONFIG_MERCURY missing: C1=0x{cc[1]:02x}"
    # C4 carries the duplex bit
    assert cc[4] & hp.CONFIG_DUPLEX, f"CONFIG_DUPLEX missing: C4=0x{cc[4]:02x}"
    # speed 2 (192k) lands in C1 low bits
    assert hp.cc_config(speed=2)[1] & 0x3 == 2
    print("ok  hpsdr: cc_config sets CONFIG_MERCURY + DUPLEX")


def test_cc_rx1_freq_is_bigendian_hz():
    # RX1 NCO: C0=0x04, C1..C4 = freq Hz, 32-bit big-endian.
    cc = hp.cc_rx1_freq(10_000_000)
    assert cc[0] == hp.C0_RX1_FREQ
    assert cc[1:5] == struct.pack(">I", 10_000_000), cc[1:5].hex()
    print("ok  hpsdr: cc_rx1_freq = BE Hz (10 MHz -> 00989680)")


def test_cc_rx_gain_code():
    # ADC gain: C4 = 0x40 | (dB+12), clamped 0..60. +20 dB -> code 32 -> 0x60.
    assert hp.cc_rx_gain(20)[4] == (0x40 | 32)
    assert hp.cc_rx_gain(-12)[4] == (0x40 | 0)     # min
    assert hp.cc_rx_gain(48)[4] == (0x40 | 60)     # max
    print("ok  hpsdr: cc_rx_gain code = 0x40 | (dB+12)")


def test_metis_command_and_ep2_framing():
    assert hp.metis_command(0x01)[:4] == bytes([0xEF, 0xFE, 0x04, 0x01])
    assert len(hp.metis_command(0x01)) == 64
    cc_a, cc_b = hp.cc_config(), hp.cc_rx1_freq(14_100_000)
    pkt = hp.ep2_packet(7, cc_a, cc_b)
    assert pkt[:4] == bytes([0xEF, 0xFE, 0x01, 0x02])
    assert struct.unpack(">I", pkt[4:8])[0] == 7
    assert len(pkt) == 1032                        # 8 + 512 + 512
    # each frame: 7F7F7F + 5B C&C + 504 zero
    assert pkt[8:11] == hp.SYNC and pkt[11:16] == cc_a
    assert pkt[520:523] == hp.SYNC and pkt[523:528] == cc_b
    print("ok  hpsdr: metis_command + ep2_packet framing")


def _make_ep6(iq_pairs):
    """Build a synthetic EP6 packet from up to 126 (I,Q) 24-bit pairs."""
    def frame(samples):
        payload = bytearray()
        for i, q in samples:
            payload += int(i).to_bytes(3, "big", signed=True)
            payload += int(q).to_bytes(3, "big", signed=True)
            payload += b"\x00\x00"                 # mic
        payload += b"\x00" * (504 - len(payload))
        return hp.SYNC + b"\x00" * 5 + bytes(payload)
    a = frame(iq_pairs[:63])
    b = frame(iq_pairs[63:126])
    return bytes([0xEF, 0xFE, 0x01, 0x06]) + struct.pack(">I", 1) + a + b


def test_ep6_iq_decode_roundtrip():
    pairs = [(1000, -2000), (-8_388_608, 8_388_607), (0, 0), (123456, -654321)]
    pkt = _make_ep6(pairs)
    assert hp.ep6_seq(pkt) == 1
    got = list(hp.iq_samples(pkt))
    # the 4 real pairs come back exactly (rest are zero-padded samples)
    assert got[:4] == pairs, got[:4]
    seq, n, peak, sumsq, sync_ok = hp.parse_ep6(pkt)
    assert sync_ok and n == 126                     # 63 samples x 2 frames
    assert peak == 8_388_608                        # abs(-2^23) = full-scale magnitude
    print("ok  hpsdr: EP6 24-bit BE I/Q decode roundtrip")


def test_adapter_registered_iq_provider():
    from aether_gate.adapters import get_adapter
    from aether_gate.adapters.hpsdr import HpsdrAdapter
    assert get_adapter("hpsdr") is HpsdrAdapter
    assert HpsdrAdapter.provides == "iq"
    # constructs without hardware (numpy import is deferred to open())
    a = HpsdrAdapter.__new__(HpsdrAdapter)
    print("ok  hpsdr: adapter registered as an iq provider")


def main():
    tests = [test_register_constants, test_cc_config_has_mercury_and_duplex,
             test_cc_rx1_freq_is_bigendian_hz, test_cc_rx_gain_code,
             test_metis_command_and_ep2_framing, test_ep6_iq_decode_roundtrip,
             test_adapter_registered_iq_provider]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} hpsdr tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
