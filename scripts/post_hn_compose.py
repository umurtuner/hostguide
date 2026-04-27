"""Open Hacker News submit page with Show HN post pre-pasted.

Use as a backup channel if PH gets snowed under on launch day.

Run:
    python scripts/post_hn_compose.py
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

PROFILE_DIR = ROOT / "chrome_profile_hn"
SUBMIT_URL = "https://news.ycombinator.com/submit"


def main():
    post = POSTS["hn"]
    title = post["title"]
    body = post["body"]

    print(f"[..] post: HN -> {SUBMIT_URL}")
    print(f"[..] title: {len(title)} chars / body: {len(body)} chars")

    p, ctx = launch_browser(PROFILE_DIR)
    try:
        page = ctx.new_page()
        page.goto(SUBMIT_URL, wait_until="domcontentloaded")
        time.sleep(2)

        wait_if_challenged(page, "HN")

        if "/login" in page.url or "/submit" not in page.url:
            wait_for_login(page, "/submit", "HN")
            page.goto(SUBMIT_URL, wait_until="domcontentloaded")
            time.sleep(2)

        # HN form is dead simple: input[name=title], input[name=url], textarea[name=text]
        paste_into(page, ['input[name="title"]'], title, "title")
        time.sleep(0.5)
        # Show HN posts are text-only, no URL field, body goes in textarea
        paste_into(page, ['textarea[name="text"]'], body, "body")
        hold_open()
    finally:
        ctx.close()
        p.stop()


if __name__ == "__main__":
    main()
