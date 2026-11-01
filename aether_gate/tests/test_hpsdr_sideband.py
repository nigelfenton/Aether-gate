#
# Aether-gate — HPSDR SSB sideband selection (no hardware, no network).
# Copyright (C) 2026 Ozy K6OZY. GPL-3.0-or-later.
#
"""The HPSDR adapter's SSB demod must actually select a sideband.

It used to take real(z) for USB and real(conj(z)) for LSB — and
real(conj(z)) == real(z), so the two modes were byte-identical and both
sidebands folded into the audio. The soapy adapter had the same defect and was
fixed in 24f256f (2026-08-01); this pins the same property for HPSDR: a tone
ABOVE the slice must pass USB and be rejected by LSB, and the mirror for a tone
below.

⚠ NUMPY IS OPTIONAL IN THIS PROJECT, so this file SKIPS without it (same
pattern as test_demod_equivalence). It runs under pytest and as a bare module.
"""
import sys

try:
    import numpy as np
except ImportError:                                  # pragma: no cover
    np = None

SAMP = 48_000
TONE_HZ = 1000.0


def _skip(reason):
    """Skip under pytest; exit 0 as a bare module (see test_demod_equivalence)."""
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(reason, allow_module_level=True)
    print(f"SKIP: {reason}")
    raise SystemExit(0)


if np is None:
    _skip("numpy not installed - the demod path it tests is numpy-only")


def _demod_rms(mode, tone_hz):
    """Feed a complex tone `tone_hz` above the slice through get_audio(); return
    the RMS of the demodulated audio after the filters have settled."""
    from aether_gate.adapters.hpsdr.adapter import HpsdrAdapter

    a = HpsdrAdapter(samp_rate=SAMP, center_hz=14_100_000.0)
    a._np = np
    a._init_demod()                                  # what open() runs, minus the socket
    a._mode = mode
    a._agc_level = a._agc_target = 1.0               # hold AGC gain at unity: compare levels
    t = np.arange(int(SAMP * 0.5)) / SAMP
    tone = (0.1 * np.exp(2j * np.pi * tone_hz * t)).astype(np.complex64)
    for blk in np.array_split(tone, 20):
        a._audio_q.append(blk)
    out = []
    while True:
        a._agc_level = a._agc_target                 # AGC adapts per call; keep it fixed
        chunk = a.get_audio(256, slice_hz=a.center_hz)
        if chunk is None:
            break
        out.extend(chunk)
    audio = np.asarray(out[2000:])                   # past the FIR settle
    assert len(audio) > 1000, f"too little audio demodulated ({len(audio)} samples)"
    return float(np.sqrt(np.mean(audio * audio)))


def test_usb_passes_upper_and_lsb_rejects_it():
    u = _demod_rms("USB", +TONE_HZ)
    l = _demod_rms("LSB", +TONE_HZ)
    assert u > 10 * l, (
        f"+1 kHz tone: USB rms {u:.4f} vs LSB rms {l:.4f} — sideband selection "
        f"not selecting (ratio {u / max(l, 1e-12):.1f}x, need >10x)")


def test_lsb_passes_lower_and_usb_rejects_it():
    l = _demod_rms("LSB", -TONE_HZ)
    u = _demod_rms("USB", -TONE_HZ)
    assert l > 10 * u, (
        f"-1 kHz tone: LSB rms {l:.4f} vs USB rms {u:.4f} — sideband selection "
        f"not selecting (ratio {l / max(u, 1e-12):.1f}x, need >10x)")


def _main():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    print("test_hpsdr_sideband:", "all checks passed" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())
