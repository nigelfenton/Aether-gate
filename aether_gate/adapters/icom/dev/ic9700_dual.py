# DUAL-RECEIVER detection probe — the foundation for the two-slice model.
# Answers three questions the dual-slice feature needs:
#   1. Is SUB/dualwatch ON?         -> read 07 D2 (dualwatch on/off), and
#      infer from whether 25 01 (other vfo) differs from 25 00.
#   2. Does SUB deliver its OWN scope waterfall? -> tally scope frames by
#      receiver byte at offset marker+2 (27 00 <rcvr>): 00=MAIN, 01=SUB.
#   3. Can we read SUB freq (25 01) AND SUB mode (26 01)?
# Nigel: turn SUB/dualwatch ON (so both bands show), put MAIN + SUB on
# different bands, leave it ~30s. Read-only; sets nothing.
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV

CIV_TO_MODE = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY",
               0x05: "FM", 0x06: "CW-R", 0x07: "RTTY-R", 0x08: "DV", 0x12: "FM-N"}


def unbcd(b):
    f, m = 0, 1
    for x in b:
        f += (x & 0x0F) * m; m *= 10
        f += (x >> 4) * m; m *= 10
    return f


def fmt(hz):
    return f"{hz/1e6:9.4f}" if hz else "    --   "


class Dual(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._d
        self.f_sel = None
        self.f_other = None
        self.mode_sel = None
        self.mode_other = None
        self.dualwatch = None            # 07 D2 payload
        self.scope_rcvr = {}             # receiver byte -> frame count

    def _d(self, d):
        # scope frames start "27 00 00" then a 12-byte header (marker+3..+15)
        # that holds div-current/div-total + mode/bounds. The receiver id, IF
        # present, lives in that header — capture the header hex of each
        # DISTINCT layout so we can SEE where MAIN vs SUB differs (don't guess
        # the offset). Key = the 12 header bytes; value = count.
        m = d.find(b"\x27\x00\x00")
        if m >= 0 and m + 15 <= len(d):
            hdr = bytes(d[m + 3:m + 15]).hex()
            self.scope_rcvr[hdr] = self.scope_rcvr.get(hdr, 0) + 1
        # control replies
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
                elif cmd == 0x26 and len(data) >= 2:
                    if data[0] == 0x00:
                        self.mode_sel = CIV_TO_MODE.get(data[1], f"0x{data[1]:02x}")
                    elif data[0] == 0x01:
                        self.mode_other = CIV_TO_MODE.get(data[1], f"0x{data[1]:02x}")
                elif cmd == 0x07 and len(data) >= 2 and data[0] == 0xD2:
                    self.dualwatch = data[1]
            i = d.find(b"\xfe\xfe", end)

    def poll(self):
        self._send_civ(bytes([0x25, 0x00]))
        self._send_civ(bytes([0x25, 0x01]))
        self._send_civ(bytes([0x26, 0x00]))
        self._send_civ(bytes([0x26, 0x01]))
        self._send_civ(bytes([0x07, 0xD2]))


if __name__ == "__main__":
    RIP, RPORT, USER, PASS, LIP = sys.argv[1:6]
    h = Ic9700Handler(LIP, RIP, int(RPORT), USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAILED:", h._fail); h.stop(); sys.exit(1)
    st = Dual(LIP, RIP, h.civ_port, h._civ_sock, 0xA2)
    st.start()
    t0 = time.time()
    while time.time() - t0 < 12 and st.f_sel is None:
        st.poll(); time.sleep(1.0)
    if st.f_sel is None:
        print("STREAM DEAD / deaf - abort (wait 40s, retry)")
        st.stop(); h.stop(); sys.exit(1)

    print()
    print("  ####################################################")
    print("  #  GO — turn SUB/dualwatch ON, MAIN+SUB different   #")
    print("  #  bands (e.g. MAIN 2m, SUB 23cm). Watching 35s.    #")
    print("  ####################################################")
    print("   t  SEL(2500) mode  OTHER(2501) mode  dualwatch  #scope-hdr-variants")
    print("  " + "-" * 66)
    last = None
    for k in range(35):
        st.poll()
        time.sleep(1.0)
        dw = "?" if st.dualwatch is None else ("ON" if st.dualwatch else "off")
        cur = (st.f_sel, st.mode_sel, st.f_other, st.mode_other, dw)
        if cur != last:
            print(f"  {k:2d} {fmt(st.f_sel)} {str(st.mode_sel):4s} "
                  f"{fmt(st.f_other)} {str(st.mode_other):4s}   {dw:3s}   "
                  f"{len(st.scope_rcvr)} variant(s)", flush=True)
            last = cur
    print()
    print("  SCOPE HEADER VARIANTS (12 bytes after 27 00 00; count):")
    for hdr, n in sorted(st.scope_rcvr.items(), key=lambda x: -x[1]):
        print(f"    {hdr}  x{n}")
    print("  KEY: TWO distinct header variants (one per receiver) => SUB streams")
    print("       its own scope => real 2-slice. ONE variant => only MAIN scope,")
    print("       slice 1 = control-only. dualwatch(07 D2) ON + distinct OTHER")
    print("       freq/mode => the trigger to create slice 2.")
    st.stop(); h.stop()
