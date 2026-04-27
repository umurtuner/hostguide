"""Shared helpers for the post_*_compose.py family.

Each composer script handles one channel (Reddit, IH, PH forum, HN, etc).
This module centralises:
  - clipboard copy (pbcopy on macOS, xclip on Linux)
  - Cloudflare / CAPTCHA detection with human-in-loop pause
  - launch the persistent Chrome context with anti-detection flags
  - "leave open" loop after composer is ready
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:
    print("[err] playwright not installed - pip install playwright && playwright install chromium",
          file=sys.stderr)
    sys.exit(1)


def copy_to_clipboard(text: str) -> bool:
    if sys.platform == "darwin":
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(text.encode("utf-8"))
        return True
    if sys.platform.startswith("linux"):
        try:
            p = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"))
            return True
        except FileNotFoundError:
            return False
    return False


def paste_into(page: Page, selectors: list[str], text: str, label: str) -> bool:
    """Click into the first matching selector and paste via clipboard."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=3000):
                el.click()
                time.sleep(0.5)
                el.fill("")  # clear any placeholder content; ignored if contenteditable
                copy_to_clipboard(text)
                time.sleep(0.3)
                if sys.platform == "darwin":
                    page.keyboard.press("Meta+V")
                else:
                    page.keyboard.press("Control+V")
                time.sleep(0.7)
                print(f"[ok] pasted {label} ({len(text)} chars) into {sel}")
                return True
        except Exception:
            continue
    print(f"[warn] could not auto-find {label} field; text on clipboard, paste manually.")
    copy_to_clipboard(text)
    return False


CHALLENGE_SIGNALS = [
    "challenges.cloudflare",
    "/cdn-cgi/challenge",
    "cf-chl-",
    "captcha",
    "just a moment",  # Cloudflare default title
    "verifying you are human",
    "press and hold",  # PerimeterX style
    "are you a robot",
]


def wait_if_challenged(page: Page, label: str = "page", max_sec: int = 300) -> None:
    """If a CF/CAPTCHA/challenge page is detected, pause and let user solve.

    Polls every 5s for up to max_sec. Returns as soon as the challenge clears.
    """
    start = time.time()
    warned = False
    while time.time() - start < max_sec:
        url = (page.url or "").lower()
        try:
            title = (page.title() or "").lower()
        except Exception:
            title = ""
        if not any(s in url or s in title for s in CHALLENGE_SIGNALS):
            if warned:
                print(f"[ok] {label} challenge cleared.")
            return
        if not warned:
            print(f"\n[!] CHALLENGE on {label}. Solve it in the browser - polling every 5s.")
            warned = True
        time.sleep(5)
    print(f"[err] {label} challenge not cleared after {max_sec}s; continuing anyway.",
          file=sys.stderr)


def wait_for_login(page: Page, expected_url_fragment: str, label: str, max_sec: int = 300) -> None:
    """Block until the URL contains expected_url_fragment (i.e. user is past login)."""
    start = time.time()
    warned = False
    while time.time() - start < max_sec:
        if expected_url_fragment in (page.url or ""):
            if warned:
                print(f"[ok] {label} login complete.")
            return
        if not warned:
            print(f"\n[!] {label} appears to require login. Sign in in the browser.")
            warned = True
        time.sleep(5)
    print(f"[err] {label} login timed out after {max_sec}s.", file=sys.stderr)


def launch_browser(profile_dir: Path):
    """Persistent Chromium with anti-detection flags. Caller must close ctx."""
    profile_dir.mkdir(exist_ok=True)
    p = sync_playwright().start()
    ctx = p.chromium.launch_persistent_context(
        str(profile_dir),
        headless=False,
        viewport={"width": 1280, "height": 900},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )
    return p, ctx


def hold_open():
    """Block until Ctrl+C. Used after composer is pre-pasted."""
    print("\n" + "=" * 60)
    print("COMPOSER READY. Review. Click POST/SUBMIT when satisfied.")
    print("Browser stays open. Ctrl+C this terminal when done.")
    print("=" * 60 + "\n")
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n[ok] closing browser.")
