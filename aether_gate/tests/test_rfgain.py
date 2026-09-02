#
# Aether-gate — RF-gain contract tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""AE's RF Gain slider sends dB, in the range the adapter advertised.

Two separate defects met here, both found live on 2026-08-31:

  * the soapy adapter had no set_gain at all, so `display pan set … rfgain=`
    was silently dropped by the engine's hasattr() guard and the only way to
    change gain on an SDR was to restart the gate with a different --gain;
  * the engine documented the value as 0..100 and the one adapter that did
    implement the seam rescaled against that, while AetherSDR actually sends
    dB (IRadioBackend::setPanRfGain -> `display pan set %1 rfgain=%2`).

Nothing answered `display pan rfgain_info` either, which left AE on the Flex
6000 default of -8..32 step 8 — five positions on a scale unrelated to the
hardware.

Run:  python -m pytest aether_gate/tests/test_rfgain.py
"""


class FakeConn:
    """Captures the bytes the Radio would write back to AE."""
    def __init__(self):
        self.out = bytearray()

    def sendall(self, b):
        self.out.extend(b)


def _soapy(lo=20.0, hi=59.0):
    from aether_gate.adapters.soapy import SoapyAdapter
    a = SoapyAdapter(driver="none", samp_rate=250_000.0, gain_db=12.0)
    a._gain_lo, a._gain_hi = lo, hi          # what _open_hw reads off the device
    return a


# --- the adapter seam ------------------------------------------------------

def test_soapy_exposes_the_seam_the_engine_looks_for():
    # The engine guards both with hasattr(); missing either is a silent no-op,
    # which is precisely how the soapy gain slider did nothing for a whole night.
    a = _soapy()
    assert hasattr(a, "set_gain")
    assert hasattr(a, "gain_range")


def test_soapy_gain_range_is_the_device_range_not_a_guess():
    lo, hi, step = _soapy(lo=20.0, hi=59.0).gain_range()
    assert (lo, hi) == (20, 59)
    assert step >= 1                          # AE rejects step <= 0


def test_soapy_gain_range_widens_to_whole_dB():
    # floor/ceil, so the advertised travel never promises more than the device has
    lo, hi, _ = _soapy(lo=20.4, hi=58.6).gain_range()
    assert (lo, hi) == (20, 59)


def test_soapy_set_gain_is_dB_and_defers_to_the_reader_thread():
    a = _soapy(lo=20.0, hi=59.0)
    a.set_gain(45)
    # dB in, dB pending — NOT a percentage rescale
    assert a._gain_to == 45.0
    # and it must NOT have been applied inline: set_gain runs on the TCP command
    # thread, and racing an in-flight readStream is what the retune storm taught.
    assert a.gain_db == 12.0


def test_soapy_set_gain_clamps_to_the_advertised_range():
    a = _soapy(lo=20.0, hi=59.0)
    a.set_gain(1000)
    assert a._gain_to == 59.0
    a.set_gain(-1000)
    assert a._gain_to == 20.0


def test_hpsdr_set_gain_is_dB_not_a_percentage():
    from aether_gate.adapters.hpsdr.adapter import HpsdrAdapter, LNA_MIN_DB, LNA_MAX_DB
    a = HpsdrAdapter()
    assert a.gain_range() == (LNA_MIN_DB, LNA_MAX_DB, 1)
    # The regression: under the old 0..100 rescale, 32 dB became -12+32/100*60 = +7.2 dB.
    a.set_gain(32)
    assert a.gain_db == 32
    a.set_gain(LNA_MAX_DB)
    assert a.gain_db == LNA_MAX_DB            # the top of travel is reachable
    a.set_gain(999)
    assert a.gain_db == LNA_MAX_DB            # clamp, don't refuse
    a.set_gain(-999)
    assert a.gain_db == LNA_MIN_DB


# --- the wire ---------------------------------------------------------------

def _radio(adapter):
    from aether_gate.core import Radio
    return Radio("127.0.0.1", None, adapter=adapter, port=5992)


def test_rfgain_info_answers_with_the_adapter_range():
    r = _radio(_soapy(lo=20.0, hi=59.0))
    pid = r._new_pan()
    conn = FakeConn()
    r.on_line(conn, f"C1|display pan rfgain_info 0x{pid:08X}")
    body = conn.out.decode().strip().split("|")[-1]
    # AE parses "low,high,step" and ignores the reply unless step > 0
    assert body == "20,59,1"


def test_rfgain_info_stays_empty_for_an_adapter_without_the_seam():
    from aether_gate.adapters import SimAdapter
    r = _radio(SimAdapter(model="FLEX-6600"))
    pid = r._new_pan()
    conn = FakeConn()
    r.on_line(conn, f"C1|display pan rfgain_info 0x{pid:08X}")
    line = conn.out.decode().strip()
    # An empty body makes AE keep its own default — the pre-existing behaviour.
    assert line.startswith("R1|")
    assert line.split("|")[-1] == ""


def test_rfgain_set_reaches_the_adapter_in_dB():
    a = _soapy(lo=20.0, hi=59.0)
    r = _radio(a)
    pid = r._new_pan()
    conn = FakeConn()
    r.on_line(conn, f"C1|display pan set 0x{pid:08X} rfgain=45")
    assert a._gain_to == 45.0
