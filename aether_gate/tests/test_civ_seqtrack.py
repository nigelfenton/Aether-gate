#
# Aether-gate — IC-9700 RS-BA1 sequence-tracker tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The radio->us seq tracker (UdpBase._track_rx) decides which datagrams count as
"missing" and thus which retransmit requests we fire back. Getting the 16-bit
wraparound wrong manufactures phantom gaps -> a retransmit storm that piles onto
an already-struggling scope stream (the "deaf scope" amplifier). These tests pin
the wrap arithmetic and the missing/backlog behavior WITHOUT any socket.

Run:  python -m aether_gate.tests.test_civ_seqtrack
"""
import sys


class _Tracker:
    """UdpBase with the RX-tracker state set up by hand — no socket bound.
    We only exercise _track_rx / _seq_delta, which touch none of the transport."""
    def __init__(self):
        import threading
        from aether_gate.adapters.icom.udpbase import UdpBase
        self.u = UdpBase.__new__(UdpBase)
        self.u._lock = threading.Lock()
        self.u._rx_last = None
        self.u._rx_missing = {}
        self.u.n_rx_clears = 0

    def feed(self, *seqs):
        for s in seqs:
            self.u._track_rx(s & 0xFFFF)

    @property
    def missing(self):
        return set(self.u._rx_missing)

    @property
    def clears(self):
        return self.u.n_rx_clears


def test_seq_delta_wraps():
    from aether_gate.adapters.icom.udpbase import UdpBase
    assert UdpBase._seq_delta(5, 3) == 2
    assert UdpBase._seq_delta(3, 5) == -2
    # forward across the 0xFFFF -> 0 rollover is a SMALL positive step, not -65535
    assert UdpBase._seq_delta(0x0001, 0xFFFF) == 2
    assert UdpBase._seq_delta(0x0000, 0xFFFF) == 1
    # backward across the boundary is a small negative
    assert UdpBase._seq_delta(0xFFFF, 0x0001) == -2
    print("ok  seq_delta: signed distance wraps at 0x10000")


def test_no_missing_on_contiguous():
    t = _Tracker()
    t.feed(10, 11, 12, 13, 14)
    assert t.missing == set(), t.missing
    assert t.clears == 0
    print("ok  tracker: contiguous stream -> nothing missing")


def test_gap_is_recorded_then_filled():
    t = _Tracker()
    t.feed(10, 11)          # last=11
    t.feed(15)              # 12,13,14 missing
    assert t.missing == {12, 13, 14}, t.missing
    t.feed(13)              # arrives late -> removed from missing
    assert t.missing == {12, 14}, t.missing
    print("ok  tracker: forward gap recorded, late arrival clears it")


def test_gap_across_wrap_is_not_a_phantom_storm():
    # THE regression: a normal small forward gap straddling the 0xFFFF boundary
    # must record only the true in-between seqs — NOT reset, NOT enqueue ~65k.
    t = _Tracker()
    t.feed(0xFFFD, 0xFFFE)  # last = 0xFFFE
    t.feed(0x0001)          # 0xFFFF and 0x0000 are the two genuinely-missing seqs
    assert t.missing == {0xFFFF, 0x0000}, t.missing
    assert t.clears == 0, "a small wrap gap must not trigger a tracker reset"
    print("ok  tracker: small gap across 0xFFFF -> 2 missing, no reset/storm")


def test_big_jump_resets_cleanly():
    # A jump larger than the miss window (radio resynced / we fell far behind)
    # resets rather than enqueueing a huge phantom-missing set.
    t = _Tracker()
    t.feed(100, 101)
    t.feed(100 + 1000)      # way past MAX_MISSING
    assert t.missing == set(), t.missing
    assert t.clears == 1, t.clears
    print("ok  tracker: oversized jump resets, no phantom backlog")


def test_backward_seq_resets_not_negative_gap():
    # An out-of-order/backward seq (delta <= 0) must reset, never loop building
    # a 65k-entry missing set.
    t = _Tracker()
    t.feed(500, 501, 502)
    t.feed(400)             # backward
    assert t.missing == set(), t.missing
    assert t.clears == 1, t.clears
    print("ok  tracker: backward seq resets cleanly")


def main():
    tests = [test_seq_delta_wraps, test_no_missing_on_contiguous,
             test_gap_is_recorded_then_filled, test_gap_across_wrap_is_not_a_phantom_storm,
             test_big_jump_resets_cleanly, test_backward_seq_resets_not_negative_gap]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} seq-tracker tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
