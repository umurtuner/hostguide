"""Open Indie Hackers Tasks/Milestones with launch post pre-pasted.

Run:
    python scripts/post_ih_compose.py
    python scripts/post_ih_compose.py --section milestones
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
                              wait_for_login, wait_if_challenged)

PROFILE_DIR = ROOT / "chrome_profile_ih"

SECTION_URLS = {
    "tasks": "https://www.indiehackers.com/tasks/post",
    "milestones": "https://www.indiehackers.com/milestones/post",
    "default": "https://www.indiehackers.com/post",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--section", choices=list(SECTION_URLS.keys()), default="default")
    args = parser.parse_args()

    post = POSTS["ih"]
    title = post["title"]
    body = post["body"]
    url = SECTION_URLS[args.section]

    print(f"[..] post: ih -> {url}")
    print(f"[..] title: {len(title)} chars / body: {len(body)} chars")

    p, ctx = launch_browser(PROFILE_DIR)
    try:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)

        wait_if_challenged(page, "indie hackers")

        if "/post" not in page.url and "/milestones" not in page.url and "/tasks" not in page.url:
            wait_for_login(page, "indiehackers.com", "indie hackers")
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(3)

        title_sels = [
            'input[name="title"]',
            'input[placeholder*="title" i]',
            '[data-testid="title-input"]',
        ]
        body_sels = [
            'textarea[name="body"]',
            'textarea[placeholder*="describe" i]',
            'textarea[placeholder*="content" i]',
            '[contenteditable="true"]',
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
