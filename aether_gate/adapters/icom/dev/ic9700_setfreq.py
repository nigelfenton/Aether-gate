#
# Aether-gate - IC-9700 LAN CI-V WRITE test: set freq -> verify -> restore.
# Proves bidirectional control. Restores the original VFO before exit.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV

RIP, RPORT, USER, PASS, LIP = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
CIV_ADDR = int(sys.argv[6], 16) if len(sys.argv) > 6 else 0xA2
TEST_HZ = 145500000   # 145.500 MHz (2m FM simplex) - the nudge target


def decode_bcd(b):
    f, mult = 0, 1
    for byte in b:
        f += (byte & 0x0F) * mult; mult *= 10
        f += (byte >> 4) * mult; mult *= 10
    return f


def encode_bcd(hz):
    out = bytearray()
    for _ in range(5):
        lo = hz % 10; hz //= 10
        hi = hz % 10; hz //= 10
        out.append((hi << 4) | lo)
    return bytes(out)


class CivWrite(Ic9700Civ):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._on_ctrl
        self.freq = None
        self.last_ack = None      # 'OK' (FB) / 'NG' (FA)

    def _on_iamready(self):
        self._send_openclose(opening=True)
        self.read_freq()

    def read_freq(self):
        self.freq = None
        self._send_civ(bytes([0x03]))

    def set_freq(self, hz):
        self.last_ack = None
        self._send_civ(bytes([0x05]) + encode_bcd(hz))

    def _on_ctrl(self, d):
        i = d.find(b"\xfe\xfe")
        while i >= 0:
            end = d.find(b"\xfd", i)
            if end < 0:
                break
            f = d[i:end + 1]
            if len(f) >= 6 and f[2] == CONTROLLER_CIV:
                cmd, data = f[4], f[5:-1]
                if cmd == 0x03 and len(data) >= 5:
                    self.freq = decode_bcd(data[:5])
                elif cmd == 0xFB:
                    self.last_ack = "OK"
                elif cmd == 0xFA:
                    self.last_ack = "NG"
            i = d.find(b"\xfe\xfe", end)


def wait_freq(civ, want=None, t=4.0):
    end = time.time() + t
    while time.time() < end:
        if civ.freq is not None and (want is None or civ.freq == want):
            return civ.freq
        time.sleep(0.15)
    return civ.freq


h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
print("auth...")
if not h.connect(timeout=9.0):
    print("AUTH FAILED:", h._fail); h.stop(); sys.exit(1)
print(f"  ports civ={h.civ_port} audio={h.audio_port}")

civ = CivWrite(LIP, RIP, h.civ_port, h._civ_sock, CIV_ADDR)
civ.start()

baseline = wait_freq(civ)
print(f"  BASELINE freq: {baseline} Hz = {(baseline or 0)/1e6:.5f} MHz")
if not baseline:
    print("RESULT: could not read baseline; aborting (no write attempted)"); civ.stop(); h.stop(); sys.exit(1)

print(f"  -> SET {TEST_HZ/1e6:.5f} MHz ...")
civ.set_freq(TEST_HZ)
time.sleep(0.6)
civ.read_freq()
got = wait_freq(civ, want=TEST_HZ)
print(f"     ack={civ.last_ack}  readback={got} Hz = {(got or 0)/1e6:.5f} MHz  "
      f"{'OK' if got == TEST_HZ else 'MISMATCH'}")

print(f"  -> RESTORE {baseline/1e6:.5f} MHz ...")
civ.set_freq(baseline)
time.sleep(0.6)
civ.read_freq()
back = wait_freq(civ, want=baseline)
print(f"     ack={civ.last_ack}  readback={back} Hz = {(back or 0)/1e6:.5f} MHz  "
      f"{'RESTORED' if back == baseline else 'NOT RESTORED!'}")

if got == TEST_HZ and back == baseline:
    print("RESULT: WRITE PATH OK - set+verify+restore all confirmed")
else:
    print("RESULT: WRITE INCOMPLETE - check ack/readback above")

civ.stop()
h.stop()
