#
# Aether-gate — Setup UI security tests (no hardware, no network beyond loopback).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The Setup UI (:8730) holds radio logins and can start, stop and reconfigure
the gate, and it listens on the LAN. These pin the properties that keep it from
being a credential leak or a remote control for anyone who can reach it:

  * a saved radio password is never in any GET response (profiles, status,
    known-info) and never on the gate's command line -- it reaches the gate as
    AETHER_GATE_PW in its environment instead;
  * a blank password on Start or Save means "the saved one", since the page can
    no longer read it back;
  * the profiles and PIN files are owner-only (0600) on POSIX;
  * without a session, every /api/ call is 401 and the pages show the PIN form;
  * the first PIN can be set once; a wrong PIN is refused;
  * a Host that is not an IP literal or this machine's own name is refused
    (DNS rebinding), a POST that is not JSON is refused (forces a CORS
    preflight we never answer), and a POST whose Origin is another site is
    refused (CSRF).

The subject is the gate's OWN setup server, reached over loopback -- a
socket-owning test in the sense AGENTS.md asks PRs to disclose. It binds port 0
on 127.0.0.1 and skips if it cannot. subprocess.Popen is replaced with a
recorder, so no gate is ever launched.

Run:  python -m aether_gate.tests.test_setup_security
"""
import http.client
import http.server
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
from unittest import SkipTest     # pytest honours it too

from aether_gate import setup

SECRET = "Radio-Pw-5e3f!"
ICOM = {"adapter": "icom9700", "radio_ip": "127.0.0.1", "user": "ham", "password": SECRET}


class FakePopen:
    """Records what the launcher would have run; never runs anything."""
    calls = []

    def __init__(self, argv, env=None, **kw):
        FakePopen.calls.append({"argv": list(argv), "env": dict(env or {})})
        self.pid = 4242

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class Env:
    """A private HOME for the profile/PIN files, a fake Popen, a clean slate."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="ag-setup-test-")
        self.saved = (setup.PROFILES_PATH, setup.AUTH_PATH, setup.subprocess.Popen, setup._proc)
        setup.PROFILES_PATH = os.path.join(self.dir, ".aether-gate", "profiles.json")
        setup.AUTH_PATH = os.path.join(self.dir, ".aether-gate", "setup-auth.json")
        setup.subprocess.Popen = FakePopen
        setup._proc = None
        setup._sessions.clear()
        setup._fails.update(n=0, until=0.0)
        FakePopen.calls.clear()
        self.open_env = os.environ.pop("AETHER_GATE_SETUP_OPEN", None)
        return self

    def __exit__(self, *exc):
        (setup.PROFILES_PATH, setup.AUTH_PATH, setup.subprocess.Popen, setup._proc) = self.saved
        if self.open_env is not None:
            os.environ["AETHER_GATE_SETUP_OPEN"] = self.open_env
        shutil.rmtree(self.dir, ignore_errors=True)


class Server:
    def __enter__(self):
        try:
            self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), setup.Handler)
        except OSError as e:                         # fail fast, per AGENTS.md
            raise SkipTest(f"cannot bind a loopback port: {e}")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None, host=None, origin=None, ctype="application/json",
            cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            if ctype:
                h["Content-Type"] = ctype
        if origin:
            h["Origin"] = origin
        if cookie:
            h["Cookie"] = cookie
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode("utf-8", "replace")
        set_cookie = r.getheader("Set-Cookie") or ""
        c.close()
        return r.status, text, set_cookie


def _login(s):
    code, _, sc = s.req("POST", "/api/auth/setup", {"pin": "7391"})
    assert code == 200, f"first PIN setup should succeed, got {code}"
    tok = sc.split(";", 1)[0]
    assert tok.startswith("ag_session=") and len(tok) > 20, sc
    for flag in ("HttpOnly", "SameSite=Strict"):
        assert flag in sc, f"session cookie lacks {flag}: {sc}"
    return tok


# ------------------------------------------------------------------ unit level
def test_password_never_on_the_command_line():
    argv = setup._build_argv(ICOM)
    assert SECRET not in argv and "--pass" not in argv, argv
    assert setup._child_env(ICOM).get("AETHER_GATE_PW") == SECRET
    assert "AETHER_GATE_PW" not in setup._child_env({"adapter": "sim"})
    print("ok  argv: the password is not on the gate's command line; it is AETHER_GATE_PW")


def test_start_hands_the_password_over_by_environment():
    with Env():
        code, resp = setup._start(dict(ICOM))
        assert code == 200, resp
        call = FakePopen.calls[-1]
        assert SECRET not in call["argv"], call["argv"]
        assert call["env"].get("AETHER_GATE_PW") == SECRET
        assert SECRET not in json.dumps(resp) and SECRET not in json.dumps(setup._status())
    print("ok  start: env carries the password; the start reply and status do not")


