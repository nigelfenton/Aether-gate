# Reproduce the gate's zero-gap bring-up burst (openclose + scope-enable x3
# + speed + span + freq-read + mode-read) and report whether the stream
# survives: scope frames flowing? 03/04/15-02 replies parsed?  Compare with
# --paced (50 ms gaps) to isolate burst-overrun vs a poisonous command.
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom9700 import _Ic9700Stream

RIP, RPORT, USER, PASS, LIP = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
PACED = "--paced" in sys.argv
GAP = 0.05 if PACED else 0.0


class BurstStream(_Ic9700Stream):
    def _on_iamready(self):
        # exactly the gate's bring-up, with configurable pacing
        self._send_openclose(opening=True)
        for cmd in (bytes([0x27, 0x10, 0x01]), bytes([0x27, 0x11, 0x01]),
                    bytes([0x27, 0x12, 0x00])):
            if GAP: time.sleep(GAP)
            self._send_civ(cmd)
        if GAP: time.sleep(GAP)
        self.set_speed(0)
        if GAP: time.sleep(GAP)
        self.set_span(500_000)
        if GAP: time.sleep(GAP)
        self._send_civ(bytes([0x03]))
        if GAP: time.sleep(GAP)
        self._send_civ(bytes([0x04]))


h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
print(f"auth... (paced={PACED})")
if not h.connect(timeout=9.0):
    print("AUTH FAILED:", h._fail)
    h.stop(); sys.exit(1)

civ = BurstStream(LIP, RIP, h.civ_port, h._civ_sock, 0xA2)
civ.start()

t0 = time.time()
while time.time() - t0 < 10:
    time.sleep(1.0)
    civ.poll_smeter()
    print(f"  +{time.time()-t0:4.1f}s scope_frames={civ.frames} freq={civ.freq_hz} "
          f"mode={civ.mode} smeter={civ.smeter_raw}")

ok = civ.frames > 5 and civ.freq_hz
print("RESULT:", "STREAM HEALTHY" if ok else "STREAM DEAD/PARTIAL",
      f"(frames={civ.frames}, freq={civ.freq_hz}, smeter={civ.smeter_raw})")
civ.stop(); h.stop()
