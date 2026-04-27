"""Enrich Airbnb host queue with LinkedIn URLs + verified emails via Apollo.io.

Why Apollo over manual LinkedIn scraping:
  - LinkedIn ToS prohibits scraping. Apollo aggregates the same data legally
    plus adds verified email enrichment in one API call.
  - $49/mo plan = 1,000 monthly credits, enough for ~700 host enrichments.
  - Returns: linkedin_url, email, title, company, location.

Setup:
  export APOLLO_API_KEY=...   (get from https://apollo.io/settings/integrations/api)

Run:
  python scripts/enrich_linkedin.py miami
  python scripts/enrich_linkedin.py miami --limit 20
  python scripts/enrich_linkedin.py --all          # every queued city

Output:
  outreach_crm/linkedin_<city>.csv
    listing_id, host_name, city, linkedin_url, email, title, company,
    confidence, lookup_method, looked_up_at

Note on rate limits / cost: Apollo's people/match endpoint is 1 credit per hit
(0 if no match). We dedupe by host_name+city before calling and skip rows
already present in linkedin_<city>.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print("[err] requests not installed - pip install requests", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).parent.parent
CRM_DIR = ROOT / "outreach_crm"

APOLLO_BASE = "https://api.apollo.io/v1"
APOLLO_KEY = os.getenv("APOLLO_API_KEY", "")

CITY_TO_LOC = {
    "miami": "Miami, Florida",
    "lisbon": "Lisbon, Portugal",
    "madrid": "Madrid, Spain",
    "austin": "Austin, Texas",
    "medellin": "Medellín, Colombia",
    "bogota": "Bogotá, Colombia",
    "tampa": "Tampa, Florida",
    "orlando": "Orlando, Florida",
    "dublin": "Dublin, Ireland",
    "nashville": "Nashville, Tennessee",
    "savannah": "Savannah, Georgia",
    "scottsdale": "Scottsdale, Arizona",
    "destin": "Destin, Florida",
}

FIELDS = [
    "listing_id", "host_name", "city", "linkedin_url", "email",
    "title", "company", "confidence", "lookup_method", "looked_up_at",
]


def _apollo_lookup(name: str, city_label: str) -> dict | None:
    """Call Apollo people/match. Returns the matched person or None."""
    if not APOLLO_KEY:
        return None

    parts = name.strip().split(maxsplit=1)
    if not parts:
        return None
    first = parts[0]
    last = parts[1] if len(parts) > 1 else ""

    payload = {
        "first_name": first,
        "last_name": last,
        "reveal_personal_emails": False,
        "reveal_phone_number": False,
    }
    if city_label:
        payload["organization_locations"] = [city_label]

    try:
        r = requests.post(
            f"{APOLLO_BASE}/people/match",
            headers={
                "Cache-Control": "no-cache",
                "Content-Type": "application/json",
                "X-Api-Key": APOLLO_KEY,
            },
            json=payload,
            timeout=15,
        )
        if r.status_code != 200:
            print(f"  [warn] apollo {r.status_code} for '{name}': {r.text[:120]}",
                  file=sys.stderr)
            return None
        return r.json().get("person")
    except Exception as e:
        print(f"  [warn] apollo exc for '{name}': {e}", file=sys.stderr)
        return None


def _existing_keys(out_path: Path) -> set[tuple[str, str]]:
    if not out_path.exists():
        return set()
    keys: set[tuple[str, str]] = set()
    with out_path.open() as f:
        for row in csv.DictReader(f):
            keys.add((row.get("listing_id", ""), row.get("host_name", "")))
    return keys


def enrich_city(city: str, limit: int = 0) -> int:
    queue_path = CRM_DIR / f"queue_{city}.jsonl"
    out_path = CRM_DIR / f"linkedin_{city}.csv"

    if not queue_path.exists():
        print(f"  [skip] {city}: no queue_{city}.jsonl")
        return 0

    queue = [json.loads(l) for l in queue_path.open() if l.strip()]
    seen = _existing_keys(out_path)
    city_label = CITY_TO_LOC.get(city, "")

    new_file = not out_path.exists()
    appended = 0

    with out_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()

        for q in queue:
            if limit and appended >= limit:
                break
            key = (q["listing_id"], q["host_name"])
            if key in seen:
                continue

            person = _apollo_lookup(q["host_name"], city_label)
            row = {
                "listing_id": q["listing_id"],
                "host_name": q["host_name"],
                "city": city,
                "linkedin_url": "",
                "email": "",
                "title": "",
                "company": "",
                "confidence": "0",
                "lookup_method": "apollo" if APOLLO_KEY else "skipped_no_key",
                "looked_up_at": datetime.now().isoformat(),
            }
            if person:
                row["linkedin_url"] = person.get("linkedin_url", "") or ""
                row["email"] = person.get("email", "") or ""
                row["title"] = person.get("title", "") or ""
                org = person.get("organization") or {}
                row["company"] = org.get("name", "") or ""
                row["confidence"] = str(person.get("contact_emails", [{}])[0]
                                        .get("verification_status", "")) if person.get("contact_emails") else ""

            writer.writerow(row)
            appended += 1
            time.sleep(0.3)  # be polite to Apollo

    return appended


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("city", nargs="?", help="city slug (or use --all)")
    parser.add_argument("--all", action="store_true",
                        help="enrich every city with a queue file")
    parser.add_argument("--limit", type=int, default=0,
                        help="max lookups per city (0 = no cap)")
    args = parser.parse_args()

    if not APOLLO_KEY:
        print("[!] APOLLO_API_KEY not set - will write rows with empty LinkedIn data.")
        print("    Get a key at https://apollo.io/settings/integrations/api")
        print("    or do manual lookups using the rows as a worklist.\n")

    if args.all:
        cities = sorted(p.stem.removeprefix("queue_")
                        for p in CRM_DIR.glob("queue_*.jsonl"))
    elif args.city:
        cities = [args.city]
    else:
        parser.error("provide a city or --all")

    grand = 0
    for c in cities:
        added = enrich_city(c, limit=args.limit)
        grand += added
        print(f"  [ok] {c}: +{added} rows -> outreach_crm/linkedin_{c}.csv")

    print(f"\n[done] {grand} new rows enriched.")


if __name__ == "__main__":
    main()
