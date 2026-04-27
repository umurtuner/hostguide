"""Launch-day blast: post the launch announcement across every channel.

Runs each channel's composer-prep script in sequence with --submit. If a
channel hits Cloudflare or a CAPTCHA, the underlying script pauses for
human-in-loop. Spaced 90s apart so each finishes before the next opens.

Channels included (auto-submit):
  - X      (post_x_compose.py)
  - IH     (post_ih_compose.py)
  - HN     (post_hn_compose.py)        - backup, only if --include-hn
  - Reddit (post_reddit_compose.py)    - sparingly, --include-reddit

NOT included (manual only):
  - PH forum   (PH staff actively monitor for automation - composer only)
  - LinkedIn   (skipped per P&G rule, see memory)
  - Airbnb Community (existing thread URLs, manual reply)

Run on launch morning (recommended ~07:05 UTC, just after PH goes live):
    python scripts/launch_day_blast.py
    python scripts/launch_day_blast.py --include-reddit --include-hn
    python scripts/launch_day_blast.py --dry-run

Pre-flight: each channel needs its chrome_profile_<channel>/ already
logged in. Run each composer once without --submit before launch day to
seed the session.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "scripts"

CHANNELS = [
    {
        "name": "x_launch",
        "script": "post_x_compose.py",
        "args": ["--post=launch_short", "--submit"],
        "wait_after_sec": 90,
    },
    {
        "name": "ih_softlaunch",
        "script": "post_ih_compose.py",
        "args": ["--submit"],
        "wait_after_sec": 90,
    },
]

OPTIONAL = {
    "reddit": {
        "name": "reddit_airbnb_hosts",
        "script": "post_reddit_compose.py",
        "args": ["--subreddit=airbnb_hosts", "--submit"],
        "wait_after_sec": 120,
    },
    "hn": {
        "name": "hn_show",
        "script": "post_hn_compose.py",
        "args": ["--submit"],
        "wait_after_sec": 90,
    },
}


def run_channel(ch: dict, dry_run: bool) -> int:
    cmd = [sys.executable, str(SCRIPTS / ch["script"])] + ch["args"]
    print(f"\n{'=' * 60}")
    print(f"[blast] {ch['name']}: {' '.join(cmd[1:])}")
    print(f"{'=' * 60}")

    if dry_run:
        print("[dry-run] would execute, skipping.")
        return 0

    try:
        r = subprocess.run(cmd, cwd=str(ROOT), timeout=600)
        return r.returncode
    except subprocess.TimeoutExpired:
        print(f"[err] {ch['name']} timed out after 10 min", file=sys.stderr)
        return 124


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-reddit", action="store_true",
                        help="add r/airbnb_hosts post (Reddit may shadowban)")
    parser.add_argument("--include-hn", action="store_true",
                        help="add Show HN backup post")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    plan = list(CHANNELS)
    if args.include_reddit:
        plan.append(OPTIONAL["reddit"])
    if args.include_hn:
        plan.append(OPTIONAL["hn"])

    print(f"\nLaunch-day blast plan: {len(plan)} channel(s)")
    for c in plan:
        print(f"  - {c['name']}  ({c['script']})")
    print()

    fails = []
    for i, ch in enumerate(plan):
        rc = run_channel(ch, dry_run=args.dry_run)
        if rc != 0:
            fails.append(ch["name"])
            print(f"[fail] {ch['name']} returned {rc}")
        if i < len(plan) - 1 and not args.dry_run:
            wait = ch["wait_after_sec"]
            print(f"\n[wait] {wait}s before next channel ...")
            time.sleep(wait)

    print("\n" + "=" * 60)
    print(f"Blast complete. {len(plan) - len(fails)}/{len(plan)} channels succeeded.")
    if fails:
        print(f"Failures: {', '.join(fails)} - re-run those manually.")
    print("=" * 60 + "\n")
    print("Manual still required:")
    print("  - PH forum thread (use scripts/post_ph_forum_compose.py)")
    print("  - PH product page comments throughout the day")
    print("  - Airbnb Community thread updates (existing 2 threads)")


if __name__ == "__main__":
    main()
