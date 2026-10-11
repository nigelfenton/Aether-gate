#
# Aether-gate — tests for the one-click updater.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""The updater runs unattended on an appliance owned by someone who cannot
recover it from a shell. So the properties under test are not "does it install"
but "what does it leave behind when it goes wrong":

  * a bad tarball must not touch the live tree at all
  * a release that installs but will not run must be ROLLED BACK automatically
  * a malicious/mangled tar must not write outside the staging directory
  * the live tree must never be left missing, whatever fails

Everything is exercised against real directories in a tmpdir with a locally
built tarball — no network, no GitHub, no hardware.
"""
import io
import os
import sys
import tarfile
import tempfile

try:
    from aether_gate import updater
except ImportError:                                   # pragma: no cover
    updater = None


def _skip(reason):
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(reason, allow_module_level=True)
    print(f"SKIP: {reason}")
    raise SystemExit(0)


if updater is None:
    _skip("aether_gate.updater not importable")


# --------------------------------------------------------------------------
# helpers: build a fake "release" tarball shaped like GitHub's
# --------------------------------------------------------------------------
def _make_pkg(root, name="aether_gate", complete=True, version="9.9.9"):
    """A tree that looks like the gate package (or deliberately does not)."""
    pkg = os.path.join(root, name)
    os.makedirs(os.path.join(pkg, "core"), exist_ok=True)
    os.makedirs(os.path.join(pkg, "adapters"), exist_ok=True)
    io.open(os.path.join(pkg, "__init__.py"), "w").write(f'__version__ = "{version}"\n')
    if complete:
        io.open(os.path.join(pkg, "__main__.py"), "w").write("")
        io.open(os.path.join(pkg, "setup.py"), "w").write("")
    io.open(os.path.join(pkg, "core", "__init__.py"), "w").write("")
    io.open(os.path.join(pkg, "adapters", "__init__.py"), "w").write("")
    return pkg


def _tar_of(pkg_parent, tarpath, prefix="nigelfenton-Aether-gate-abc1234"):
    """GitHub tarballs unpack to <owner>-<repo>-<sha>/ — mimic that shape."""
    with tarfile.open(tarpath, "w:gz") as t:
        t.add(pkg_parent, arcname=prefix)
    return tarpath


def _fake_release(tmp, tarpath, tag="v9.9.9"):
    """Patch latest_release + _download so nothing touches the network."""
    updater.latest_release = lambda include_prerelease=False, timeout=None: {
        "tag": tag, "name": tag, "notes": "", "tarball": "file://local", "url": ""}

    def _dl(url, dest, timeout=None):
        with open(tarpath, "rb") as src, open(dest, "wb") as dst:
            data = src.read()
            dst.write(data)
        return len(data)

    updater._download = _dl


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_incomplete_release_never_touches_the_live_tree():
    """A truncated/wrong download must fail BEFORE the swap.

    This is the one that matters most: the live tree is the only working copy on
    the appliance, and an install that half-lands leaves an operator with a radio
    that will not start and no way to fix it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        live_parent = os.path.join(tmp, "gate")
        live = _make_pkg(live_parent, version="1.0.0")
        marker = os.path.join(live, "IAMLIVE")
        io.open(marker, "w").write("original")

        src = os.path.join(tmp, "src")
        _make_pkg(src, complete=False)                      # missing __main__/setup
        tarpath = _tar_of(src, os.path.join(tmp, "rel.tar.gz"))
        _fake_release(tmp, tarpath)

        res = updater.install(None, live, logfn=lambda m: None)
        assert not res["ok"], "an incomplete release must not report success"
        assert "incomplete" in res["message"].lower(), res["message"]
        assert os.path.exists(marker), "the LIVE tree was modified by a failed install"
        assert io.open(marker).read() == "original"


