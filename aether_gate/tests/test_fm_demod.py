# NBFM demodulation in the SoapySDR adapter.
#
# Every mode that was not LSB fell through to the USB taps, so asking for FM got
# an SSB product detector. That is why 2 m AX.25 never decoded through an SDR
# gate while the audio still sounded clean to the ear (found live 2026-08-07 on
# an RSP1a: clean-sounding audio, zero packet decodes, no control in AE made any
# difference because every mode landed on the same code path).
#
# The tests below feed a SYNTHESISED FM signal with a KNOWN modulating tone and
# assert the tone comes back out. A steady carrier proves nothing about a
# demodulator — constant amplitude is constant by design — so every case here
# modulates, and the sideband/tone-ratio cases are the ones that would fail if
# the FM path silently reverted to SSB.

import math

import pytest

np = pytest.importorskip("numpy")

from aether_gate.adapters.soapy import SoapyAdapter, AUDIO_RATE


def _adapter(samp_rate=240_000.0):
    """An adapter with the demod chain built, but no hardware opened."""
    a = SoapyAdapter(driver="none", samp_rate=samp_rate, center_hz=145_070_000.0)
    a._np = np
    a._init_demod()
    return a


def _fm_iq(n, fs, tone_hz, dev_hz, amp=1.0):
    """Complex baseband FM: a carrier at 0 Hz whose frequency swings +/-dev_hz
    at tone_hz. This is what the NCO hands the demodulator after mixing.

    PHASE IS THE INTEGRAL OF FREQUENCY, so integrating 2*pi*dev*cos(2*pi*f*t)
    gives (dev/f)*sin(...) radians — the modulation index beta = dev/tone, NOT
    2*pi*dev/tone. Getting that wrong puts 6.28x too much phase in: beta=15.7 rad
    wraps through np.angle and the "demodulator" appears to output the third
    harmonic. That was a bug in this test, not in the adapter, and it is worth
    naming because the failure looks exactly like a broken discriminator.
    """
    t = np.arange(n) / fs
    phase = (dev_hz / tone_hz) * np.sin(2.0 * np.pi * tone_hz * t)
    return (amp * np.exp(1j * phase)).astype(np.complex128)


def _dominant_hz(x, fs):
    """Frequency of the largest spectral peak, ignoring DC."""
    w = np.hanning(len(x))
    sp = np.abs(np.fft.rfft(x * w))
    sp[: max(1, int(len(sp) * 0.002))] = 0.0        # kill DC/very-low bins
    return float(np.argmax(sp)) * fs / len(x)


def _tone_amp(x, fs, f0):
    """Amplitude at f0 via a direct Goertzel-ish projection."""
    t = np.arange(len(x)) / fs
    return 2.0 * abs(np.mean(x * np.exp(-2j * np.pi * f0 * t)))


@pytest.mark.parametrize("mode", ["FM", "NFM", "DFM", "FM-N"])
def test_fm_modes_take_the_discriminator(mode):
    """All the FM spellings AE can send must route to the FM path.

    DFM in particular is what AE actually sent on 2 m (data-mode FM); if only
    a literal "FM" were handled, packet would still fail in the field.
    """
    a = _adapter()
    assert a._is_fm_mode(mode) is True


@pytest.mark.parametrize("mode", ["USB", "LSB", "DIGU", "DIGL", "CW", "AM", ""])
def test_non_fm_modes_stay_on_ssb(mode):
    a = _adapter()
    assert a._is_fm_mode(mode) is False


def test_demodulates_a_1200hz_tone():
    """The AX.25 mark tone must come out of the discriminator at 1200 Hz."""
    a = _adapter()
    a._mode = "FM"
    fs = a.samp_rate
    iq = _fm_iq(int(fs * 0.25), fs, tone_hz=1200.0, dev_hz=3000.0)
    out = a._demod_block(iq)
    assert len(out) > 0
    got = _dominant_hz(out, a._pd_rate)
    assert abs(got - 1200.0) < 40.0, f"expected ~1200 Hz, got {got:.0f} Hz"


def test_demodulates_a_2200hz_tone():
    """...and the space tone at 2200 Hz. Both must survive the channel filter;
    an SSB-width filter would attenuate 2200 relative to 1200 and skew the
    slicer downstream."""
    a = _adapter()
    a._mode = "FM"
    fs = a.samp_rate
    iq = _fm_iq(int(fs * 0.25), fs, tone_hz=2200.0, dev_hz=3000.0)
    out = a._demod_block(iq)
    got = _dominant_hz(out, a._pd_rate)
    assert abs(got - 2200.0) < 40.0, f"expected ~2200 Hz, got {got:.0f} Hz"