def test_blank_password_means_the_saved_one():
    with Env():
        setup._save_profiles({"profiles": {"shack": dict(ICOM)}, "autostart": None})
        code, resp = setup._start(dict(ICOM, password="", profile="shack"))
        assert code == 200, resp
        assert FakePopen.calls[-1]["env"].get("AETHER_GATE_PW") == SECRET
    print("ok  start: a blank password with a profile name uses the saved password")


def test_private_files_are_owner_only():
    if os.name == "nt":
        print("skip files: POSIX modes do not apply on Windows")
        return
    with Env():
        setup._save_profiles({"profiles": {"shack": dict(ICOM)}, "autostart": None})
        setup._set_pin("7391")
        for path in (setup.PROFILES_PATH, setup.AUTH_PATH):
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode & 0o077 == 0, f"{os.path.basename(path)} is {oct(mode)}, want 0600"
    print("ok  files: profiles.json and setup-auth.json are 0600")


# ------------------------------------------------------------------ over HTTP
def test_no_session_no_api():
    with Env(), Server() as s:
        code, _, _ = s.req("GET", "/api/profiles")
        assert code == 401, f"/api/profiles without a session: {code}"
        code, _, _ = s.req("POST", "/api/stop", {})
        assert code == 401, f"/api/stop without a session: {code}"
        code, text, _ = s.req("GET", "/")
        assert code == 200 and "setup PIN" in text and "/api/start" not in text, \
            "the page without a session should be the PIN form, not the launcher"
    print("ok  http: no session -> 401 on /api, the PIN form on /")


def test_pin_first_run_once_and_wrong_pin_refused():
    with Env(), Server() as s:
        tok = _login(s)
        code, _, _ = s.req("POST", "/api/auth/setup", {"pin": "0000"})
        assert code == 409, f"a second first-run setup must be refused, got {code}"
        setup._fails.update(n=0, until=0.0)
        code, _, sc = s.req("POST", "/api/auth/login", {"pin": "1111"})
        assert code == 401 and "ag_session=" not in sc, code
        code, _, _ = s.req("GET", "/api/status", cookie=tok)
        assert code == 200
    print("ok  http: PIN set once; a wrong PIN gets no session")


def test_no_get_ever_returns_the_password():
    with Env(), Server() as s:
        tok = _login(s)
        code, _, _ = s.req("POST", "/api/profiles/save",
                           {"name": "shack", "cfg": dict(ICOM), "autostart": False}, cookie=tok)
        assert code == 200
        code, _, _ = s.req("POST", "/api/start", dict(ICOM, password="", profile="shack"),
                           cookie=tok)
        assert code == 200
        for path in ("/api/profiles", "/api/status", "/api/known"):
            code, text, _ = s.req("GET", path, cookie=tok)
            assert code == 200, (path, code)
            assert SECRET not in text, f"{path} leaked the radio password"
        prof = json.loads(s.req("GET", "/api/profiles", cookie=tok)[1])["profiles"]["shack"]
        assert prof["password"] == "" and prof["password_saved"] is True, prof
        # saving again with a blank password keeps the stored one
        s.req("POST", "/api/profiles/save",
              {"name": "shack", "cfg": dict(ICOM, password=""), "autostart": False}, cookie=tok)
        assert setup._load_profiles()["profiles"]["shack"]["password"] == SECRET
    print("ok  http: profiles/status/known never contain the password; blank save keeps it")


def test_rebinding_and_cross_site_requests_refused():
    with Env(), Server() as s:
        tok = _login(s)
        code, _, _ = s.req("GET", "/api/profiles", cookie=tok,
                           host=f"evil.example:{s.port}")
        assert code == 403, f"a foreign Host must be refused (DNS rebinding), got {code}"
        code, _, _ = s.req("POST", "/api/stop", {}, cookie=tok, ctype="text/plain")
        assert code == 415, f"a non-JSON POST must be refused, got {code}"
        code, _, _ = s.req("POST", "/api/stop", {}, cookie=tok, origin="http://evil.example")
        assert code == 403, f"a cross-origin POST must be refused, got {code}"
        code, _, _ = s.req("POST", "/api/stop", {}, cookie=tok,
                           origin=f"http://127.0.0.1:{s.port}")
        assert code == 200, f"a same-origin JSON POST must work, got {code}"
    print("ok  http: foreign Host 403, non-JSON POST 415, cross-origin POST 403, same-origin OK")


def main():
    tests = [test_password_never_on_the_command_line,
             test_start_hands_the_password_over_by_environment,
             test_blank_password_means_the_saved_one, test_private_files_are_owner_only,
             test_no_session_no_api, test_pin_first_run_once_and_wrong_pin_refused,
             test_no_get_ever_returns_the_password,
             test_rebinding_and_cross_site_requests_refused]
    for t in tests:
        try:
            t()
        except SkipTest as e:
            print(f"skip {t.__name__}: {e}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} setup-security tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
