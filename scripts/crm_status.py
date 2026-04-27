"""CRM status dashboard - one shot view across all city queues.

Run:  python scripts/crm_status.py
      python scripts/crm_status.py --city miami
      python scripts/crm_status.py --pending      # only show cities with pending work
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
CRM_DIR = ROOT / "outreach_crm"

TIERS = {
    "A": ["miami", "lisbon", "madrid", "austin"],
    "B": ["medellin", "bogota", "tampa", "orlando"],
    "C": ["dublin", "nashville", "savannah", "scottsdale", "destin"],
}
TIER_OF = {c: t for t, cs in TIERS.items() for c in cs}


def _city_stats(city: str) -> dict:
    csv_path = CRM_DIR / f"{city}_contacts.csv"
    queue_path = CRM_DIR / f"queue_{city}.jsonl"

    queued = sum(1 for _ in queue_path.open()) if queue_path.exists() else 0
    statuses: Counter = Counter()
    channels: Counter = Counter()

    if csv_path.exists():
        with csv_path.open() as f:
            for row in csv.DictReader(f):
                statuses[row.get("status", "?")] += 1
                channels[row.get("channel", "?")] += 1

    return {
        "city": city,
        "tier": TIER_OF.get(city, "-"),
        "queued": queued,
        "pending": statuses.get("pending", 0),
        "sent": statuses.get("sent", 0),
        "replied": statuses.get("replied", 0),
        "converted": statuses.get("converted", 0),
        "channels": dict(channels),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city")
    parser.add_argument("--pending", action="store_true",
                        help="only show cities with pending work")
    args = parser.parse_args()

    cities = [args.city] if args.city else sorted(TIER_OF.keys())
    rows = [_city_stats(c) for c in cities]

    if args.pending:
        rows = [r for r in rows if r["pending"] > 0]

    print(f"\n{'TIER':<5}{'CITY':<13}{'QUEUE':>7}{'PEND':>6}{'SENT':>6}{'REPLY':>7}{'CONV':>6}")
    print("-" * 50)

    totals = Counter()
    for tier in ["A", "B", "C", "-"]:
        tier_rows = [r for r in rows if r["tier"] == tier]
        if not tier_rows:
            continue
        for r in tier_rows:
            print(f"{r['tier']:<5}{r['city']:<13}"
                  f"{r['queued']:>7}{r['pending']:>6}{r['sent']:>6}"
                  f"{r['replied']:>7}{r['converted']:>6}")
            for k in ("queued", "pending", "sent", "replied", "converted"):
                totals[k] += r[k]
        print()

    print("-" * 50)
    print(f"{'':<5}{'TOTAL':<13}"
          f"{totals['queued']:>7}{totals['pending']:>6}{totals['sent']:>6}"
          f"{totals['replied']:>7}{totals['converted']:>6}")

    if totals['sent']:
        reply_rate = 100.0 * totals['replied'] / totals['sent']
        conv_rate = 100.0 * totals['converted'] / totals['sent']
        print(f"\nReply rate:      {reply_rate:.1f}%   "
              f"Conversion rate: {conv_rate:.1f}%")


if __name__ == "__main__":
    main()
