#
# Aether-gate - setup / launcher web UI.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Web launcher that replaces the CLI flags: a first-page hint, per-radio-family
field groups (Icom / Kenwood / dongle / sim), saved profiles with connect-on-launch,
and Start/Stop that spawns `python -m aether_gate ...` (reusing all the CLI).

    python -m aether_gate.setup        # or bare `python -m aether_gate`
"""
import http.server
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

from .adapters import available
from .adapters.icom import radios as icom_radios
from .adapters.kenwood import radios as kenwood_radios
from .adapters.yaesu import radios as yaesu_radios

SETUP_PORT = 8730
PROFILES_PATH = os.path.join(os.path.expanduser("~"), ".aether-gate", "profiles.json")

_proc = None
_lock = threading.Lock()
_last_argv = []


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _icom_json():
    out = {}
    for m in icom_radios.supported():
        r = icom_radios.get(m)
        out[m] = {"civ_addr": f"0x{r.civ_addr:02X}", "transport": r.transport,
                  "advertise": r.advertise, "has_scope": r.has_scope,
                  "verified": r.verified, "bands": [b.name for b in r.bands]}
    return out


def _kenwood_json():
    out = {}
    for m in kenwood_radios.supported():
        r = kenwood_radios.get(m)
        out[m] = {"hamlib_model": r.hamlib_model, "advertise": r.advertise,
                  "spectrum": r.spectrum, "hf_dongle_needed": r.hf_dongle_needed,
                  "verified": r.verified, "bands": [b.name for b in r.bands]}
    return out


def _yaesu_json():
    out = {}
    for m in yaesu_radios.supported():
        r = yaesu_radios.get(m)
        out[m] = {"hamlib_model": r.hamlib_model, "advertise": r.advertise,
                  "spectrum": r.spectrum, "hf_dongle_needed": r.hf_dongle_needed,
                  "verified": r.verified, "bands": [b.name for b in r.bands]}
    return out


# --- saved profiles -------------------------------------------------------
def _load_profiles():
    try:
        with open(PROFILES_PATH) as f:
            d = json.load(f)
        return {"profiles": d.get("profiles", {}), "autostart": d.get("autostart")}
    except Exception:
        return {"profiles": {}, "autostart": None}


def _save_profiles(state):
    os.makedirs(os.path.dirname(PROFILES_PATH), exist_ok=True)
    with open(PROFILES_PATH, "w") as f:
        json.dump(state, f, indent=2)


# --- argv builder ---------------------------------------------------------
def _build_argv(cfg):
    a = [sys.executable, "-u", "-m", "aether_gate", "--adapter", cfg.get("adapter", "sim")]
    def add(flag, key):
        v = str(cfg.get(key, "")).strip()
        if v:
            a.extend([flag, v])
    ad = cfg.get("adapter")
    if ad == "sim":
        add("--pattern", "pattern")
    elif ad == "icom9700":
        add("--radio-ip", "radio_ip"); add("--user", "user"); add("--pass", "password")
        add("--radio-local-ip", "radio_local_ip"); add("--civ-addr", "civ_addr")
        add("--icom-model", "icom_model")
    elif ad == "icom7300":
        add("--usb-civ-port", "usb_civ_port"); add("--usb-civ-baud", "usb_civ_baud")
        add("--civ-addr", "civ_addr"); add("--usb-audio-device", "usb_audio_device")
    elif ad == "kenwood":
        add("--kw-model", "kw_model"); add("--rig-serial-port", "rig_serial_port")
        add("--rig-baud", "rig_baud"); add("--rigctld-host", "rigctld_host")
        add("--rigctld-port", "rigctld_port")
        add("--soapy-driver", "soapy_driver"); add("--gain", "gain"); add("--direct-samp", "direct_samp")
    elif ad == "yaesu":
        add("--yaesu-model", "yaesu_model"); add("--rig-serial-port", "rig_serial_port")
        add("--rig-baud", "rig_baud"); add("--rigctld-host", "rigctld_host")
        add("--rigctld-port", "rigctld_port")
        add("--soapy-driver", "soapy_driver"); add("--gain", "gain"); add("--direct-samp", "direct_samp")
    elif ad == "soapy":
        add("--soapy-driver", "soapy_driver"); add("--soapy-args", "soapy_args")
        add("--gain", "gain"); add("--direct-samp", "direct_samp"); add("--samp-rate", "samp_rate")
    add("--model", "model"); add("--serial", "serial"); add("--station", "station")
    add("--ip", "ip"); add("--ae", "ae"); add("--port", "port"); add("--ctl-port", "ctl_port")
    add("--fps", "fps"); add("--bins", "bins")
    return a


def _missing_fields(cfg):
    ad = cfg.get("adapter", "sim")
    req = {
        "icom9700": [("radio_ip", "Radio IP"), ("user", "Username"), ("password", "Password")],
        "icom7300": [("usb_civ_port", "USB CI-V serial port")],
        "kenwood": [("kw_model", "Radio model")],
        "yaesu": [("yaesu_model", "Radio model")],
    }.get(ad, [])
    miss = [lbl for k, lbl in req if not str(cfg.get(k, "")).strip()]
    if ad in ("kenwood", "yaesu") and not (str(cfg.get("rig_serial_port", "")).strip()
                                           or str(cfg.get("rigctld_host", "")).strip()):
        miss.append("Serial port (or a running rigctld host)")
    return miss


def _start(cfg):
    global _proc, _last_argv
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return 409, {"ok": False, "error": "already running - Stop first"}
        miss = _missing_fields(cfg)
        if miss:
            return 400, {"ok": False, "error": "Fill in: " + ", ".join(miss)}
        argv = _build_argv(cfg)
        try:
            _proc = subprocess.Popen(argv); _last_argv = argv
            return 200, {"ok": True, "pid": _proc.pid, "argv": argv}
        except Exception as e:
            return 500, {"ok": False, "error": str(e)}


def _status():
    with _lock:
        running = _proc is not None and _proc.poll() is None
        return {"running": running, "pid": (_proc.pid if running else None), "argv": _last_argv}


# --- "Known info" health checks ------------------------------------------
def _classify_ip(ip):
    try:
        b = [int(x) for x in ip.split(".")]
    except Exception:
        return "warn", "unrecognised address"
    if ip.startswith("127."):
        return "bad", "loopback - AE on other machines can't reach the gate here"
    if b[0] == 100 and 64 <= b[1] <= 127:
        return "warn", "CGNAT/Tailscale range - set --ip to a real LAN address so AE can reach it"
    if ip.startswith("10.") or ip.startswith("192.168.") or (b[0] == 172 and 16 <= b[1] <= 31):
        return "ok", "private LAN address"
    return "warn", "not a private LAN address - check AE can reach it"


def _probe_icom(ip, port=50001, timeout=1.2):
    """Unicast RS-BA1 are-you-there; True if the radio answers I-am-here."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("", 0)); lp = s.getsockname()[1]
        o = _local_ip().split(".")
        my_id = (int(o[2]) << 24) | (int(o[3]) << 16) | (lp & 0xFFFF)
        s.sendto(struct.pack("<IHHII", 0x10, 0x03, 0, my_id, 0), (ip, int(port)))
        s.settimeout(timeout)
        d = s.recvfrom(64)[0]; s.close()
        return len(d) >= 6 and struct.unpack("<H", d[4:6])[0] == 0x04
    except Exception:
        return False


