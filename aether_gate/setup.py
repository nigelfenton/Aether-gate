#
# Aether-gate - setup / launcher web UI.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Web launcher that replaces the CLI flags: a first-page hint, per-radio-family
field groups (Icom / Kenwood / dongle / sim), saved profiles with connect-on-launch,
and Start/Stop that spawns `python -m aether_gate ...` (reusing all the CLI).

    python -m aether_gate.setup        # or bare `python -m aether_gate`
"""
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import secrets
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
AUTH_PATH = os.path.join(os.path.expanduser("~"), ".aether-gate", "setup-auth.json")

# --- who may use this page ------------------------------------------------
# The Setup UI listens on the LAN (an appliance is configured from another
# machine) and it holds radio logins, so it is gated three ways:
#   * a setup PIN, created on first visit (router-style first run), checked
#     with PBKDF2 and held as an HttpOnly/SameSite=Strict session cookie;
#   * a Host check: only IP literals and this machine's own names are served,
#     which is what defeats DNS rebinding (a rebinding page arrives with the
#     attacker's hostname in Host);
#   * POSTs must be JSON and, when a browser sends Origin, same-origin. JSON
#     makes a cross-site POST a preflighted request, and we never answer
#     preflights, so another web page cannot drive Start/Stop/Save.
# AETHER_GATE_SETUP_OPEN=1 drops the PIN (a bench box on an isolated LAN);
# the Host/Origin checks stay. AETHER_GATE_SETUP_HOSTS adds extra hostnames.
# Forgot the PIN? Delete ~/.aether-gate/setup-auth.json and reload the page.
SESSION_TTL_S = 12 * 3600
PIN_MIN_LEN = 4
_PBKDF2_ITERS = 200_000
_sessions = {}                     # token -> expiry (process memory; restart = log in again)
_fails = {"n": 0, "until": 0.0}    # crude brute-force brake on /api/auth/login

_proc = None
_lock = threading.Lock()
_last_argv = []

# Profile fields that are secrets: accepted, stored (0600), handed to the gate
# through its environment -- and never returned by any GET or put on a command
# line, where `ps` and /proc/<pid>/cmdline show them to every local user.
SECRET_FIELDS = ("password",)


def _truthy(v):
    return str(v or "").strip().lower() not in ("", "0", "false", "no", "off")


def _setup_open():
    return _truthy(os.environ.get("AETHER_GATE_SETUP_OPEN"))


def _private_write(path, text):
    """Write a file only its owner can read (0600, in a 0700 directory)."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)       # an existing looser file keeps its mode across replace on some OSes
    except OSError:
        pass


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
    _private_write(PROFILES_PATH, json.dumps(state, indent=2))


def _public_profiles(state=None):
    """Profiles as the browser may see them: secrets blanked, flagged as saved."""
    st = _load_profiles() if state is None else state
    out = {}
    for name, cfg in st.get("profiles", {}).items():
        c = dict(cfg)
        for k in SECRET_FIELDS:
            if c.get(k):
                c[k] = ""
                c[k + "_saved"] = True
        out[name] = c
    return {"profiles": out, "autostart": st.get("autostart")}


def _clean_cfg(cfg):
    """Drop the UI-only keys before a cfg is stored or started."""
    c = dict(cfg or {})
    c.pop("profile", None)
    for k in SECRET_FIELDS:
        c.pop(k + "_saved", None)
    return c


def _with_saved_secrets(cfg, profile):
    """A blank secret means "keep the saved one": the page can no longer read
    it back, so it sends nothing and the server fills it in."""
    c = _clean_cfg(cfg)
    saved = _load_profiles().get("profiles", {}).get(profile or "", {})
    for k in SECRET_FIELDS:
        if not str(c.get(k, "")).strip() and saved.get(k):
            c[k] = saved[k]
    return c


def _redact_argv(argv):
    out, hide = [], False
    for a in argv:
        out.append("***" if hide else a)
        hide = a in ("--pass",)
    return out


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
        # NOT --pass: the password goes to the gate as AETHER_GATE_PW (see
        # _child_env). On a command line every local user can read it.
        add("--radio-ip", "radio_ip"); add("--user", "user")
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


