#
# Aether-gate - IC-9700 LAN CI-V CONTROL probe (read-only): read freq + mode.
# Proves the control path (CI-V read/parse) independent of the scope subsystem.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ, CONTROLLER_CIV

RIP, RPORT, USER, PASS, LIP = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
CIV_ADDR = int(sys.argv[6], 16) if len(sys.argv) > 6 else 0xA2

MODES = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY",
         0x05: "FM", 0x06: "CW-R", 0x07: "RTTY-R", 0x08: "DV", 0x12: "FM-N"}


def decode_bcd_freq(b):
    """5 BCD bytes, LSB digit-pair first -> Hz."""
    f, mult = 0, 1
    for byte in b:
        f += (byte & 0x0F) * mult; mult *= 10
        f += (byte >> 4) * mult; mult *= 10
    return f


class CivControl(Ic9700Civ):
    """Reuses the CI-V transport but issues control READS instead of scope enable,
    and captures every CI-V frame the radio addresses back to us (to=0xE0)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.on_data = self._on_ctrl
        self.replies = []          # (cmd, datahex)
        self.freq = None
        self.mode = None
        self.filt = None

    def _on_iamready(self):
        self._send_openclose(opening=True)
        self._send_civ(bytes([0x03]))   # read operating frequency
        self._send_civ(bytes([0x04]))   # read operating mode

    def _on_ctrl(self, d):
        i = d.find(b"\xfe\xfe")
        while i >= 0:
            end = d.find(b"\xfd", i)
            if end < 0:
                break
            self._parse(d[i:end + 1])
            i = d.find(b"\xfe\xfe", end)

    def _parse(self, f):
        if len(f) < 6:
            return
        to_, frm, cmd = f[2], f[3], f[4]
        if to_ != CONTROLLER_CIV:        # ignore our own echoes (to=radio)
            return
        data = f[5:-1]
        self.replies.append((cmd, data.hex()))
        if cmd == 0x03 and len(data) >= 5:
            self.freq = decode_bcd_freq(data[:5])
        elif cmd == 0x04 and len(data) >= 1:
            self.mode = data[0]
            self.filt = data[1] if len(data) > 1 else None


h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
print("auth...")
if not h.connect(timeout=9.0):
    print("AUTH/PORTS FAILED:", h._fail, "authed:", h.authenticated.is_set())
    h.stop(); sys.exit(1)
print(f"  civ_port={h.civ_port} audio_port={h.audio_port} token=0x{h.token:08x}")

civ = CivControl(LIP, RIP, h.civ_port, h._civ_sock, CIV_ADDR)
print(f"opening CI-V control stream (civ_addr=0x{CIV_ADDR:02x})...")
civ.start()

deadline = time.time() + 8
while time.time() < deadline and (civ.freq is None or civ.mode is None):
    time.sleep(0.2)

print(f"  CI-V replies seen: {[(hex(c), h_) for c, h_ in civ.replies][:8]}")
if civ.freq is not None:
    print(f"  FREQUENCY: {civ.freq} Hz = {civ.freq/1e6:.6f} MHz")
if civ.mode is not None:
    print(f"  MODE: 0x{civ.mode:02x} ({MODES.get(civ.mode, '?')})  filter=0x{(civ.filt or 0):02x}")
if civ.freq is not None and civ.mode is not None:
    print("RESULT: CONTROL READ OK - CI-V read/parse path works end-to-end")
elif civ.replies:
    print("RESULT: PARTIAL - got CI-V replies but not freq/mode (check decode)")
else:
    print("RESULT: NO CI-V REPLIES (stream open but radio silent to 03/04)")

civ.stop()
h.stop()
