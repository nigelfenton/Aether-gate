#
# Aether-gate — startup update check (best-effort, non-fatal).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""One quiet log line at startup if a newer release exists on GitHub.

Design constraints (learned the hard way in this shack):
  * NEVER block startup or raise — a version check must fail SILENTLY and safely.
    No network / GitHub down / rate-limited / no releases yet -> just skip.
  * Runs on a daemon thread so it can't hold the process open or delay the gate.
  * Uses the /releases LIST endpoint, not /releases/latest: the latter EXCLUDES
    pre-releases (returns 404 when the only release is a pre-release), which is
    exactly Aether-gate's current state. We pick the newest by semver, and by
    default include pre-releases (opt out via include_prerelease=False).
  * Opt-out: set AETHER_GATE_NO_UPDATE_CHECK=1 (or pass enabled=False).

Pure stdlib (urllib), no new dependencies.
"""
import json
import os
import re
import threading
import urllib.request

REPO = "nigelfenton/Aether-gate"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
_TIMEOUT = 4  # seconds — short; this is best-effort


def _parse_semver(tag):
    """'v0.2.0' / '0.2.0-rc1' -> (0, 2, 0, is_final). Returns None if unparseable.

    is_final is True for a plain release, False for a pre-release suffix (so a
    final sorts ABOVE a pre-release of the same x.y.z)."""
    if not tag:
        return None
    m = re.match(r"[vV]?(\d+)\.(\d+)\.(\d+)(.*)$", tag.strip())
    if not m:
        return None
    major, minor, patch, suffix = m.groups()
    is_final = suffix.strip() in ("", "+")  # no '-rc'/'-dev' etc.
    return (int(major), int(minor), int(patch), is_final)


def _newer(candidate, current):
    """True if semver `candidate` is strictly newer than `current`. Either may be
    None (unparseable) -> treat as not-newer (fail safe, no false 'update')."""
    c, cur = _parse_semver(candidate), _parse_semver(current)
    if c is None or cur is None:
        return False
    return c > cur


def _check(current_version, include_prerelease, logfn):
    try:
        req = urllib.request.Request(
            RELEASES_URL,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"aether-gate/{current_version}"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(data, list):
            return
        # newest parseable release tag (skip drafts; skip pre-releases unless allowed)
        best_tag, best_key = None, None
        for rel in data:
            if rel.get("draft"):
                continue
            if rel.get("prerelease") and not include_prerelease:
                continue
            key = _parse_semver(rel.get("tag_name"))
            if key is None:
                continue
            if best_key is None or key > best_key:
                best_key, best_tag = key, rel.get("tag_name")
        if best_tag and _newer(best_tag, current_version):
            logfn(f"[update] Aether-gate {best_tag} available "
                  f"(you have {current_version}) -> {RELEASES_PAGE}")
    except Exception:
        # Offline, rate-limited, DNS, 404-no-releases, JSON junk — all non-fatal.
        # Silence is intended: a version check must never spam or crash startup.
        pass


def check_for_update(current_version, *, enabled=True, include_prerelease=True,
                     logfn=print):
    """Fire a best-effort update check on a daemon thread. Returns immediately.

    current_version: the app's own version (aether_gate.__version__).
    enabled:         master switch; also honours AETHER_GATE_NO_UPDATE_CHECK=1.
    include_prerelease: count pre-releases as candidates (default True).
    logfn:           where the one-line notice goes (default print; pass the
                     gate's log()).
    """
    if not enabled or os.environ.get("AETHER_GATE_NO_UPDATE_CHECK", "") in ("1", "true", "yes"):
        return
    t = threading.Thread(
        target=_check,
        args=(current_version, include_prerelease, logfn),
        name="update-check", daemon=True)
    t.start()
