#
# Aether-gate — panadapter resolution control (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Bin width = span / bins, and both halves are operator-settable at runtime.

The bug this control was born from, 2026-08-31: an operator wanting finer bins
on an RSPdx asked for 256 kS/s — a plausible-looking number that is not a 2 MS/s
decimation. SoapySDRPlay3 does not reject it. It logs

    [WARNING] invalid sample rate. Sample rate unchanged.

and returns normally, leaving the device at 2 MS/s. The request was for FOUR
TIMES FINER bins and what landed was four times COARSER, with only a driver
warning to say so. So the rate is snapped to something the device actually
offers before it is ever handed to the driver.

Run:  python -m pytest aether_gate/tests/test_resolution.py
"""
import pytest

np = pytest.importorskip("numpy")

import time

from aether_gate.adapters.soapy import SoapyAdapter, RATE_DEBOUNCE_S


class _FakeDevice:
    """An SDRplay-shaped device: only 2 MS/s decimations, and it IGNORES the rest."""
    RATES = [62500.0, 125000.0, 250000.0, 500000.0, 1000000.0, 2000000.0]

    def __init__(self, rate=250000.0):
        self.rate = rate
        self.sets = []

    def listSampleRates(self, d, c):
        return list(self.RATES)

    def setSampleRate(self, d, c, hz):
        self.sets.append(hz)
        if hz in self.RATES:              # anything else: warn to stderr and no-op
            self.rate = hz

    def getSampleRate(self, d, c):
        return self.rate


def _adapter(rate=250000.0):
    a = SoapyAdapter(driver="none", samp_rate=rate)
    a._np = np
    a._sdr = _FakeDevice(rate)
    a._SOAPY_SDR_RX = 0
    return a


def _reader_tick(a, settled=True):
    """Do what _read_loop does with a pending rate, without a real stream.

    `settled` models the debounce: the real loop only acts once a request has
    sat still for RATE_DEBOUNCE_S, so a tick mid-drag must do nothing.
    """
    a._stop_stream = lambda: None
    a._start_stream = lambda: None
    a._verify_stream = lambda timeout_s=2.0: True
    if settled:
        a._rate_req_at -= RATE_DEBOUNCE_S + 0.01     # as if the operator let go
    if (a._rate_to is not None
            and time.monotonic() - a._rate_req_at >= RATE_DEBOUNCE_S):
        want = float(a._rate_to)
        if abs(want - a.samp_rate) > 1.0:
            a._apply_samp_rate(want)
        a._rate_to = None


# --- the snap ---------------------------------------------------------------

def test_an_unsupported_rate_is_snapped_not_passed_through():
    a = _adapter(2_000_000.0)
    a.set_samp_rate(256_000, wait_s=0.0)          # the 2026-08-31 request
    assert a._rate_to == 250_000.0, "256 kS/s must snap to the nearest offered rate"
    _reader_tick(a)
    assert a._sdr.sets == [250_000.0], "the driver must never see the raw request"
    assert a.samp_rate == 250_000.0


def test_a_supported_rate_is_passed_through_untouched():
    a = _adapter(2_000_000.0)
    a.set_samp_rate(500_000, wait_s=0.0)
    _reader_tick(a)
    assert a._sdr.sets == [500_000.0]
    assert a.samp_rate == 500_000.0


def test_the_rate_is_taken_from_the_device_readback_not_the_request():
    # This driver's setters lie; the whole adapter is built on never trusting one.
    a = _adapter(250_000.0)
    a._sdr.RATES = [250_000.0]                    # device will refuse everything else
    a._rate_to = 125_000.0                        # bypass the snap, as a wedged driver would
    _reader_tick(a)
    assert a.samp_rate == 250_000.0, "must report what the device says, not what we asked"


def test_no_device_means_no_rate_change():
    a = _adapter()
    a._sdr = None
    assert a.set_samp_rate(125_000, wait_s=0.0) is None
    assert a._rate_to is None


# --- the consequences of a rate change -------------------------------------

def test_the_demod_chain_is_rebuilt_for_the_new_rate():
    # samp_rate feeds the staged decimation; leaving it stale starves the audio
    # clock, which is the click-every-1.3s failure _init_demod documents.
    a = _adapter(500_000.0)
    a._init_demod()
    before = a._decim
    a.set_samp_rate(125_000, wait_s=0.0)
    _reader_tick(a)
    assert a._decim != before
    assert a._pd_rate == pytest.approx(a.samp_rate / a._decim)


def test_a_rate_change_drops_stale_audio():
    a = _adapter(500_000.0)
    a._init_demod()
    a._audio_q.append(np.zeros(64, dtype=np.complex64))
    a._iq_resid = np.zeros(8, dtype=np.complex64)
    a.set_samp_rate(125_000, wait_s=0.0)
    _reader_tick(a)
    assert len(a._audio_q) == 0, "queued IQ is at the old rate — it would click"
    assert a._iq_resid is None


def test_the_span_follows_the_rate():
    # The pan window IS the sample rate on an IQ adapter (see set_span).
    a = _adapter(500_000.0)
    a.set_samp_rate(125_000, wait_s=0.0)
    _reader_tick(a)
    assert a.current_span_hz() == 125_000.0


def test_zooming_in_does_not_strand_the_zoom_out():
    # max_span_hz drives AE's band-zoom button. Pinning it to the rate we happen
    # to run meant zooming in shrank the only width you could ever get back to.
    a = _adapter(500_000.0)
    a.capabilities.max_span_hz = 2_000_000.0
    a.set_samp_rate(62_500, wait_s=0.0)
    _reader_tick(a)
    assert a.capabilities.max_span_hz == 2_000_000.0


# --- AE's pan zoom reaches the radio ---------------------------------------

def test_set_span_queues_a_rate_change_instead_of_discarding_it():
    # The regression: set_span returned samp_rate and threw the request away, so
    # AE's zoom reached the gate and died one call short of the radio.
    a = _adapter(500_000.0)
    a.set_span(125_000.0)
    assert a._rate_to == 125_000.0


def test_set_span_snaps_to_a_rate_the_device_can_run():
    a = _adapter(500_000.0)
    a.set_span(100_000.0)                     # between two offered rates
    assert a._rate_to in _FakeDevice.RATES


def test_set_span_reports_the_rate_running_now_not_the_request():
    # IRadioBackend.h: "Callers must not assume the requested value was taken."
    # The core labels the bins with the width the IQ CURRENTLY has; the change
    # is re-advertised by the span sync once it lands.
    a = _adapter(500_000.0)
    assert a.set_span(62_500.0) == 500_000.0


def test_set_span_never_blocks_the_command_thread():
    import time as _t
    a = _adapter(500_000.0)
    t0 = _t.monotonic()
    a.set_span(62_500.0)                      # no reader thread is running
    assert _t.monotonic() - t0 < 0.25, "set_span waited; it runs on the TCP command thread"


def test_a_zoom_drag_is_coalesced_into_one_rate_change():
    # ~30 bandwidth commands a second, each a stop/set/rebuild/start cycle.
    from aether_gate.adapters.soapy import RATE_DEBOUNCE_S
    a = _adapter(2_000_000.0)
    for hz in (1_000_000, 500_000, 250_000, 125_000, 62_500):
        a.set_span(hz)
        _reader_tick(a, settled=False)        # the reader runs THROUGHOUT the drag
    assert a._sdr.sets == [], "applied a rate mid-drag instead of waiting for it to settle"
    _reader_tick(a)                           # the operator lets go
    assert a._sdr.sets == [62_500.0], "must apply the value the drag ENDED on, once"


# --- the wire: AE has to be told the geometry changed -----------------------

class FakeConn:
    def __init__(self):
        self.out = bytearray()

    def sendall(self, b):
        self.out.extend(b)


def _radio(adapter=None):
    from aether_gate.core import Radio
    return Radio("127.0.0.1", None, adapter=adapter, port=5992, bins=4096)


def test_more_bins_narrows_the_bin_width_at_the_same_span():
    r = _radio()
    r.span_mhz = 0.25
    r.set_resolution(bins=1024)
    before = r.resolution()["bin_hz"]
    r.set_resolution(bins=2048)
    after = r.resolution()
    assert after["bins"] == 2048
    assert after["bin_hz"] == pytest.approx(before / 2.0, abs=0.001)   # bin_hz is a rounded readout


def test_a_bins_change_re_advertises_x_pixels_to_AE():
    # AE draws its frequency grid from the pan status. Change the bin count
    # without re-emitting and it paints the old grid over the new data.
    r = _radio()
    r._new_pan()
    r.conn = FakeConn()
    r.set_resolution(bins=2048)
    assert "x_pixels=2048" in r.conn.out.decode()


def test_bins_are_clamped_to_what_one_datagram_can_carry():
    # 16384 bins raised EMSGSIZE mid-send, the stream loop broke out, and the
    # panadapter stayed dark until the gate restarted (2026-08-31).
    from aether_gate.core.engine import max_pan_bins
    r = _radio()
    r.set_resolution(bins=10 ** 9)
    assert r.bins == max_pan_bins()
    r.set_resolution(bins=0)
    assert r.bins == 64


def test_one_segment_fills_a_datagram_without_overflowing_it():
    # bins_per_packet is the SEGMENT size, so it must be the largest that still
    # fits — one bin more has to overflow, or every frame wastes datagram space.
    from aether_gate.core.engine import (bins_per_packet, udp_maxdgram,
                                         fft_packet, wf_packet)
    n = bins_per_packet()
    assert len(fft_packet(1, 0, [0] * n, 0)) <= udp_maxdgram()
    assert len(wf_packet(2, 0, [0] * n, 0.0, 1.0, 0)) <= udp_maxdgram()
    assert len(wf_packet(2, 0, [0] * (n + 1), 0.0, 1.0, 0)) > udp_maxdgram()


@pytest.mark.parametrize("maxdgram", [9216, 65507],
                         ids=["macos-9216", "linux-windows-65507"])
def test_a_full_width_frame_segments_into_datagrams_that_each_fit(monkeypatch, maxdgram):
    # The whole point of segmenting: the frame ceiling is no longer bounded by
    # the datagram limit, but no individual datagram may exceed it.
    #
    # The limit is FORCED, not read off the host, because it is a platform
    # constant: macOS sends at most 9216 bytes per datagram, Linux and Windows
    # 65507. Under the first a 16384-bin frame is four segments; under the
    # second it fits one datagram and the loop runs once. Both are legal, and
    # the segmenting code has to be exercised everywhere, not only on the
    # platform it was written on — asserting `total > per` against the host's
    # own limit held on macOS and failed by construction on the other two.
    from aether_gate.core import engine
    from aether_gate.core.engine import (bins_per_packet, max_pan_bins,
                                         udp_maxdgram, fft_packet, wf_packet)
    monkeypatch.setattr(engine, "_UDP_MAXDGRAM", maxdgram)
    assert udp_maxdgram() == maxdgram
    total, per = max_pan_bins(), bins_per_packet()
    if maxdgram < 16384:
        assert total > per, "a 9216-byte limit must segment, or macOS stays pinned at 4096 bins"
    else:
        assert per >= total, "at 65507 bytes a full frame fits one datagram"
    px = [0] * total
    segments = 0
    for off in range(0, total, per):
        seg = px[off:off + per]
        assert len(fft_packet(1, 0, seg, 7, off, total)) <= maxdgram
        assert len(wf_packet(2, 0, seg, 0.0, 1.0, 3,
                             first_bin=off, total_bins=total)) <= maxdgram
        segments += 1
    assert segments == -(-total // per)         # ceil: 4 on macOS, 1 elsewhere


def test_segments_declare_the_frame_width_and_their_own_offset():
    # AE stitches on (start_bin, total_bins); if a segment reported its own
    # length as the frame width, each datagram would reset the assembler and
    # only the last chunk would ever be drawn.
    import struct
    from aether_gate.core.engine import fft_packet, wf_packet
    VITA_HDR_BYTES = 28   # vita_header() is seven big-endian uint32s
    pkt = fft_packet(1, 0, [11, 22, 33], 9, start_bin=4096, total_bins=16384)
    start, num, size, total, frame = struct.unpack(
        ">HHHHI", pkt[VITA_HDR_BYTES:VITA_HDR_BYTES + 12])
    assert (start, num, size, total, frame) == (4096, 3, 2, 16384, 9)

    pkt = wf_packet(2, 0, [11, 22, 33], 0.0, 1.0, 5,
                    first_bin=8192, total_bins=16384)
    sub = pkt[VITA_HDR_BYTES:VITA_HDR_BYTES + 36]
    width = struct.unpack(">H", sub[20:22])[0]
    total = struct.unpack(">H", sub[32:34])[0]
    first = struct.unpack(">H", sub[34:36])[0]
    assert (width, total, first) == (3, 16384, 8192)


def test_the_vectorised_converters_match_the_scalar_ones():
    # The stream loop uses the array versions; a divergence would show up as a
    # one-count brightness/height shift on every bin, invisible until compared.
    r = _radio()
    levels = [-140.0, -103.0, -98.6, -73.0, -50.25, 0.0, 7.0, 40.0]
    assert r.dbm_to_pixels(levels) == [r.dbm_to_pixel(d) for d in levels]
    assert r.dbm_to_wf_raws(levels) == [r.dbm_to_wf_raw(d) for d in levels]


def test_a_dead_stream_loop_clears_the_flag_so_it_can_restart():
    # emit_pan_status only starts a loop when streaming is False.
    r = _radio()
    r.streaming = True
    r.run = False                       # the loop's own exit condition
    r.vita_dest = ("127.0.0.1", 1)
    r.stream_loop()
    assert r.streaming is False


def test_an_adapter_without_the_seam_reports_no_rate_control():
    from aether_gate.adapters import SimAdapter
    r = _radio(SimAdapter(model="FLEX-6600"))
    res = r.resolution()
    assert res["can_set_rate"] is False
    assert res["rates"] == []
    r.set_resolution(samp_rate_hz=125_000)        # must be a no-op, not a crash


def test_AE_pan_zoom_wire_text_reaches_the_radio():
    """The whole seam, end to end: AE's wire text -> engine -> adapter.

    This is the link that was missing. AE has always sent
    "display pan set <id> bandwidth=<MHz>" (AetherSDR RadioModel.cpp), the
    engine has always parsed it into _set_pan_span_hz, and set_span threw it
    away — so the operator's zoom reached the gate and stopped one call short
    of the radio, and resolution could only be changed by restarting the gate.
    """
    a = _adapter(500_000.0)
    r = _radio(a)
    pid = r._new_pan()
    r.on_line(FakeConn(), f"C1|display pan set 0x{pid:08X} bandwidth=0.062500")
    assert a._rate_to == 62_500.0


def test_AE_zoom_to_an_impossible_span_snaps_rather_than_refusing():
    # AE's zoom is continuous; the device has ~19 discrete rates. A zoom past
    # the bottom must land on the narrowest the device can run, not be dropped.
    a = _adapter(500_000.0)
    r = _radio(a)
    pid = r._new_pan()
    r.on_line(FakeConn(), f"C1|display pan set 0x{pid:08X} bandwidth=0.001000")
    assert a._rate_to == min(_FakeDevice.RATES)


def test_the_pan_status_advertises_the_span_the_radio_actually_runs():
    # AE draws its frequency axis from bandwidth= in the pan status. Echoing
    # the REQUEST rather than the running rate is the axis error that made
    # signals paint at the wrong width and clicks tune short.
    a = _adapter(500_000.0)
    r = _radio(a)
    pid = r._new_pan()
    conn = FakeConn()
    r.conn = conn
    r.on_line(conn, f"C1|display pan set 0x{pid:08X} bandwidth=0.062500")
    assert "bandwidth=0.500000" in conn.out.decode(), \
        "advertised the requested span before the radio had taken it"


# --- the span sync: how AE finally learns what the radio took ---------------

def test_the_span_sync_adopts_the_rate_the_radio_actually_took():
    # AE zooms, the adapter snaps and defers; this is the only thing that ever
    # tells AE the width its bins really cover.
    a = _adapter(500_000.0)
    r = _radio(a)
    r._new_pan()
    r.conn = FakeConn()
    r.span_mhz = 0.5
    r.on_line(r.conn, "C1|display pan set 0x40000000 bandwidth=0.062500")
    _reader_tick(a)                                   # the change lands later
    assert r.span_mhz == 0.5, "adopted the request before the radio had taken it"
    assert r._sync_span() is True
    assert r.span_mhz == pytest.approx(0.0625)
    assert "bandwidth=0.062500" in r.conn.out.decode()


def test_the_span_sync_is_quiet_when_nothing_moved():
    # It runs twice a second on the stream thread; it must not re-emit forever.
    a = _adapter(250_000.0)
    r = _radio(a)
    r._new_pan()
    r.conn = FakeConn()
    r._sync_span()                                    # adopt 250 kHz once
    r.conn.out.clear()
    assert r._sync_span() is False
    assert r.conn.out == bytearray()


def test_the_span_sync_ignores_an_adapter_without_the_seam():
    from aether_gate.adapters import SimAdapter
    r = _radio(SimAdapter(model="FLEX-6600"))
    assert r._sync_span() is False


def test_the_pan_fft_spans_more_than_one_readstream_block():
    """A 16384-bin pan must get 16384 real samples, not 4096 interpolated up.

    get_iq used to ignore its length argument and return one 4096-sample block,
    so every bin count above 4096 was cosmetic: the true resolution bandwidth
    stayed samp_rate/4096 and iq_to_dbm merely interpolated. The tell was a
    noise floor that did not move when the advertised bin width changed 8x.
    """
    np = pytest.importorskip("numpy")
    from aether_gate.adapters.soapy import SoapyAdapter

    fs = 125_000.0
    a = SoapyAdapter(driver="none", samp_rate=fs, center_hz=3_722_000.0)
    a._np = np
    for _ in range(4):                      # what the reader delivers in ~131 ms
        a._pan_ring.append(np.zeros(4096, dtype=np.complex128))

    out = a.get_iq(16384, 3_722_000.0, fs)
    assert out is not None
    assert len(out) == 16384


def test_a_short_ring_degrades_to_what_exists_rather_than_lying():
    """Right after a start or a rate change there is less history than asked
    for. Handing back the short block is correct — the pan loses resolution but
    the dBm scale stays honest. Padding would invent samples."""
    np = pytest.importorskip("numpy")
    from aether_gate.adapters.soapy import SoapyAdapter

    fs = 125_000.0
    a = SoapyAdapter(driver="none", samp_rate=fs, center_hz=3_722_000.0)
    a._np = np
    a._pan_ring.append(np.zeros(4096, dtype=np.complex128))

    out = a.get_iq(16384, 3_722_000.0, fs)
    assert len(out) == 4096
