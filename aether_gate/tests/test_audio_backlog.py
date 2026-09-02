#
# Aether-gate — the demodulator may not fall behind the antenna (no hardware).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The IQ queue feeding the demodulator is bounded in TIME, not blocks.

Measured 2026-09-01 on an RSPduo at 125 kS/s: audio trailed the panadapter by
about half a second and grew with every stall of the reader thread, because
the demod consumes at playback pace and the queue's cap was 64 blocks — 131 ms
at an RTL's 2.04 MS/s, 2.1 s at 125 kS/s. The cap is now a duration, and the
oldest blocks are dropped (and counted) when a stall leaves the queue deeper.

Run:  python -m aether_gate.tests.test_audio_backlog
"""
from aether_gate.adapters.soapy import SoapyAdapter, _AUDIO_BACKLOG_S

BLOCK = 4096


def _adapter(rate):
    return SoapyAdapter(driver="none", samp_rate=rate)


def _fill(a, blocks):
    for i in range(blocks):
        a._queue_audio([complex(i, 0)] * BLOCK)      # len() is all the bound looks at


def test_a_stall_at_a_low_rate_is_trimmed_to_the_time_bound():
    a = _adapter(125_000.0)
    _fill(a, 64)                                     # the old cap: 2.1 s at this rate
    assert a.audio_backlog_ms() <= 1000.0 * _AUDIO_BACKLOG_S + 1000.0 * BLOCK / 125_000.0
    assert a._audio_dropped == 64 - len(a._audio_q)
    assert a._audio_dropped > 50, "nearly all of a 2 s backlog must go"


def test_the_same_blocks_at_a_high_rate_are_within_the_bound_and_kept():
    a = _adapter(2_040_000.0)
    _fill(a, 64)                                     # 128 ms at this rate: fine
    assert a._audio_dropped == 0
    assert len(a._audio_q) == 64


def test_the_newest_blocks_are_the_ones_kept():
    a = _adapter(125_000.0)
    _fill(a, 64)
    assert a._audio_q[-1][0] == complex(63, 0)
    assert a._audio_q[0][0].real > 0, "the oldest block must be the one that went"


def test_backlog_readout_follows_the_queue():
    a = _adapter(125_000.0)
    assert a.audio_backlog_ms() == 0.0
    _fill(a, 2)
    assert abs(a.audio_backlog_ms() - 1000.0 * 2 * BLOCK / 125_000.0) < 1e-6


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
