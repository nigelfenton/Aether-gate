# Can we READ which VFO (A or B) each receiver is on? SDR9700 only has 07 00/
# 07 01 as SETS (select A/B), no clean read. This probe tries candidate reads
# and — the real test — watches whether ANY value changes when Nigel flips
# A<->B on the rig. If a byte tracks the flip, that's our VFO-letter source;
# if nothing moves, the 9700 doesn't report current VFO -> fall back to slice
# ordering for the A/B letter.
# Candidates polled each second (payload hex printed only when it CHANGES):
#   07        bare "read vfo mode" (some Icoms echo current A/B)
#   1A 06     ? data/vfo state (model-specific; harmless read)
#   25 00     selected freq  (moves on A<->B if A and B differ in freq)
#   26 00     selected mode
# Nigel: with the 9700 on any band, FLIP VFO A <-> B a few times during the
# window (change A and B to DIFFERENT freqs first so a swap is visible).
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV


def unbcd(b):
    f, m = 0, 1
    for x in b:
        f += (x & 0x0F) * m; m *= 10
        f += (x >> 4) * m; m *= 10
    return f


class VfoAB(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._d
        self.vals = {}      # label -> current payload hex (last seen)
        self.hist = {}      # label -> set of distinct values seen (did it change?)

    def _note(self, label, payload):
        self.vals[label] = payload
        self.hist.setdefault(label, set()).add(payload)

    def _d(self, d):
        if d.find(b"\x27\x00\x00") >= 0:
            self._on_civ(d)
        i = d.find(b"\xfe\xfe")
        while i >= 0:
            end = d.find(b"\xfd", i)
            if end < 0:
                break
            f = d[i:end + 1]
            if len(f) >= 6 and f[2] in (CONTROLLER_CIV, 0x00):
                body = f[4:-1]
                cmd = body[0]
                if cmd == 0x07:
                    self._note("07 (vfo mode)", body[1:].hex())
                elif cmd == 0x1A and len(body) >= 2 and body[1] == 0x06:
                    self._note("1A 06", body[2:].hex())
                elif cmd == 0x25 and len(body) >= 6 and body[1] == 0x00:
                    self._note("25 00 sel-freq", f"{unbcd(body[2:7])/1e6:.4f}")
                elif cmd == 0x26 and len(body) >= 2 and body[1] == 0x00:
                    self._note("26 00 sel-mode", body[2:].hex())
            i = d.find(b"\xfe\xfe", end)

    def poll(self):
        self._send_civ(bytes([0x07]))
        self._send_civ(bytes([0x1A, 0x06]))
        self._send_civ(bytes([0x25, 0x00]))
        self._send_civ(bytes([0x26, 0x00]))


if __name__ == "__main__":
    RIP, RPORT, USER, PASS, LIP = sys.argv[1:6]
    h = Ic9700Handler(LIP, RIP, int(RPORT), USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAILED:", h._fail); h.stop(); sys.exit(1)
    v = VfoAB(LIP, RIP, h.civ_port, h._civ_sock, 0xA2)
    v.start()
    t0 = time.time()
    while time.time() - t0 < 12 and not v.vals:
        v.poll(); time.sleep(1.0)
    if not v.vals:
        print("STREAM DEAD - abort"); v.stop(); h.stop(); sys.exit(1)

    print("\n  ###############################################")
    print("  #  GO — FLIP VFO A <-> B on the rig a few times #")
    print("  #  (set A and B to different freqs first).       #")
    print("  #  Watching 40s.                                 #")
    print("  ###############################################")
    last = {}
    for k in range(40):
        v.poll()
        time.sleep(1.0)
        for label, val in sorted(v.vals.items()):
            if last.get(label) != val:
                print(f"  {k:2d}s  {label:18s} = {val}", flush=True)
                last[label] = val
    print("\n  VERDICT — which reads CHANGED across the A<->B flips:")
    for label, seen in sorted(v.hist.items()):
        changed = len(seen) > 1
        mark = "*** TRACKS VFO ***" if changed else "(static)"
        print(f"    {label:18s} {len(seen)} distinct value(s)  {mark}")
    print("\n  A read that TRACKS = our VFO-letter source. All static = the 9700")
    print("  doesn't report current VFO over CI-V -> letter falls back to slice order.")
    v.stop(); h.stop()