def test_a_release_that_will_not_run_is_rolled_back():
    """Structurally fine, but broken on THIS machine -> automatic rollback."""
    with tempfile.TemporaryDirectory() as tmp:
        live_parent = os.path.join(tmp, "gate")
        live = _make_pkg(live_parent, version="1.0.0")
        io.open(os.path.join(live, "IAMLIVE"), "w").write("original")

        src = os.path.join(tmp, "src")
        _make_pkg(src, complete=True, version="9.9.9")
        tarpath = _tar_of(src, os.path.join(tmp, "rel.tar.gz"))
        _fake_release(tmp, tarpath)

        # a verify step that always fails, standing in for "new version won't import"
        res = updater.install(None, live, logfn=lambda m: None,
                              verify_cmd=[sys.executable, "-c", "raise SystemExit(1)"])

        assert not res["ok"], "a release that fails verification must not report success"
        assert res.get("rolled_back"), "it must roll back, not leave the broken tree in place"
        assert os.path.exists(os.path.join(live, "IAMLIVE")), "the working version was NOT restored"
        assert io.open(os.path.join(live, "IAMLIVE")).read() == "original"


def test_successful_install_swaps_and_keeps_the_previous_version():
    with tempfile.TemporaryDirectory() as tmp:
        live_parent = os.path.join(tmp, "gate")
        live = _make_pkg(live_parent, version="1.0.0")
        io.open(os.path.join(live, "IAMLIVE"), "w").write("original")

        src = os.path.join(tmp, "src")
        _make_pkg(src, complete=True, version="9.9.9")
        tarpath = _tar_of(src, os.path.join(tmp, "rel.tar.gz"))
        _fake_release(tmp, tarpath)

        res = updater.install(None, live, logfn=lambda m: None,
                              verify_cmd=[sys.executable, "-c", "pass"])
        assert res["ok"], res.get("message")
        assert res["installed"] == "v9.9.9"
        # the new tree is live...
        assert '9.9.9' in io.open(os.path.join(live, "__init__.py")).read()
        assert not os.path.exists(os.path.join(live, "IAMLIVE"))
        # ...and the old one is still on disk to go back to
        backups = [d for d in os.listdir(live_parent) if d.startswith("aether_gate.backup-")]
        assert backups, "the previous version was not kept"
        assert os.path.exists(os.path.join(live_parent, backups[0], "IAMLIVE"))


