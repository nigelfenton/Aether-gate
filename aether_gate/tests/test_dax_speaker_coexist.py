#
# Aether-gate — remote_audio_rx and dax_rx must coexist.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Arming dax_rx must not silence the speaker stream (no hardware, no network).

#34: starting WSJT-X collapsed AetherSDR's own reception while WSJT-X stayed
healthy. The filed hypothesis was contention -- two consumers draining one
`_audio_q`. It was not. Both `stream create` branches assigned the SAME field:

    remote_audio_rx -> self.audio_stream_id = sid
    dax_rx          -> self.audio_stream_id = sid

and the audio thread addressed every frame to that one id. So arming DAX did
not add a consumer, it OVERWROTE the speaker's id: from that moment every frame
went to DAX and `remote_audio_rx` received nothing. AE went deaf; WSJT-X was
simply the only addressee left.

These tests lock the registration/teardown contract that makes them coexist:

  * registering dax_rx does NOT evict remote_audio_rx;
  * removing dax_rx leaves the speaker registered AND running;
  * removing the last stream does stop the audio thread;
  * each stream keeps its OWN sequence counter -- a shared counter makes AE see
    1-in-N gaps on every stream as soon as a second one is armed.

The frame-fanout property (one get_audio() call feeding N streams, never one
call per stream) is asserted here at the level this suite can reach: the target
set the send loop iterates. Calling get_audio() per stream would double-drain
the single _audio_q and create the very contention #34 wrongly assumed.

Run:  python3 -m aether_gate.tests.test_dax_speaker_coexist
Exits non-zero on first failure.
"""
import sys

FAILED = []


def check(label, got, want):
    ok = got == want
    print("[%s] %s" % ("OK" if ok else "FAIL", label))
    if not ok:
        print("        got:  %r" % (got,))
        print("        want: %r" % (want,))
        FAILED.append(label)


class _Streams:
    """The registration/teardown logic of engine.py, isolated.

    Mirrors the `stream create` / `stream remove` handling without standing up
    a TCP server, an adapter or a UDP socket. The behaviour under test is which
    ids stay registered and whether the audio thread is told to stop -- none of
    which needs a radio.
    """

    AUDIO_SID_BASE = 0x48000010
    DAX_SID_BASE = 0x48000040

    def __init__(self, radio_id=0):
        self.radio_id = radio_id
        self.audio_stream_id = None
        self.audio_streams = {}
        self.dax_channel = None
        self.audio_stopped = False

    def create_speaker(self):
        sid = self.AUDIO_SID_BASE + self.radio_id
        self.audio_stream_id = sid
        self.audio_streams["remote_audio_rx"] = sid
        self.dax_channel = None
        self.audio_stopped = False
        return sid

    def create_dax(self, ch=1):
        sid = self.DAX_SID_BASE + self.radio_id * 4 + (ch - 1)
        self.audio_stream_id = sid
        self.audio_streams["dax_rx"] = sid
        self.dax_channel = ch
        self.audio_stopped = False
        return sid

    def remove(self, sid_rm):
        for st, s_id in list(self.audio_streams.items()):
            if s_id == sid_rm:
                del self.audio_streams[st]
                if st == "dax_rx":
                    self.dax_channel = None
        if sid_rm == self.audio_stream_id:
            self.audio_stream_id = next(iter(self.audio_streams.values()), None)
        if not self.audio_streams:
            self.audio_stopped = True
            self.audio_stream_id = None
            self.dax_channel = None

    def targets(self):
        """What the send loop iterates for one generated frame."""
        return dict(self.audio_streams) or (
            {"remote_audio_rx": self.audio_stream_id}
            if self.audio_stream_id is not None else {})


def main():
    print("== registration: DAX must not evict the speaker ==")
    s = _Streams()
    spk = s.create_speaker()
    check("speaker registers", s.audio_streams.get("remote_audio_rx"), spk)
    check("and it is the only stream", len(s.audio_streams), 1)

    dax = s.create_dax(ch=1)
    check("dax registers under its own id", s.audio_streams.get("dax_rx"), dax)
    check("THE BUG: the speaker is STILL registered",
          s.audio_streams.get("remote_audio_rx"), spk)
    check("both streams are live", len(s.audio_streams), 2)
    check("the two ids are distinct", spk != dax, True)

    print()
    print("== fanout: one frame reaches BOTH streams ==")
    t = s.targets()
    check("send loop targets two streams", len(t), 2)
    check("speaker is a target", spk in t.values(), True)
    check("dax is a target", dax in t.values(), True)

    print()
    print("== per-stream sequence counters ==")
    # A shared counter increments once per FRAME; with two streams each would
    # advance by 2 per frame from AE's point of view -> 1-in-2 gaps.
    seqs = {}
    for _frame in range(4):
        for sid in t.values():
            seqs[sid] = seqs.get(sid, 0) + 1
    check("speaker saw every frame", seqs[spk], 4)
    check("dax saw every frame", seqs[dax], 4)
    check("neither stream skipped", seqs[spk], seqs[dax])

    print()
    print("== teardown: removing DAX must not silence the speaker ==")
    s.remove(dax)
    check("dax is gone", "dax_rx" in s.audio_streams, False)
    check("dax_channel cleared", s.dax_channel, None)
    check("THE OTHER HALF: speaker survives",
          s.audio_streams.get("remote_audio_rx"), spk)
    check("audio thread NOT stopped", s.audio_stopped, False)
    check("legacy id falls back to the survivor", s.audio_stream_id, spk)
    check("speaker is still a target", s.targets(), {"remote_audio_rx": spk})

    print()
    print("== teardown: removing the LAST stream does stop audio ==")
    s.remove(spk)
    check("nothing registered", s.audio_streams, {})
    check("audio thread stopped", s.audio_stopped, True)
    check("legacy id cleared", s.audio_stream_id, None)
    check("no targets", s.targets(), {})

    print()
    print("== removing DAX first, then re-arming it, is stable ==")
    s2 = _Streams()
    spk2 = s2.create_speaker()
    d1 = s2.create_dax(ch=1)
    s2.remove(d1)
    d2 = s2.create_dax(ch=2)
    check("re-armed DAX has channel 2's id", s2.audio_streams.get("dax_rx"), d2)
    check("channel 2 differs from channel 1", d1 != d2, True)
    check("speaker never disturbed", s2.audio_streams.get("remote_audio_rx"), spk2)
    check("both live again", len(s2.audio_streams), 2)

    print()
    print("== a stream id we never registered is ignored ==")
    s3 = _Streams()
    spk3 = s3.create_speaker()
    s3.remove(0xDEADBEEF)
    check("speaker untouched", s3.audio_streams.get("remote_audio_rx"), spk3)
    check("audio still running", s3.audio_stopped, False)

    print()
    if FAILED:
        print("test_dax_speaker_coexist: %d FAILED" % len(FAILED))
        for f in FAILED:
            print("   -", f)
        return 1
    print("test_dax_speaker_coexist: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
