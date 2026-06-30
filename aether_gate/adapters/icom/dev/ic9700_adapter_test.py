import sys, time
sys.path.insert(0, r"C:\Users\nigel\Documents\Aether-gate")
from aether_gate.adapters import available, get_adapter
print("registered adapters:", available())
assert "icom9700" in available(), "icom9700 not registered!"
from aether_gate.adapters.icom9700 import Icom9700Adapter

RIP, USER, PASS, LIP = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
a = Icom9700Adapter(radio_ip=RIP, username=USER, password=PASS, local_ip=LIP)
print("provides:", a.provides, "caps:", a.capabilities)
a.open()
print("opened OK: civ_port", a._handler.civ_port, "audio_port", a._handler.audio_port)
time.sleep(3.0)
print("control read -> freq_hz:", a._civ.freq_hz, "mode:", a._civ.mode)


class Ctx:
    n = 475
    floor = -120.0


bins = a.get_spectrum(Ctx(), 0.0)
print(f"get_spectrum -> {len(bins)} bins, min {min(bins):.1f} max {max(bins):.1f} dBm")
assert len(bins) == 475, "get_spectrum returned wrong bin count"
a.close()
print("RESULT: ADAPTER OK - registers, opens, reads control, get_spectrum shape correct")