def test_tar_traversal_is_refused():
    """A member with ../ must be refused rather than written outside the target.

    A release tarball is untrusted input even from our own repo — a compromised
    or corrupted asset must not be able to write into /etc or the user's home.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tarpath = os.path.join(tmp, "evil.tar.gz")
        payload = os.path.join(tmp, "payload")
        os.makedirs(payload, exist_ok=True)
        io.open(os.path.join(payload, "x"), "w").write("pwned")
        with tarfile.open(tarpath, "w:gz") as t:
            t.add(os.path.join(payload, "x"), arcname="../../escaped")

        dest = os.path.join(tmp, "dest")
        os.makedirs(dest, exist_ok=True)
        refused = False
        with tarfile.open(tarpath, "r:gz") as t:
            try:
                updater._safe_extract(t, dest)
            except RuntimeError as e:
                refused = "outside destination" in str(e)
        assert refused, "a path-traversal member was NOT refused"
        assert not os.path.exists(os.path.join(tmp, "escaped")), "the tar escaped its destination"


def test_status_is_honest_when_github_is_unreachable():
    """Offline must read as 'could not check', never as 'up to date'.

    Reporting up-to-date when the check failed would silently strand an operator
    on an old version with no indication anything was wrong.
    """
    updater.latest_release = lambda include_prerelease=False, timeout=None: None
    st = updater.status("0.3.0")
    assert st["available"] is False
    assert st["latest"] is None
    assert "could not reach" in st["message"].lower(), st["message"]


def test_running_gate_is_detected_even_when_started_by_systemd():
    """The refuse-while-running guard must see a SERVICE, not just our own child.

    ⚠ REGRESSION TEST FOR A REAL ESCAPE. The first version checked only `_proc`,
    the process this web UI started. On the appliance the gate normally runs as
    a systemd unit, so `_proc` is None — and on a Pi 4 the update installed
    underneath a live, streaming gate. The unit tests passed throughout, because
    they only ever exercised the `_proc` path.
    """
    from aether_gate import setup as gsetup

    real_run = gsetup.subprocess.run

    class _R:
        def __init__(self, out):
            self.stdout = out

    # no gate anywhere -> not running
    gsetup.subprocess.run = lambda *a, **k: _R(b"")
    try:
        running, how = gsetup._gate_running()
        assert not running, "reported a gate running when none was"

        # only this web UI -> still not running
        gsetup.subprocess.run = lambda *a, **k: _R(
            b"982 /usr/bin/python3 -u -m aether_gate.setup --no-browser\n")
        running, how = gsetup._gate_running()
        assert not running, "mistook the setup UI itself for a running gate"

        # a systemd-launched gate -> MUST be detected
        gsetup.subprocess.run = lambda *a, **k: _R(
            b"982 /usr/bin/python3 -u -m aether_gate.setup --no-browser\n"
            b"112030 /usr/bin/python3 -u -m aether_gate --adapter soapy --rx-only\n")
        running, how = gsetup._gate_running()
        assert running, "a systemd-started gate was NOT detected - update would run under a live radio"
        assert how == "service", f"expected 'service', got {how!r}"

        # ⚠ SECOND ESCAPE, also found on the Pi 4: `pgrep -af aether_gate`
        # returns anything that MENTIONS the name, including the diagnostic
        # shell command doing the search. A substring test made the guard fire
        # with NO gate running, which blocks updates forever rather than
        # permitting a bad one — the opposite failure, equally useless.
        gsetup.subprocess.run = lambda *a, **k: _R(
            b"982 /usr/bin/python3 -u -m aether_gate.setup --no-browser\n"
            b'160264 bash -c pgrep -af aether_gate; echo "checking aether_gate"\n'
            b"160300 tail -f /var/log/aether_gate.log\n"
            b"160301 vim /home/aethergate/gate/aether_gate/setup.py\n")
        running, how = gsetup._gate_running()
        assert not running, (
            "a shell/editor merely MENTIONING aether_gate was mistaken for a running gate")

        # a different module in the package is not the gate either
        gsetup.subprocess.run = lambda *a, **k: _R(
            b"982 /usr/bin/python3 -m aether_gate.tests.test_updater\n")
        running, how = gsetup._gate_running()
        assert not running, "a helper module was mistaken for the gate"
    finally:
        gsetup.subprocess.run = real_run


def _refusal_with(pgrep_out, cgroup="", systemctl_out=b""):
    """The update refusal's message, with pgrep, /proc and systemctl injected."""
    from aether_gate import setup as gsetup

    class _R:
        def __init__(self, out):
            self.stdout = out

    def fake_run(args, *a, **k):
        return _R(pgrep_out if args[0] == "pgrep" else systemctl_out)

    real_run, real_cg = gsetup.subprocess.run, getattr(gsetup, "_read_cgroup", None)
    gsetup.subprocess.run = fake_run
    gsetup._read_cgroup = lambda pid: cgroup
    try:
        code, body = gsetup._update_install({})
    finally:
        gsetup.subprocess.run = real_run
        if real_cg is None:
            del gsetup._read_cgroup
        else:
            gsetup._read_cgroup = real_cg
    assert code == 409 and body["ok"] is False, f"update was not refused: {code} {body}"
    return body["message"]


_YAESU = (b"982 /usr/bin/python3 -u -m aether_gate.setup --no-browser\n"
          b"12018 /usr/bin/python3 -u -m aether_gate --adapter yaesu --radio FT-847\n")


def _never_names_the_setup_ui(msg):
    """#58: following the advice must not stop the page it is shown on."""
    assert "*" not in msg, f"advice contains a glob: {msg!r}"
    assert "aether-gate-setup" not in msg, f"advice names the Setup UI's own unit: {msg!r}"