def _child_env(cfg):
    """The gate's environment: ours, plus the radio password as AETHER_GATE_PW.

    A process's environment is readable only by its own user and root, unlike
    its command line, and __main__.apply_env_defaults() already maps
    AETHER_GATE_PW onto --pass (dest="pw")."""
    env = dict(os.environ)
    env.pop("AETHER_GATE_PW", None)            # never hand on the launcher's own
    pw = str(cfg.get("password", "")).strip()
    if pw:
        env["AETHER_GATE_PW"] = pw
    return env


def _start(cfg):
    global _proc, _last_argv
    cfg = _with_saved_secrets(cfg, (cfg or {}).get("profile"))
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return 409, {"ok": False, "error": "already running - Stop first"}
        miss = _missing_fields(cfg)
        if miss:
            return 400, {"ok": False, "error": "Fill in: " + ", ".join(miss)}
        argv = _build_argv(cfg)
        try:
            _proc = subprocess.Popen(argv, env=_child_env(cfg)); _last_argv = argv
            return 200, {"ok": True, "pid": _proc.pid, "argv": _redact_argv(argv)}
        except Exception as e:
            return 500, {"ok": False, "error": str(e)}


def _status():
    with _lock:
        running = _proc is not None and _proc.poll() is None
        return {"running": running, "pid": (_proc.pid if running else None),
                "argv": _redact_argv(_last_argv)}


# --- setup PIN + sessions -------------------------------------------------
def _auth_record():
    try:
        with open(AUTH_PATH) as f:
            d = json.load(f)
        return d if d.get("hash") and d.get("salt") else None
    except Exception:
        return None


def _pin_hash(pin, salt, iters=_PBKDF2_ITERS):
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), iters).hex()


def _set_pin(pin):
    salt = secrets.token_hex(16)
    _private_write(AUTH_PATH, json.dumps({"salt": salt, "iter": _PBKDF2_ITERS,
                                          "hash": _pin_hash(pin, salt)}))


def _pin_ok(pin):
    rec = _auth_record()
    if rec is None:
        return False
    got = _pin_hash(str(pin), rec["salt"], int(rec.get("iter", _PBKDF2_ITERS)))
    return hmac.compare_digest(got, rec["hash"])


def _new_session():
    tok = secrets.token_urlsafe(32)
    now = time.time()
    for t, exp in list(_sessions.items()):
        if exp < now:
            _sessions.pop(t, None)
    _sessions[tok] = now + SESSION_TTL_S
    return tok


def _allowed_hosts():
    names = {"localhost", "aethergate.local"}
    for n in (socket.gethostname(), socket.getfqdn()):
        if n:
            names.add(n.lower())
            names.add(n.lower().split(".")[0] + ".local")
    extra = os.environ.get("AETHER_GATE_SETUP_HOSTS", "")
    names.update(h.strip().lower() for h in extra.split(",") if h.strip())
    return names


