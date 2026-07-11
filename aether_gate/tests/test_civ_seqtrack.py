#
# Aether-gate — IC-9700 RS-BA1 sequence-tracker tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Transport ported from github.com/w5jwp/SDR9700 (GPL-3.0) by Justin W5JWP.
#
"""The radio->us seq tracker (UdpBase._track_rx_seq) decides which datagrams count
as "missing" and thus which retransmit requests we fire back. This is a faithful
port of SDR9700's rxSeqBuf/rxMissing bookkeeping: a MAP of received seqs, with a
"large seq gap" guard that clears + re-seeds on a big forward jump, a rollback, OR
a seq below the current window (which includes the 16-bit wrap). These tests pin
that behavior to the reference (UdpBase.cpp dataReceived, lines ~264-333) WITHOUT
any socket.

NB the old from-spec tracker used a single `_rx_last` int + a signed `_seq_delta`
that "cleverly" recorded the true in-between seqs across the 0xFFFF wrap. SDR9700
does NOT do that — it treats `seq < firstKey` as a large-gap RESET (line 270). That
extra cleverness was part of the transport we replaced (it mis-judged gaps and
drove the deaf-scope stall); these tests assert the reference behavior instead.

Run:  python -m aether_gate.tests.test_civ_seqtrack
"""
import sys
import threading


class _Tracker:
    """A UdpBase with just the RX-tracker state set up by hand — no socket bound.
    Exercises _track_rx_seq / the rxSeqBuf map, which touch no transport I/O."""
    def __init__(self):
        from aether_gate.adapters.icom.udpbase import UdpBase
        self.u = UdpBase.__new__(UdpBase)
        self.u._lock = threading.Lock()
        self.u._rx_seq_buf = {}
        self.u._rx_missing = {}
        self.u.n_rx_clears = 0

    def feed(self, *seqs):
        for s in seqs:
            self.u._track_rx_seq(s & 0xFFFF, 1000)   # received_at ms constant

    @property
    def missing(self):
        return set(self.u._rx_missing)

    @property
    def clears(self):
        return self.u.n_rx_clears


def test_no_missing_on_contiguous():
    t = _Tracker()
    t.feed(10, 11, 12, 13, 14)
    assert t.missing == set(), t.missing
    assert t.clears == 0
    print("ok  tracker: contiguous stream -> nothing missing")


def test_gap_is_recorded_then_filled():
    t = _Tracker()
    t.feed(10, 11)          # last = 11
    t.feed(15)              # 12,13,14 genuinely missing
    assert t.missing == {12, 13, 14}, t.missing
    t.feed(13)              # arrives late -> removed from missing
    assert t.missing == {12, 14}, t.missing
    print("ok  tracker: forward gap recorded, late arrival clears it")


def test_wrap_is_a_clean_reset_like_the_reference():
    # SDR9700 treats a seq below the current window (which a 0xFFFF->0x0000 wrap
    # is) as a "large seq gap" -> clear rxSeqBuf + rxMissing, re-seed from this
    # seq (UdpBase.cpp:270-284). NOT the old code's "record the 2 in-between
    # seqs" cleverness. A wrap happens once per 65536 packets; the reset+reseed
    # is seamless (the reference never freezes on it).
    t = _Tracker()
    t.feed(0xFFFD, 0xFFFE)  # window high = 0xFFFE
    t.feed(0x0001)          # 0x0001 < firstKey(0xFFFD) -> reset + reseed
    assert t.missing == set(), t.missing
    assert t.clears == 1, t.clears
    print("ok  tracker: 0xFFFF wrap -> clean reset+reseed (reference behavior)")


def test_big_jump_resets_cleanly():
    # A forward jump larger than MAX_MISSING (radio resynced / we fell far behind)
    # resets rather than enqueueing a huge phantom-missing set.
    t = _Tracker()
    t.feed(100, 101)
    t.feed(100 + 1000)      # way past MAX_MISSING
    assert t.missing == set(), t.missing
    assert t.clears == 1, t.clears
    print("ok  tracker: oversized forward jump resets, no phantom backlog")


def test_backward_seq_resets():
    # An out-of-order/backward seq (below firstKey) resets, never loops building
    # a 65k-entry missing set.
    t = _Tracker()
    t.feed(500, 501, 502)
    t.feed(400)             # backward, below firstKey
    assert t.missing == set(), t.missing
    assert t.clears == 1, t.clears
    print("ok  tracker: backward seq resets cleanly")


def main():
    tests = [test_no_missing_on_contiguous, test_gap_is_recorded_then_filled,
             test_wrap_is_a_clean_reset_like_the_reference,
             test_big_jump_resets_cleanly, test_backward_seq_resets]
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
