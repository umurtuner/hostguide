"""Reconcile Stripe payments against credits.json.

Pulls successful checkout sessions from Stripe since a given date and compares
against hostguide/data/credits.json (and Redis if configured). Prints any email
that paid but did not receive credits. This is for recovering missed webhook
events (e.g. the apex TLS outage April 10-13, 2026).

Usage:
    python scripts/reconcile_stripe_credits.py --since 2026-04-09
    python scripts/reconcile_stripe_credits.py --since 2026-04-09 --fix
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT.parent))

import stripe  # noqa: E402

from hostguide.src.app import (  # noqa: E402
    TIERS,
    _add_credits,
    _load_credits,
    STRIPE_SECRET,
)


def _since_ts(date_str: str) -> int:
    dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def iter_paid_sessions(since_ts: int):
    params = {
        "limit": 100,
        "created": {"gte": since_ts},
        "expand": ["data.customer", "data.line_items"],
    }
    starting_after = None
    while True:
        if starting_after:
            params["starting_after"] = starting_after
        page = stripe.checkout.Session.list(**params)
        for s in page.data:
            if s.payment_status == "paid":
                yield s
        if not page.has_more:
            return
        starting_after = page.data[-1].id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="ISO date YYYY-MM-DD")
    parser.add_argument("--fix", action="store_true",
                        help="Actually grant missing credits (otherwise dry-run)")
    args = parser.parse_args()

    if not STRIPE_SECRET:
        print("  [err] STRIPE_SECRET not configured")
        return

    stripe.api_key = STRIPE_SECRET
    since_ts = _since_ts(args.since)
    print(f"  scanning Stripe paid sessions since {args.since} ({since_ts})")

    credits_db = _load_credits()
    print(f"  credits.json has {len(credits_db)} emails")

    missing = []
    seen = 0
    for s in iter_paid_sessions(since_ts):
        seen += 1
        email = (s.customer_details.email if s.customer_details else None) or s.customer_email
        if not email:
            email = (s.metadata or {}).get("email", "")
        email = (email or "").lower().strip()
        tier = (s.metadata or {}).get("tier", "single")
        tier_cfg = TIERS.get(tier, TIERS["single"])
        expected = tier_cfg["guides"]

        user = credits_db.get(email, {})
        has_credits = user.get("credits", 0) > 0 or s.id in (user.get("dedup_keys") or [])

        tag = "[OK]" if has_credits else "[MISSING]"
        print(f"  {tag} {email or '(no email)'} tier={tier} +{expected} session={s.id}")
        if not has_credits:
            missing.append((email, tier, s.id, expected))

    print()
    print(f"  total paid sessions: {seen}")
    print(f"  missing credits: {len(missing)}")

    if not missing:
        return

    if args.fix:
        for email, tier, sid, expected in missing:
            if not email:
                print(f"  [skip] session {sid} has no email")
                continue
            _add_credits(email, expected, tier, dedup_key=sid)
            print(f"  [fixed] {email} +{expected} ({tier}) from {sid}")
    else:
        print("  dry-run - rerun with --fix to grant missing credits")


if __name__ == "__main__":
    main()
