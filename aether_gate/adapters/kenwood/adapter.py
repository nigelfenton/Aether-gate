#
# Aether-gate - Kenwood (and any hamlib CAT rig) adapter: hamlib control + IF-tap SDR spectrum.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Bridge a Kenwood (or any hamlib-supported CAT rig) into AetherSDR as a Flex.

The two axes are sourced SEPARATELY and combined here (see RADIO_SUPPORT.md):
  * CONTROL  = hamlib `rigctld` — freq / mode / PTT / S-meter. Vendor-neutral, so
               the SAME adapter serves Kenwood, Yaesu, Elecraft, Icom-USB by just
               changing the rigctld model number. No spectrum/audio over CAT.
  * SPECTRUM = a SoapySDR dongle (the existing SoapyAdapter, provides="iq"), tapped
               off-air / IF / antenna and STEERED to follow the rig's tuned freq.

`provides = "iq"` (delegated to the soapy dongle; the core runs the FFT). This
adapter's own job is the glue: read the rig's freq/mode from hamlib and keep the
dongle centred on the rig (CAT-steer), so AE's panadapter tracks where the rig is
tuned even though the spectrum is the dongle's.

⚠ STATUS: scaffold. Untested end-to-end (needs a Kenwood + a dongle + rigctld
running). The soapy half is HW-proven (RTL-SDR V4 on the Pi5); the hamlib half
is new. The CAT-steer cadence is deliberately gentle (poll the rig ~2-3x/s) —
mirrors the lesson from the IC-9700 (don't flood the rig's CAT).
"""
import threading
import time

from ..base import RadioAdapter, AdapterCaps, Meters
from ..soapy import SoapyAdapter
from ..hamlib.rigctld import Rigctld
from .radios import get as get_kenwood


class KenwoodAdapter(RadioAdapter):
    """hamlib-controlled CAT rig + IF-tap SoapySDR spectrum, presented as a Flex."""

    provides = "iq"          # spectrum/IQ comes from the dongle; core does the FFT

    def __init__(self, model="TS-2000",
                 # hamlib control
                 rigctld_host="127.0.0.1", rigctld_port=4532, hamlib_model=None,
                 # rigctld auto-spawn (if serial_port given, we launch rigctld ourselves;
                 # otherwise we connect to an already-running rigctld at host:port)
                 serial_port=None, serial_baud=4800, rigctld_bin="rigctld",
                 serial_conf="serial_handshake=None,rts_state=ON,dtr_state=ON",
                 # soapy spectrum dongle (defaults suit an RTL-SDR Blog V4)
                 soapy_driver="rtlsdr", soapy_args="", samp_rate=2_040_000,
                 gain_db=40.0, direct_samp=None, agc=False,
                 # AE identity
                 advertise=None, serial="GATEKENW", station="Aether-gate Kenwood"):
        self.model = model
        row = get_kenwood(model)
        self._row = row
        # advertise: explicit arg > registry row > sensible default
        adv = advertise or (row.advertise if row else "FLEX-6600")
        bands = tuple(b.name for b in row.bands) if row else ()

        self._hamlib_model = hamlib_model or (row.hamlib_model if row else None)
        # rigctld auto-spawn config. serial_conf default carries the TS-450 fix
        # (RTS+DTR asserted, no handshake) — proven necessary 2026-07-02 or hamlib
        # times out even though the radio answers.
        self._serial_port = serial_port
        self._serial_baud = int(serial_baud)
        self._rigctld_bin = rigctld_bin
        self._serial_conf = serial_conf
        self._rigctld_proc = None
        self._ctl = Rigctld(rigctld_host, rigctld_port)

        # The dongle does the spectrum; start it centred wherever the rig is (updated on open()).
        self._sdr = SoapyAdapter(driver=soapy_driver, device_args=soapy_args,
                                 samp_rate=samp_rate, gain_db=gain_db,
                                 center_hz=14_100_000.0, model=adv,
                                 serial=serial, station=station,
                                 direct_samp=direct_samp, agc=agc)

        # bands= advertised to AE (radio-declared-bands); tx_capable True (real transceiver)
        self.capabilities = AdapterCaps(model=adv, serial=serial, station=station,
                                        tx_capable=True,
                                        min_span_hz=48_000.0, max_span_hz=samp_rate,
                                        bands=bands)

        # rig state (from hamlib), refreshed by the single worker thread
        self._freq_hz = None
        self._mode = None
        self._smeter_db = None
        self._set_freq_target = None    # AE-requested freq (worker applies, coalesced)
        self._set_mode_target = None    # AE-requested mode
        self._ae_drive_t = 0.0          # when AE last drove a set (suppresses dial-read echo)
        self._poll_run = False
        self._poll_thread = None

    # --- rigctld auto-spawn ---------------------------------------------
    def _spawn_rigctld(self):
        """Launch rigctld ourselves for a serial rig, with the serial config
        baked in (so the user doesn't have to remember the RTS/DTR/handshake
        incantation). Only when serial_port was given; else we assume an
        already-running daemon at rigctld_host:port."""
        import subprocess
        cmd = [self._rigctld_bin, "-m", str(self._hamlib_model),
               "-r", self._serial_port, "-s", str(self._serial_baud),
               "-t", str(self._ctl.port)]
        if self._serial_conf:
            cmd += ["--set-conf", self._serial_conf]
        print(f"[kenwood] spawning rigctld: {' '.join(cmd)}", flush=True)
        self._rigctld_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.STDOUT)
        # wait until rigctld is actually accepting connections (it can take a
        # couple of seconds to open the serial port + bind on a Pi) — poll up to 8s.
        import socket as _sock
        end = time.monotonic() + 8.0
        while time.monotonic() < end:
            time.sleep(0.4)
            if self._rigctld_proc.poll() is not None:
                raise RuntimeError(f"rigctld exited immediately (rc={self._rigctld_proc.returncode}) "
                                   f"— check serial port {self._serial_port} / model {self._hamlib_model}")
            try:
                s = _sock.create_connection(("127.0.0.1", self._ctl.port), 0.5)
                s.close()
                print("[kenwood] rigctld ready", flush=True)
                return
            except OSError:
                continue
        print("[kenwood] WARNING: rigctld not accepting connections after 8s", flush=True)

    # --- lifecycle -------------------------------------------------------
    def open(self):
        # 0. if a serial port was given, spawn rigctld ourselves (with the fix)
        if self._serial_port:
            self._spawn_rigctld()
        # 1. hamlib control up first (so we know where the rig is tuned)
        self._ctl.connect()
        f = self._ctl.get_freq_hz()
        m = self._ctl.get_mode()
        if f:
            self._freq_hz = f
            self._sdr.center_hz = float(f)          # centre the dongle on the rig before it opens
            self._sdr._slice_hz = float(f)
        if m:
            self._mode = m
        # 2. dongle spectrum up, centred on the rig
        self._sdr.open()
        if m:
            self._sdr.set_mode(m)
        # 3. start the gentle CAT-steer poll
        self._poll_run = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True,
                                             name="kenwood-catsteer")
        self._poll_thread.start()

    def close(self):
        self._poll_run = False
        try: self._sdr.close()
        except Exception: pass
        try: self._ctl.close()
        except Exception: pass
        if self._rigctld_proc:                 # kill our spawned rigctld
            try:
                self._rigctld_proc.terminate()
                self._rigctld_proc.wait(timeout=3)
            except Exception:
                try: self._rigctld_proc.kill()
                except Exception: pass

    # --- the SINGLE hamlib worker: ONLY this thread touches rigctld -------
    # 4800-baud serial can't take contention: the old design had the poll
    # thread AND the control-thread setters both hitting rigctld, corrupting
    # request/reply and dropping the link on rapid AE tunes (the "crash +
    # recover to 14.100" symptom). Now ALL rigctld I/O is serialised through
    # this one loop: it services a pending AE freq/mode SET first (coalesced —
    # only the latest target), else does one READ per tick to follow the dial.
    def _poll_loop(self):
        read_i = 0
        while self._poll_run:
            time.sleep(0.20)
            try:
                # 1. AE asked to SET freq? (coalesced: only the latest wins)
                tgt = self._set_freq_target
                if tgt is not None:
                    self._set_freq_target = None
                    if self._ctl.set_freq_hz(tgt):
                        self._freq_hz = tgt
                        self._sdr.set_slice(float(tgt))
                    self._ae_drive_t = time.monotonic()   # suppress dial-read echo briefly
                    continue
                # 2. AE asked to SET mode?
                mtgt = self._set_mode_target
                if mtgt is not None:
                    self._set_mode_target = None
                    ok = self._ctl.set_mode(mtgt)
                    if ok:
                        self._mode = mtgt
                        self._sdr.set_mode(mtgt)
                    continue
                # 3. otherwise READ the rig to follow the dial. Round-robin so
                #    each read is one cheap serial transaction. Hold freq-read
                #    off for 1.5s after an AE set so we don't fight the user.
                read_i += 1
                if read_i % 3 == 0:
                    if time.monotonic() - getattr(self, "_ae_drive_t", 0) > 1.5:
                        f = self._ctl.get_freq_hz()
                        if f and abs((self._freq_hz or 0) - f) > 1:
                            self._freq_hz = f
                            self._sdr.set_slice(float(f))
                elif read_i % 3 == 1:
                    if time.monotonic() - getattr(self, "_ae_drive_t", 0) > 1.5:
                        m = self._ctl.get_mode()
                        if m and m != self._mode:
                            self._mode = m
                            self._sdr.set_mode(m)
                else:
                    s = self._ctl.get_smeter_db()
                    if s is not None:
                        self._smeter_db = s
            except Exception:
                pass   # a CAT hiccup must not kill the worker

    # --- spectrum (delegated to the dongle) -----------------------------
    def get_iq(self, n, center_hz, span_hz):
        return self._sdr.get_iq(n, center_hz, span_hz)

    def get_audio(self, *a, **k):
        return self._sdr.get_audio(*a, **k) if hasattr(self._sdr, "get_audio") else None

    # --- control (AE -> rig): POST a target; the worker does the hamlib I/O.
    # Non-blocking + coalescing — rapid AE tunes just overwrite the target, so
    # the serial link never backs up (fixes the crash-on-rapid-tune).
    def retune(self, center_hz):
        self._set_freq_target = int(center_hz)
        self._sdr.retune(center_hz)          # dongle centre is cheap + local, do it now

    def set_slice(self, slice_hz):
        self._set_freq_target = int(slice_hz)
        self._sdr.set_slice(slice_hz)        # local demod target, immediate

    def set_mode(self, mode):
        self._set_mode_target = mode
        self._mode = mode                    # optimistic: reflect intent NOW so the
                                             # radio->AE sync doesn't read stale + fight it
        self._ae_drive_t = time.monotonic()  # also guard the freq/mode read loop briefly
        self._sdr.set_mode(mode)             # local, immediate

    def set_span(self, span_hz):
        # dongle span is fixed by sample rate; nothing to push to the rig.
        pass

    # --- readback (rig -> AE) -------------------------------------------
    def initial_center_hz(self):
        return float(self._freq_hz) if self._freq_hz else None

    def initial_mode(self):
        return self._mode

    def radio_freq_hz(self):
        return self._freq_hz

    def radio_mode(self):
        return self._mode

    def receivers(self):
        """Single receiver -> one VFO. The engine's radio->AE sync
        (_sync_receivers) drives slice 0 from this list, so the rig's dial
        (read by the worker) reaches AE. One entry = one slice, no SUB."""
        if not self._freq_hz:
            return []
        return [{"freq_hz": self._freq_hz, "mode": self._mode}]

    def read_meters(self):
        # hamlib S-meter is dB relative to S9 (S9 = -73 dBm on HF).
        if self._smeter_db is None:
            return Meters()
        return Meters(s_meter_dbm=-73.0 + float(self._smeter_db))

    def diagnostics(self):
        return {
            "radio": self.model + " (hamlib CAT + IF-tap SDR)",
            "presented_as": self.capabilities.model,
            "link": {"transport": "hamlib rigctld + SoapySDR dongle",
                     "host": f"rigctld {self._ctl.host}:{self._ctl.port}",
                     "state": "connected" if self._ctl.connected else "down",
                     "hamlib_model": self._hamlib_model,
                     "dongle": self._sdr.driver},
            "vfos": ([{"name": "rig VFO", "freq_hz": self._freq_hz,
                       "mode": self._mode, "selected": True}] if self._freq_hz else []),
            "meters": {"s_meter_dbm": (round(-73.0 + self._smeter_db, 1)
                                       if self._smeter_db is not None else None)},
            "flags": {"cat_control": self._ctl.connected},
        }
