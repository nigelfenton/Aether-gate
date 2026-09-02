# The soapy audio chain must run starvation-free at ANY device sample rate.
#
# History (2026-08-01, RSP1a + sig gen on 2 m): the decimator used
# round(samp_rate / 24000). At 500 kS/s that picks 21, consuming 504 k
# input samples per second of audio from a tap that only produces 500 k —
# a 0.8% structural deficit that clicked every ~1.3 s at any frequency, in
# any mode, with any signal. The fix floors the decimation and follows it
# with a phase-continuous fractional resampler onto the 24 kHz grid.
#
# This test feeds EXACTLY real-time's worth of IQ plus 0.4% slack — enough
# for pipeline priming, deliberately LESS than the old policy's 0.8%
# structural deficit (a 1% slack was tried first and silently absolved the
# old code; the red-harness caught that). The old chain starves before the
# end (red); the fixed chain fits (green). It also
# pushes a known tone through the full NCO + FIR + resampler path and
# asserts it comes out at the right audio frequency — so a resampler that
# kept the buffers happy but warped time would still fail.

import numpy as np
import pytest

from aether_gate.adapters.soapy import SoapyAdapter, AUDIO_RATE

SAMP = 500_000.0            # the rate that exposed the bug (not a 24 k multiple)
TONE_HZ = 1_000.0           # baseband tone -> 1 kHz USB audio
CHUNK = 480                 # 20 ms of 24 kHz audio per get_audio call
SECONDS = 10


def _bench_adapter():
    a = SoapyAdapter(driver="sdrplay", samp_rate=SAMP, center_hz=144_100_000.0)
    a._np = np
    a._init_demod()
    return a


def test_audio_survives_realtime_supply_at_non_multiple_rate():
    a = _bench_adapter()
    n_chunks = SECONDS * AUDIO_RATE // CHUNK                 # 500 calls = 10 s of audio
    supply = int(SECONDS * SAMP * 1.004)                     # real-time + 0.4% slack ONLY
    block, t0 = 8192, 0
    out = []

    def top_up():
        nonlocal t0, supply
        while supply > 0 and len(a._audio_q) < 64:        # the test feeds the queue directly
            n = min(block, supply)
            t = (t0 + np.arange(n)) / SAMP
            a._audio_q.append(
                (0.1 * np.exp(2j * np.pi * TONE_HZ * t)).astype(np.complex64))
            t0 += n
            supply -= n

    starved = 0
    for _ in range(n_chunks):
        top_up()
        chunk = a.get_audio(CHUNK)
        if chunk is None:
            starved += 1
        else:
            out.extend(chunk)

    assert starved == 0, (
        f"audio starved {starved}/{n_chunks} chunks on a real-time supply — "
        f"the chain consumes more input than the device produces "
        f"(decim={a._decim}, ratio={a._rs_ratio:.4f})")

    # The tone must come out at TONE_HZ on the 24 kHz grid (time not warped).
    sig = np.asarray(out[AUDIO_RATE:])                       # skip 1 s of AGC settling
    spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
    peak_hz = np.argmax(spec) * AUDIO_RATE / len(sig)
    assert peak_hz == pytest.approx(TONE_HZ, abs=10), (
        f"tone landed at {peak_hz:.1f} Hz, expected {TONE_HZ:.0f} — resampler warps time")


def test_ssb_sideband_selection_actually_selects():
    # real(conj(z)) == real(z): the old 'LSB' path was a mathematical no-op, so
    # USB and LSB were byte-identical and both sidebands folded together —
    # found by ear ("strangely in usb and lsb ... no difference", 2026-08-01).
    # A +1 kHz tone is UPPER sideband: USB must pass it, LSB must reject it.
    t = np.arange(int(SAMP * 0.5)) / SAMP
    tone = (0.1 * np.exp(2j * np.pi * 1000.0 * t)).astype(np.complex64)
    blocks = np.array_split(tone, 20)

    a_usb = _bench_adapter()
    a_usb._mode = "USB"
    usb = np.concatenate([a_usb._demod_block(b) for b in blocks])
    a_lsb = _bench_adapter()
    a_lsb._mode = "LSB"
    lsb = np.concatenate([a_lsb._demod_block(b) for b in blocks])

    u = np.sqrt(np.mean(usb[2000:] ** 2))
    l = np.sqrt(np.mean(lsb[2000:] ** 2))
    assert u > 10 * l, (
        f"USB rms {u:.4f} vs LSB rms {l:.4f} — sideband selection not selecting "
        f"(ratio {u / max(l, 1e-12):.1f}x, need >10x)")


def test_sweet_spot_ratio_is_passthrough():
    a = SoapyAdapter(samp_rate=2_040_000)                    # 85 * 24 kHz exactly
    a._np = np
    a._init_demod()
    assert a._decim == 85
    assert a._rs_ratio == 1.0
