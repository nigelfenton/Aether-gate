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
import threading
import time

from .core.engine import (Radio, BINS, FPS, SIGNAL_WIDTH_KHZ, DEFAULT_PORT,
                           DISCOVERY_PORT, local_ip, log, start_control_server)
from .adapters import get_adapter, available


def build_adapter(name, args):
    cls = get_adapter(name)
    if name == "sim":
        return cls(pattern=args.pattern, model=args.model)
    if name == "soapy":
        return cls(driver=args.soapy_driver, device_args=args.soapy_args,
                   samp_rate=args.samp_rate, gain_db=args.gain,
                   model=args.model, serial=args.serial, station=args.station,
                   direct_samp=args.direct_samp, agc=args.agc)
    if name == "icom9700":
        # give the 9700 a distinct identity unless the user overrode the shared defaults
        serial = args.serial if args.serial != "GATE0001" else "GATE9700"
        station = args.station if args.station != "aether-gate 1" else "aether-gate 9700"
        return cls(radio_ip=args.radio_ip, username=args.user, password=args.pw,
                   local_ip=args.ip, radio_port=args.radio_port,
                   civ_addr=int(str(args.civ_addr), 16), model=args.model,
                   serial=serial, station=station)
    return cls()


def main(argv=None):
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
    # soapy adapter options
    ap.add_argument("--soapy-driver", default="rtlsdr", help="soapy adapter: SoapySDR driver (rtlsdr/airspy/sdrplay/...)")
    ap.add_argument("--soapy-args", default="", help="soapy adapter: extra device args, comma kv (e.g. serial=00000001)")
    ap.add_argument("--samp-rate", type=float, default=2_040_000, help="soapy adapter: sample rate (Hz)")
    ap.add_argument("--gain", type=float, default=40.0, help="soapy adapter: RX gain dB (ignored if --agc)")
    ap.add_argument("--agc", action="store_true", help="soapy adapter: enable hardware AGC")
    ap.add_argument("--direct-samp", default=None, help="soapy adapter: RTL direct-sampling mode (Q=2 for HF on non-V4)")
    # icom9700 adapter options
    ap.add_argument("--radio-ip", default=None, help="icom9700 adapter: IC-9700 LAN IP")
    ap.add_argument("--user", default=None, help="icom9700 adapter: radio Network username")
    ap.add_argument("--pass", dest="pw", default=None, help="icom9700 adapter: radio Network password")
    ap.add_argument("--radio-port", type=int, default=50001, help="icom9700 adapter: control port (default 50001)")
    ap.add_argument("--civ-addr", default="A2", help="icom9700 adapter: radio CI-V address hex (default A2)")
    ap.add_argument("--serial", default="GATE0001", help="advertised Flex serial (unique per gate; avoids AE chooser collisions)")
    ap.add_argument("--station", default="aether-gate 1", help="station name AE displays (number per dongle: 'aether-gate 1', 'aether-gate 2', ...)")
    args = ap.parse_args(argv)

    if args.adapter == "icom9700" and not (args.radio_ip and args.user and args.pw):
        ap.error("--adapter icom9700 requires --radio-ip, --user and --pass")

    ip = args.ip or local_ip()
    adapter = build_adapter(args.adapter, args)
    adapter.open()

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
    try:
        radio.serve()
    except KeyboardInterrupt:
        radio.run = False
        log("bye")
    finally:
        try:
            adapter.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
