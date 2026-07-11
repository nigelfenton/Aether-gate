#
# Aether-gate — IC-9700 CI-V dispatch tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Transport ported from github.com/w5jwp/SDR9700 (GPL-3.0) by Justin W5JWP.
#
"""_Ic9700Stream._dispatch() parses control CI-V (freq/mode/S-meter replies +
front-panel transceive broadcasts) out of the datagrams the ported UdpCivData
transport hands it. Those datagrams ALSO carry the raw band-scope waveform
frames (`FE FE E0 A2 27 00 00 <~500 amplitude bytes> FD`), and the amplitude
bytes routinely contain stray `FD` terminators and `FE FE` sequences.

REGRESSION (2026-07-10): a scope waveform frame must NEVER be scanned for CI-V
control frames — otherwise 5 random amplitude bytes decode as a `25 00`/`00`
BCD frequency and the reported freq jumps wildly around the band (144.4 ->
144.6 -> 145.1 in ~1s), so AE can never land on the real receive frequency.
The fix: when a datagram contains `27 00 00`, extract the scope and RETURN
without running the generic `fe fe ... fd` scan (freq/mode/S-meter replies
always arrive in their OWN datagrams).

Run:  python3 -m aether_gate.tests.test_ic9700_dispatch
"""
import sys


def _bare_stream():
    """An _Ic9700Stream with just the fields _dispatch touches — no transport.
    _on_civ (scope extraction) is stubbed so we test only the control parse."""
    from aether_gate.adapters.icom9700 import _Ic9700Stream
    s = _Ic9700Stream.__new__(_Ic9700Stream)
    s.freq_hz = None
    s.mode = None
    s.rx2_freq_hz = None
    s.rx2_mode = None
    s.other_freq_hz = None
    s.other_mode = None
    s.dualwatch = False
    s.smeter_raw = None
    s._reading_rx2 = False
    s.n_fa = 0
    s.n_fb = 0
    s._scope_seen = []
    s._on_civ = lambda d: s._scope_seen.append(d)   # stub scope extraction
    return s


def _civ(*payload_bytes, radio=0xA2, ctrl=0xE0):
    # FE FE <ctrl> <radio> <payload...> FD  — a reply TO us is a2->e0
    return bytes([0xFE, 0xFE, ctrl, radio]) + bytes(payload_bytes) + bytes([0xFD])


def test_real_freq_reply_is_parsed():
    s = _bare_stream()
    # 25 00 <5 BCD> selected-VFO freq = 145.210000, BCD lo-pair first
    # (145210000 Hz -> 00 00 21 45 01).
    bcd = bytes([0x00, 0x00, 0x21, 0x45, 0x01])
    s._dispatch(_civ(0x25, 0x00, *bcd))
    assert s.freq_hz == 145_210_000, s.freq_hz
    print("ok  dispatch: a real 25 00 freq reply is parsed (145.210)")


def test_real_mode_reply_is_parsed():
    s = _bare_stream()
    # 26 00 <mode> <filt> ; mode 05 = FM
    s._dispatch(_civ(0x26, 0x00, 0x05, 0x01))
    assert s.mode == "FM", s.mode
    print("ok  dispatch: a real 26 00 mode reply is parsed (FM)")


def test_scope_frame_never_corrupts_freq():
    # The bug: a scope waveform frame whose amplitude bytes happen to contain
    # `FE FE 00 A2 00 <5 bytes> FD` would decode as a transceive freq. Craft
    # exactly that poison and assert freq_hz stays None.
    s = _bare_stream()
    s.freq_hz = None
    # a plausible scope frame: FE FE E0 A2 27 00 00 <waveform...> FD
    # embed a poison CI-V-looking run inside the waveform amplitude bytes.
    poison = bytes([0xFE, 0xFE, 0x00, 0xA2, 0x00,
                    0x00, 0x00, 0x60, 0x14, 0x14,   # would BCD-decode to ~141.x MHz
                    0xFD])
    waveform = bytes(range(240, 256)) * 4 + poison + bytes(range(200, 216)) * 4
    frame = bytes([0xFE, 0xFE, 0xE0, 0xA2, 0x27, 0x00, 0x00]) + waveform + bytes([0xFD])
    s._dispatch(frame)
    assert s.freq_hz is None, f"scope waveform corrupted freq_hz -> {s.freq_hz}"
    assert len(s._scope_seen) == 1, "scope frame should still be extracted once"
    print("ok  dispatch: a scope frame with poison bytes does NOT corrupt freq")


def test_scope_frame_does_not_block_a_later_real_reply():
    # After a scope frame, a genuine freq reply in its OWN datagram still parses.
    s = _bare_stream()
    frame = bytes([0xFE, 0xFE, 0xE0, 0xA2, 0x27, 0x00, 0x00]) + bytes(range(0, 200)) + bytes([0xFD])
    s._dispatch(frame)
    assert s.freq_hz is None
    bcd = bytes([0x00, 0x00, 0x21, 0x45, 0x01])       # 145.210
    s._dispatch(_civ(0x25, 0x00, *bcd))
    assert s.freq_hz == 145_210_000, s.freq_hz
    print("ok  dispatch: a real reply after a scope frame still parses")


def main():
    tests = [test_real_freq_reply_is_parsed,
             test_real_mode_reply_is_parsed,
             test_scope_frame_never_corrupts_freq,
             test_scope_frame_does_not_block_a_later_real_reply]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} dispatch tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
