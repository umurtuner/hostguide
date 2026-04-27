"""Open Reddit submit page for r/airbnb_hosts with launch post pre-pasted.

Reddit is heavy on Cloudflare and anti-bot. This script uses a persistent
profile (sign in once) and pauses if a challenge appears.

Run:
    python scripts/post_reddit_compose.py
    python scripts/post_reddit_compose.py --subreddit airbnb
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from copy_post import POSTS
from _compose_helpers import (hold_open, launch_browser, paste_into,
                              try_submit, wait_for_login, wait_if_challenged)

PROFILE_DIR = ROOT / "chrome_profile_reddit"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subreddit", default="airbnb_hosts",
                        help="subreddit to submit to (default: airbnb_hosts)")
    parser.add_argument("--submit", action="store_true",
                        help="auto-click Post after paste (Reddit may shadowban — use sparingly)")
    args = parser.parse_args()

    post = POSTS["reddit"]
    title = post["title"]
    body = post["body"]
    url = f"https://www.reddit.com/r/{args.subreddit}/submit"

    print(f"[..] post: reddit -> r/{args.subreddit}")
    print(f"[..] title: {len(title)} chars / body: {len(body)} chars")

    p, ctx = launch_browser(PROFILE_DIR)
    try:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)

        wait_if_challenged(page, "reddit submit")

        if "/submit" not in page.url:
            wait_for_login(page, "/submit", "reddit")
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(3)
            wait_if_challenged(page, "reddit submit")

        title_sels = [
            'textarea[name="title"]',
            'textarea[placeholder*="title" i]',
            '[name="title"]',
            'input[placeholder*="title" i]',
        ]
        body_sels = [
            '[name="text"]',
            'textarea[placeholder*="text" i]',
            '[contenteditable="true"]',
            'div[role="textbox"]',
        ]

        paste_into(page, title_sels, title, "title")
        time.sleep(1)

        # Reddit may require clicking "Text Post" tab first
        for tab_sel in ['button:has-text("Text Post")', 'button:has-text("Post")']:
            try:
                tab = page.locator(tab_sel).first
                if tab.is_visible(timeout=1500):
                    tab.click()
                    time.sleep(1)
                    break
            except Exception:
                pass

        paste_into(page, body_sels, body, "body")

        if args.submit:
            time.sleep(1)
            try_submit(page, [
                'button:has-text("Post")',
                'button:has-text("Submit")',
                '[role="button"]:has-text("Post")',
            ], "reddit submit")
            time.sleep(5)
        else:
            hold_open()
    finally:
        ctx.close()
        p.stop()


if __name__ == "__main__":
    main()
