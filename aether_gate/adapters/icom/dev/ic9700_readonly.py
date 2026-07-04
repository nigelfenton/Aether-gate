# ic9700_readonly — settle "what does 25 01 actually return?" with ZERO swaps.
#
# Claim under test (a datasheet-style source): 25 01 reads the SUB RECEIVER
# directly, no VFO flip. Our 2026-07-02 probe found 25 01 = VFO B of the
# SELECTED receiver (same rx as 25 00). These conflict. This probe is PURE
# READ-ONLY (no 07 B0, no writes) — safe on a live rig — and prints exactly
# what 03 / 25 00 / 26 00 / 25 01 / 26 01 return, several times, so we can see
# it directly with MAIN and SUB cleanly on DIFFERENT bands.
#
# Run (rig in dual-RX, MAIN one band, SUB another):
#   python -m aether_gate.adapters.icom.dev.ic9700_readonly <RIP> <RPORT> <USER> <PASS> <LIP>
import sys, time
sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV

MODES = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY",
         0x05: "FM", 0x06: "CW-R", 0x07: "RTTY-R", 0x08: "DV", 0x12: "FM-N"}


def unbcd(b):
    f, m = 0, 1
    for x in b:
        f += (x & 0xF) * m; m *= 10
        f += (x >> 4) * m; m *= 10
    return f


def M(hz):
    return f"{hz/1e6:.4f} MHz" if hz else "--"


class RO(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._d
        self.v = {}      # label -> value, latest wins

    def _d(self, d):
        i = d.find(b"\xfe\xfe")
        while i >= 0:
            e = d.find(b"\xfd", i)
            if e < 0:
                break
            b = d[i:e + 1]
            if len(b) >= 6 and b[2] in (CONTROLLER_CIV, 0x00):
                body = b[4:-1]
                if body[0] == 0x03 and len(body) >= 6:
                    self.v['03 (main freq)'] = M(unbcd(body[1:6]))
                elif body[0] == 0x25 and len(body) >= 6:
                    key = '25 00 (sel VFO A)' if body[1] == 0 else '25 01 (SUB? / VFO B?)'
                    self.v[key] = M(unbcd(body[2:7]))
                elif body[0] == 0x26 and len(body) >= 2:
                    key = '26 00 mode' if body[1] == 0 else '26 01 mode'
                    self.v[key] = MODES.get(body[2], f"?{body[2]:02x}")
            i = d.find(b"\xfe\xfe", e)

    def rd(self, payload, settle=0.5):
        self._send_civ(payload); time.sleep(settle)


def main():
    RIP, RPORT, USER, PASS, LIP = sys.argv[1:6]
    h = Ic9700Handler(LIP, RIP, int(RPORT), USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAIL", h._fail); h.stop(); return 1
    r = RO(LIP, RIP, h.civ_port, h._civ_sock, 0xA2); r.start()
    time.sleep(1.0)

    print("\n=== READ-ONLY sweep (no swaps) — 3 passes ===")
    for p in range(3):
        r.rd(bytes([0x03]))            # main freq
        r.rd(bytes([0x25, 0x00]))      # selected VFO A
        r.rd(bytes([0x26, 0x00]))      # selected mode
        r.rd(bytes([0x25, 0x01]))      # <-- the command in question
        r.rd(bytes([0x26, 0x01]))      # its mode
        print(f"\n-- pass {p+1} --")
        for k in ['03 (main freq)', '25 00 (sel VFO A)', '26 00 mode',
                  '25 01 (SUB? / VFO B?)', '26 01 mode']:
            print(f"   {k:24s} = {r.v.get(k, '(no reply)')}")

    print("\n=== INTERPRETATION ===")
    main_f = r.v.get('03 (main freq)')
    v25_01 = r.v.get('25 01 (SUB? / VFO B?)')
    print(f"   MAIN band (03)      = {main_f}")
    print(f"   25 01 returned      = {v25_01}")
    print("   If 25 01 == your SUB receiver's band  -> the datasheet is RIGHT (no swap needed!)")
    print("   If 25 01 is on the SAME band as 03/25 00 -> it's MAIN's VFO B (swap still needed)")
    r.stop(); h.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
