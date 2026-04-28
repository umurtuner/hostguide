"""Step through today's Airbnb Contact-Host queue with the message pre-pasted.

WHY composer-prep, never auto-submit: Airbnb bans auto-messaging within
hours. A ban affects your hosting income + the scraper data source for
the entire HostGuide product. Existing scraper docstring is explicit.

Flow:
  1. Read CRM CSVs for rows with status='queued_today' (set by
     daily_outreach.py)
  2. Open browser at the first listing URL using the existing
     chrome_profile_airbnb session (must be logged in)
  3. Try to auto-click "Contact host" + paste the personalized message
  4. WAIT for you to click Send manually + press Enter in this terminal
  5. Mark that listing as 'sent' in the CRM
  6. Open the next listing
  7. After every 10 sends, prompt for a 5-minute break (Airbnb anti-spam)

Run:
    python scripts/post_airbnb_compose.py
    python scripts/post_airbnb_compose.py --max 10
    python scripts/post_airbnb_compose.py --status queued_today
    python scripts/post_airbnb_compose.py --status pending --max 5
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from _compose_helpers import (copy_to_clipboard, launch_browser, paste_into,
                              wait_if_challenged)

CRM_DIR = ROOT / "outreach_crm"
PROFILE_DIR = ROOT / "chrome_profile_airbnb"

PAUSE_AFTER_N = 10  # take a 5-min break after every N sends
PAUSE_SEC = 300


def load_today_queue(status_filter: str = "queued_today") -> list[dict]:
    """Return list of (city, listing_id, host_name, neighborhood,
    listing_url, message) dicts for all CRM rows matching status_filter."""
    by_id: dict[str, dict] = {}

    # First pass: messages live in queue_<city>.jsonl
    for jf in CRM_DIR.glob("queue_*.jsonl"):
        city = jf.stem.replace("queue_", "")
        for line in jf.open():
            if not line.strip():
                continue
            row = json.loads(line)
            row["city"] = city
            by_id[row["listing_id"]] = row

    # Second pass: filter by CRM status
    out: list[dict] = []
    for cf in CRM_DIR.glob("*_contacts.csv"):
        with cf.open() as f:
            for row in csv.DictReader(f):
                if (row.get("channel") == "contact_host"
                        and row.get("status") == status_filter
                        and row.get("listing_id") in by_id):
                    out.append(by_id[row["listing_id"]])
    return out


def mark_sent(city: str, listing_id: str) -> bool:
    path = CRM_DIR / f"{city}_contacts.csv"
    if not path.exists():
        return False
    rows = []
    found = False
    with path.open() as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if (row.get("listing_id") == listing_id
                    and row.get("channel") == "contact_host"
                    and row.get("status") in ("queued_today", "pending")):
                row["status"] = "sent"
                row["contacted_at"] = datetime.now().isoformat()
                found = True
            rows.append(row)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return found


def open_contact_host(page, listing_url: str, message: str) -> bool:
    """Navigate to listing, click Contact host, paste message. Returns
    True if message text was placed in the form."""
    page.goto(listing_url, wait_until="domcontentloaded")
    time.sleep(3)
    wait_if_challenged(page, "airbnb listing")

    contact_selectors = [
        'a[href*="/contact_host"]',
        'button:has-text("Contact host")',
        'a:has-text("Contact host")',
        'button:has-text("Contact Host")',
        '[data-testid*="contact"]',
    ]
    clicked = False
    for sel in contact_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2500):
                btn.scroll_into_view_if_needed()
                time.sleep(0.5)
                btn.click()
                time.sleep(3)
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        print("  [warn] could not auto-click Contact host. Click it yourself.")
        copy_to_clipboard(message)
        return False

    wait_if_challenged(page, "contact-host form")

    body_selectors = [
        'textarea[name="message"]',
        'textarea[placeholder*="message" i]',
        'textarea',
        '[contenteditable="true"]',
    ]
    return paste_into(page, body_selectors, message, "message")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", default="queued_today",
                        choices=["queued_today", "pending"],
                        help="which CRM status to step through")
    parser.add_argument("--max", type=int, default=0,
                        help="cap on this session (0 = whole queue)")
    args = parser.parse_args()

    queue = load_today_queue(args.status)
    if not queue:
        print(f"[empty] no CRM rows with status='{args.status}'.")
        return

    if args.max:
        queue = queue[:args.max]

    print(f"\n{'=' * 60}")
    print(f"AIRBNB CONTACT-HOST STEP-THROUGH ({len(queue)} hosts)")
    print(f"{'=' * 60}")
    print("For each host:")
    print("  1. Browser opens the listing + contact form, pastes message.")
    print("  2. YOU click Send in the browser.")
    print("  3. Switch to this terminal and press Enter to advance.")
    print(f"  4. After every {PAUSE_AFTER_N} sends, {PAUSE_SEC // 60}min break.")
    print()
    print("Type 's' + Enter to skip a host (without marking sent).")
    print("Type 'q' + Enter to quit early.")
    print("Type 'm' + Enter to mark sent + advance (use if Send button worked).")
    print()

    p, ctx = launch_browser(PROFILE_DIR)
    sent_count = 0
    skipped: list[str] = []
    try:
        page = ctx.new_page()
        page.goto("https://www.airbnb.com/", wait_until="domcontentloaded")
        time.sleep(3)
        wait_if_challenged(page, "airbnb home")
        if "/login" in page.url or "signup" in page.url.lower():
            print("[!] Sign in to Airbnb in the browser, then press Enter here.")
            input()

        for i, q in enumerate(queue, 1):
            print(f"\n--- {i}/{len(queue)}: {q['host_name']} ({q['neighborhood']}, {q['city']}) ---")
            print(f"    listing: {q.get('listing_url', '')}")

            ok = open_contact_host(page, q["listing_url"], q["message"])
            if ok:
                print("    [ok] message pasted. Click Send in browser.")
            else:
                print("    [warn] auto-paste failed. Message on clipboard - paste manually.")

            choice = input("    [Enter=mark sent / s=skip / q=quit] > ").strip().lower()
            if choice == "q":
                print("    quitting early.")
                break
            if choice == "s":
                skipped.append(q["listing_id"])
                continue

            if mark_sent(q["city"], q["listing_id"]):
                sent_count += 1
                print(f"    [ok] marked sent. ({sent_count} this session)")
            else:
                print(f"    [warn] could not mark {q['listing_id']} sent in CRM")

            if sent_count > 0 and sent_count % PAUSE_AFTER_N == 0 and i < len(queue):
                print(f"\n[break] {PAUSE_AFTER_N} sent. Pausing {PAUSE_SEC // 60} min for anti-spam.")
                print("        Press Enter to skip the break, or Ctrl+C to stop.")
                try:
                    for _ in range(PAUSE_SEC):
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass

    finally:
        ctx.close()
        p.stop()

    print(f"\n{'=' * 60}")
    print(f"SESSION COMPLETE: {sent_count} sent, {len(skipped)} skipped")
    if skipped:
        print(f"Skipped IDs: {', '.join(skipped[:5])}{'...' if len(skipped) > 5 else ''}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
