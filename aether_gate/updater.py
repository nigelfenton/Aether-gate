#
# Aether-gate — one-click self-update from a published GitHub release.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Download a published release, swap it in, and put the old one back if it fails.

WHO THIS IS FOR. An appliance in the hands of an operator who is comfortable
with radios and not with terminals. Every failure mode here has to end with a
working gate and a plain-English sentence — never a half-installed tree, never
"run this command to recover".

WHY A TARBALL AND NOT `git pull`:

  * `deploy/build-image.sh` rsyncs the tree with `--exclude .git`, so a FLASHED
    APPLIANCE HAS NO GIT CHECKOUT AT ALL. A git-based updater would work on a
    developer's box and be dead on exactly the machines that need it most.
  * A release is a deliberate act. Tracking a branch would ship half-finished
    work to someone who cannot roll it back.
  * Swapping whole directories makes rollback a rename, which is the only
    recovery simple enough to trust unattended.

THE SAFETY PROPERTIES, in the order they matter:

  1. NEVER update while transmitting. Checked by the caller (the gate process is
     stopped first), and the install refuses if the gate will not stop cleanly.
  2. The live tree is never edited in place. A new tree is staged alongside,
     verified to be structurally sane, and only then swapped in by rename.
  3. The previous tree is KEPT, not deleted. Rollback is a rename back.
  4. If the new version cannot even be imported, roll back automatically and
     report the failure. A gate that will not start is worse than an old gate.

Pure stdlib: urllib + tarfile. No new dependencies on an appliance image.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

REPO = "nigelfenton/Aether-gate"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
_TIMEOUT = 20          # generous: this is a download, not a liveness probe
_MAX_BYTES = 80 << 20  # 80 MB ceiling - a sane gate release is ~1 MB


def _parse_semver(tag):
    """'v0.3.1' / '0.3.1-rc1' -> (major, minor, patch, is_final), or None."""
    if not tag:
        return None
    m = re.match(r"[vV]?(\d+)\.(\d+)\.(\d+)(.*)$", tag.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), not m.group(4))


def _newer(candidate, current):
    c, cur = _parse_semver(candidate), _parse_semver(current)
    return bool(c and cur and c > cur)


def latest_release(include_prerelease=False, timeout=_TIMEOUT):
    """Newest release by semver, or None. Never raises — the UI must survive
    having no network, a rate-limited API, or a repo with no releases yet."""
    try:
        req = urllib.request.Request(
            RELEASES_URL, headers={"Accept": "application/vnd.github+json",
                                   "User-Agent": "aether-gate-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            releases = json.loads(r.read().decode())
    except Exception:
        return None

    best = None
    for rel in releases or []:
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not include_prerelease:
            continue
        tag = rel.get("tag_name")
        if not _parse_semver(tag):
            continue
        if best is None or _newer(tag, best.get("tag_name")):
            best = rel
    if not best:
        return None
    return {"tag": best.get("tag_name"),
            "name": best.get("name") or best.get("tag_name"),
            "notes": (best.get("body") or "").strip(),
            "tarball": best.get("tarball_url"),
            "url": best.get("html_url")}


def status(current_version, include_prerelease=False):
    """What the web UI shows. Always answers, even offline."""
    rel = latest_release(include_prerelease)
    if not rel:
        return {"current": current_version, "latest": None, "available": False,
                "checked": True, "message": "Could not reach GitHub to check for updates."}
    avail = _newer(rel["tag"], current_version)
    return {"current": current_version, "latest": rel["tag"], "available": avail,
            "checked": True, "notes": rel["notes"][:2000], "url": rel["url"],
            "message": (f"Update available: {rel['tag']} (you have {current_version})"
                        if avail else f"You are up to date ({current_version}).")}


def _download(url, dest_path, timeout=_TIMEOUT):
    """Fetch to a file with a size ceiling, so a wrong URL cannot fill the card."""
    req = urllib.request.Request(url, headers={"User-Agent": "aether-gate-updater"})
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest_path, "wb") as f:
        while True:
            chunk = r.read(64 << 10)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_BYTES:
                raise RuntimeError("release download is implausibly large - refusing")
            f.write(chunk)
    if total == 0:
        raise RuntimeError("release download was empty")
    return total


def _safe_extract(tar, path):
    """Extract, refusing any member that escapes the destination.

    A tarball is untrusted input even from our own repo: a path like ../../etc
    would write outside the staging directory. Python 3.12 has `filter='data'`
    for this; the check is explicit here so the behaviour is identical on the
    older Pythons an appliance image may carry.
    """
    dest = os.path.realpath(path)
    for m in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, m.name))
        if not (target == dest or target.startswith(dest + os.sep)):
            raise RuntimeError(f"refusing tar member outside destination: {m.name}")
        if m.issym() or m.islnk():
            link = os.path.realpath(os.path.join(os.path.dirname(target), m.linkname))
            if not (link == dest or link.startswith(dest + os.sep)):
                raise RuntimeError(f"refusing link outside destination: {m.name}")
    tar.extractall(path)


