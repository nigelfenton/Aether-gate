#
# Aether-gate - CLI entry point.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Run Aether-gate with a chosen adapter:

    python -m aether_gate --adapter sim --pattern test_card
    python -m aether_gate --adapter sim --model FLEX-6700 --ae 10.0.0.107
    python -m aether_gate --adapter icom9700 --radio-ip 10.0.0.7 --user nigel --pass *** --ae 10.0.0.103

The core threads (discovery, UDP prime, control TCP serve) are wired exactly as
flex-sim wires them; only the signal source is swapped for the adapter.
"""
import argparse
import os
import signal
import threading
import time

from .core.engine import (Radio, BINS, FPS, SIGNAL_WIDTH_KHZ, DEFAULT_PORT,
                           DISCOVERY_PORT, local_ip, log, start_control_server)
from .adapters import get_adapter, available
from .adapters.icom.radios import get as get_icom, lan_radios


def apply_env_defaults(ap, environ=None):
    """Let every flag also be supplied as AETHER_GATE_<DEST>.

    --radio-ip -> AETHER_GATE_RADIO_IP, --rx-only -> AETHER_GATE_RX_ONLY=1.
    Precedence is CLI > env > built-in default: an explicit flag always wins, so
    nothing that works today changes behaviour.

    Generic on purpose -- a new add_argument() gets an env var for free, with no
    mapping table to drift out of sync with the flags.

    NOTE the name follows argparse DEST, not the flag spelling: the --pass flag
    is dest="pw", so its variable is AETHER_GATE_PW.

    Why: a container or a systemd unit can configure the gate without a
    hand-edited command line -- and without a password sitting on one.
    """
    env = os.environ if environ is None else environ
    truthy = lambda v: v.strip().lower() not in ("", "0", "false", "no", "off")
    for act in ap._actions:
        if not act.option_strings:
            continue                                   # positionals have no env form
        val = env.get("AETHER_GATE_" + act.dest.upper())
        if val is None:
            continue
        if isinstance(act, argparse._StoreTrueAction):
            act.default = truthy(val)
        elif isinstance(act, argparse._StoreFalseAction):
            act.default = not truthy(val)
        else:
            act.default = val          # argparse applies type= to a string default
        act.required = False
    return ap


def build_adapter(name, args):
    cls = get_adapter(name)
    if name == "sim":
        # honour --station/--serial so AE labels it "Aether-gate", not "flex-sim"
        station = args.station if args.station != "aether-gate 1" else "Aether-gate"
        return cls(pattern=args.pattern, model=args.model,
                   serial=args.serial, station=station)
    if name == "soapy":
        return cls(driver=args.soapy_driver, device_args=args.soapy_args,
                   samp_rate=args.samp_rate, gain_db=args.gain,
                   model=args.model, serial=args.serial, station=args.station,
                   direct_samp=args.direct_samp, agc=args.agc)
    if name == "icom9700":
        # give the 9700 a distinct identity unless the user overrode the shared defaults
        row = get_icom(args.icom_model)
        tag = row.model.replace("IC-", "").replace("-", "")
        serial = args.serial if args.serial != "GATE0001" else f"GATE{tag}"
        station = args.station if args.station != "aether-gate 1" else f"Icom-{row.model}"
        # Advertise the Flex the ROW names: FLEX-6700 for a rig with 2m, FLEX-6600 for
        # an HF+6m rig like the 7610 (blanket-bumping every LAN Icom to 6700 handed AE
        # a phantom 2m band on HF-only radios).
        model = args.model if args.model != "FLEX-6600" else row.advertise
        a = cls(radio_ip=args.radio_ip, username=args.user, password=args.pw,
                local_ip=args.radio_local_ip, radio_port=args.radio_port,
                civ_addr=int(str(args.civ_addr), 16), icom_model=row.model, model=model,
                serial=serial, station=station,
                usb_civ_port=args.usb_civ_port, usb_civ_baud=args.usb_civ_baud,
                rx_only=args.rx_only)
        a.lan_mod_min = args.lan_mod_min       # auto-fix LAN MOD Level on connect
        return a
    if name == "icom7300":
        serial = args.serial if args.serial != "GATE0001" else "GATE7300"
        station = args.station if args.station != "aether-gate 1" else "Icom-IC-7300"
        model = args.model if args.model != "FLEX-6600" else "FLEX-6600"
        return cls(usb_civ_port=args.usb_civ_port, usb_civ_baud=args.usb_civ_baud,
                   civ_addr=int(str(args.civ_addr), 16), model=model,
                   serial=serial, station=station,
                   usb_audio_device=args.usb_audio_device)
    if name == "kenwood":
        serial = args.serial if args.serial != "GATE0001" else "GATEKENW"
        # include the actual Kenwood model so the AE chooser reads e.g.
        # "Aether-gate TS-450S", not a generic "Kenwood"
        station = args.station if args.station != "aether-gate 1" else f"Kenwood-{args.kw_model}"
        return cls(model=args.kw_model,
                   rigctld_host=args.rigctld_host, rigctld_port=args.rigctld_port,
                   hamlib_model=args.hamlib_model,
                   serial_port=args.rig_serial_port, serial_baud=args.rig_baud,
                   rigctld_bin=args.rigctld_bin,
                   soapy_driver=args.soapy_driver, soapy_args=args.soapy_args,
                   samp_rate=args.samp_rate, gain_db=args.gain,
                   direct_samp=args.direct_samp, agc=args.agc,
                   advertise=(args.model if args.model != "FLEX-6600" else None),
                   serial=serial, station=station, enable_tx=args.enable_tx)
    if name == "yaesu":
        serial = args.serial if args.serial != "GATE0001" else "GATEYAES"
        # e.g. "Aether-gate Yaesu-FT-847" in the AE chooser
        station = args.station if args.station != "aether-gate 1" else f"Yaesu-{args.yaesu_model}"
        # Yaesu adapter resolves advertise/hamlib_model/bands from its own registry
        # row; --model only overrides advertise if the user changed it off the default.
        return cls(model=args.yaesu_model,
                   rigctld_host=args.rigctld_host, rigctld_port=args.rigctld_port,
                   # None => let the Yaesu registry pick the model id (FT-847=1001);
                   # only override when the user passed --hamlib-model explicitly.
                   hamlib_model=args.hamlib_model,
                   serial_port=args.rig_serial_port, serial_baud=args.rig_baud,
                   rigctld_bin=args.rigctld_bin,
                   soapy_driver=args.soapy_driver, soapy_args=args.soapy_args,
                   samp_rate=args.samp_rate, gain_db=args.gain,
                   direct_samp=args.direct_samp, agc=args.agc,
                   advertise=(args.model if args.model != "FLEX-6600" else None),
                   serial=serial, station=station, enable_tx=args.enable_tx)
    if name == "hpsdr":
        serial = args.serial if args.serial != "GATE0001" else "GATEHPSD"
        station = args.station if args.station != "aether-gate 1" else "Radioberry-HPSDR"
        # FLEX-6700 (covers 2m) unless the user overrode the shared default
        model = args.model if args.model != "FLEX-6600" else "FLEX-6700"
        # HPSDR-1 sample rate is one of 48/96/192/384 kHz; --samp-rate default is
        # the soapy 2.04M, so fold anything >=384k down to 384k for HPSDR.
        sr = int(args.samp_rate) if int(args.samp_rate) in (48000, 96000, 192000, 384000) else 48000
        return cls(radio_ip=args.radio_ip, local_ip=args.radio_local_ip,
                   samp_rate=sr, gain_db=int(args.gain),
                   model=model, serial=serial, station=station)
    return cls()


def main(argv=None):
    import sys
    raw = list(sys.argv[1:] if argv is None else argv)
    # First-run UX: bare `python -m aether_gate` (or `--setup`) opens the Radio Setup
    # web UI in the browser (pick a radio, hit Start) instead of silently starting the
    # sim. Any explicit adapter flags still run the gate directly (and the launcher
    # spawns children WITH flags, so no recursion).
    if not raw or "--setup" in raw:
        from .setup import main as setup_main
        return setup_main()

    ap = argparse.ArgumentParser(prog="aether_gate",
                                 description="Universal radio bridge - presents any radio to AetherSDR as a Flex 6000.")
    ap.add_argument("--adapter", default="sim", choices=available(),
                    help="signal source adapter (default: sim)")
    ap.add_argument("--pattern", default="carrier",
                    help="sim adapter: test pattern (e.g. test_card, carrier, two_tone)")
    ap.add_argument("--model", default="FLEX-6600",
                    help="advertised radio model (drives slice cap)")
    ap.add_argument("--ip", default=None, help="our IP (default: autodetect)")
    ap.add_argument("--ae", default=None, help="AE IP to unicast discovery to (optional)")
    ap.add_argument("--bins", type=int, default=BINS)
    ap.add_argument("--fps", type=int, default=FPS)
    ap.add_argument("--width-khz", type=float, default=SIGNAL_WIDTH_KHZ)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="control/data port to bind+advertise (default 4992; set e.g. 5992 to coexist with AE on one host)")
    ap.add_argument("--ctl-port", type=int, default=8731, help="web control-panel port (0 to disable)")
    ap.add_argument("--no-update-check", action="store_true",
                    help="skip the startup check for a newer release (also: env AETHER_GATE_NO_UPDATE_CHECK=1)")
    # soapy adapter options
    ap.add_argument("--soapy-driver", default="rtlsdr", help="soapy adapter: SoapySDR driver (rtlsdr/airspy/sdrplay/...)")
    ap.add_argument("--soapy-args", default="", help="soapy adapter: extra device args, comma kv (e.g. serial=00000001)")
    ap.add_argument("--samp-rate", type=float, default=2_040_000, help="soapy adapter: sample rate (Hz)")
    ap.add_argument("--gain", type=float, default=40.0, help="soapy adapter: RX gain dB (ignored if --agc)")
    ap.add_argument("--agc", action="store_true", help="soapy adapter: enable hardware AGC")
    ap.add_argument("--direct-samp", default=None, help="soapy adapter: RTL direct-sampling mode (Q=2 for HF on non-V4)")
    # Icom adapter options
    ap.add_argument("--radio-ip", default=None, help="icom9700 adapter: IC-9700 LAN IP")
    ap.add_argument("--user", default=None, help="icom9700 adapter: radio Network username")
    ap.add_argument("--pass", dest="pw", default=None, help="icom9700 adapter: radio Network password")
    ap.add_argument("--radio-port", type=int, default=50001, help="icom9700 adapter: control port (default 50001)")
    ap.add_argument("--radio-local-ip", default=None, help="icom9700 adapter: local IP that reaches the radio (default: autodetect; set when the radio LAN differs from --ip, e.g. gate advertised on Tailscale but radio on the LAN)")
    ap.add_argument("--icom-model", default="IC-9700",
                    help="icom9700 adapter: which LAN Icom (%s). Drives band coverage, "
                         "the bands= advert to AE, the advertised Flex model and the "
                         "default CI-V address." % "/".join(lan_radios()))
    ap.add_argument("--civ-addr", default="A2", help="Icom adapter: radio CI-V address hex (default A2 for 9700; use 94 for 7300)")
    ap.add_argument("--usb-civ-port", default=None, help="Icom USB CI-V serial port (e.g. COM7 or /dev/ttyUSB0). Required for icom7300; optional RX2 helper for icom9700.")
    ap.add_argument("--usb-civ-baud", type=int, default=115200, help="Icom USB CI-V baud (default 115200)")
    ap.add_argument("--lan-mod-min", type=int, default=128, help="icom9700 adapter: on connect, raise the rig's LAN MOD Level to at least this (0..255) so TX audio modulates (0 = bare carrier). Default 128 (=50%%). Set 0 to disable the auto-fix.")
    ap.add_argument("--usb-audio-device", default=None, help="icom7300 adapter: ALSA capture device for RX audio (default: auto USB Audio CODEC/plughw card)")
    # kenwood adapter options (hamlib control + IF-tap SDR spectrum; reuses --soapy-*/--gain/--samp-rate)
    ap.add_argument("--kw-model", default="TS-2000", help="kenwood adapter: Kenwood model (TS-2000/TS-590SG/TS-890S)")
    ap.add_argument("--rigctld-host", default="127.0.0.1", help="kenwood adapter: rigctld daemon host")
    ap.add_argument("--rigctld-port", type=int, default=4532, help="kenwood adapter: rigctld TCP port (default 4532)")
    ap.add_argument("--hamlib-model", type=int, default=None, help="kenwood adapter: override the rigctld model id (rigctl -l)")
    ap.add_argument("--rig-serial-port", default=None, help="kenwood adapter: serial port of the rig (e.g. COM10 or /dev/ttyUSB0). If set, the gate SPAWNS rigctld itself (with the RTS/DTR/no-handshake config); else it connects to an already-running rigctld.")
    ap.add_argument("--rig-baud", type=int, default=4800, help="kenwood adapter: rig serial baud (TS-450 = 4800)")
    ap.add_argument("--rigctld-bin", default="rigctld", help="kenwood adapter: rigctld executable path (default: on PATH)")
    # yaesu adapter options (same hamlib+soapy plumbing as kenwood; shares --rigctld-*/--rig-*/--soapy-*)
    ap.add_argument("--yaesu-model", default="FT-847", help="yaesu adapter: Yaesu model (FT-847/FT-991A/FTDX10/FT-710/FT-817)")
    ap.add_argument("--serial", default="GATE0001", help="advertised Flex serial (unique per gate; avoids AE chooser collisions)")
    ap.add_argument("--station", default="aether-gate 1", help="station name AE displays (number per dongle: 'aether-gate 1', 'aether-gate 2', ...)")
    ap.add_argument("--enable-tx", action="store_true",
                    help="advertise tx_capable=True to AE for a CAT rig (kenwood/yaesu). OFF by "
                         "default: no PTT is wired yet, so this only makes AE OFFER TX — it does "
                         "NOT key the radio. Do not enable until a tested PTT seam exists.")
    ap.add_argument("--rx-only", action="store_true",
                    help="hard-disable transmit: refuse PTT at the CI-V layer, no-op arm_tx, "
                         "and advertise tx_capable=False so AE greys its TX button. For an "
                         "unattended / permanent gateway. Env: AETHER_GATE_RX_ONLY=1")

    apply_env_defaults(ap)
    args = ap.parse_args(argv)

    if args.adapter == "icom9700" and not (args.radio_ip and args.user and args.pw):
        ap.error("--adapter icom9700 requires --radio-ip, --user and --pass")
    if args.adapter == "icom9700":
        _row = get_icom(args.icom_model)
        if _row is None:
            ap.error(f"--icom-model {args.icom_model!r} is not a known LAN Icom "
                     f"({', '.join(lan_radios())})")
        # default the CI-V address from the model unless the user set one
        if args.civ_addr == "A2":
            args.civ_addr = f"{_row.civ_addr:02X}"
    if args.adapter == "icom7300":
        if not args.usb_civ_port:
            ap.error("--adapter icom7300 requires --usb-civ-port")
        if args.civ_addr == "A2":
            args.civ_addr = "94"

    ip = args.ip or local_ip()
    adapter = build_adapter(args.adapter, args)

    # GRACEFUL SIGTERM. `systemctl stop` (and any supervisor: nssm/launchd)
    # sends SIGTERM, whose Python default is to kill the process WITHOUT
    # running our finally: adapter.close() — so a service stop would skip the
    # 0x05 disconnect and strand a phantom session, the exact bug fixed for
    # Ctrl-C. Turn SIGTERM into the SAME graceful path as Ctrl-C by raising
    # KeyboardInterrupt into the main thread: it unwinds through the try/finally
    # below, so close() (→0x05) always runs. Best-effort — signal is a no-op on
    # platforms lacking SIGTERM (Windows delivers it for our own Popen kills).
    def _graceful(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _graceful)
    except (ValueError, AttributeError, OSError):
        pass                              # not main thread / no SIGTERM here

    # adapter.open() lives INSIDE the try/finally so a connect failure still
    # runs adapter.close() — for the IC-9700 that's what sends the clean 0x05
    # disconnect and releases the radio's session. Skipping it (open() used to
    # be up here, outside the finally) left a phantom RS-BA1 session that
    # blocked the next Start ("came up then jumped to the other radio"). On a
    # failed connect we clean up and STOP — the radio needs an untouched
    # settle (power-cycle, or Menu>Set>Network>Network function OFF~10s>ON)
    # before it will accept a fresh login; hammering retries only resets its
    # stale timer. Fix it at the radio, then Start again.
    try:
        try:
            adapter.open()
        except Exception as e:
            # open()'s own wrapper already tore the session down (sent 0x05);
            # the finally: adapter.close() below is the belt-and-braces. Just
            # report and STOP.
            log(f"adapter open failed: {e}")
            log("cleaned up (sent disconnect). NOT retrying — let the radio "
                "settle (power-cycle or toggle its Network function off/on), "
                "then Start again.")
            return 1

        radio = Radio(ip, args.ae, args.pattern, args.bins, args.fps, args.width_khz,
                      port=args.port, model=args.model, adapter=adapter)
        threading.Thread(target=radio.discovery_loop, daemon=True).start()
        threading.Thread(target=radio.prime_loop, daemon=True).start()
        if args.ctl_port:
            start_control_server(radio, args.ctl_port)

        log(f"aether-gate - adapter={args.adapter} provides={adapter.provides} "
            f"model={radio.model} serial={radio.serial} ip={ip}")
        log(f"discovery -> AE's UDP :{DISCOVERY_PORT}; control/data on :{args.port}"
            + ("  (same-host mode)" if args.port != DISCOVERY_PORT else ""))
        if args.ctl_port:
            log(f"** control panel: http://{ip}:{args.ctl_port}/ **")
        # Best-effort "is there a newer release?" check — background, non-fatal,
        # silent on any failure; opt out with AETHER_GATE_NO_UPDATE_CHECK=1.
        from . import __version__
        from .update_check import check_for_update
        check_for_update(__version__, enabled=not args.no_update_check, logfn=log)
        radio.serve()
    except KeyboardInterrupt:
        # Ctrl-C OR SIGTERM (see _graceful) — flip the serve loop off and let
        # the finally run close()/0x05. Also covers a stop DURING open()/startup
        # (radio may not exist yet), so guard the attribute.
        try:
            radio.run = False
        except NameError:
            pass
        log("bye")
    finally:
        try:
            adapter.close()
        except Exception:
            pass


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())   # propagate rc=1 on open-failure so systemd Restart=on-failure fires