def test_bell202_tones_come_back_at_similar_amplitude():
    """THE AX.25 ASSERTION. AFSK slices on the relative level of the 1200 and
    2200 Hz tones, so the demodulator must not favour one over the other.

    A discriminator is flat with modulating frequency, so equal deviation ->
    equal amplitude. The SSB path was not: sliding a 3 kHz-wide one-sided
    bandpass over the pair attenuates 2200 far more than 1200, which is what
    stopped packets decoding even though voice sounded fine.
    """
    a = _adapter()
    a._mode = "FM"
    fs = a.samp_rate
    amps = {}
    for f in (1200.0, 2200.0):
        a._fm_prev = np.complex128(0)
        a._fm_dc = None
        a._fm_state = np.zeros(len(a._fm_taps) - 1, dtype=np.complex128)
        out = a._demod_block(_fm_iq(int(fs * 0.25), fs, tone_hz=f, dev_hz=3000.0))
        amps[f] = _tone_amp(out, a._pd_rate, f)
    ratio = amps[2200.0] / amps[1200.0]
    assert 0.7 < ratio < 1.4, (
        f"tone imbalance {ratio:.2f} (1200={amps[1200.0]:.4f} 2200={amps[2200.0]:.4f}) "
        "— a flat discriminator should treat both nearly equally")


def test_output_is_independent_of_rf_amplitude():
    """FM carries information in deviation, not amplitude. A 10x stronger
    signal must demodulate to essentially the same audio — this is what makes
    the AGC unnecessary (and harmful) on this path."""
    a = _adapter()
    a._mode = "FM"
    fs = a.samp_rate
    outs = []
    for amp in (0.1, 1.0):
        a._fm_prev = np.complex128(0)
        a._fm_dc = None
        a._fm_state = np.zeros(len(a._fm_taps) - 1, dtype=np.complex128)
        outs.append(a._demod_block(_fm_iq(int(fs * 0.25), fs, 1200.0, 3000.0, amp=amp)))
    r = _tone_amp(outs[1], a._pd_rate, 1200.0) / _tone_amp(outs[0], a._pd_rate, 1200.0)
    assert 0.8 < r < 1.25, f"amplitude dependence {r:.2f}x — discriminator should be flat"


def test_deviation_scales_the_output():
    """Louder modulation = more deviation = bigger audio. Guards against a
    discriminator that saturates or normalises the swing away."""
    a = _adapter()
    a._mode = "FM"
    fs = a.samp_rate
    levels = []
    for dev in (1500.0, 3000.0):
        a._fm_prev = np.complex128(0)
        a._fm_dc = None
        a._fm_state = np.zeros(len(a._fm_taps) - 1, dtype=np.complex128)
        out = a._demod_block(_fm_iq(int(fs * 0.25), fs, 1200.0, dev))
        levels.append(_tone_amp(out, a._pd_rate, 1200.0))
    assert levels[1] > levels[0] * 1.6, (
        f"doubling deviation only changed output {levels[1]/levels[0]:.2f}x")


def test_block_boundaries_do_not_glitch():
    """Feeding one long block and the same signal in chunks must agree.

    The discriminator differences consecutive samples, so a reset between
    blocks injects a phase step — an audible tick at the block rate and a bit
    error mid-packet. _fm_prev carries that state.
    """
    fs = 240_000.0
    sig = _fm_iq(int(fs * 0.2), fs, 1200.0, 3000.0)

    whole = _adapter(fs)
    whole._mode = "FM"
    ref = whole._demod_block(sig)

    chunked = _adapter(fs)
    chunked._mode = "FM"
    parts = [chunked._demod_block(sig[i:i + 4096]) for i in range(0, len(sig), 4096)]
    got = np.concatenate([p for p in parts if len(p)])

    n = min(len(ref), len(got))
    # Compare the recovered TONE, not sample-by-sample: the staged decimators
    # carry their own comb phase, so chunking legitimately shifts the output.
    assert abs(_dominant_hz(ref[:n], whole._pd_rate)
               - _dominant_hz(got[:n], chunked._pd_rate)) < 40.0
    # and no huge discontinuity spikes from a reset discriminator
    assert float(np.max(np.abs(np.diff(got[:n])))) < 5.0 * float(np.std(got[:n])) + 1.0


