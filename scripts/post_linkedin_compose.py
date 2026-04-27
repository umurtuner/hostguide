"""Open LinkedIn composer with the launch announcement pre-pasted.

Pattern (matches scripts/post_fb_groups.py): opens a persistent Chrome
profile, navigates to the composer, pastes the text via clipboard, then
LEAVES THE BROWSER OPEN. You review and click Post manually.

First run: the browser will open and ask you to log in to LinkedIn. Do
that once. The session persists in chrome_profile_linkedin/ for next time.

Run:
    python scripts/post_linkedin_compose.py
    python scripts/post_linkedin_compose.py --post=teaser
    python scripts/post_linkedin_compose.py --text-file path/to/custom.txt
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[err] playwright not installed - pip install playwright && playwright install chromium",
          file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).parent.parent
PROFILE_DIR = ROOT / "chrome_profile_linkedin"

PH_URL = "https://www.producthunt.com/products/hostguide-2"

POSTS = {
    "launch": f"""My 47th "where's the grocery store?" message broke me. So I built HostGuide.

I host on Airbnb in Geneva. Every week, the same questions: where's the metro, where's coffee, where's the beach. My welcome book had answers. Nobody read it - 12 pages of Canva, outdated within a month, generic by design.

So I built a tool that generates a printable neighborhood guide from any Airbnb listing URL in 60 seconds. Walking times to transit and groceries, top-rated cafes within 10 minutes, local ride apps (Bolt, Grab, Careem), tipping norms, emergency numbers, and a QR code guests scan for the digital version. All tailored to the exact lat/lng of the place.

My guest messages dropped by 70%. Reviews started mentioning "the guide was so helpful."

It's launching on Product Hunt on Tuesday May 12 as HostGuide. Side project - I still run MarTech for Pampers by day - but it solves a real problem I had.

If you host on Airbnb, or know someone who does, you can follow the coming-soon page so you get a ping when it ships:

{PH_URL}

First guide on the house for everyone who follows.""",

    "teaser": f"""Quietly shipping a side project on Tuesday May 12.

Six months of nights and weekends building HostGuide - a tool that generates printable neighborhood welcome books for Airbnb hosts in 60 seconds.

Coming-soon page if you want a ping at ship time:
{PH_URL}""",

    "comment": "P.S. - if you want to test it before launch, drop your Airbnb URL in a reply and I'll generate a guide for it tonight.",
}


def copy_to_clipboard(text: str) -> None:
    """macOS pbcopy. On Linux/Windows, swap for xclip / clip."""
    if sys.platform != "darwin":
        print("[warn] non-macOS - clipboard copy may not work, paste manually if needed",
              file=sys.stderr)
        return
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf-8"))


def wait_for_login(page) -> None:
    """Poll until the user lands on /feed/. First-run login flow."""
    print("\n[!] If LinkedIn shows the login page, sign in now in the browser window.")
    print("    Waiting up to 5 minutes for /feed/ ...")
    for _ in range(60):  # 5 min
        url = page.url
        if "/feed/" in url or "/in/" in url:
            print("[ok] logged in.")
            return
        time.sleep(5)
    print("[err] timeout waiting for login. Re-run after signing in.", file=sys.stderr)
    sys.exit(1)


def open_composer(page) -> bool:
    """Click Start a post and wait for the composer modal."""
    print("[..] opening composer ...")
    selectors = [
        'button.share-box-feed-entry__trigger',
        'button:has-text("Start a post")',
        'button[aria-label*="Start a post"]',
        '[aria-label="Start a post"]',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                btn.click()
                time.sleep(2)
                # Composer modal has a contenteditable
                editable = page.locator('[contenteditable="true"]').first
                if editable.is_visible(timeout=5000):
                    editable.click()
                    time.sleep(1)
                    print("[ok] composer open and focused.")
                    return True
        except Exception:
            continue
    print("[err] could not open composer - LinkedIn UI may have changed.", file=sys.stderr)
    return False


def paste(page, text: str) -> None:
    """Paste from system clipboard. Falls back to typing if needed."""
    copy_to_clipboard(text)
    time.sleep(0.5)
    if sys.platform == "darwin":
        page.keyboard.press("Meta+V")
        time.sleep(1)
        print("[ok] pasted text via clipboard.")
    else:
        page.keyboard.type(text, delay=10)
        print("[ok] typed text directly.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--post", choices=list(POSTS.keys()), default="launch",
                        help="which canned post to compose (default: launch)")
    parser.add_argument("--text-file", help="path to a file with custom text")
    args = parser.parse_args()

    if args.text_file:
        text = Path(args.text_file).read_text()
    else:
        text = POSTS[args.post]

    PROFILE_DIR.mkdir(exist_ok=True)
    print(f"[..] profile dir: {PROFILE_DIR}")
    print(f"[..] post type:   {args.post} ({len(text)} chars)")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        page.goto("https://www.linkedin.com/feed/")
        time.sleep(3)

        if "/feed/" not in page.url and "/in/" not in page.url:
            wait_for_login(page)

        if not open_composer(page):
            print("\n[!] Composer didn't open automatically. The browser is staying open -")
            print("    click 'Start a post' yourself, then paste from clipboard (Cmd+V).")
            copy_to_clipboard(text)
        else:
            paste(page, text)

        print("\n" + "=" * 60)
        print("COMPOSER READY. Review the text. Click POST when satisfied.")
        print("Browser will stay open. Kill this terminal (Ctrl+C) when done.")
        print("=" * 60 + "\n")

        # Hold the browser open
        try:
            while True:
                time.sleep(30)
        except KeyboardInterrupt:
            print("\n[ok] closing browser.")
            ctx.close()


if __name__ == "__main__":
    main()
