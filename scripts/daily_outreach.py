"""Pick today's outreach batch across all city queues.

Strategy: 30 messages/day, weighted by tier:
  Tier A (PH-launch fuel)  : 15/day  (50%)
  Tier B (volume plays)    : 10/day  (33%)
  Tier C (long tail)       :  5/day  (17%)

Pulls oldest pending from each queue first (FIFO so nobody waits forever),
writes a single daily.md with copy-paste blocks. Marks them as 'queued_today'
in the CRM so they don't get picked again tomorrow.

Run:  python scripts/daily_outreach.py
      python scripts/daily_outreach.py --target 50
      python scripts/daily_outreach.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
CRM_DIR = ROOT / "outreach_crm"

TIER_QUOTA = {"A": 0.50, "B": 0.33, "C": 0.17}
TIERS = {
    "A": ["miami", "lisbon", "madrid", "austin"],
    "B": ["medellin", "bogota", "tampa", "orlando"],
    "C": ["dublin", "nashville", "savannah", "scottsdale", "destin"],
}


def _load_queue(city: str) -> list[dict]:
    path = CRM_DIR / f"queue_{city}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open() if line.strip()]


def _crm_status(city: str) -> dict[str, str]:
    """listing_id -> status from CRM CSV."""
    path = CRM_DIR / f"{city}_contacts.csv"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    with path.open() as f:
        for row in csv.DictReader(f):
            if row.get("channel") == "contact_host":
                out[row.get("listing_id", "")] = row.get("status", "")
    return out


def _pending_for_city(city: str) -> list[dict]:
    queue = _load_queue(city)
    status = _crm_status(city)
    return [q for q in queue if status.get(q["listing_id"], "pending") == "pending"]


def _pick_for_tier(tier: str, n: int) -> list[tuple[str, dict]]:
    """Pull n oldest-pending messages, round-robin across cities in this tier."""
    pools = {c: _pending_for_city(c) for c in TIERS[tier]}
    out: list[tuple[str, dict]] = []
    while len(out) < n and any(pools.values()):
        for c in TIERS[tier]:
            if pools[c] and len(out) < n:
                out.append((c, pools[c].pop(0)))
    return out


def _mark_queued_today(city: str, listing_ids: list[str]):
    path = CRM_DIR / f"{city}_contacts.csv"
    if not path.exists() or not listing_ids:
        return
    today = date.today().isoformat()
    rows = []
    fieldnames = None
    with path.open() as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if (row.get("listing_id") in listing_ids
                    and row.get("channel") == "contact_host"
                    and row.get("status") == "pending"):
                row["status"] = "queued_today"
                row["notes"] = (row.get("notes", "") + f" | queued {today}").strip(" |")
            rows.append(row)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=30,
                        help="total messages for today (default 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print plan but don't update CRM or write daily.md")
    args = parser.parse_args()

    today = date.today().isoformat()
    plan: dict[str, list[tuple[str, dict]]] = {}
    for tier, share in TIER_QUOTA.items():
        n = round(args.target * share)
        plan[tier] = _pick_for_tier(tier, n)

    total = sum(len(p) for p in plan.values())
    if total == 0:
        print("All queues empty - nothing to do today.")
        return

    print(f"\nDaily outreach plan ({today}) - {total} messages\n")
    for tier in ["A", "B", "C"]:
        rows = plan[tier]
        by_city: dict[str, int] = {}
        for c, _ in rows:
            by_city[c] = by_city.get(c, 0) + 1
        breakdown = ", ".join(f"{c}={n}" for c, n in by_city.items())
        print(f"  Tier {tier}: {len(rows):>2}  ({breakdown})")

    if args.dry_run:
        print("\n[dry-run] no files written, no CRM changes.")
        return

    out_dir = CRM_DIR / "daily"
    out_dir.mkdir(exist_ok=True)
    md_path = out_dir / f"daily_{today}.md"

    lines = [
        f"# Outreach batch - {today}",
        f"Total: {total} messages",
        "",
        "Open each profile URL, click Contact Host, paste the message block.",
        "Mark sent in batches: `python scripts/build_outreach_queue.py <city> --mark-sent <id>`",
        "",
        "---",
        "",
    ]

    by_city_ids: dict[str, list[str]] = {}
    idx = 0
    for tier in ["A", "B", "C"]:
        for city, q in plan[tier]:
            idx += 1
            by_city_ids.setdefault(city, []).append(q["listing_id"])
            lines.append(f"## {idx}. [{tier}] {city.title()} - {q['host_name']} ({q['neighborhood']})")
            lines.append(f"- listing_id: `{q['listing_id']}`")
            lines.append(f"- Profile: {q.get('host_profile_url') or '(none)'}")
            lines.append(f"- Listing: {q.get('listing_url')}")
            lines.append("")
            lines.append("```")
            lines.append(q["message"])
            lines.append("```")
            lines.append("")
            lines.append("---")
            lines.append("")

    md_path.write_text("\n".join(lines))

    for city, ids in by_city_ids.items():
        _mark_queued_today(city, ids)

    print(f"\n[ok] wrote {md_path}")
    print(f"[ok] marked {sum(len(v) for v in by_city_ids.values())} rows as queued_today\n")


if __name__ == "__main__":
    main()