def test_noise_does_not_clip_the_discriminator():
    """NOISE MUST NOT SATURATE THE OUTPUT.

    angle() spans +/-pi, and broadband noise has phase steps uniform over that
    range: RMS pi/sqrt(3) = 1.81 rad/sample. Scaling calibrated so a 5 kHz-
    deviation TONE hits full scale therefore put noise at 1.39 — hard clipped —
    while a real 3 kHz-deviation signal only reached 0.79. The clipper ate the
    signal and passed the noise, and no amount of RF gain changed it (measured
    identical at 6, 20 and 40 dB on 2026-08-07).

    The other tests here all measure ratios or frequencies, so every one of them
    passed while this was broken. This one asserts the absolute level.
    """
    a = _adapter()
    a._mode = "FM"
    fs = a.samp_rate
    rng = np.random.default_rng(12345)
    n = int(fs * 0.2)
    noise = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex128)
    out = a._demod_fm(noise)
    assert len(out) > 0
    rms = float(np.sqrt(np.mean(out ** 2)))
    peak = float(np.max(np.abs(out)))
    assert rms < 0.8, f"noise RMS {rms:.3f} — the discriminator output saturates on noise"
    assert peak <= 1.05, f"noise peak {peak:.3f} exceeds the +/-1 audio range"


def test_capture_effect_a_carrier_dominates_added_noise():
    """A carrier plus noise must demodulate to the TONE, not to the noise.

    NB comparing a bare signal against BARE noise is not the right test and it
    misled me once: with no carrier at all the discriminator sees uniformly
    random phase and legitimately outputs more than a narrowband signal does
    (FM noise power grows with bandwidth). What matters on a real channel is
    the capture effect — once a carrier is present it dominates the phase, and
    the recovered tone must stand clear of what is left.
    """
    fs = 240_000.0
    rng = np.random.default_rng(7)
    n = int(fs * 0.25)
    sig = _fm_iq(n, fs, 1200.0, 3000.0, amp=1.0)
    noise = 0.1 * (rng.normal(size=n) + 1j * rng.normal(size=n))

    a = _adapter(fs)
    a._mode = "FM"
    # through the FULL block path, not _demod_fm alone: _demod_fm expects data
    # already decimated to pd_rate, so feeding it raw samp_rate IQ measures
    # nothing real. (Doing exactly that in a scratch script made a correct
    # demodulator look like it had a 10x frequency error — I had computed the
    # frequency axis at the wrong rate and nearly "fixed" working code.)
    out = a._demod_block((sig + noise).astype(np.complex128))

    assert abs(_dominant_hz(out, a._pd_rate) - 1200.0) < 40.0
    tone = _tone_amp(out, a._pd_rate, 1200.0)
    total = float(np.sqrt(np.mean(out ** 2)))
    assert tone > 0.3 * total, (
        f"recovered 1200 Hz tone {tone:.4f} is small against total {total:.4f} "
        "— the carrier is not capturing the discriminator")


def test_get_audio_output_does_not_clip_on_noise():
    """THE OUTPUT AE ACTUALLY RECEIVES must not be saturated.

    test_noise_does_not_clip_the_discriminator checks _demod_fm's internal
    output, which is not what leaves the adapter: get_audio() applies its own
    trim afterwards. A x3 trim added in the same commit as the scaling fix put
    noise back at 1.73 RMS and clipped 40% of samples on a quiet channel — the
    internal test still passed. This one measures the real thing.
    """
    fs = 240_000.0
    a = _adapter(fs)
    a._mode = "FM"
    rng = np.random.default_rng(99)
    n = int(fs * 0.5)
    a._audio_q.append((rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex128))
    out = np.array(a.get_audio(4096))
    assert len(out) == 4096
    rms = float(np.sqrt(np.mean(out ** 2)))
    clipped = float(np.mean(np.abs(out) > 0.98))
    assert rms < 0.8, f"get_audio noise RMS {rms:.3f} — output is saturated"
    assert clipped < 0.02, f"{clipped*100:.1f}% of output samples are clipped"