def _host_name(hostport):
    """'10.0.0.5:8730' -> '10.0.0.5'; '[::1]:8730' -> '::1'; 'Pi.local' -> 'pi.local'."""
    h = (hostport or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _host_allowed(hostport):
    name = _host_name(hostport)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True                 # an IP literal cannot be a rebinding hostname
    except ValueError:
        return name in _allowed_hosts()


# --- one-click update ----------------------------------------------------
#
# The operator this is for is comfortable with radios, not terminals, so both
# endpoints answer in whole sentences and neither can leave a half-installed
# tree. See updater.py for the swap/rollback design.
_update_lock = threading.Lock()
_update_busy = False


def _installed_version():
    """The version ON DISK, not the one this process imported at startup.

    ⚠ After an update the swapped-in tree has a NEW __init__.py, but this
    long-running web UI still holds the OLD __version__ in memory. Reporting
    that made the banner keep offering an update the operator had just
    installed — harmless (installing twice is idempotent) but baffling for
    exactly the person this feature exists for. Observed on the Pi 4: disk said
    0.4.0, the page said 0.3.0.

    Falls back to the imported value if the file cannot be read, so a permissions
    problem degrades to "slightly stale" rather than "no version at all".
    """
    from . import __version__
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("__version__"):
                    return line.split("=", 1)[1].split("#")[0].strip().strip('"').strip("'")
    except Exception:
        pass
    return __version__


def _update_status():
    from . import updater
    __version__ = _installed_version()
    try:
        st = updater.status(__version__)
    except Exception as e:                    # never let a check break the page
        return {"current": __version__, "latest": None, "available": False,
                "checked": False, "message": f"Could not check for updates: {e}"}
    st["busy"] = _update_busy
    return st


def _gate_running():
    """Is ANY gate running — ours, or one started by systemd?

    ⚠ Checking only `_proc` (the child this UI started) is not enough, and that
    mistake shipped once: on the appliance the gate normally runs as a SYSTEMD
    UNIT (aether-gate-9700.service and friends), so `_proc` is None and the
    guard never fires. Caught on a Pi 4 — the update installed underneath a
    live, streaming gate.

    Returns (running, how) so the message can tell the operator WHICH thing to
    stop; "press Stop" is useless advice when the gate is a service they never
    started from this page.
    """
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return True, "ui"
    # Any `python -m aether_gate` that is not this setup UI. pgrep is on every
    # Debian/Pi OS image; if it is missing we fail OPEN rather than block
    # updates forever, since the swap itself is still safe and reversible.
    try:
        out = subprocess.run(["pgrep", "-af", "aether_gate"],
                             capture_output=True, timeout=5).stdout.decode(errors="replace")
    except Exception:
        return False, None

    me = os.getpid()
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == me:
            continue                      # this process
        cmd = parts[1]

        # ⚠ MATCH THE INVOCATION, NOT THE STRING. `pgrep -af aether_gate` also
        # returns anything that merely MENTIONS the name — a shell running
        # `pgrep -af aether_gate`, an editor, a log tail, an ssh command line.
        # A substring test made the guard fire with no gate running at all
        # (seen on the Pi 4: the matching line was the diagnostic command
        # itself). Require an actual `-m aether_gate` module launch.
        if "-m aether_gate" not in cmd:
            continue
        if "-m aether_gate.setup" in cmd:
            continue                      # that's this web UI
        # `-m aether_gate.something_else` is not the gate either; the gate is
        # launched as the bare package.
        tail = cmd.split("-m aether_gate", 1)
        after = tail[1][:1] if len(tail) > 1 else ""
        if after and after != " ":
            continue
        return True, "service"
    return False, None


def _update_install(body):
    """Install the newest release. REFUSES while a gate is running.

    Stopping first is the operator's decision, not ours: the gate may be mid-QSO
    or feeding a decoder, and swapping the code under a live radio session is
    exactly the surprise this whole feature exists to avoid.
    """
    global _update_busy
    from . import updater

    running, how = _gate_running()
    if running:
        return 409, {"ok": False,
                     "message": ("The gate is running. Press Stop first, then update."
                                 if how == "ui" else
                                 "A gate is running as a system service. Stop it before "
                                 "updating (sudo systemctl stop aether-gate-*), then try again.")}

    with _update_lock:
        if _update_busy:
            return 409, {"ok": False, "message": "An update is already in progress."}
        _update_busy = True
    try:
        live = os.path.dirname(os.path.abspath(__file__))     # .../gate/aether_gate
        res = updater.install(body.get("tag"), live, logfn=lambda m: print(m, flush=True))
        return (200 if res.get("ok") else 500), res
    finally:
        _update_busy = False


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
            # entries are SoapySDRKwargs (a swig map, no .get) — copy to dicts first
            devd = [dict(d) for d in devs]
            add("SDR devices", "dongles", f"{len(devd)} found",
                "ok" if devd else "warn",
                ", ".join(str(d.get("driver", "?")) for d in devd) if devd
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

    def _json(self, code, obj, cookie=None):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def end_headers(self):
        self.send_header("X-Frame-Options", "DENY")          # no clickjacking the Start button
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    # --- gatekeeping -------------------------------------------------------
    def _session_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ag_session":
                return v
        return ""

    def _authed(self):
        if _setup_open():
            return True
        exp = _sessions.get(self._session_token())
        return bool(exp and exp > time.time())

    def _host_ok(self):
        if _host_allowed(self.headers.get("Host")):
            return True
        self._json(403, {"ok": False, "error": "unrecognised Host - open this page by the "
                         "gate's IP address or its own name (see AETHER_GATE_SETUP_HOSTS)"})
        return False

    def _post_ok(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json(415, {"ok": False, "error": "POST bodies must be application/json"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            ohost = origin.split("://", 1)[-1].rstrip("/")
            if ohost.lower() != (self.headers.get("Host") or "").lower():
                self._json(403, {"ok": False, "error": "cross-origin request refused"})
                return False
        return True

    def _auth_state(self):
        self._json(200, {"pin_set": _auth_record() is not None, "authed": self._authed(),
                         "open": _setup_open()})

    def _auth_post(self, p, body):
        def cookie(tok):
            return (f"ag_session={tok}; HttpOnly; SameSite=Strict; Path=/; "
                    f"Max-Age={SESSION_TTL_S}")
        if p.startswith("/api/auth/logout"):
            _sessions.pop(self._session_token(), None)
            self._json(200, {"ok": True},
                       cookie="ag_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
            return
        pin = str(body.get("pin", ""))
        if p.startswith("/api/auth/setup"):
            if _auth_record() is not None:
                self._json(409, {"ok": False, "error": "a setup PIN is already set - log in"})
                return
            if len(pin) < PIN_MIN_LEN:
                self._json(400, {"ok": False,
                                 "error": f"PIN needs at least {PIN_MIN_LEN} characters"})
                return
            _set_pin(pin)
            self._json(200, {"ok": True}, cookie=cookie(_new_session()))
            return
        if p.startswith("/api/auth/login"):
            if time.time() < _fails["until"]:
                self._json(429, {"ok": False, "error": "too many wrong PINs - wait a minute"})
                return
            if _pin_ok(pin):
                _fails["n"] = 0
                self._json(200, {"ok": True}, cookie=cookie(_new_session()))
                return
            _fails["n"] += 1
            if _fails["n"] >= 10:
                _fails["n"], _fails["until"] = 0, time.time() + 60
            time.sleep(1.0)                                   # slows guessing to ~1/s
            self._json(401, {"ok": False, "error": "wrong PIN"})
            return
        self._json(404, {})

    def do_GET(self):
        if not self._host_ok():
            return
        p = self.path
        if p.startswith("/api/auth/state"):
            self._auth_state()
            return
        if not self._authed():
            if p.startswith("/api/"):
                self._json(401, {"ok": False, "error": "log in first"})
            else:
                self._send(200, AUTH_PAGE, "text/html; charset=utf-8")
            return
        if p == "/" or p.startswith("/index"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif p.startswith("/api/adapters"):
            self._json(200, available())
        elif p.startswith("/api/radios"):
            self._json(200, {"icom": _icom_json(), "kenwood": _kenwood_json(),
                             "yaesu": _yaesu_json()})
        elif p.startswith("/api/profiles"):
            self._json(200, _public_profiles())
        elif p.startswith("/api/status"):
            self._json(200, _status())
        elif p.startswith("/api/known"):
            self._json(200, _known_checks())
        elif p.startswith("/api/update"):
            self._json(200, _update_status())
        elif p.startswith("/known"):
            self._send(200, KNOWN_PAGE, "text/html; charset=utf-8")
        else:
            self._json(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        if not (self._host_ok() and self._post_ok()):
            return
        try:
            body = json.loads(raw.decode()) if raw else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        p = self.path
        if p.startswith("/api/auth/"):
            self._auth_post(p, body)
            return
        if not self._authed():
            self._json(401, {"ok": False, "error": "log in first"})
            return
        if p.startswith("/api/update/install"):
            code, resp = _update_install(body); self._json(code, resp)
        elif p.startswith("/api/start"):
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
            cfg = _clean_cfg(body.get("cfg", {}))
            old = st["profiles"].get(name, {})
            for k in SECRET_FIELDS:            # blank = keep the saved secret
                if not str(cfg.get(k, "")).strip() and old.get(k):
                    cfg[k] = old[k]
            st["profiles"][name] = cfg
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
<h1>Aether-gate</h1><div class=sub>Radio setup &amp; launcher &mdash; present any radio to AetherSDR as a Flex &middot; <a href="/known" target=_blank>Known info / status &#8599;</a> &middot; <a href="#" onclick="logout();return false">Log out</a></div>
<div class=card id=updcard style="display:none">
 <div id=updmsg class=hint></div>
 <div id=updactions style="margin-top:10px;display:none">
  <button id=updgo>Install update</button>
  <span class=hint id=updnote style="margin-left:10px"></span>
 </div>
</div>

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
// Every POST is JSON: the server refuses anything else (so another web page
// cannot drive this one), and a 401 means the session ended -> show the login.
async function post(u,o){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o||{})});
 if(r.status===401){location.reload();} return r;}
async function logout(){await post('/api/auth/logout',{});location.reload();}
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
 c.profile=document.getElementById('profname').value.trim();   // lets a blank password mean "the saved one"
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
 // The server never sends a saved password back; say it is there instead.
 const pw=document.getElementById('password');
 if(pw)pw.placeholder=c.password_saved?'saved - leave blank to keep it':'';
 if(c.adapter){document.getElementById('adapter').value=c.adapter;}
 // yaesu profiles store canonical keys; mirror them back into the …2 DOM fields.
 if(c.adapter==='yaesu'){const set=(id,v)=>{const e=document.getElementById(id);if(e&&v!==undefined)e.value=v;};
   set('rig_serial_port2',c.rig_serial_port);set('rig_baud2',c.rig_baud);
   set('rigctld_host2',c.rigctld_host);set('rigctld_port2',c.rigctld_port);
   set('soapy_driver2',c.soapy_driver);set('gain2',c.gain);set('direct_samp2',c.direct_samp);}
 if(c.adapter==='icom7300'&&c.civ_addr){const e=document.getElementById('civ_addr_7300');if(e)e.value=c.civ_addr;}
 onAdapter();}
async function start(){msg('...');
 const r=await (await post('/api/start',cfg())).json();
 msg(r.ok?'started':('⚠ '+(r.error||'failed')),r.ok);poll();}
async function stop(){await post('/api/stop',{});msg('stopped');poll();}
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
 const r=await (await post('/api/profiles/save',
   {name,cfg:cfg(),autostart:document.getElementById('autostart').checked})).json();
 if(r.ok){msg('saved "'+name+'"',true);await loadProfiles();document.getElementById('profsel').value=name;}else msg('⚠ '+r.error);}
async function delProfile(){const n=document.getElementById('profsel').value;if(!n)return;
 await post('/api/profiles/delete',{name:n});await loadProfiles();msg('deleted "'+n+'"');}
async function poll(){const rs=await fetch('/api/status');if(rs.status===401){location.reload();return;}const s=await rs.json();
 document.getElementById('dot').style.background=s.running?'#3fb950':'#6e7681';
 document.getElementById('st').textContent=s.running?('RUNNING (pid '+s.pid+')'):'stopped';
 document.getElementById('argv').textContent=(s.argv&&s.argv.length)?s.argv.slice(3).join(' '):'';
 const cp=document.getElementById('ctl_port').value||'8731';
 document.getElementById('panellink').href='http://'+location.hostname+':'+cp+'/';
 UPD_RUNNING=s.running;}

// --- updates -------------------------------------------------------------
// Deliberately quiet: the card stays hidden unless there is something to say,
// so the page does not nag someone who just wants to start their radio.
let UPD_RUNNING=false, UPD_TAG=null;
async function checkUpdate(){
 let u; try{u=await (await fetch('/api/update')).json();}catch(e){return;}
 const card=document.getElementById('updcard'), acts=document.getElementById('updactions');
 if(u.available){
  UPD_TAG=u.latest;
  card.style.display=''; acts.style.display='';
  document.getElementById('updmsg').innerHTML='<b>'+u.message+'</b>';
  document.getElementById('updnote').textContent=
    UPD_RUNNING?'Press Stop first — the gate must not be running.':'Takes about a minute.';
  document.getElementById('updgo').disabled=!!UPD_RUNNING;
 } else if(!u.checked){
  card.style.display=''; acts.style.display='none';
  document.getElementById('updmsg').textContent=u.message;
 } else { card.style.display='none'; }
}
async function doUpdate(){
 const btn=document.getElementById('updgo'), note=document.getElementById('updnote');
 btn.disabled=true; note.textContent='Updating — do not power off…';
 let r; try{
  r=await (await post('/api/update/install',{tag:UPD_TAG})).json();
 }catch(e){ note.textContent='Update failed: '+e; btn.disabled=false; return; }
 document.getElementById('updmsg').innerHTML='<b>'+(r.message||'')+'</b>';
 if(r.ok){
  note.innerHTML='Now press <b>Start</b> to run the new version.';
  document.getElementById('updactions').style.display='none';
 } else {
  note.textContent=r.rolled_back?'Your working version was put back.':'';
  btn.disabled=false;
 }
}
document.getElementById('updgo').onclick=doUpdate;
setTimeout(checkUpdate,1500);
init();
</script></body></html>"""


AUTH_PAGE = r"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Aether-gate - setup PIN</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;max-width:420px;margin:0 auto;padding:28px 20px}
 h1{color:#58a6ff;margin:0 0 4px} .sub{color:#8b949e;margin:0 0 16px;font-size:14px;line-height:1.5}
 label{display:block;margin:12px 0 4px;font-size:13px;color:#adbac7}
 input{width:100%;box-sizing:border-box;background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:9px;font-size:16px}
 button{margin-top:14px;font-size:15px;font-weight:600;border:none;border-radius:6px;padding:10px 18px;cursor:pointer;color:#fff;background:#238636}
 #msg{margin-top:10px;color:#d29922;font-size:14px} code{background:#161b22;padding:1px 4px;border-radius:4px}
</style></head><body>
<h1>Aether-gate</h1>
<div class=sub id=intro>Checking&hellip;</div>
<form id=f onsubmit="go();return false" style="display:none">
 <label for=pin>Setup PIN</label><input id=pin type=password autocomplete=current-password autofocus>
 <div id=confirmbox style="display:none"><label for=pin2>Type it again</label><input id=pin2 type=password autocomplete=new-password></div>
 <button id=btn>Continue</button><div id=msg></div>
</form>
<script>
let FIRST=false;
async function init(){
 const s=await (await fetch('/api/auth/state')).json();
 if(s.authed){location.reload();return;}
 FIRST=!s.pin_set;
 document.getElementById('intro').innerHTML=FIRST
  ?'<b>First run: choose a setup PIN.</b> This page starts radios and keeps their logins, so it asks for a PIN from now on (at least 4 characters). Forgot it later? Delete <code>~/.aether-gate/setup-auth.json</code> on the gate and reload.'
  :'Enter the setup PIN for this gate.';
 document.getElementById('confirmbox').style.display=FIRST?'':'none';
 document.getElementById('btn').textContent=FIRST?'Set PIN':'Log in';
 document.getElementById('f').style.display='';
}
async function go(){
 const pin=document.getElementById('pin').value, m=document.getElementById('msg');
 if(FIRST&&pin!==document.getElementById('pin2').value){m.textContent='The two PINs differ.';return;}
 const r=await fetch(FIRST?'/api/auth/setup':'/api/auth/login',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({pin})});
 const j=await r.json(); if(j.ok){location.reload();} else {m.textContent=j.error||'Refused';}
}
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
    if _setup_open():
        print("  AETHER_GATE_SETUP_OPEN is set: no setup PIN. Anyone on this LAN can use the page.")
    elif _auth_record() is None:
        print("  No setup PIN yet: the first person to open the page chooses one.")
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
