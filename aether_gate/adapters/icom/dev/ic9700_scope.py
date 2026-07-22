import statistics
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ

RIP, RPORT, USER, PASS, LIP = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
CIV_ADDR = int(sys.argv[6], 16) if len(sys.argv) > 6 else 0xA2

h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
print("auth...")
if not h.connect(timeout=9.0):
    print("AUTH/PORTS FAILED:", h._fail, "authed:", h.authenticated.is_set())
    h.stop()
    sys.exit(1)
print(f"  civ_port={h.civ_port} audio_port={h.audio_port} token=0x{h.token:08x}")

civ = Ic9700Civ(LIP, RIP, h.civ_port, h._civ_sock, civ_addr=CIV_ADDR)
print(f"opening CI-V stream (civ_addr=0x{CIV_ADDR:02x})...")
civ.start()
time.sleep(1.0)
civ.set_span(500_000)   # ±500 kHz — widest view, most likely to catch real signals

deadline = time.time() + 13
resent = 0
while time.time() < deadline:
    # resend the scope-enable a couple of times early in case the band/scope
    # needs a nudge to start sweeping
    if resent < 2 and civ.max_byte == 0 and civ.frames and time.time() > deadline - 10:
        civ.enable_scope()
        resent += 1
    time.sleep(0.25)

print(f"  scope frames received: {civ.frames}")
print(f"  datagram length histogram: {dict(sorted(civ.dgram_lens.items()))}")
for i, (ln, s) in enumerate(civ.samples):
    body = s if ln < 400 else s[:120] + "..." + s[-8:]
    print(f"  --- sample {i} (len {ln}): {body}")
print(f"  PEAK raw byte across all frames: {civ.max_byte} "
      f"(dBm peak ~ {(-130 + min(civ.max_byte,159)/159*120):.1f})")
if civ.bounds_raw:
    print(f"  mode/bounds 12 bytes: {civ.bounds_raw}")
if civ.first_raw:
    print(f"  first frame raw: {civ.first_raw[:80]}...{civ.first_raw[-8:]}")
if civ.best_raw and civ.max_byte > 0:
    print(f"  busiest frame raw: {civ.best_raw}")
if civ.latest_dbm:
    a = civ.latest_dbm
    print(f"  last frame: bins={len(a)} min={min(a):.1f} max={max(a):.1f} "
          f"median={statistics.median(a):.1f} dBm")
    if civ.max_byte > 0:
        print("RESULT: SCOPE OK - live dBm with real variation")
    else:
        print("RESULT: STREAM OK but flat floor (band quiet / key up a signal to confirm)")
else:
    print("RESULT: NO SCOPE DATA (check civ_addr / scope enable)")

civ.stop()
h.stop()