def _list_serial_ports():
    ports = []
    try:
        if os.name == "nt":
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
            i = 0
            while True:
                try:
                    ports.append(winreg.EnumValue(k, i)[1]); i += 1
                except OSError:
                    break
        else:
            import glob
            ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    except Exception:
        pass
    return ports


def _known_checks():
    out = []
    def add(group, label, value, status, detail=""):
        out.append({"group": group, "label": label, "value": value, "status": status, "detail": detail})

    ip = _local_ip(); st, det = _classify_ip(ip)
    add("Gate host", "Advertise IP", ip, st, det)

    try:
        import numpy; add("Dependencies", "numpy", numpy.__version__, "ok")
    except Exception:
        add("Dependencies", "numpy", "MISSING", "bad", "required for the core FFT")
    try:
        import SoapySDR
        add("Dependencies", "SoapySDR", "installed", "ok")
        try:
            devs = SoapySDR.Device.enumerate()
            add("SDR devices", "dongles", f"{len(devs)} found",
                "ok" if devs else "warn",
                ", ".join(str(d.get("driver", "?")) for d in devs) if devs
                else "none plugged in - needed for dongle / Kenwood-Yaesu IF-tap spectrum")
        except Exception as e:
            add("SDR devices", "dongles", "enumerate failed", "warn", str(e)[:80])
    except Exception:
        add("Dependencies", "SoapySDR", "not installed", "warn",
            "needed for SDR dongles + Kenwood/Yaesu IF-tap spectrum")
    has_rig = bool(shutil.which("rigctld"))
    add("Dependencies", "hamlib (rigctld)", "found" if has_rig else "not found",
        "ok" if has_rig else "warn", "" if has_rig else "needed for Kenwood/Yaesu CAT control")

    sp = _list_serial_ports()
    add("Serial ports", "detected", ", ".join(sp) if sp else "none",
        "ok" if sp else "info", "" if sp else "no COM/ttyUSB ports (needed for CAT rigs)")

    for name, cfg in _load_profiles().get("profiles", {}).items():
        ad = cfg.get("adapter")
        if ad == "icom9700" and cfg.get("radio_ip"):
            ok = _probe_icom(cfg["radio_ip"], cfg.get("radio_port", 50001))
            add("Radios (saved profiles)", name, cfg["radio_ip"], "ok" if ok else "bad",
                "responds on Icom LAN :50001" if ok
                else "no reply - powered on? Network function enabled? correct IP?")
        elif ad == "icom7300" and cfg.get("usb_civ_port"):
            port = cfg["usb_civ_port"]
            present = port in sp or os.path.exists(port)
            add("Radios (saved profiles)", name, port, "ok" if present else "bad",
                "IC-7300 CI-V serial port present" if present else "serial port not found - plugged in?")
        elif ad in ("kenwood", "yaesu") and cfg.get("rig_serial_port"):
            port = cfg["rig_serial_port"]
            present = port in sp or os.path.exists(port)
            add("Radios (saved profiles)", name, port, "ok" if present else "bad",
                "serial port present" if present else "serial port not found - plugged in?")

    stt = _status()
    add("Gate", "process", f"running (pid {stt['pid']})" if stt["running"] else "stopped",
        "ok" if stt["running"] else "info")
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        p = self.path
        if p == "/" or p.startswith("/index"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif p.startswith("/api/adapters"):
            self._json(200, available())
        elif p.startswith("/api/radios"):
            self._json(200, {"icom": _icom_json(), "kenwood": _kenwood_json(),
                             "yaesu": _yaesu_json()})
        elif p.startswith("/api/profiles"):
            self._json(200, _load_profiles())
        elif p.startswith("/api/status"):
            self._json(200, _status())
        elif p.startswith("/api/known"):
            self._json(200, _known_checks())
        elif p.startswith("/known"):
            self._send(200, KNOWN_PAGE, "text/html; charset=utf-8")
        else:
            self._json(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n).decode()) if n else {}
        except Exception:
            body = {}
        p = self.path
        if p.startswith("/api/start"):
            code, resp = _start(body); self._json(code, resp)
        elif p.startswith("/api/stop"):
            global _proc
            with _lock:
                if _proc is not None and _proc.poll() is None:
                    _proc.terminate()
                    try:
                        _proc.wait(timeout=5)
                    except Exception:
                        _proc.kill()
            self._json(200, {"ok": True})
        elif p.startswith("/api/profiles/save"):
            name = str(body.get("name", "")).strip()
            if not name:
                self._json(400, {"ok": False, "error": "Profile needs a name"}); return
            st = _load_profiles()
            st["profiles"][name] = body.get("cfg", {})
            if body.get("autostart"):
                st["autostart"] = name
            elif st.get("autostart") == name:
                st["autostart"] = None
            _save_profiles(st); self._json(200, {"ok": True})
        elif p.startswith("/api/profiles/delete"):
            name = str(body.get("name", "")).strip()
            st = _load_profiles()
            st["profiles"].pop(name, None)
            if st.get("autostart") == name:
                st["autostart"] = None
            _save_profiles(st); self._json(200, {"ok": True})
        else:
            self._json(404, {})