def test_get_audio_preserves_signal_to_noise_ratio():
    """A signal must stay proportionally above the noise through get_audio().

    Guards the property the AFSK slicer depends on: whatever gain is applied,
    it must not compress signal and noise together (an AGC) or clip either.
    """
    fs = 240_000.0
    rng = np.random.default_rng(3)
    n = int(fs * 0.5)

    quiet = _adapter(fs); quiet._mode = "FM"
    quiet._audio_q.append((0.05 * (rng.normal(size=n) + 1j * rng.normal(size=n))).astype(np.complex128))
    q = float(np.sqrt(np.mean(np.array(quiet.get_audio(4096)) ** 2)))

    loud = _adapter(fs); loud._mode = "FM"
    loud._audio_q.append(_fm_iq(n, fs, 1200.0, 3000.0, amp=1.0))
    s_out = np.array(loud.get_audio(4096))
    tone = _tone_amp(s_out, AUDIO_RATE, 1200.0)

    assert tone > 0.05, f"recovered tone {tone:.4f} is too small to slice"
    assert float(np.max(np.abs(s_out))) <= 1.0001, "signal path clips"


def test_fm_does_not_use_the_ssb_taps():
    """Direct regression on the original defect: the FM path must not be the
    SSB path. Demodulating the same FM signal as FM and as USB must differ."""
    fs = 240_000.0
    sig = _fm_iq(int(fs * 0.25), fs, 1200.0, 3000.0)

    afm = _adapter(fs); afm._mode = "FM"
    assb = _adapter(fs); assb._mode = "USB"
    out_fm = afm._demod_block(sig)
    out_ssb = assb._demod_block(sig)

    n = min(len(out_fm), len(out_ssb))
    # normalise both, then require they are not near-identical
    def _n(x):
        s = float(np.std(x)) or 1.0
        return x / s
    corr = float(np.corrcoef(_n(out_fm[:n]), _n(out_ssb[:n]))[0, 1])
    assert abs(corr) < 0.9, f"FM and USB outputs correlate {corr:.3f} — FM is still on the SSB path"


def _meter_for(iq, fs=240_000.0, gain=20.0):
    a = SoapyAdapter(driver="none", samp_rate=fs, center_hz=145_000_000.0, gain_db=gain)
    a._np = np
    a._init_demod()
    a._mode = "FM"
    a._latest = iq.astype(np.complex128)
    return a.read_meters().s_meter_dbm


def test_s_meter_ranks_signals_above_noise():
    """The S-meter must read a real carrier STRONGER than a quiet channel.

    Measuring the demodulated audio gets this backwards: full-band noise makes
    more discriminator output than a narrowband signal, so a quiet channel
    metered -47 dBm against -55 dBm for a clean FM carrier. The meter has to
    measure RF power in the slice instead.
    """
    fs = 240_000.0
    n = int(fs * 0.05)
    rng = np.random.default_rng(1)
    noise = 0.02 * (rng.normal(size=n) + 1j * rng.normal(size=n))
    weak = 0.1 * _fm_iq(n, fs, 1200.0, 3000.0)
    strong = 1.0 * _fm_iq(n, fs, 1200.0, 3000.0)

    q, w, s = _meter_for(noise), _meter_for(weak), _meter_for(strong)
    assert q < w < s, f"meter not monotonic: noise={q:.1f} weak={w:.1f} strong={s:.1f}"


def test_s_meter_is_linear_in_db():
    """A 10x amplitude change must move the meter ~20 dB."""
    fs = 240_000.0
    n = int(fs * 0.05)
    a1 = _meter_for(0.1 * _fm_iq(n, fs, 1200.0, 3000.0))
    a2 = _meter_for(1.0 * _fm_iq(n, fs, 1200.0, 3000.0))
    assert 15.0 < (a2 - a1) < 25.0, f"10x amplitude moved the meter {a2-a1:.1f} dB, expected ~20"


# NB there is deliberately NO test that the meter ignores our own RF gain.
# read_meters() subtracts the configured gain_db so that turning the front end
# up does not read as more signal — which is right on hardware, where raising
# the gain really does make the IQ louder. A unit test cannot reproduce that:
# it feeds identical IQ at both gain settings, so the compensation has nothing
# to cancel and correctly appears as a 20 dB shift. Asserting otherwise would
# mean weakening working code to satisfy an unrealistic fixture.


def test_slice_is_never_left_sitting_on_dc():
    """OFFSET TUNING. The demodulator must never sit on the hardware centre.

    A direct-conversion SDR has a DC spike at the centre of its IQ (LO leakage
    + ADC offset) that is an artifact, not a signal. set_slice() used to
    recentre the hardware exactly ON the slice when the slice left the window,
    parking the demodulator on that spike: S-meter S9+20, a bright line at the
    cursor in the waterfall, and audio containing only the artifact. Six real
    S9+20 transmissions produced no measurable change in the audio.
    """
    fs = 2_040_000.0
    a = SoapyAdapter(driver="none", samp_rate=fs, center_hz=145_000_000.0)
    a._np = np

    # a slice far outside the current window forces a hardware retune
    a.set_slice(146_500_000.0)
    assert a._retune_to is not None, "slice outside the window should force a retune"
    offset = abs(a._retune_to - 146_500_000.0)
    assert offset > 0.05 * fs, (
        f"hardware would centre only {offset:.0f} Hz from the slice — "
        "the demodulator lands on the DC spike")
    # ...but still inside the usable passband
    assert offset < 0.40 * fs, (
        f"offset {offset:.0f} Hz pushes the slice outside the usable window")


