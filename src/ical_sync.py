"""HostGuide iCal sync module.

== MIE V9 (2026-05-14) DERIVED MODULE ==
The MIE V9 brief flagged that multi-property hosts (HostGuide's target persona)
are most likely already on a PMS (Hostfully / Guesty / Hospitable / Hostaway)
that bundles a guidebook. Standalone HostGuide loses to "I'll just use what
my PMS gives me" friction unless we can match the set-and-forget UX.

Auto-iCal sync removes the #1 standalone-vs-PMS objection: hosts paste their
listing's iCal URL once, HostGuide auto-personalises the guidebook page with
booking dates without any further manual work.

Airbnb (and VRBO / Booking / Hostaway) all expose listing iCal as PUBLIC
URLs the host already has access to in their listing settings. No API
approval / no OAuth / no webhook contract — just HTTP GET + standard .ics
parsing. The iCal feed exposes BOOKING DATES ONLY (no guest names / phone /
messages); date-level personalisation is sufficient for guidebook UX
("Welcome - your stay starts Saturday, you have 4 nights with us").

This module is STANDALONE — wire into the existing host-guide.net Flask
backend via:
    from hostguide.src.ical_sync import sync_listing, get_current_booking
    booking = get_current_booking(listing_id, ical_url)
    if booking:
        page_context["check_in"] = booking.start
        page_context["nights"] = booking.nights

Or run as a cron via the CLI at the bottom of this file.

== USAGE ==

Set up dependency once (added to requirements):
    pip install icalendar python-dateutil

Programmatic:
    from hostguide.src.ical_sync import sync_listing
    bookings = sync_listing("https://www.airbnb.com/calendar/ical/12345.ics?s=ABC")
    # returns list of Booking objects with start/end/nights/status

CLI (one-shot or cron):
    python -m hostguide.src.ical_sync --url <ical_url> --listing-id <id>
    python -m hostguide.src.ical_sync --all   # sync all listings in the DB

Cron pattern (every 30 min):
    */30 * * * * cd /path/to/hostguide && python -m hostguide.src.ical_sync --all >> logs/ical_sync.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import requests

try:
    from icalendar import Calendar
except ImportError:
    Calendar = None  # graceful: callers get a clear error from sync_listing()


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYNC_STATE_DIR = PROJECT_ROOT / "data" / "ical_sync"
LOG_PATH = PROJECT_ROOT / "logs" / "ical_sync.log"

POLL_TIMEOUT_SEC = 15
USER_AGENT = "HostGuide-iCalSync/1.0 (+https://host-guide.net)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ical_sync")


@dataclass
class Booking:
    """A single calendar entry parsed from an iCal feed.

    Airbnb iCal entries do not include guest names / contact / message data
    by design - those live behind the messaging layer. We only get the
    block of time + an opaque event identifier.
    """
    listing_id: str
    uid: str            # iCal event UID (stable across syncs)
    start: date         # check-in date (inclusive)
    end: date           # check-out date (exclusive per iCal convention)
    nights: int
    summary: str        # raw SUMMARY field from iCal (often "Reserved" or "Not Available")
    status: str         # "BOOKED" if SUMMARY indicates a stay; "BLOCKED" if owner-blocked
    fetched_at: str     # ISO8601 UTC


def _parse_status(summary: str) -> str:
    """Heuristic: distinguish guest bookings from owner-blocked dates.

    Airbnb iCal SUMMARY values:
      - "Reserved" / "Booked"            -> guest booking (BOOKED)
      - "Not available" / "Blocked"      -> host blocked (BLOCKED)
      - "Airbnb (Not available)"         -> ambiguous, default to BLOCKED
    """
    s = (summary or "").lower()
    if "reserved" in s or "booked" in s or "guest" in s:
        return "BOOKED"
    return "BLOCKED"


def fetch_ical(ical_url: str) -> str:
    """HTTP GET an iCal URL and return the raw .ics text.

    Raises requests.HTTPError on non-2xx and ValueError on empty body.
    """
    resp = requests.get(
        ical_url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/calendar"},
        timeout=POLL_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    body = resp.text or ""
    if not body.strip().startswith("BEGIN:VCALENDAR"):
        raise ValueError(f"URL did not return iCal content. Got first 80 chars: {body[:80]!r}")
    return body


def parse_ical(ics_text: str, listing_id: str) -> list[Booking]:
    """Parse raw iCal text into Booking objects.

    Skips:
      - past events (DTEND < today)
      - events with no DTSTART or DTEND
      - non-VEVENT components
    """
    if Calendar is None:
        raise RuntimeError(
            "icalendar library not installed. Run: pip install icalendar python-dateutil"
        )

    cal = Calendar.from_ical(ics_text)
    today = date.today()
    fetched_at = datetime.now(timezone.utc).isoformat()
    bookings: list[Booking] = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        dtstart = component.get("DTSTART")
        dtend = component.get("DTEND")
        if dtstart is None or dtend is None:
            continue

        start = dtstart.dt if hasattr(dtstart, "dt") else dtstart
        end = dtend.dt if hasattr(dtend, "dt") else dtend
        # Normalise datetime -> date
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()

        if end < today:  # already-completed stay - skip
            continue

        nights = (end - start).days
        if nights <= 0:
            continue

        summary = str(component.get("SUMMARY", "")).strip()
        uid = str(component.get("UID", f"{listing_id}-{start.isoformat()}")).strip()

        bookings.append(Booking(
            listing_id=listing_id,
            uid=uid,
            start=start,
            end=end,
            nights=nights,
            summary=summary,
            status=_parse_status(summary),
            fetched_at=fetched_at,
        ))

    bookings.sort(key=lambda b: b.start)
    return bookings


def sync_listing(ical_url: str, listing_id: str = "default") -> list[Booking]:
    """Fetch + parse + persist bookings for one listing.

    Persists state to data/ical_sync/{listing_id}.json so other parts of the
    app (guidebook page renderer / checkout flow / etc) can read it without
    re-fetching the iCal feed.
    """
    ics_text = fetch_ical(ical_url)
    bookings = parse_ical(ics_text, listing_id=listing_id)
    _persist_bookings(listing_id, bookings)
    log.info(f"[{listing_id}] synced {len(bookings)} upcoming bookings")
    return bookings


def _persist_bookings(listing_id: str, bookings: Iterable[Booking]) -> Path:
    """Save the per-listing JSON state for downstream readers."""
    SYNC_STATE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SYNC_STATE_DIR / f"{listing_id}.json"
    payload = {
        "listing_id": listing_id,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "bookings": [
            {
                **asdict(b),
                "start": b.start.isoformat(),
                "end": b.end.isoformat(),
            }
            for b in bookings
        ],
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    return out_path


def load_persisted(listing_id: str) -> dict:
    """Load the last-synced state for a listing (for guidebook page rendering)."""
    path = SYNC_STATE_DIR / f"{listing_id}.json"
    if not path.exists():
        return {"listing_id": listing_id, "synced_at": None, "bookings": []}
    with open(path) as f:
        return json.load(f)


def get_current_booking(listing_id: str) -> Optional[dict]:
    """Return the booking that contains today's date, if any.

    Designed for guidebook page rendering:
        booking = get_current_booking(listing_id)
        if booking:
            context["check_in"] = booking["start"]
            context["nights"] = booking["nights"]
    """
    today = date.today().isoformat()
    state = load_persisted(listing_id)
    for b in state.get("bookings", []):
        # iCal end is exclusive (checkout day) so use strict less-than
        if b["start"] <= today < b["end"]:
            return b
    return None


def get_next_booking(listing_id: str) -> Optional[dict]:
    """Return the next upcoming booking after today, if any."""
    today = date.today().isoformat()
    state = load_persisted(listing_id)
    upcoming = [b for b in state.get("bookings", []) if b["start"] > today and b["status"] == "BOOKED"]
    return upcoming[0] if upcoming else None


# ============================================================
# USABILITY EXTENSIONS (per Umur 2026-05-14 - "keep digging same angle")
# ============================================================
# Hostfully Guidebooks does NOT auto-personalize from iCal.
# Touch Stay does NOT auto-personalize from iCal.
# This module's auto-personalization features ARE the differentiation.
# ============================================================

# Platform iCal URL patterns (host pastes URL, we identify the source)
PLATFORM_PATTERNS = {
    "airbnb":   ("airbnb.com/calendar/ical/",),
    "vrbo":     ("vrbo.com/icalendar/", "homeaway.com/ical/"),
    "booking":  ("admin.booking.com/hotel/hoteladmin/ical.html",),
    "hostaway": ("api.hostaway.com/v1/ical/",),
    "hospitable": ("my.hospitable.com/api/ical/",),
    "guesty":   ("app.guesty.com/api/v2/calendar/",),
    "lodgify":  ("api.lodgify.com/icalendar/",),
}


def detect_platform(ical_url: str) -> str:
    """Identify which platform an iCal URL is from (for routing-aware logic)."""
    url_lower = (ical_url or "").lower()
    for platform, patterns in PLATFORM_PATTERNS.items():
        if any(p in url_lower for p in patterns):
            return platform
    return "unknown"


def get_pre_arrival_bookings(listing_id: str, days_ahead: int = 3) -> list[dict]:
    """Return bookings whose check-in is in the next N days.

    Used by pre-arrival email scheduling: every morning the cron checks each
    listing for bookings with start = today + days_ahead, and triggers
    'send the welcome guide URL N days before arrival' email.
    """
    today = date.today()
    target = (today + timedelta(days=days_ahead)).isoformat()
    state = load_persisted(listing_id)
    return [
        b for b in state.get("bookings", [])
        if b["start"] == target and b["status"] == "BOOKED"
    ]


def get_post_checkout_bookings(listing_id: str, days_after: int = 1) -> list[dict]:
    """Return bookings whose checkout was N days ago.

    Used by post-stay review-nudge automation.
    """
    today = date.today()
    target = (today - timedelta(days=days_after)).isoformat()
    state = load_persisted(listing_id)
    return [
        b for b in state.get("bookings", [])
        if b["end"] == target and b["status"] == "BOOKED"
    ]


def get_stay_context(listing_id: str) -> Optional[dict]:
    """Return rich context for the current stay (drives guide-page personalization).

    Returns dict with:
        phase:          "before" | "during" | "after" | None (no current booking)
        booking:        the matching Booking dict
        day_n:          day number in stay (1-indexed) if phase=="during"
        days_total:     total stay length
        days_remaining: days until checkout if phase=="during"
        is_short_stay:  True if < 3 nights (drives content selection)
        is_long_stay:   True if >= 7 nights
    """
    today = date.today()
    today_iso = today.isoformat()
    state = load_persisted(listing_id)
    bookings = state.get("bookings", [])

    # During stay
    for b in bookings:
        if b["start"] <= today_iso < b["end"]:
            start = date.fromisoformat(b["start"])
            end = date.fromisoformat(b["end"])
            day_n = (today - start).days + 1
            days_total = (end - start).days
            days_remaining = (end - today).days
            return {
                "phase": "during",
                "booking": b,
                "day_n": day_n,
                "days_total": days_total,
                "days_remaining": days_remaining,
                "is_short_stay": days_total < 3,
                "is_long_stay": days_total >= 7,
            }

    # Before next stay (within 7 days)
    upcoming = [b for b in bookings if b["start"] > today_iso and b["status"] == "BOOKED"]
    if upcoming:
        next_b = upcoming[0]
        days_until = (date.fromisoformat(next_b["start"]) - today).days
        if days_until <= 7:
            return {
                "phase": "before",
                "booking": next_b,
                "days_until_arrival": days_until,
                "is_short_stay": (date.fromisoformat(next_b["end"]) - date.fromisoformat(next_b["start"])).days < 3,
                "is_long_stay": (date.fromisoformat(next_b["end"]) - date.fromisoformat(next_b["start"])).days >= 7,
            }

    # After most recent stay (within 3 days)
    past = [b for b in bookings if b["end"] <= today_iso and b["status"] == "BOOKED"]
    past.sort(key=lambda x: x["end"], reverse=True)
    if past:
        most_recent = past[0]
        days_since = (today - date.fromisoformat(most_recent["end"])).days
        if days_since <= 3:
            return {
                "phase": "after",
                "booking": most_recent,
                "days_since_checkout": days_since,
            }

    return None


def detect_overlap_conflicts(listing_id: str) -> list[dict]:
    """Surface overlapping bookings (data quality alert - usually a host error).

    Returns list of conflict pairs. Empty list if clean.
    """
    state = load_persisted(listing_id)
    bookings = sorted(state.get("bookings", []), key=lambda x: x["start"])
    conflicts = []
    for i, a in enumerate(bookings):
        for b in bookings[i+1:]:
            if b["start"] >= a["end"]:  # sorted, so we can break early
                break
            if b["start"] < a["end"]:
                conflicts.append({"booking_a": a, "booking_b": b})
    return conflicts


def sync_all_listings(sources_path: Optional[Path] = None) -> dict:
    """Batch-sync all listings from data/ical_sources.json.

    Returns summary: {listing_id: {ok: bool, count: N, error: str | None}}.
    Used by cron (e.g. */30 * * * * python -m hostguide.src.ical_sync --all).
    """
    sources = sources_path or (PROJECT_ROOT / "data" / "ical_sources.json")
    if not sources.exists():
        log.warning(f"No sources file at {sources}")
        return {}

    with open(sources) as f:
        listings = json.load(f)

    results = {}
    for entry in listings:
        listing_id = entry.get("listing_id", "")
        ical_url = entry.get("ical_url", "")
        try:
            bookings = sync_listing(ical_url, listing_id=listing_id)
            results[listing_id] = {
                "ok": True,
                "count": len(bookings),
                "platform": detect_platform(ical_url),
                "error": None,
            }
        except Exception as e:
            results[listing_id] = {
                "ok": False,
                "count": 0,
                "platform": detect_platform(ical_url),
                "error": str(e),
            }
            log.error(f"[{listing_id}] sync failed: {e}")

    return results


def find_pre_arrival_actions(days_ahead: int = 3) -> list[dict]:
    """Across all synced listings, find bookings starting in N days.

    Returns: [{listing_id, booking, action: "send_pre_arrival_email"}, ...]
    Wire to your email service (Beehiiv / Loops / Mailgun / etc) to actually send.
    """
    actions = []
    if not SYNC_STATE_DIR.exists():
        return actions
    for state_file in SYNC_STATE_DIR.glob("*.json"):
        listing_id = state_file.stem
        bookings = get_pre_arrival_bookings(listing_id, days_ahead=days_ahead)
        for b in bookings:
            actions.append({
                "listing_id": listing_id,
                "booking": b,
                "action": "send_pre_arrival_email",
                "days_ahead": days_ahead,
            })
    return actions


def find_post_checkout_actions(days_after: int = 1) -> list[dict]:
    """Across all synced listings, find bookings that checked out N days ago.

    Returns: [{listing_id, booking, action: "send_review_nudge"}, ...]
    """
    actions = []
    if not SYNC_STATE_DIR.exists():
        return actions
    for state_file in SYNC_STATE_DIR.glob("*.json"):
        listing_id = state_file.stem
        bookings = get_post_checkout_bookings(listing_id, days_after=days_after)
        for b in bookings:
            actions.append({
                "listing_id": listing_id,
                "booking": b,
                "action": "send_review_nudge",
                "days_after": days_after,
            })
    return actions


# ---- CLI ----

def main():
    parser = argparse.ArgumentParser(
        description="HostGuide iCal sync - poll Airbnb/VRBO/etc public calendars and cache booking dates"
    )
    parser.add_argument("--url", help="iCal URL to sync (one-shot)")
    parser.add_argument("--listing-id", default="default", help="Listing ID for one-shot sync")
    parser.add_argument("--all", action="store_true", help="Sync all listings from data/ical_sources.json")
    parser.add_argument("--show", help="Show persisted state for a listing_id (debug)")
    parser.add_argument("--context", help="Show stay context (before/during/after) for a listing_id")
    parser.add_argument("--pre-arrival", type=int, metavar="DAYS",
                        help="Find all bookings starting in N days across all listings (for cron / email scheduling)")
    parser.add_argument("--post-checkout", type=int, metavar="DAYS",
                        help="Find all bookings that checked out N days ago (for review-nudge cron)")
    parser.add_argument("--conflicts", help="Detect overlapping bookings for a listing_id (data quality alert)")
    args = parser.parse_args()

    if args.show:
        state = load_persisted(args.show)
        print(json.dumps(state, indent=2))
        return

    if args.context:
        ctx = get_stay_context(args.context)
        print(json.dumps(ctx, indent=2, default=str))
        return

    if args.pre_arrival is not None:
        actions = find_pre_arrival_actions(days_ahead=args.pre_arrival)
        print(json.dumps(actions, indent=2, default=str))
        return

    if args.post_checkout is not None:
        actions = find_post_checkout_actions(days_after=args.post_checkout)
        print(json.dumps(actions, indent=2, default=str))
        return

    if args.conflicts:
        confs = detect_overlap_conflicts(args.conflicts)
        if not confs:
            print(f"No conflicts for {args.conflicts}")
        else:
            print(json.dumps(confs, indent=2, default=str))
        return

    if args.all:
        results = sync_all_listings()
        if not results:
            log.warning("No data/ical_sources.json found - nothing to sync")
            sys.exit(0)
        print(json.dumps(results, indent=2, default=str))
        return

    if args.url:
        bookings = sync_listing(args.url, listing_id=args.listing_id)
        print(f"Found {len(bookings)} upcoming booking(s):")
        for b in bookings:
            print(f"  {b.start} -> {b.end} ({b.nights}n) {b.status} - {b.summary}")
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
