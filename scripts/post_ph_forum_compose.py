"""Open Product Hunt forum thread composer for p/hostguide-2 with post pre-pasted.

Run:
    python scripts/post_ph_forum_compose.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from copy_post import POSTS
from _compose_helpers import (hold_open, launch_browser, paste_into,
                              wait_for_login, wait_if_challenged)

PROFILE_DIR = ROOT / "chrome_profile_ph"
FORUM_URL = "https://www.producthunt.com/p/hostguide-2"


def main():
    post = POSTS["ph_forum"]
    title = post["title"]
    body = post["body"]

    print(f"[..] post: PH forum -> {FORUM_URL}")
    print(f"[..] title: {len(title)} chars / body: {len(body)} chars")

    p, ctx = launch_browser(PROFILE_DIR)
    try:
        page = ctx.new_page()
        page.goto(FORUM_URL, wait_until="domcontentloaded")
        time.sleep(3)

        wait_if_challenged(page, "PH forum")

        if "producthunt.com" not in page.url or "/login" in page.url:
            wait_for_login(page, "producthunt.com", "PH")
            page.goto(FORUM_URL, wait_until="domcontentloaded")
            time.sleep(3)

        # Try to find and click "Start new thread" / "New post" button
        for sel in [
            'button:has-text("Start new thread")',
            'a:has-text("Start new thread")',
            'button:has-text("New post")',
            'button:has-text("Create thread")',
            '[aria-label*="new thread" i]',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2500):
                    btn.click()
                    time.sleep(2)
                    print(f"[ok] clicked: {sel}")
                    break
            except Exception:
                continue

        wait_if_challenged(page, "PH thread composer")

        title_sels = [
            'input[placeholder*="title" i]',
            'input[name*="title" i]',
            'textarea[placeholder*="title" i]',
        ]
        body_sels = [
            'textarea[placeholder*="describe" i]',
            'textarea[placeholder*="thoughts" i]',
            'textarea[placeholder*="post" i]',
            '[contenteditable="true"]',
            'textarea',
        ]

        paste_into(page, title_sels, title, "title")
        time.sleep(1)
        paste_into(page, body_sels, body, "body")
        hold_open()
    finally:
        ctx.close()
        p.stop()


if __name__ == "__main__":
    main()
