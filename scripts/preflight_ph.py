"""Pre-flight check before Product Hunt launch.

Run this T-48, T-24, and morning-of. Prints PASS/FAIL for every gate so we
catch missing assets, broken endpoints, or stale env config before launch.

Run:  python scripts/preflight_ph.py
"""
from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
SITE = "https://www.host-guide.net"

REQUIRED_PH_ASSETS = [
    "static/ph/logo_240.png",
    "static/ph/gallery_1_hero.png",
    "static/ph/gallery_2_og.png",
    "static/ph/gallery_3_guide.png",
    "static/ph/gallery_4_qr.png",
]

OPTIONAL_PH_ASSETS = [
    ("static/ph/gallery_5_dashboard.png", "dashboard screenshot (2x conversion)"),
    ("static/ph/demo.gif", "20s screen recording (2x conversion)"),
]

REQUIRED_RENDER_ENV = [
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "HOSTGUIDE_DOMAIN",
    "HOSTGUIDE_ADMIN_SECRET",
    "ANTHROPIC_API_KEY",
]


def _check_url(url: str, expect: int = 200) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "preflight"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = r.status == expect
            return ok, f"{r.status}"
    except Exception as e:
        return False, str(e)[:80]


def _line(label: str, ok: bool, detail: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))


def check_assets() -> bool:
    print("\nAssets")
    all_ok = True
    for rel in REQUIRED_PH_ASSETS:
        p = ROOT / rel
        ok = p.exists() and p.stat().st_size > 0
        _line(rel, ok, "" if ok else "missing")
        all_ok = all_ok and ok
    for rel, why in OPTIONAL_PH_ASSETS:
        p = ROOT / rel
        ok = p.exists() and p.stat().st_size > 0
        mark = "OK" if ok else "OPT"
        print(f"  [{mark}] {rel} - {why}")
    return all_ok


def check_site() -> bool:
    print("\nSite")
    all_ok = True
    for path, expect in [("/", 200), ("/static/ph/logo_240.png", 200)]:
        ok, detail = _check_url(SITE + path, expect)
        _line(SITE + path, ok, detail)
        all_ok = all_ok and ok
    return all_ok


def check_env_hint() -> bool:
    print("\nLocal env hints (for Render parity check)")
    for key in REQUIRED_RENDER_ENV:
        present = bool(os.environ.get(key))
        mark = "SET" if present else "UNSET"
        note = "" if present else "verify on Render dashboard, not required locally"
        print(f"  [{mark}] {key} {note}")
    return True


def check_git_clean() -> bool:
    print("\nGit")
    import subprocess
    r = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                       capture_output=True, text=True)
    clean = not r.stdout.strip()
    _line("working tree clean", clean,
          "uncommitted changes - launch should run from a tagged commit"
          if not clean else "")
    r2 = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%h %s"],
                        capture_output=True, text=True)
    print(f"  [INFO] HEAD: {r2.stdout.strip()}")
    return clean


def main():
    print(f"HostGuide Product Hunt pre-flight - {SITE}")
    results = [
        ("assets", check_assets()),
        ("site", check_site()),
        ("env", check_env_hint()),
        ("git", check_git_clean()),
    ]
    print("\nSummary")
    for name, ok in results:
        _line(name, ok)
    fail = [n for n, ok in results if not ok and n != "env"]
    if fail:
        print(f"\n{len(fail)} gate(s) failed: {', '.join(fail)}")
        sys.exit(1)
    print("\nAll critical gates passed.")


if __name__ == "__main__":
    main()
