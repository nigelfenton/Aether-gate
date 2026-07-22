# Scope discriminator: is the all-zero waveform a protocol problem or physics?
# Tunes MAIN to the continuous NOAA weather carrier (162.550 MHz, inside the
# 9700's 118-174 RX range), watches the scope peak, then restores the original
# frequency.  Also tries the 5-byte-BCD span form (the index form 27 15 00 07
# was echoed but the frame header kept saying +/-25k).
import sys
import time

sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters.icom.handler import Ic9700Handler
from aether_gate.adapters.icom.civ import Ic9700Civ

RIP, RPORT, USER, PASS, LIP = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
CIV_ADDR = int(sys.argv[6], 16) if len(sys.argv) > 6 else 0xA2
NOAA_HZ = 162_550_000


def bcd_freq(hz):
    s = f"{hz:010d}"                      # "0162550000"
    pairs = [s[i:i + 2] for i in range(0, 10, 2)]   # ["01","62","55","00","00"]
    return bytes(int(p, 16) for p in reversed(pairs))


def decode_bounds(bounds_hex):
    b = bytes.fromhex(bounds_hex)
    freq = int("".join(f"{x:02x}" for x in reversed(b[1:6])))
    span = int("".join(f"{x:02x}" for x in reversed(b[6:11])))
    return b[0], freq, span


def watch(civ, seconds, label):
    civ.max_byte = 0
    civ.best_raw = None
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.25)
    n = len(civ.latest_dbm) if civ.latest_dbm else 0
    print(f"  [{label}] frames={civ.frames} bins={n} peak_raw={civ.max_byte} "
          f"(~{-130 + min(civ.max_byte, 159) / 159 * 120:.1f} dBm)  FB={civ.n_fb} FA={civ.n_fa}")
    return civ.max_byte


h = Ic9700Handler(LIP, RIP, RPORT, USER, PASS)
print("auth...")
if not h.connect(timeout=9.0):
    print("AUTH/PORTS FAILED:", h._fail)
    h.stop()
    sys.exit(1)

civ = Ic9700Civ(LIP, RIP, h.civ_port, h._civ_sock, civ_addr=CIV_ADDR)
civ.start()
time.sleep(1.5)

orig = None
if civ.bounds_raw:
    mode, freq, span = decode_bounds(civ.bounds_raw)
    orig = freq
    print(f"  scope header says: mode={mode} center={freq/1e6:.4f} MHz span={span/1e3:.0f} kHz")

p0 = watch(civ, 4, "baseline 146.52")

# --- span: try the 5-byte BCD form (500 kHz) --------------------------------
civ._send_civ(bytes([0x27, 0x15, 0x00]) + bcd_freq(500_000)[:5])
time.sleep(1.5)
if civ.bounds_raw:
    civ.first_raw = None      # re-latch bounds from the next frame
    civ.bounds_raw = None
    time.sleep(1.0)
if civ.bounds_raw:
    mode, freq, span = decode_bounds(civ.bounds_raw)
    print(f"  after BCD span set: center={freq/1e6:.4f} MHz span={span/1e3:.0f} kHz")

# --- tune MAIN to the NOAA continuous carrier --------------------------------
print(f"tuning MAIN -> NOAA {NOAA_HZ/1e6:.3f} MHz ...")
civ._send_civ(bytes([0x05]) + bcd_freq(NOAA_HZ))
time.sleep(1.5)
civ.first_raw = None
civ.bounds_raw = None
time.sleep(1.0)
if civ.bounds_raw:
    mode, freq, span = decode_bounds(civ.bounds_raw)
    print(f"  scope header now: center={freq/1e6:.4f} MHz span={span/1e3:.0f} kHz")
p1 = watch(civ, 6, "NOAA 162.550")

# --- restore ------------------------------------------------------------------
if orig:
    print(f"restoring {orig/1e6:.4f} MHz ...")
    civ._send_civ(bytes([0x05]) + bcd_freq(orig))
    time.sleep(1.0)

if p1 > 10:
    print("RESULT: SCOPE PATH WORKS - carrier visible; 146.52 was genuinely below -130/pixel")
elif p1 > 0:
    print("RESULT: scope moved off zero but weak - check antenna/preamp; path works")
else:
    print("RESULT: still all-zero ON A KNOWN CARRIER - scope waveform path is broken radio-side")

civ.stop()
h.stop()