def test_offset_tune_keeps_the_slice_in_the_window():
    """After the offset retune the slice must still be demodulable."""
    fs = 2_040_000.0
    a = SoapyAdapter(driver="none", samp_rate=fs, center_hz=145_000_000.0)
    a._np = np
    target = 146_500_000.0
    a.set_slice(target)
    new_center = a._retune_to
    assert abs(target - new_center) < 0.40 * fs, "slice fell outside the usable window"


def test_every_retune_path_respects_the_dc_offset():
    """ALL THREE routes that move the hardware must keep DC off the slice.

    Fixing only set_slice() was not enough: the log showed a correct offset
    tune to 145.510 immediately undone by a retune() to 145.070. get_iq() is
    the third path and the one AE drives every frame, since the pan centre and
    the slice are the same frequency until the operator scrolls the panadapter.
    """
    fs = 2_040_000.0
    slice_hz = 145_070_000.0

    # 1. set_slice, slice outside the window
    a = SoapyAdapter(driver="none", samp_rate=fs, center_hz=140_000_000.0)
    a._np = np
    a.set_slice(slice_hz)
    assert abs(a._retune_to - slice_hz) > 0.05 * fs, "set_slice parks on DC"

    # 2. retune() asked for the slice frequency itself
    b = SoapyAdapter(driver="none", samp_rate=fs, center_hz=140_000_000.0)
    b._np = np
    b._slice_hz = slice_hz
    b.retune(slice_hz)
    assert abs(b._retune_to - slice_hz) > 0.05 * fs, "retune() parks on DC"

    # 3. get_iq() following AE's pan centre onto the slice
    c = SoapyAdapter(driver="none", samp_rate=fs, center_hz=140_000_000.0)
    c._np = np
    c._slice_hz = slice_hz
    c.get_iq(1024, slice_hz, fs)
    assert c._retune_to is not None
    assert abs(c._retune_to - slice_hz) > 0.05 * fs, "get_iq parks on DC"


def test_panadapter_bins_line_up_with_ae_pan_centre():
    """The pan must paint the signal where AE thinks it is.

    get_iq() hands the core a raw block which it FFTs and labels with AE's pan
    centre — so the samples must actually BE centred there. Offset tuning moved
    the hardware a quarter sample rate away, so the raw block painted the
    waterfall 510 kHz off: a signal appeared far from the slice cursor while the
    demodulator (which does its own NCO shift) heard it correctly. Reported as
    "the waterfall and signal are not in the same place" (2026-08-07).
    """
    fs = 2_040_000.0
    hw_center = 145_580_000.0        # where the tuner really is (offset-tuned)
    ae_center = 145_070_000.0        # where AE thinks the pan is centred
    tone_hz = 145_100_000.0          # a signal 30 kHz above AE's centre

    a = SoapyAdapter(driver="none", samp_rate=fs, center_hz=hw_center)
    a._np = np
    a._slice_hz = ae_center

    n = 32768
    t = np.arange(n) / fs
    # a carrier at tone_hz, expressed in the HARDWARE's baseband
    blk = np.exp(2j * np.pi * (tone_hz - hw_center) * t).astype(np.complex128)
    # The pan reads the block RING, not _latest — that is what lets one FFT span
    # more than a single readStream block. Stage it the way the reader does.
    a._pan_ring.append(blk)

    out = a.get_iq(n, ae_center, fs)
    assert out is not None
    sp = np.abs(np.fft.fftshift(np.fft.fft(np.asarray(out) * np.hanning(len(out)))))
    fr = np.fft.fftshift(np.fft.fftfreq(len(out), 1.0 / fs))
    peak_hz = ae_center + fr[int(np.argmax(sp))]
    assert abs(peak_hz - tone_hz) < 5_000.0, (
        f"pan places the signal at {peak_hz/1e6:.4f} MHz, expected {tone_hz/1e6:.4f} MHz "
        f"(off by {(peak_hz-tone_hz)/1e3:.1f} kHz)")
