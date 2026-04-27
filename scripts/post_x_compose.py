"""Open X (Twitter) composer with launch content pre-pasted.

Same pattern as scripts/post_linkedin_compose.py and post_fb_groups.py:
opens persistent Chrome profile, navigates to the X composer, pastes
text via clipboard, leaves browser open. You review and click Post.

X allows long posts (Premium) and threads. This script handles single
posts. For the 5-tweet thread, run with --post=thread1, post it, then
re-run with --post=thread2 (etc.) and reply to your own previous tweet.

First run: log in to X manually in the popped browser.

Run:
    python scripts/post_x_compose.py                  # default: launch_short
    python scripts/post_x_compose.py --post=teaser
    python scripts/post_x_compose.py --post=thread1
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
    print("[err] playwright not installed", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).parent.parent
PROFILE_DIR = ROOT / "chrome_profile_x"

PH_URL = "https://www.producthunt.com/products/hostguide-2"
SITE = "https://www.host-guide.net"

POSTS = {
    "teaser": f"""Quietly shipping a side project on PH tomorrow (Tue Apr 28).

HostGuide - generates printable Airbnb welcome books from any listing URL in 60s.

Coming-soon page: {PH_URL}""",

    "launch_short": f"""I just launched HostGuide on Product Hunt.

Paste any Airbnb URL, get a printable neighborhood guide for your guests in 60 seconds. No more "where's the grocery store?" messages.

{PH_URL}""",

    "thread1": """I just launched HostGuide on Product Hunt.

Paste any Airbnb URL, get a printable neighborhood guide for your guests in 60 seconds. No more "where's the grocery store?" messages.""",

    "thread2": """Why I built it:

I host in Geneva and got tired of answering the same guest questions every week. My Canva welcome book was outdated within a month. Every "city guide" on Google is SEO spam.

So I made one that's personalized to each listing's exact lat/lng.""",

    "thread3": """What's inside:
- Walking times to metro and groceries
- Top-rated cafes and restaurants within 10min
- Local ride apps (Bolt, Grab, Careem)
- Tipping and emergency numbers
- QR code guests scan to get the digital version

All in a branded PDF you drop in your welcome book.""",

    "thread4": f"""Pricing: $4.99 one-time, or $14.99 for a 5-pack. No subscription.

Works in 30+ countries so far. If you host anywhere in the world, drop your listing URL below and I'll test yours live.""",

    "thread5": f"""PH launch: {PH_URL}
Site: {SITE}

Back to answering PH comments.""",

    "rank_update": f"""HostGuide is currently #X on Product Hunt today.

If you've ever hosted on Airbnb and wished your welcome book actually answered the questions guests ask, this one's for you: {PH_URL}""",
}


def copy_to_clipboard(text: str) -> None:
    if sys.platform != "darwin":
        return
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf-8"))


def wait_for_login(page) -> None:
    print("\n[!] If X shows the login page, sign in now in the browser window.")
    print("    Waiting up to 5 minutes for /home ...")
    for _ in range(60):
        if "/home" in page.url or "/compose" in page.url:
            print("[ok] logged in.")
            return
        time.sleep(5)
    print("[err] timeout waiting for login.", file=sys.stderr)
    sys.exit(1)


def open_composer(page) -> bool:
    print("[..] navigating to composer ...")
    page.goto("https://x.com/compose/post")
    time.sleep(3)
    selectors = [
        '[data-testid="tweetTextarea_0"]',
        '[role="textbox"]',
        'div[contenteditable="true"]',
    ]
    for sel in selectors:
        try:
            box = page.locator(sel).first
            if box.is_visible(timeout=5000):
                box.click()
                time.sleep(1)
                print("[ok] composer focused.")
                return True
        except Exception:
            continue
    return False


def paste(page, text: str) -> None:
    copy_to_clipboard(text)
    time.sleep(0.5)
    if sys.platform == "darwin":
        page.keyboard.press("Meta+V")
        time.sleep(1)
        print(f"[ok] pasted {len(text)} chars via clipboard.")
    else:
        page.keyboard.type(text, delay=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--post", choices=list(POSTS.keys()), default="launch_short",
                        help="canned post name (default: launch_short)")
    parser.add_argument("--text-file", help="path to file with custom text")
    parser.add_argument("--submit", action="store_true",
                        help="auto-click Post after pasting (use for launch-day blast)")
    args = parser.parse_args()

    text = Path(args.text_file).read_text() if args.text_file else POSTS[args.post]

    PROFILE_DIR.mkdir(exist_ok=True)
    print(f"[..] profile:  {PROFILE_DIR}")
    print(f"[..] post:     {args.post} ({len(text)} chars)")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        page.goto("https://x.com/home")
        time.sleep(3)

        if "/home" not in page.url and "/compose" not in page.url:
            wait_for_login(page)

        opened = open_composer(page)
        if not opened:
            print("\n[!] Composer didn't open. Browser stays open - paste from clipboard manually.")
            copy_to_clipboard(text)
        else:
            paste(page, text)

        if args.submit and opened:
            time.sleep(1)
            for sel in ['[data-testid="tweetButton"]', 'button:has-text("Post")']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=3000) and btn.is_enabled(timeout=1000):
                        btn.click()
                        time.sleep(3)
                        print(f"[ok] clicked Post: {sel}")
                        break
                except Exception:
                    continue
            print("[ok] submitted. Closing in 5s.")
            time.sleep(5)
            ctx.close()
            return

        print("\n" + "=" * 60)
        print("COMPOSER READY. Review. Click POST when satisfied.")
        print("Browser stays open. Ctrl+C this terminal when done.")
        print("=" * 60 + "\n")
        try:
            while True:
                time.sleep(30)
        except KeyboardInterrupt:
            ctx.close()


if __name__ == "__main__":
    main()