PAGE = r"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Aether-gate - setup</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;max-width:660px;margin:0 auto;padding:20px}
 h1{color:#58a6ff;margin:0 0 2px} .sub{color:#8b949e;margin:0 0 14px;font-size:14px}
 label{display:block;margin:12px 0 4px;font-size:13px;color:#adbac7}
 input,select{width:100%;box-sizing:border-box;background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:8px}
 .row{display:flex;gap:12px}.row>div{flex:1}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;margin:12px 0}
 button{font-size:14px;font-weight:600;border:none;border-radius:6px;padding:9px 16px;cursor:pointer;color:#fff}
 #go{background:#238636}#stop{background:#da3633;margin-left:8px}
 .small{background:#30363d}
 .dot{display:inline-block;width:11px;height:11px;border-radius:50%;background:#6e7681;margin-right:8px}
 .hint{font-size:13px;color:#adbac7;line-height:1.5}
 .hintbox{background:#132030;border-left:3px solid #58a6ff;border-radius:6px;padding:11px 14px;margin:10px 0}
 .verify{color:#d29922;font-size:12px} .ok{color:#3fb950;font-size:12px} #st{font-weight:600}
 .adv summary{color:#58a6ff;cursor:pointer;margin-top:8px;font-size:13px}
 a{color:#58a6ff}
</style></head><body>
<h1>Aether-gate</h1><div class=sub>Radio setup &amp; launcher &mdash; present any radio to AetherSDR as a Flex &middot; <a href="/known" target=_blank>Known info / status &#8599;</a></div>

<div class=hintbox id=hint>
 <b>Getting started:</b>
 <span id=hinttext>① Pick your radio type below. ② Fill in the connection details for that radio.
 ③ Press <b>Start</b> &mdash; it appears in AetherSDR&apos;s radio chooser as &ldquo;Aether-gate&rdquo;.</span>
</div>

<div class=card>
  <span class=dot id=dot></span><span id=st>checking...</span>
  <div class=hint id=argv style="margin-top:4px;color:#6e7681;font-size:12px"></div>
</div>

<div class=card>
  <label>Saved profiles</label>
  <div class=row>
    <div><select id=profsel onchange=loadProfile()><option value="">&mdash; new / unsaved &mdash;</option></select></div>
    <div style="flex:0 0 auto"><button class=small onclick=delProfile()>Delete</button></div>
  </div>
  <div class=row style="margin-top:8px">
    <div><input id=profname placeholder="profile name (e.g. Shack IC-9700)"></div>
    <div style="flex:0 0 auto"><button class=small onclick=saveProfile()>Save profile</button></div>
  </div>
  <label style="margin-top:8px"><input type=checkbox id=autostart style="width:auto"> Connect this profile automatically on launch</label>
</div>

<label>Radio type</label>
<select id=adapter onchange=onAdapter()></select>

<!-- ICOM (LAN) -->
<div id=box_icom class=fambox style=display:none>
  <div class=card>
    <label>Icom model</label>
    <select id=icom_model onchange="onRadio('icom')"></select>
    <div class=hint id=icom_hint style="margin-top:4px"></div>
    <div class=row>
      <div><label>Radio IP</label><input id=radio_ip placeholder=10.0.0.7></div>
      <div><label>CI-V address</label><input id=civ_addr placeholder=0xA2></div>
    </div>
    <div class=row>
      <div><label>Network username</label><input id=user placeholder=nigel></div>
      <div><label>Network password</label><input id=password type=password></div>
    </div>
    <details class=adv><summary>Advanced</summary>
      <label>Local IP that reaches the radio (blank = auto)</label>
      <input id=radio_local_ip placeholder="auto (e.g. 10.0.0.103)">
    </details>
  </div>
</div>

<!-- IC-7300 (USB CI-V + USB audio) -->
<div id=box_icom7300 class=fambox style=display:none>
  <div class=card>
    <div class=row>
      <div><label>USB CI-V serial port</label><input id=usb_civ_port placeholder="/dev/ttyUSB0"></div>
      <div><label>Baud</label><input id=usb_civ_baud value=115200></div>
    </div>
    <div class=row>
      <div><label>CI-V address</label><input id=civ_addr_7300 value=0x94></div>
      <div><label>USB audio device</label><input id=usb_audio_device placeholder="auto (USB Audio CODEC)"></div>
    </div>
    <div class=hint style="margin-top:4px;color:#6e7681">TX/PTT stays disabled. RTS and DTR are held low when opening and closing the serial port.</div>
  </div>
</div>

<!-- KENWOOD (CAT via hamlib + IF-tap dongle) -->
<div id=box_kenwood class=fambox style=display:none>
  <div class=card>
    <label>Kenwood model</label>
    <select id=kw_model onchange="onRadio('kenwood')"></select>
    <div class=hint id=kw_hint style="margin-top:4px"></div>
    <div class=row>
      <div><label>Serial port (COM / /dev/ttyUSB)</label><input id=rig_serial_port placeholder="COM10 or /dev/ttyUSB0"></div>
      <div><label>Baud</label><input id=rig_baud value=4800></div>
    </div>
    <div class=hint style="margin-top:4px;color:#6e7681">RTS+DTR are asserted and hardware handshake disabled automatically (the TS-450 needs this). Leave the serial port blank only if you point at an already-running rigctld below.</div>
    <label style="margin-top:10px">Spectrum dongle (Kenwood has no scope over CAT &mdash; an SDR gives the waterfall)</label>
    <div class=row>
      <div><label style="margin-top:0">SoapySDR driver</label><input id=soapy_driver value=rtlsdr></div>
      <div><label style="margin-top:0">Gain (dB)</label><input id=gain value=40></div>
    </div>
    <details class=adv><summary>Advanced (remote rigctld / HF dongle)</summary>
      <div class=row>
        <div><label>rigctld host (if already running elsewhere)</label><input id=rigctld_host placeholder=127.0.0.1></div>
        <div><label>rigctld port</label><input id=rigctld_port placeholder=4532></div>
      </div>
      <label>RTL direct-sampling (Q=2 for HF on non-V4 dongles)</label>
      <input id=direct_samp placeholder="(blank for V4 / VHF)">
    </details>
  </div>
</div>

<!-- YAESU (CAT via hamlib + IF-tap dongle) -->
<div id=box_yaesu class=fambox style=display:none>
  <div class=card>
    <label>Yaesu model</label>
    <select id=yaesu_model onchange="onRadio('yaesu')"></select>
    <div class=hint id=yaesu_hint style="margin-top:4px"></div>
    <div class=row>
      <div><label>Serial port (COM / /dev/ttyUSB)</label><input id=rig_serial_port2 placeholder="COM10 or /dev/ttyUSB0"></div>
      <div><label>Baud</label><input id=rig_baud2 value=4800></div>
    </div>
    <div class=hint style="margin-top:4px;color:#6e7681">Yaesu CAT: older rigs (FT-847/817/857/897) are typically 4800 8N2; newer USB rigs (FT-991A) 38400. hamlib uses the model’s own serial defaults. Leave the serial port blank only if you point at an already-running rigctld below.</div>
    <label style="margin-top:10px">Spectrum dongle (Yaesu has no scope over CAT &mdash; an SDR gives the waterfall)</label>
    <div class=row>
      <div><label style="margin-top:0">SoapySDR driver</label><input id=soapy_driver2 value=rtlsdr></div>
      <div><label style="margin-top:0">Gain (dB)</label><input id=gain2 value=40></div>
    </div>
    <details class=adv><summary>Advanced (remote rigctld / HF dongle)</summary>
      <div class=row>
        <div><label>rigctld host (if already running elsewhere)</label><input id=rigctld_host2 placeholder=127.0.0.1></div>
        <div><label>rigctld port</label><input id=rigctld_port2 placeholder=4532></div>
      </div>
      <label>RTL direct-sampling (Q=2 for HF on non-V4 dongles)</label>
      <input id=direct_samp2 placeholder="(blank for V4 / VHF)">
    </details>
  </div>
</div>

<!-- DONGLE (SoapySDR) -->
<div id=box_soapy class=fambox style=display:none>
  <div class=card>
    <div class=row>
      <div><label>SoapySDR driver</label><input id=s_driver value=rtlsdr></div>
      <div><label>Gain (dB)</label><input id=s_gain value=40></div>
    </div>
    <div class=row>
      <div><label>Sample rate (Hz)</label><input id=samp_rate value=2040000></div>
      <div><label>Device args</label><input id=soapy_args placeholder="serial=00000001"></div>
    </div>
  </div>
</div>

<!-- SIM -->
<div id=box_sim class=fambox style=display:none>
  <div class=card>
    <label>Test pattern</label>
    <select id=pattern><option>test_card</option><option>carrier</option><option>two_tone</option><option>ssb</option><option>cw</option><option>noise</option></select>
  </div>
</div>

<div class=card>
  <div class=row>
    <div><label>Advertise as (Flex model)</label><input id=model placeholder=FLEX-6700></div>
    <div><label>Station name (AE label)</label><input id=station placeholder=Aether-gate></div>
  </div>
  <label>Serial (unique per gate)</label><input id=serial placeholder=GATE9700>
</div>

<details class=adv><summary>Network / advanced</summary>
  <div class=card>
    <div class=row>
      <div><label>Advertise our IP</label><input id=ip placeholder=auto></div>
      <div><label>AE IP (unicast discovery)</label><input id=ae placeholder="AE's IP (optional)"></div>
    </div>
    <div class=row>
      <div><label>Port</label><input id=port value=4992></div>
      <div><label>Signal-panel port</label><input id=ctl_port value=8731></div>
    </div>
    <div class=row>
      <div><label>FPS</label><input id=fps placeholder=25></div>
      <div><label>Bins</label><input id=bins placeholder=auto></div>
    </div>
  </div>
</details>

<div style="margin:16px 0">
  <button id=go onclick=start()>&#9654; Start</button>
  <button id=stop onclick=stop()>&#9632; Stop</button>
  <span id=msg style="margin-left:12px;font-size:13px"></span>
</div>
<div class=hint style="color:#6e7681;font-size:12px">Signal panel (once started): <a id=panellink target=_blank>open</a></div>

<script>
let RADIOS={icom:{},kenwood:{},yaesu:{}};
const FIELDS=['adapter','pattern','radio_ip','civ_addr','user','password','radio_local_ip',
 'usb_civ_port','usb_civ_baud','civ_addr_7300','usb_audio_device',
 'kw_model','rig_serial_port','rig_baud','rigctld_host','rigctld_port','soapy_driver','gain','direct_samp',
 'yaesu_model','rig_serial_port2','rig_baud2','rigctld_host2','rigctld_port2','soapy_driver2','gain2','direct_samp2',
 's_driver','s_gain','samp_rate','soapy_args','icom_model','model','station','serial','ip','ae','port','ctl_port','fps','bins'];
const HINTS={
 icom:'① Icom LAN rig (IC-9700 etc.): enter the radio’s IP + the Network username/password you set in its menu. CI-V address auto-fills. ② Start.',
 icom7300:'① IC-7300 USB: set the CI-V serial port. Audio comes from the radio’s USB Audio CODEC. ② Start.',
 kenwood:'① Kenwood CAT rig: pick the model, set the serial COM port + baud (TS-450 = 4800). A SoapySDR dongle gives the waterfall. ② Start.',
 yaesu:'① Yaesu CAT rig: pick the model, set the serial COM port + baud (FT-847 = 4800; FT-991A = 38400). A SoapySDR dongle gives the waterfall. ② Start.',
 soapy:'① An SDR dongle (RTL-SDR/Airspy/SDRplay): pick the driver + gain. ② Start.',
 sim:'Test source — no radio needed. Pick a pattern and Start to check AE sees the gate.'};

async function init(){
 const ads=await (await fetch('/api/adapters')).json();
 const nice={icom9700:'Icom (LAN)',icom7300:'IC-7300 (USB)',kenwood:'Kenwood (CAT)',yaesu:'Yaesu (CAT)',soapy:'SDR dongle',sim:'Test / sim'};
 const as=document.getElementById('adapter'); as.innerHTML='';
 ['icom7300','icom9700','kenwood','yaesu','soapy','sim'].filter(a=>ads.includes(a)).forEach(a=>{
   const o=document.createElement('option');o.value=a;o.textContent=nice[a]||a;as.appendChild(o);});
 RADIOS=await (await fetch('/api/radios')).json();
 fill('icom_model',Object.keys(RADIOS.icom)); fill('kw_model',Object.keys(RADIOS.kenwood));
 fill('yaesu_model',Object.keys(RADIOS.yaesu));
 await loadProfiles(); onAdapter(); onRadio('icom'); onRadio('kenwood'); onRadio('yaesu'); poll(); setInterval(poll,2000);
}
function fill(id,keys){const s=document.getElementById(id);s.innerHTML='';keys.forEach(k=>{const o=document.createElement('option');o.value=o.textContent=k;s.appendChild(o);});}
function fam(){const a=document.getElementById('adapter').value;return a==='icom9700'?'icom':a==='icom7300'?'icom7300':a==='kenwood'?'kenwood':a==='yaesu'?'yaesu':a==='soapy'?'soapy':'sim';}
function onAdapter(){
 const f=fam();
 ['icom','icom7300','kenwood','yaesu','soapy','sim'].forEach(x=>document.getElementById('box_'+x).style.display=(x===f)?'block':'none');
 document.getElementById('hinttext').innerHTML=HINTS[f];
 if(f==='sim'){document.getElementById('model').placeholder='FLEX-6600';}
 if(f==='icom7300'){document.getElementById('model').value='FLEX-6600';
   document.getElementById('station').value='Icom-IC-7300';document.getElementById('serial').value='GATE7300';}
}
function onRadio(fam){
 if(fam==='icom'){const m=document.getElementById('icom_model').value,r=RADIOS.icom[m];if(!r)return;
   document.getElementById('civ_addr').value=r.civ_addr;document.getElementById('model').value=r.advertise;
   document.getElementById('station').value='aether-gate '+m.replace('IC-','').toLowerCase();
   document.getElementById('serial').value='GATE'+m.replace('IC-','');
   document.getElementById('icom_hint').innerHTML='bands '+r.bands.join(', ')+badge(r.verified);}
 if(fam==='kenwood'){const m=document.getElementById('kw_model').value,r=RADIOS.kenwood[m];if(!r)return;
   document.getElementById('model').value=r.advertise;
   document.getElementById('station').value='aether-gate '+m.toLowerCase();
   document.getElementById('serial').value='GATE'+m.replace('TS-','TS');
   document.getElementById('kw_hint').innerHTML='hamlib -m '+r.hamlib_model+' &middot; bands '+r.bands.join(', ')
     +' &middot; spectrum: '+r.spectrum+(r.hf_dongle_needed?' (needs HF-capable dongle e.g. RTL-SDR V4)':'')+badge(r.verified);}
 if(fam==='yaesu'){const m=document.getElementById('yaesu_model').value,r=RADIOS.yaesu[m];if(!r)return;
   document.getElementById('model').value=r.advertise;
   document.getElementById('station').value='aether-gate '+m.toLowerCase();
   document.getElementById('serial').value='GATE'+m.replace('-','');
   document.getElementById('yaesu_hint').innerHTML='hamlib -m '+r.hamlib_model+' &middot; bands '+r.bands.join(', ')
     +' &middot; spectrum: '+r.spectrum+(r.hf_dongle_needed?' (needs HF-capable dongle e.g. RTL-SDR V4)':'')+badge(r.verified);}
}
function badge(v){return v?' <span class=ok>&#10004; verified</span>':' <span class=verify>&#9888; VERIFY</span>';}
function cfg(){
 const c={}; FIELDS.forEach(i=>{const el=document.getElementById(i);if(el)c[i]=el.value;});
 c.adapter=document.getElementById('adapter').value;
 // dongle box uses s_driver/s_gain -> map to soapy_* for the argv
 if(fam()==='soapy'){c.soapy_driver=c.s_driver;c.gain=c.s_gain;}
 if(fam()==='icom7300'){c.civ_addr=c.civ_addr_7300||'0x94';}
 // yaesu box uses …2-suffixed ids (to avoid dup ids with the kenwood box)
 // -> map onto the canonical keys the argv builder reads.
 if(fam()==='yaesu'){c.rig_serial_port=c.rig_serial_port2;c.rig_baud=c.rig_baud2;
   c.rigctld_host=c.rigctld_host2;c.rigctld_port=c.rigctld_port2;
   c.soapy_driver=c.soapy_driver2;c.gain=c.gain2;c.direct_samp=c.direct_samp2;}
 return c;
}
function setCfg(c){FIELDS.forEach(i=>{const el=document.getElementById(i);if(el&&c[i]!==undefined)el.value=c[i];});
 if(c.adapter){document.getElementById('adapter').value=c.adapter;}
 // yaesu profiles store canonical keys; mirror them back into the …2 DOM fields.
 if(c.adapter==='yaesu'){const set=(id,v)=>{const e=document.getElementById(id);if(e&&v!==undefined)e.value=v;};
   set('rig_serial_port2',c.rig_serial_port);set('rig_baud2',c.rig_baud);
   set('rigctld_host2',c.rigctld_host);set('rigctld_port2',c.rigctld_port);
   set('soapy_driver2',c.soapy_driver);set('gain2',c.gain);set('direct_samp2',c.direct_samp);}
 if(c.adapter==='icom7300'&&c.civ_addr){const e=document.getElementById('civ_addr_7300');if(e)e.value=c.civ_addr;}
 onAdapter();}
async function start(){msg('...');
 const r=await (await fetch('/api/start',{method:'POST',body:JSON.stringify(cfg())})).json();
 msg(r.ok?'started':('⚠ '+(r.error||'failed')),r.ok);poll();}
async function stop(){await fetch('/api/stop',{method:'POST',body:'{}'});msg('stopped');poll();}
function msg(t,ok){const m=document.getElementById('msg');m.textContent=t;m.style.color=ok?'#3fb950':'#d29922';}
// --- profiles ---
let PROFILES={};
async function loadProfiles(){const st=await (await fetch('/api/profiles')).json();PROFILES=st.profiles||{};
 const s=document.getElementById('profsel');s.innerHTML='<option value="">— new / unsaved —</option>';
 Object.keys(PROFILES).forEach(n=>{const o=document.createElement('option');o.value=o.textContent=n;
   if(n===st.autostart)o.textContent=n+'  (auto)';s.appendChild(o);});}
function loadProfile(){const n=document.getElementById('profsel').value;if(!n||!PROFILES[n])return;
 setCfg(PROFILES[n]);document.getElementById('profname').value=n;
 setTimeout(()=>{onRadio('icom');onRadio('kenwood');onRadio('yaesu');},0);msg('loaded "'+n+'"',true);}
async function saveProfile(){const name=document.getElementById('profname').value.trim();
 if(!name){msg('⚠ give the profile a name');return;}
 const r=await (await fetch('/api/profiles/save',{method:'POST',body:JSON.stringify(
   {name,cfg:cfg(),autostart:document.getElementById('autostart').checked})})).json();
 if(r.ok){msg('saved "'+name+'"',true);await loadProfiles();document.getElementById('profsel').value=name;}else msg('⚠ '+r.error);}
async function delProfile(){const n=document.getElementById('profsel').value;if(!n)return;
 await fetch('/api/profiles/delete',{method:'POST',body:JSON.stringify({name:n})});await loadProfiles();msg('deleted "'+n+'"');}
async function poll(){const s=await (await fetch('/api/status')).json();
 document.getElementById('dot').style.background=s.running?'#3fb950':'#6e7681';
 document.getElementById('st').textContent=s.running?('RUNNING (pid '+s.pid+')'):'stopped';
 document.getElementById('argv').textContent=(s.argv&&s.argv.length)?s.argv.slice(3).join(' '):'';
 const cp=document.getElementById('ctl_port').value||'8731';
 document.getElementById('panellink').href='http://'+location.hostname+':'+cp+'/';}
init();
</script></body></html>"""


KNOWN_PAGE = r"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Aether-gate - known info</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;max-width:660px;margin:0 auto;padding:20px}
 h1{color:#58a6ff;margin:0 0 2px} .sub{color:#8b949e;margin:0 0 14px;font-size:13px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;margin:12px 0}
 .gh{color:#58a6ff;font-weight:600;font-size:13px;margin-bottom:6px}
 .row{padding:5px 0;border-top:1px solid #21262d;display:flex;align-items:center;flex-wrap:wrap}
 .row:first-of-type{border-top:none}
 .dot{width:11px;height:11px;border-radius:50%;display:inline-block;margin-right:9px;flex:0 0 auto}
 .lbl{flex:0 0 46%;font-size:14px} .val{color:#adbac7;font-size:14px}
 .det{flex-basis:100%;color:#6e7681;font-size:12px;margin:2px 0 0 20px}
 a{color:#58a6ff}
</style></head><body>
<h1>Aether-gate &mdash; known info</h1>
<div class=sub>Turn it on and check: <b style=color:#3fb950>green</b> = good &middot;
 <b style=color:#d29922>amber</b> = check &middot; <b style=color:#da3633>red</b> = problem &middot;
 <b style=color:#6e7681>grey</b> = info. Auto-refreshes every 3 s.</div>
<div id=out>checking&hellip;</div>
<div style=margin-top:14px><a href="/">&larr; back to setup</a></div>
<script>
const COL={ok:'#3fb950',warn:'#d29922',bad:'#da3633',info:'#6e7681'};
function esc(s){return String(s).replace(/</g,'&lt;');}
async function poll(){
 let rows; try{ rows=await (await fetch('/api/known')).json(); }catch(e){ return; }
 const groups={}; rows.forEach(r=>{(groups[r.group]=groups[r.group]||[]).push(r);});
 let h='';
 for(const g in groups){
  h+='<div class=card><div class=gh>'+esc(g)+'</div>';
  groups[g].forEach(r=>{
   h+='<div class=row><span class=dot style="background:'+(COL[r.status]||'#6e7681')+'"></span>'
     +'<span class=lbl>'+esc(r.label)+'</span><span class=val>'+esc(r.value)+'</span>'
     +(r.detail?'<div class=det>'+esc(r.detail)+'</div>':'')+'</div>';
  });
  h+='</div>';
 }
 document.getElementById('out').innerHTML=h||'<div class=card>no checks</div>';
}
poll(); setInterval(poll,3000);
</script></body></html>"""


def main(argv=None):
    ip = _local_ip()
    # connect-on-launch: if a saved profile is flagged autostart, start it now
    st = _load_profiles()
    auto = st.get("autostart")
    if auto and auto in st.get("profiles", {}):
        code, resp = _start(st["profiles"][auto])
        print(f"autostart profile '{auto}': {resp}")

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", SETUP_PORT), Handler)
    url = f"http://127.0.0.1:{SETUP_PORT}/"
    print(f"Aether-gate setup UI -> http://{ip}:{SETUP_PORT}/  (and {url})")
    if "--no-browser" not in (argv or sys.argv[1:]):
        try:
            import webbrowser
            threading.Timer(0.7, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        with _lock:
            if _proc is not None and _proc.poll() is None:
                _proc.terminate()
        print("\nbye")


if __name__ == "__main__":
    main()
