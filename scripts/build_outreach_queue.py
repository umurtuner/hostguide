"""Build a copy-paste-ready Contact Host outreach queue for a given city.

Reads listings.json + hosts.json from hostguide/output/<city>/, dedupes against
outreach_crm/<city>_contacts.csv, and writes:

  outreach_crm/queue_<city>.md      - human-friendly queue (copy/paste)
  outreach_crm/queue_<city>.jsonl   - machine-readable log (one row per host)

We do NOT send anything. Airbnb bans auto-messaging fast. The workflow is:
  1. Run this script to build the queue
  2. Open Airbnb in your browser, profile-by-profile
  3. Copy the message block, paste into Contact Host form
  4. Mark sent in the CRM (run with --mark-sent <listing_id>)

Usage:
    python scripts/build_outreach_queue.py miami
    python scripts/build_outreach_queue.py miami --limit 20
    python scripts/build_outreach_queue.py miami --mark-sent 1486407707332155573
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT.parent))

from hostguide.src.outreach import generate_contact_host, SITE
from hostguide.src.scraper import Listing

OUTPUT = ROOT / "output"
CRM_DIR = ROOT / "outreach_crm"
CRM_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class QueueRow:
    listing_id: str
    host_name: str
    host_profile_url: str
    neighborhood: str
    listing_url: str
    message: str


def _load_listings(city: str) -> list[dict]:
    path = OUTPUT / city / "listings.json"
    if not path.exists():
        print(f"  [skip] {path} not found")
        return []
    return json.loads(path.read_text())


def _load_hosts(city: str) -> dict[str, dict]:
    """Return map listing_id -> host dict."""
    path = OUTPUT / city / "hosts.json"
    if not path.exists():
        return {}
    hosts = json.loads(path.read_text())
    return {h.get("listing_id", ""): h for h in hosts}


def _load_crm(city: str) -> set[str]:
    """Return set of listing_ids already contacted via contact_host channel."""
    path = CRM_DIR / f"{city}_contacts.csv"
    if not path.exists():
        return set()
    sent = set()
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("channel") == "contact_host" and row.get("status") in ("sent", "replied", "converted"):
                sent.add(row.get("listing_id", ""))
    return sent


def _append_crm(city: str, rows: list[QueueRow]):
    """Append pending rows to the CRM so we have a record they're queued."""
    path = CRM_DIR / f"{city}_contacts.csv"
    new_file = not path.exists()
    existing_keys = set()
    if not new_file:
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_keys.add((row.get("listing_id", ""), row.get("channel", "")))

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "listing_id", "host_name", "city", "channel", "status",
            "contacted_at", "guide_url", "email", "fb_profile", "ig_handle", "notes"
        ])
        if new_file:
            writer.writeheader()
        added = 0
        for r in rows:
            key = (r.listing_id, "contact_host")
            if key in existing_keys:
                continue
            writer.writerow({
                "listing_id": r.listing_id,
                "host_name": r.host_name,
                "city": city,
                "channel": "contact_host",
                "status": "pending",
                "contacted_at": "",
                "guide_url": "",
                "email": "",
                "fb_profile": r.host_profile_url,
                "ig_handle": "",
                "notes": r.neighborhood,
            })
            added += 1
    return added