def test_update_refusal_names_the_real_unit():
    """#57: the refusal must name the unit the gate actually runs under.

    ⚠ REGRESSION TEST FOR A REAL ESCAPE. The message said `sudo systemctl stop
    aether-gate-*`; on a Pi 5 the gate units are named after the radio
    (yaesu-gate.service, hpsdr-gate.service ...), so the glob matched none of
    them and the update stayed refused with no clue what to try next.
    """
    msg = _refusal_with(_YAESU, cgroup="0::/system.slice/yaesu-gate.service\n",
                        systemctl_out=b"NRestarts=0\nActiveState=active\nSubState=running\n")
    assert "yaesu-gate.service" in msg and "12018" in msg, msg
    assert "sudo systemctl stop yaesu-gate.service" in msg, msg
    assert "loop" not in msg, f"a healthy unit was called a restart loop: {msg!r}"
    _never_names_the_setup_ui(msg)

    # cgroup v1 names the unit on the systemd line, among others
    msg = _refusal_with(_YAESU, cgroup=("12:cpu,cpuacct:/system.slice/yaesu-gate.service\n"
                                        "1:name=systemd:/system.slice/yaesu-gate.service\n"),
                        systemctl_out=b"NRestarts=0\nSubState=running\n")
    assert "sudo systemctl stop yaesu-gate.service" in msg, msg


def test_update_refusal_says_when_the_unit_is_crash_looping():
    """#57: a unit restarting in a loop re-trips the guard every RestartSec, so
    "stop it" must be "stop the UNIT" and say why. Seen with 1575 restarts in a
    day, the radio's RTL dongle unplugged."""
    msg = _refusal_with(_YAESU, cgroup="0::/system.slice/kenwood-gate.service\n",
                        systemctl_out=(b"NRestarts=1575\nActiveState=activating\n"
                                       b"SubState=auto-restart\n"))
    assert "kenwood-gate.service" in msg and "loop" in msg and "1575" in msg, msg
    assert "sudo systemctl stop kenwood-gate.service" in msg, msg
    _never_names_the_setup_ui(msg)


def test_update_refusal_without_a_unit_shows_the_process():
    """No systemd unit to name (Windows, macOS, a terminal): name the PID and
    the command line rather than inventing a unit."""
    msg = _refusal_with(_YAESU, cgroup="")
    assert "12018" in msg and "--adapter yaesu" in msg, msg
    assert "systemctl" not in msg, f"suggested systemctl with no unit to name: {msg!r}"
    _never_names_the_setup_ui(msg)


def test_update_refusal_never_names_the_setup_ui():
    """#58: even if the gate's cgroup somehow resolves to the Setup UI's unit,
    the advice must not tell the operator to stop the page they are reading."""
    msg = _refusal_with(_YAESU, cgroup="0::/system.slice/aether-gate-setup.service\n")
    _never_names_the_setup_ui(msg)


def test_reported_version_comes_from_disk_not_from_memory():
    """After an update the page must report the NEW version, not the imported one.

    ⚠ REGRESSION TEST. The setup UI is long-running: it imports __version__ once
    at startup, and an update swaps in a tree with a different __init__.py
    underneath it. Reporting the in-memory value made the banner keep offering
    an update that had just been installed — observed on the Pi 4, where the
    disk said 0.4.0 and the page still said 0.3.0.
    """
    from aether_gate import setup as gsetup
    import aether_gate

    on_disk = gsetup._installed_version()
    assert on_disk, "no version could be read from disk"
    # it must PARSE the file, not echo the module attribute
    assert on_disk == aether_gate.__version__, (
        "disk and memory disagree in a clean tree - the reader is wrong")

    # and it must survive a __init__.py it cannot read, rather than returning nothing
    import builtins
    real_open = builtins.open

    def _boom(*a, **k):
        raise OSError("denied")

    builtins.open = _boom
    try:
        assert gsetup._installed_version() == aether_gate.__version__, (
            "an unreadable __init__.py must fall back to the imported version")
    finally:
        builtins.open = real_open


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
    print("test_updater:", "all checks passed" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())
