#
# Aether-gate — the Setup UI's browser-tab icon (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Every Setup UI page carries the Aether-gate icon inline, and it is the icon that
ships beside the Windows program.

Inline (a data: URI) on purpose: the PIN page is served before login, when a
separate /favicon request would get the PIN page back instead of an image. So the
link has to be in each page's <head>, and it has to decode to the shipped SVG.

Run:  python -m aether_gate.tests.test_setup_icon
Stdlib only, so it can join CI's no-install job.
"""
import base64
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SVG_PATH = os.path.join(HERE, "..", "..", "packaging", "windows", "aether-gate.svg")
ICO_PATH = os.path.join(HERE, "..", "..", "packaging", "windows", "aether-gate.ico")


def _icon_href(page):
    m = re.search(r'<link rel=icon type="image/svg\+xml" href="data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)">', page)
    return m.group(1) if m else None


def test_every_page_has_the_icon_in_its_head():
    from aether_gate import setup
    for name in ("PAGE", "AUTH_PAGE", "KNOWN_PAGE"):
        page = getattr(setup, name)
        head = page.split("</head>", 1)[0]
        assert _icon_href(head), f"{name}: no inline SVG icon link in <head>"
        assert head.count("rel=icon") == 1, f"{name}: icon link appears {head.count('rel=icon')} times"
    print("ok  PAGE, AUTH_PAGE and KNOWN_PAGE each carry one inline icon link in <head>")


def test_icon_is_the_shipped_svg():
    from aether_gate import setup
    with open(SVG_PATH, encoding="utf-8") as f:
        shipped = f.read().strip()
    for name in ("PAGE", "AUTH_PAGE", "KNOWN_PAGE"):
        svg = base64.b64decode(_icon_href(getattr(setup, name))).decode("utf-8")
        assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>"), f"{name}: data is not an SVG"
        assert svg == shipped, f"{name}: inline icon differs from packaging/windows/aether-gate.svg"
    print("ok  the inline icon decodes to packaging/windows/aether-gate.svg")


def test_windows_icon_has_the_usual_sizes():
    # An .ico starts with a 6-byte header (0, 1, count) and a 16-byte entry per image;
    # width/height 0 in an entry means 256.
    with open(ICO_PATH, "rb") as f:
        data = f.read()
    assert data[:4] == b"\x00\x00\x01\x00", "packaging/windows/aether-gate.ico is not an .ico"
    count = int.from_bytes(data[4:6], "little")
    sizes = sorted((data[6 + 16 * i] or 256) for i in range(count))
    for want in (16, 32, 48, 256):
        assert want in sizes, f"aether-gate.ico has no {want} px image (has {sizes})"
    print(f"ok  aether-gate.ico holds {sizes}")


def main():
    tests = [test_every_page_has_the_icon_in_its_head, test_icon_is_the_shipped_svg,
             test_windows_icon_has_the_usual_sizes]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} setup-icon tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