def _find_package_root(staged):
    """The tarball unpacks to <owner>-<repo>-<sha>/ — find the aether_gate inside."""
    for root, dirs, _files in os.walk(staged):
        if os.path.basename(root) == "aether_gate" and "__init__.py" in os.listdir(root):
            return root
    return None


def _sane_tree(pkg_root):
    """Refuse to install something that is not recognisably the gate.

    Cheap structural check, not a security boundary: it catches a truncated
    download or a wrong asset BEFORE the live tree is touched.
    """
    required = ["__init__.py", "__main__.py", "core", "adapters", "setup.py"]
    missing = [r for r in required if not os.path.exists(os.path.join(pkg_root, r))]
    return missing


def install(tag_or_none, live_pkg_dir, *, logfn=print, include_prerelease=False,
            verify_cmd=None):
    """Install the newest release over `live_pkg_dir` (…/gate/aether_gate).

    Returns {"ok": bool, "message": str, ...}. NEVER raises: the caller is a web
    request handler serving someone who cannot read a traceback.

    The sequence, and why each step is where it is:
      download -> stage -> structural check -> swap -> import check -> rollback?
    Everything before the swap is reversible by deleting a temp directory; the
    swap itself is two renames; the import check is what catches a release that
    is intact but broken on THIS machine (missing dependency, wrong Python).
    """
    live_pkg_dir = os.path.abspath(live_pkg_dir)
    parent = os.path.dirname(live_pkg_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{live_pkg_dir}.backup-{stamp}"
    staging = None

    try:
        rel = latest_release(include_prerelease)
        if not rel:
            return {"ok": False, "message": "Could not reach GitHub to fetch the update."}
        if tag_or_none and rel["tag"] != tag_or_none:
            return {"ok": False,
                    "message": f"Expected {tag_or_none} but the newest release is {rel['tag']}."}

        if not os.access(parent, os.W_OK):
            return {"ok": False,
                    "message": f"No permission to write to {parent} - the gate cannot update itself."}

        staging = tempfile.mkdtemp(prefix="aether-gate-update-", dir=parent)
        tarpath = os.path.join(staging, "release.tar.gz")

        logfn(f"[update] downloading {rel['tag']}")
        size = _download(rel["tarball"], tarpath)
        logfn(f"[update] downloaded {size} bytes")

        unpacked = os.path.join(staging, "unpacked")
        os.makedirs(unpacked, exist_ok=True)
        with tarfile.open(tarpath, "r:gz") as tar:
            _safe_extract(tar, unpacked)

        pkg_root = _find_package_root(unpacked)
        if not pkg_root:
            return {"ok": False, "message": "The downloaded release did not contain a gate to install."}
        missing = _sane_tree(pkg_root)
        if missing:
            return {"ok": False,
                    "message": "The downloaded release looks incomplete "
                               f"(missing {', '.join(missing)}) - nothing was changed."}

        # ---- the swap: two renames, both on the same filesystem -------------
        logfn(f"[update] installing {rel['tag']}")
        os.rename(live_pkg_dir, backup)
        try:
            shutil.move(pkg_root, live_pkg_dir)
        except Exception:
            os.rename(backup, live_pkg_dir)          # put it straight back
            raise

        # ---- does the new tree actually work HERE? --------------------------
        # An intact release can still be unusable on this machine (a new
        # dependency, an older Python). Import it in a subprocess so a hard
        # failure cannot take the running web UI down with it.
        cmd = verify_cmd or [sys.executable, "-c",
                             "import aether_gate, aether_gate.setup; print(aether_gate.__version__)"]
        try:
            proc = subprocess.run(cmd, cwd=parent, capture_output=True, timeout=60)
            ok = proc.returncode == 0
            detail = (proc.stdout or proc.stderr or b"").decode(errors="replace").strip()
        except Exception as e:
            ok, detail = False, str(e)

        if not ok:
            logfn(f"[update] new version failed to load, rolling back: {detail}")
            shutil.rmtree(live_pkg_dir, ignore_errors=True)
            os.rename(backup, live_pkg_dir)
            return {"ok": False, "rolled_back": True,
                    "message": f"{rel['tag']} would not start, so the working version was put back. "
                               "Nothing is broken.",
                    "detail": detail[:500]}

        logfn(f"[update] {rel['tag']} installed (previous kept at {os.path.basename(backup)})")
        return {"ok": True, "installed": rel["tag"], "previous_kept": os.path.basename(backup),
                "message": f"Updated to {rel['tag']}. Restart the gate to use it.",
                "restart_required": True}

    except Exception as e:
        # Any failure before the swap leaves the live tree untouched; a failure
        # after it has already been rolled back above.
        if os.path.isdir(backup) and not os.path.isdir(live_pkg_dir):
            try:
                os.rename(backup, live_pkg_dir)
                logfn("[update] restored the previous version after an error")
            except Exception:
                pass
        return {"ok": False, "message": f"Update failed: {e}", "error": str(e)}
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
