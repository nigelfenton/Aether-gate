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