def build(city: str, limit: int = 0) -> list[QueueRow]:
    listings_raw = _load_listings(city)
    if not listings_raw:
        return []
    hosts = _load_hosts(city)
    already = _load_crm(city)

    seen_hosts: set[str] = set()  # one message per host, not per listing
    queue: list[QueueRow] = []

    for raw in listings_raw:
        lid = raw.get("listing_id", "")
        if not lid or lid in already:
            continue

        host_id = raw.get("host_id", "")
        if host_id and host_id in seen_hosts:
            continue

        try:
            listing = Listing(**{k: v for k, v in raw.items() if k in Listing.__annotations__})
        except TypeError:
            listing = Listing(
                listing_id=lid,
                title=raw.get("title", ""),
                url=raw.get("url", ""),
                city=raw.get("city", city),
                neighborhood=raw.get("neighborhood", ""),
                host_name=raw.get("host_name", ""),
                host_id=host_id,
                host_profile_url=raw.get("host_profile_url", ""),
            )

        host_data = hosts.get(lid, {})
        profile_url = (
            host_data.get("airbnb_profile_url")
            or listing.host_profile_url
            or (f"https://www.airbnb.com/users/show/{host_id}" if host_id else "")
        )

        message = generate_contact_host(listing, guide_url=SITE)

        queue.append(QueueRow(
            listing_id=lid,
            host_name=listing.host_name or "Host",
            host_profile_url=profile_url,
            neighborhood=listing.neighborhood or listing.city,
            listing_url=listing.url,
            message=message,
        ))

        if host_id:
            seen_hosts.add(host_id)

        if limit and len(queue) >= limit:
            break

    return queue


def write_queue(city: str, queue: list[QueueRow]):
    md_path = CRM_DIR / f"queue_{city}.md"
    jsonl_path = CRM_DIR / f"queue_{city}.jsonl"

    lines = [
        f"# Contact Host outreach queue - {city.title()}",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Total: {len(queue)}",
        "",
        "## How to use",
        "1. Open each Airbnb profile URL below",
        "2. Click Contact Host on one of their listings",
        "3. Paste the message block as-is",
        "4. Send. Do no more than 20/day from one account.",
        "5. Mark sent in CRM: `python scripts/build_outreach_queue.py " + city + " --mark-sent <listing_id>`",
        "",
        "---",
        "",
    ]

    for i, r in enumerate(queue, 1):
        lines.append(f"## {i}. {r.host_name} - {r.neighborhood}")
        lines.append(f"- listing_id: `{r.listing_id}`")
        lines.append(f"- Profile: {r.host_profile_url or '(no profile URL)'}")
        lines.append(f"- Listing: {r.listing_url}")
        lines.append("")
        lines.append("```")
        lines.append(r.message)
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    md_path.write_text("\n".join(lines))

    with open(jsonl_path, "w") as f:
        for r in queue:
            f.write(json.dumps({
                "listing_id": r.listing_id,
                "host_name": r.host_name,
                "host_profile_url": r.host_profile_url,
                "neighborhood": r.neighborhood,
                "listing_url": r.listing_url,
                "message": r.message,
            }) + "\n")

    return md_path, jsonl_path


def mark_sent(city: str, listing_id: str) -> bool:
    path = CRM_DIR / f"{city}_contacts.csv"
    if not path.exists():
        print(f"  [err] {path} not found")
        return False

    rows = []
    found = False
    with open(path) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if row.get("listing_id") == listing_id and row.get("channel") == "contact_host":
                row["status"] = "sent"
                row["contacted_at"] = datetime.now().isoformat()
                found = True
            rows.append(row)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return found


def main():
    parser = argparse.ArgumentParser(description="Build Contact-Host outreach queue")
    parser.add_argument("city", help="City folder under output/ (e.g., miami, lisbon)")
    parser.add_argument("--limit", type=int, default=0, help="Max messages to queue")
    parser.add_argument("--mark-sent", metavar="LISTING_ID",
                        help="Mark a queued message as sent in the CRM")
    args = parser.parse_args()

    city = args.city.lower()

    if args.mark_sent:
        ok = mark_sent(city, args.mark_sent)
        print(f"  {'[ok]' if ok else '[err]'} mark_sent {args.mark_sent}")
        return

    queue = build(city, limit=args.limit)
    if not queue:
        print(f"  [empty] nothing to queue for {city}")
        return

    md_path, jsonl_path = write_queue(city, queue)
    added = _append_crm(city, queue)

    print(f"  [ok] {len(queue)} hosts queued ({added} new in CRM)")
    print(f"        {md_path}")
    print(f"        {jsonl_path}")


if __name__ == "__main__":
    main()
