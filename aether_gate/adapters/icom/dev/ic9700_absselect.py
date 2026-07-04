# ic9700_absselect — test ABSOLUTE receiver selects (07 D0/D1) vs the toggle (07 B0).
#
# Problem: read_rx2 used 07 B0 (a TOGGLE: swap MAIN<->SUB). Over a real link a
# single 07 B0 occasionally misses/races -> MAIN & SUB stay INVERTED -> the
# gate's MAIN and RX2 slices trade places (Nigel: "slices keep changing", a
# ~2-min flip). A toggle used for reads is inherently fragile.
#
# Hypothesis: 07 D0 = select MAIN, 07 D1 = select SUB are ABSOLUTE (idempotent)
# — selecting MAIN twice is still MAIN, so a missed/duplicated select can't
# invert the mapping. This probe tests that over USB, read-only-ish (selects +
# freq reads; restores MAIN at the end).
#
# Run (rig in dual-RX, MAIN + SUB on DIFFERENT bands):
#   python -m aether_gate.adapters.icom.dev.ic9700_absselect COM7
import sys, time
try:
    import serial
except Exception:
    print("pyserial required"); sys.exit(1)


def unbcd(b):
    f, m = 0, 1
    for x in b:
        f += (x & 0xF) * m; m *= 10
        f += (x >> 4) * m; m *= 10
    return f


def M(h):
    return f"{h/1e6:.4f}" if h else "--"


PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
s = serial.Serial(PORT, BAUD, timeout=0.5)


def civ(payload, w=0.2):
    s.reset_input_buffer()
    s.write(bytes([0xFE, 0xFE, 0xA2, 0xE0]) + payload + bytes([0xFD]))
    time.sleep(w)
    return s.read(96)


def read_sel_freq():
    r = civ(bytes([0x25, 0x00]))
    i = r.find(bytes([0xFE, 0xFE, 0xE0, 0xA2, 0x25]))
    if i < 0:
        return None
    b = r[i + 5:]; e = b.find(0xFD)
    return unbcd(b[1:6]) if e >= 6 else None


def select(which, w=0.3):
    # 07 D0 = MAIN select, 07 D1 = SUB select (absolute)
    civ(bytes([0x07, 0xD0 if which == "MAIN" else 0xD1]), w)


print(f"=== port {PORT} @ {BAUD} ===")

# A) baseline via toggle-free absolute selects
print("\n--- A) 07 D0 (MAIN) then 07 D1 (SUB), read each ---")
select("MAIN"); mf = read_sel_freq()
select("SUB");  sf = read_sel_freq()
select("MAIN"); mf2 = read_sel_freq()
print(f"  07 D0 -> MAIN = {M(mf)}")
print(f"  07 D1 -> SUB  = {M(sf)}")
print(f"  07 D0 -> MAIN = {M(mf2)}  (should equal first MAIN)")

# B) IDEMPOTENCE: select MAIN 3x in a row — must NOT drift
print("\n--- B) idempotence: 07 D0 x3 (MAIN must stay put, no toggle drift) ---")
vals = []
for _ in range(3):
    select("MAIN"); vals.append(read_sel_freq())
print("  MAIN x3:", [M(v) for v in vals],
      "->", "STABLE-OK" if len(set(vals)) == 1 else "DRIFTED-FAIL")

# C) STRESS: 10 rapid MAIN/SUB/MAIN cycles — do MAIN & SUB stay themselves?
print("\n--- C) 10 rapid MAIN->SUB->MAIN cycles (the toggle failed HERE) ---")
main_seen, sub_seen, bad = set(), set(), 0
for i in range(10):
    select("MAIN"); m = read_sel_freq()
    select("SUB");  sub = read_sel_freq()
    select("MAIN"); m2 = read_sel_freq()
    main_seen.add(m); sub_seen.add(sub)
    if m != m2:
        bad += 1
        print(f"  cycle {i+1}: MAIN {M(m)} -> after SUB read, MAIN {M(m2)}  MISMATCH!")
print(f"  MAIN values seen: {[M(v) for v in main_seen]}")
print(f"  SUB  values seen: {[M(v) for v in sub_seen]}")
print(f"  mismatches: {bad}/10  ->", "ABSOLUTE SELECT IS STABLE-OK" if bad == 0
      and len(main_seen) == 1 and len(sub_seen) == 1 else "still unstable")

select("MAIN")   # leave it on MAIN
s.close()
