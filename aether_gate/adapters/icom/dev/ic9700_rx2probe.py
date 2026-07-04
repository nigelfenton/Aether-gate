# ic9700_rx2probe — prove the RX2 swap-read/-write recipe the dual-slice FIX needs.
#
# Background (established 2026-07-02, ic9700_findrx2): 25 00 / 25 01 = VFO A / B
# of the SELECTED receiver (BOTH belong to whichever RX is MAIN). The 2nd
# RECEIVER (RX2) is ONLY reachable via 07 B0 (swap MAIN<->SUB): 07 B0 -> read
# 25 00 -> 07 B0 back. The current adapter WRONGLY feeds slice 1 from 25 01
# (= RX1 VFO B), which is why AE's 2nd slice collapses onto RX1 VFO B after a
# few seconds and a tune to slice B reverts.
#
# This probe validates, ON HARDWARE, the three operations the fix relies on:
#   1) READ  RX2 freq+mode via 07 B0 -> 25 00/26 00 -> 07 B0 back
#   2) CLEAN swap-back — MAIN (25 00) reads the SAME value before and after
#   3) WRITE RX2 (tune it) via the swap, and confirm MAIN is undisturbed
#
# SAFE: read-mostly; the one optional write nudges RX2 by a small offset and
# restores it. Everything swaps back to MAIN at the end. NO scope enabled.
#
# Run (rig must have BOTH receivers on, on DIFFERENT bands — e.g. MAIN 2m,
# SUB 23cm):
#   python -m aether_gate.adapters.icom.dev.ic9700_rx2probe <RIP> <RPORT> <USER> <PASS> <LIP> [--tune]
#   (--tune performs the RX2 write test; omit for read-only)
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


def bcd(hz):
    out = bytearray(); hz = int(hz)
    for _ in range(5):
        lo = hz % 10; hz //= 10
        hi = hz % 10; hz //= 10
        out.append((hi << 4) | lo)
    return bytes(out)


def M(hz):
    return f"{hz/1e6:.4f}" if hz else "--"


class Probe(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._d
        self.freq = None     # last 25 00 (selected VFO freq)
        self.mode = None     # last 26 00 (selected VFO mode)

    def _d(self, d):
        i = d.find(b"\xfe\xfe")
        while i >= 0:
            e = d.find(b"\xfd", i)
            if e < 0:
                break
            b = d[i:e + 1]
            if len(b) >= 6 and b[2] in (CONTROLLER_CIV, 0x00):
                body = b[4:-1]
                if body[0] == 0x25 and len(body) >= 6 and body[1] == 0x00:
                    self.freq = unbcd(body[2:7])
                elif body[0] == 0x26 and len(body) >= 2 and body[1] == 0x00:
                    self.mode = MODES.get(body[2])
            i = d.find(b"\xfe\xfe", e)

    def read_sel(self, settle=0.6):
        """Read the currently-selected receiver's freq (25 00) + mode (26 00)."""
        self.freq = self.mode = None
        self._send_civ(bytes([0x25, 0x00])); time.sleep(settle / 2)
        self._send_civ(bytes([0x26, 0x00])); time.sleep(settle / 2)
        return self.freq, self.mode

    def swap(self, settle=0.6):
        self._send_civ(bytes([0x07, 0xB0])); time.sleep(settle)


def main():
    args = sys.argv[1:]
    do_tune = "--tune" in args
    args = [a for a in args if a != "--tune"]
    RIP, RPORT, USER, PASS, LIP = args[:5]

    h = Ic9700Handler(LIP, RIP, int(RPORT), USER, PASS)
    print("auth...")
    if not h.connect(timeout=9.0):
        print("AUTH FAIL", h._fail); h.stop(); return 1
    r = Probe(LIP, RIP, h.civ_port, h._civ_sock, 0xA2); r.start()

    # settle the read path
    t0 = time.time()
    while time.time() - t0 < 12 and r.read_sel()[0] is None:
        pass
    main0_f, main0_m = r.freq, r.mode
    if main0_f is None:
        print("DEAD — no read reply"); r.stop(); h.stop(); return 1
    print(f"\n[MAIN before]           25 00 = {M(main0_f)}  {main0_m}")

    # 1) READ RX2 via swap
    r.swap()
    rx2_f, rx2_m = r.read_sel()
    print(f"[after 07 B0 -> RX2]    25 00 = {M(rx2_f)}  {rx2_m}")

    # 3) optional WRITE RX2 (nudge +10 kHz, then restore) while swapped-in
    if do_tune and rx2_f:
        tgt = rx2_f + 10_000
        print(f"[--tune] writing RX2 25 00 <- {M(tgt)} (+10kHz)")
        r._send_civ(bytes([0x25, 0x00]) + bcd(tgt)); time.sleep(0.5)
        got, _ = r.read_sel()
        print(f"         RX2 now       25 00 = {M(got)}  ({'MOVED ok' if got and abs(got-tgt)<50 else 'DID NOT MOVE'})")
        r._send_civ(bytes([0x25, 0x00]) + bcd(rx2_f)); time.sleep(0.5)   # restore RX2
        print(f"         RX2 restored -> {M(r.read_sel()[0])}")

    # 2) swap back, confirm MAIN unchanged (clean swap-back)
    r.swap()
    main1_f, main1_m = r.read_sel()
    print(f"[after 07 B0 -> MAIN]   25 00 = {M(main1_f)}  {main1_m}")

    print("\n=== VERDICT ===")
    diff_band = rx2_f and main0_f and abs(rx2_f - main0_f) > 1_000_000
    print(f"  RX2 read a DIFFERENT band than MAIN?  {'YES (07 B0 reaches RX2)' if diff_band else 'NO - same band, single-RX or probe mis-set'}")
    clean = main1_f and main0_f and abs(main1_f - main0_f) < 50
    print(f"  swap-back restored MAIN cleanly?      {'YES' if clean else 'NO - MAIN changed! ('+M(main0_f)+' -> '+M(main1_f)+')'}")
    print("  => if both YES, the fix's swap-read/-write recipe is sound.")

    r.stop(); h.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
