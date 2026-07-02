# INDEPENDENT RECEIVER ADDRESSING probe — foundation for tunable dual-slice.
# The dual-slice feature needs to tune MAIN and SUB independently WITHOUT
# disturbing the other (critical for satellite: MAIN=downlink, SUB=uplink,
# Doppler-tracked in opposite directions). This tests two candidate methods:
#
#   Method A (unselected-write): 25 01 <bcd> — write the OTHER vfo directly,
#            leaving the selected one untouched.
#   Method B (select-then-write): 07 D0/D1 to select MAIN/SUB, then 25 00.
#
# For each, we read BOTH vfos (25 00 sel + 25 01 other) before and after and
# report whether the intended rx moved AND the other stayed put.
# Reads MAIN via a small offset from its current freq (in-band, safe).
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


def bcd(hz):
    out = bytearray(); hz = int(hz)
    for _ in range(5):
        lo = hz % 10; hz //= 10
        hi = hz % 10; hz //= 10
        out.append((hi << 4) | lo)
    return bytes(out)


def M(hz):
    return f"{hz/1e6:.4f}" if hz else "--"


class RxAddr(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._d
        self.f_sel = None
        self.f_other = None
        self.n_fa = 0
        self.n_fb = 0

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
                cmd, data = f[4], f[5:-1]
                if cmd == 0x25 and len(data) >= 6:
                    if data[0] == 0x00:
                        self.f_sel = unbcd(data[1:6])
                    elif data[0] == 0x01:
                        self.f_other = unbcd(data[1:6])
                elif cmd == 0xFB:
                    self.n_fb += 1
                elif cmd == 0xFA:
                    self.n_fa += 1
            i = d.find(b"\xfe\xfe", end)

    def read_both(self, settle=0.8):
        self._send_civ(bytes([0x25, 0x00]))
        self._send_civ(bytes([0x25, 0x01]))
        time.sleep(settle)
        return self.f_sel, self.f_other


if __name__ == "__main__":
    RIP, RPORT, USER, PASS, LIP = sys.argv[1:6]
    h = Ic9700Handler(LIP, RIP, int(RPORT), USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAILED:", h._fail); h.stop(); sys.exit(1)
    r = RxAddr(LIP, RIP, h.civ_port, h._civ_sock, 0xA2)
    r.start()
    t0 = time.time()
    while time.time() - t0 < 12 and r.f_sel is None:
        r.read_both()
    if r.f_sel is None:
        print("STREAM DEAD - abort"); r.stop(); h.stop(); sys.exit(1)

    sel0, oth0 = r.read_both()
    print(f"\n  START:  SEL(25 00)={M(sel0)}   OTHER(25 01)={M(oth0)}")
    if not oth0:
        print("  !! OTHER vfo reads blank — is dualwatch/SUB ON? (need both rx active)")
        print("     Turn SUB on with MAIN+SUB on different bands, then rerun.")
        r.stop(); h.stop(); sys.exit(0)

    # nudge targets: +10 kHz on each, within band, restore after
    sel_t = sel0 + 10_000
    oth_t = oth0 + 10_000

    print(f"\n  METHOD A — write OTHER directly (25 01 -> {M(oth_t)}), SEL must NOT move:")
    fa0 = r.n_fa
    r._send_civ(bytes([0x25, 0x01]) + bcd(oth_t))
    time.sleep(0.8)
    s, o = r.read_both()
    print(f"    result: SEL={M(s)} (was {M(sel0)})   OTHER={M(o)} (target {M(oth_t)})   "
          f"{'FA!' if r.n_fa > fa0 else 'ok'}")
    a_moved_other = o and abs(o - oth_t) < 500
    a_kept_sel = s and abs(s - sel0) < 500
    print(f"    -> OTHER moved: {a_moved_other}   SEL untouched: {a_kept_sel}")

    print(f"\n  METHOD B — select SUB (07 D1), write 25 00 -> {M(oth_t)}, select MAIN back (07 D0):")
    fa1 = r.n_fa
    r._send_civ(bytes([0x07, 0xD1]))          # select SUB
    time.sleep(0.4)
    r._send_civ(bytes([0x25, 0x00]) + bcd(oth_t + 5_000))
    time.sleep(0.6)
    sB, oB = r.read_both()
    r._send_civ(bytes([0x07, 0xD0]))          # select MAIN back
    time.sleep(0.4)
    sB2, oB2 = r.read_both()
    print(f"    while SUB selected: SEL={M(sB)}  OTHER={M(oB)}")
    print(f"    after MAIN reselected: SEL={M(sB2)}  OTHER={M(oB2)}   "
          f"{'FA!' if r.n_fa > fa1 else 'ok'}")

    print("\n  RESTORE both vfos to start...")
    r._send_civ(bytes([0x25, 0x01]) + bcd(oth0))
    time.sleep(0.5)
    # if selected drifted, put it back too
    s2, o2 = r.read_both()
    if s2 and abs(s2 - sel0) > 500:
        r._send_civ(bytes([0x25, 0x00]) + bcd(sel0))
        time.sleep(0.5)
    sF, oF = r.read_both()
    print(f"    final: SEL={M(sF)}  OTHER={M(oF)}  (start SEL={M(sel0)} OTHER={M(oth0)})")

    print("\n  VERDICT:")
    if a_moved_other and a_kept_sel:
        print("    METHOD A works — 25 01 writes the OTHER rx cleanly, SEL untouched.")
        print("    => dual-slice can tune each rx independently. Best path.")
    else:
        print("    METHOD A insufficient — check METHOD B results above (select-then-tune).")
    r.stop(); h.stop()
