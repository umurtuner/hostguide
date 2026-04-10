"""Airbnb listing scraper — Playwright-based with persistent Chrome profile.

Uses the same anti-blocking techniques as the Dubai scraper project:
- Playwright with persistent Chrome profile (preserves cookies/sessions)
- Headed mode by default (avoids headless detection)
- Dehydrated state extraction (Next.js __NEXT_DATA__ / bootstrapData)
- Human-in-loop challenge solving
- Warm scrolling to trigger lazy loading
- Configurable rate limiting

Usage:
    from hostguide.src.scraper import scrape_city
    listings = scrape_city(city_config, max_pages=5)

    # Or from CLI:
    HEADLESS=false python -m hostguide.run medellin
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import random
from dataclasses import dataclass, field, asdict
from typing import Optional, List
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, Page
except ImportError:
    sync_playwright = None
    Page = None

# ── Config (env vars, same pattern as Dubai scraper) ──
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"  # Default HEADED for Airbnb
PROFILE_DIR = os.getenv("PROFILE_DIR", str(Path(__file__).parent.parent / "chrome_profile_airbnb"))
SCROLL_STEPS = int(os.getenv("SCROLL_STEPS", "5"))
SCROLL_PAUSE_SEC = float(os.getenv("SCROLL_PAUSE_SEC", "1.2"))
DETAIL_PAUSE_SEC = float(os.getenv("DETAIL_PAUSE_SEC", "2.0"))
PAGE_LOAD_PAUSE = float(os.getenv("PAGE_LOAD_PAUSE", "3.0"))


@dataclass
class Listing:
    """A scraped Airbnb listing."""
    listing_id: str
    title: str
    url: str
    city: str
    neighborhood: str = ""
    lat: float = 0.0
    lng: float = 0.0
    price_per_night: str = ""
    currency: str = ""
    rating: float = 0.0
    reviews_count: int = 0
    host_name: str = ""
    host_id: str = ""
    host_profile_url: str = ""
    host_superhost: bool = False
    host_response_rate: str = ""
    host_website: str = ""
    host_instagram: str = ""
    host_email: str = ""
    host_facebook: str = ""
    property_type: str = ""
    bedrooms: int = 0
    bathrooms: int = 0
    guests: int = 0
    amenities: list[str] = field(default_factory=list)
    photos: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# ANTI-BLOCKING UTILITIES (from Dubai scraper playbook)
# ═══════════════════════════════════════════════════════════════

NAV_TIMES: List[float] = []


def attach_nav_monitor(page: Page) -> None:
    """Monitor frame navigations to detect refresh loops."""
    def _on_frame_navigated(frame):
        try:
            if frame == page.main_frame:
                NAV_TIMES.append(time.time())
        except Exception:
            pass
    page.on("framenavigated", _on_frame_navigated)


def is_refresh_looping(window_sec: int = 12, threshold: int = 4) -> bool:
    now = time.time()
    while NAV_TIMES and (now - NAV_TIMES[0]) > window_sec:
        NAV_TIMES.pop(0)
    return len(NAV_TIMES) >= threshold


def is_blocked(page: Page) -> bool:
    """Detect captcha, Cloudflare challenge, or access denial."""
    u = (page.url or "").lower()
    t = ""
    try:
        t = (page.title() or "").lower()
    except Exception:
        pass
    html_snippet = ""
    try:
        html_snippet = page.content()[:3000].lower()
    except Exception:
        pass
    return (
        "captcha" in u
        or "challenge" in u
        or "access denied" in t
        or "are you human" in t
        or "blocked" in t
        or "just a moment" in t
        or "cf-challenge" in html_snippet
        or ("cloudflare" in html_snippet and "challenge" in html_snippet)
        or "ray id" in t
        or "robot" in t
    )


def wait_for_human_solve(page: Page) -> None:
    """Pause for human to solve challenge in the browser."""
    print("\n  !! Challenge / captcha detected.")
    print("  -> Solve it manually in the opened browser window.")
    if sys.stdin.isatty():
        input("  Press ENTER when done... ")
    else:
        print("  Non-interactive: waiting 20s for challenge to auto-resolve...")
        time.sleep(20)

    stable_seconds = 6
    last_url = page.url
    same_for = 0
    print("  Waiting for stability...")
    while same_for < stable_seconds:
        time.sleep(1)
        current = page.url
        if is_refresh_looping():
            same_for = 0
            last_url = current
            continue
        if current == last_url:
            same_for += 1
        else:
            last_url = current
            same_for = 0
    print("  OK, continuing.\n")
    time.sleep(2)


def close_popups(page: Page) -> None:
    """Close cookie banners, translation popups, etc."""
    for sel in [
        "button:has-text('Accept')",
        "button:has-text('I accept')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "button:has-text('Agree')",
        "[data-testid='accept-btn']",
        "button[aria-label='Close']",
        "[aria-label='Close']",
        # Airbnb-specific: translation modal
        "button:has-text('Continue')",
    ]:
        try:
            page.locator(sel).first.click(timeout=800)
            time.sleep(0.3)
        except Exception:
            pass


def warm_scroll(page: Page, steps: int = None, pause: float = None) -> None:
    """Scroll down to trigger lazy loading of listings."""
    steps = steps or SCROLL_STEPS
    pause = pause or SCROLL_PAUSE_SEC
    try:
        for _ in range(steps):
            page.mouse.wheel(0, random.randint(1800, 2500))
            time.sleep(pause + random.uniform(-0.3, 0.3))
    except Exception:
        pass


def goto_with_retry(page: Page, url: str) -> None:
    """Navigate with retry via about:blank (from Dubai scraper)."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"  goto failed once: {e} — retrying via about:blank")
        page.goto("about:blank", wait_until="domcontentloaded", timeout=20000)
        time.sleep(2)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)


# ═══════════════════════════════════════════════════════════════
# DATA EXTRACTION — Airbnb dehydrated state (like haraj.py)
# ═══════════════════════════════════════════════════════════════

def extract_dehydrated_data(page: Page) -> dict:
    """Extract Airbnb's embedded JSON data from the page.

    Airbnb uses Next.js-style dehydrated data in <script> tags.
    We try multiple extraction methods.
    """
    # Method 1: __NEXT_DATA__ (Next.js standard)
    try:
        raw = page.evaluate("""() => {
            if (window.__NEXT_DATA__) return JSON.stringify(window.__NEXT_DATA__);
            return '';
        }""")
        if raw:
            return json.loads(raw)
    except Exception:
        pass

    # Method 2: data-deferred-state (Airbnb's deferred state containers)
    try:
        raw = page.evaluate("""() => {
            const el = document.querySelector('#data-deferred-state-0, #data-deferred-state, [id^="data-deferred-state"]');
            if (el) return el.textContent || '';
            return '';
        }""")
        if raw and raw.strip().startswith("{"):
            return json.loads(raw)
    except Exception:
        pass

    # Method 3: Script tags containing bootstrapData or similar
    try:
        raw = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const t = s.textContent || '';
                if (t.includes('"listingId"') || t.includes('"listing"') || t.includes('bootstrapData')) {
                    // Find the JSON object
                    const match = t.match(/\\{[\\s\\S]*"listingId"[\\s\\S]*\\}/);
                    if (match) return match[0];
                }
            }
            return '';
        }""")
        if raw and raw.strip().startswith("{"):
            return json.loads(raw)
    except Exception:
        pass

    return {}


def extract_listings_from_dehydrated(data: dict, city: str) -> list[Listing]:
    """Parse listings from Airbnb's dehydrated data (multiple structures)."""
    listings = []

    # Walk the entire data tree looking for listing-like objects
    def _walk(obj, depth=0):
        if depth > 15:
            return
        if isinstance(obj, dict):
            # Check if this looks like a listing
            lid = obj.get("listingId") or obj.get("id") or obj.get("listing_id")
            if lid and (obj.get("name") or obj.get("title") or obj.get("roomTypeCategory")):
                listing = _parse_dehydrated_listing(obj, city)
                if listing:
                    listings.append(listing)

            # Check for listing nested under "listing" key
            if "listing" in obj and isinstance(obj["listing"], dict):
                inner = obj["listing"]
                lid2 = inner.get("id") or inner.get("listingId")
                if lid2:
                    listing = _parse_dehydrated_listing(inner, city)
                    if listing:
                        listings.append(listing)

            for v in obj.values():
                _walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, depth + 1)

    _walk(data)
    return listings


def _parse_dehydrated_listing(obj: dict, city: str) -> Optional[Listing]:
    """Parse a single listing from dehydrated data."""
    lid = str(obj.get("listingId") or obj.get("id") or obj.get("listing_id") or "")
    if not lid or not lid.isdigit():
        return None

    lat = 0.0
    lng = 0.0
    coord = obj.get("coordinate") or obj.get("location") or {}
    if isinstance(coord, dict):
        lat = coord.get("latitude") or coord.get("lat") or 0.0
        lng = coord.get("longitude") or coord.get("lng") or coord.get("lon") or 0.0

    return Listing(
        listing_id=lid,
        title=obj.get("name") or obj.get("title") or "",
        url=f"https://www.airbnb.com/rooms/{lid}",
        city=city,
        neighborhood=_extract_nested(obj, ["neighborhood", "name"]) or
                      _extract_nested(obj, ["publicAddress"]) or "",
        lat=float(lat),
        lng=float(lng),
        rating=float(obj.get("avgRating") or obj.get("avgRatingLocalized") or
                     obj.get("guestControls", {}).get("avgRating") or 0),
        reviews_count=int(obj.get("reviewsCount") or obj.get("visibleReviewCount") or 0),
        host_name=_extract_nested(obj, ["user", "firstName"]) or
                  _extract_nested(obj, ["primaryHost", "firstName"]) or "",
        host_id=str(_extract_nested(obj, ["user", "id"]) or
                   _extract_nested(obj, ["primaryHost", "id"]) or ""),
        property_type=obj.get("roomTypeCategory") or obj.get("roomType") or "",
        bedrooms=int(obj.get("bedrooms") or 0),
        bathrooms=int(obj.get("bathrooms") or 0),
        guests=int(obj.get("personCapacity") or obj.get("guestCapacity") or 0),
    )


def _extract_nested(obj: dict, keys: list) -> any:
    """Safely extract a nested value."""
    current = obj
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k)
        else:
            return None
    return current


# ═══════════════════════════════════════════════════════════════
# DOM FALLBACK — extract from visible cards
# ═══════════════════════════════════════════════════════════════

def extract_listings_from_dom(page: Page, city: str) -> list[Listing]:
    """Fallback: extract listing IDs and rich info from visible DOM cards."""
    listings = []

    # Method 1: extract from card containers with aria-labels, prices, ratings
    try:
        raw = page.evaluate("""() => {
            const results = [];
            const seen = new Set();

            // Airbnb listing cards: each card has a link to /rooms/{id}
            const links = document.querySelectorAll('a[href*="/rooms/"]');
            for (const a of links) {
                const match = a.href.match(/\\/rooms\\/(\\d+)/);
                if (!match || seen.has(match[1])) continue;
                seen.add(match[1]);

                const id = match[1];
                const title = a.getAttribute('aria-label') || a.getAttribute('title') || '';

                // Walk up to find the card container (usually 3-5 levels up)
                let card = a;
                for (let i = 0; i < 8; i++) {
                    if (card.parentElement) card = card.parentElement;
                    // Stop at a container that looks like a card (has multiple children)
                    if (card.querySelectorAll('a[href*="/rooms/"]').length === 1 &&
                        card.children.length >= 2) break;
                }

                // Extract price from card text
                const cardText = card.textContent || '';
                let price = '';
                const priceMatch = cardText.match(/\\$([\\d,]+)\\s*(night|noche)/i) ||
                                   cardText.match(/([\\d,.]+)\\s*(?:COP|USD|EUR)/i) ||
                                   cardText.match(/\\$([\\d,]+)/);
                if (priceMatch) price = priceMatch[0];

                // Extract rating
                let rating = 0;
                const ratingMatch = cardText.match(/(\\d\\.\\d+)\\s*\\(/);
                if (ratingMatch) rating = parseFloat(ratingMatch[1]);
                // Also try star rating pattern
                if (!rating) {
                    const starMatch = cardText.match(/(\\d\\.\\d+)\\s*·/);
                    if (starMatch) rating = parseFloat(starMatch[1]);
                }

                // Extract review count
                let reviews = 0;
                const reviewMatch = cardText.match(/\\((\\d+)\\s*review/i) ||
                                    cardText.match(/\\((\\d+)\\)/);
                if (reviewMatch) reviews = parseInt(reviewMatch[1]);

                // Extract property type from card text
                let propType = '';
                const typePatterns = ['Entire home', 'Entire apartment', 'Private room', 'Shared room',
                                     'Entire condo', 'Entire loft', 'Entire villa', 'Hotel room',
                                     'Entire rental unit', 'Entire guest suite', 'Entire townhouse'];
                for (const tp of typePatterns) {
                    if (cardText.includes(tp)) { propType = tp; break; }
                }

                // Extract bedrooms/guests from card subtitle text
                let bedrooms = 0, guests = 0, bathrooms = 0;
                const bedMatch = cardText.match(/(\\d+)\\s*bed(?:room)?s?(?!\\s*·)/i);
                if (bedMatch) bedrooms = parseInt(bedMatch[1]);
                const guestMatch = cardText.match(/(\\d+)\\s*guest/i);
                if (guestMatch) guests = parseInt(guestMatch[1]);
                const bathMatch = cardText.match(/(\\d+)\\s*bath/i);
                if (bathMatch) bathrooms = parseInt(bathMatch[1]);

                // Extract host name if visible
                let hostName = '';
                const hostMatch = cardText.match(/Hosted by\\s+([\\w\\s]+?)(?:\\s*·|$)/i);
                if (hostMatch) hostName = hostMatch[1].trim();

                results.push({
                    id: id, title: title, price: price, rating: rating,
                    reviews: reviews, propType: propType, bedrooms: bedrooms,
                    guests: guests, bathrooms: bathrooms, hostName: hostName
                });
            }
            return JSON.stringify(results);
        }""")
        if raw:
            items = json.loads(raw)
            for item in items:
                listings.append(Listing(
                    listing_id=item["id"],
                    title=item.get("title", ""),
                    url=f"https://www.airbnb.com/rooms/{item['id']}",
                    city=city,
                    price_per_night=item.get("price", ""),
                    rating=float(item.get("rating", 0)),
                    reviews_count=int(item.get("reviews", 0)),
                    property_type=item.get("propType", ""),
                    bedrooms=int(item.get("bedrooms", 0)),
                    bathrooms=int(item.get("bathrooms", 0)),
                    guests=int(item.get("guests", 0)),
                    host_name=item.get("hostName", ""),
                ))
    except Exception:
        pass

    # Method 2: regex on full HTML
    if not listings:
        try:
            html = page.content()
            ids = set(re.findall(r'/rooms/(\d{5,})', html))
            for lid in ids:
                listings.append(Listing(
                    listing_id=lid,
                    title="",
                    url=f"https://www.airbnb.com/rooms/{lid}",
                    city=city,
                ))
        except Exception:
            pass

    return listings


# ═══════════════════════════════════════════════════════════════
# DETAIL PAGE ENRICHMENT
# ═══════════════════════════════════════════════════════════════

def enrich_listing_from_detail(page: Page, listing: Listing) -> Listing:
    """Visit a listing's detail page and extract lat/lng, host info, etc."""
    try:
        goto_with_retry(page, listing.url)
        time.sleep(DETAIL_PAUSE_SEC)

        if is_blocked(page):
            wait_for_human_solve(page)

        # Extract dehydrated data from detail page
        data = extract_dehydrated_data(page)
        if data:
            # Walk the tree looking for listing data — new Airbnb format
            # uses niobeClientData where listing ID is base64-encoded,
            # so we also scan for any dict with lat/lng + listing-like fields
            def _find(obj, depth=0):
                if depth > 15 or not isinstance(obj, dict):
                    return
                lid = str(obj.get("id") or obj.get("listingId") or "")

                # Match by listing ID (classic format)
                is_match = lid == listing.listing_id

                # Also match if this dict has lat/lng (new niobeClientData format)
                has_lat = "lat" in obj and isinstance(obj.get("lat"), (int, float)) and abs(obj.get("lat", 0)) > 1
                has_coord = False
                coord = obj.get("coordinate") or obj.get("location")
                if isinstance(coord, dict) and (coord.get("latitude") or coord.get("lat")):
                    has_coord = True

                if is_match or has_lat or has_coord:
                    if has_lat:
                        if not listing.lat:
                            listing.lat = float(obj["lat"])
                        if not listing.lng and "lng" in obj:
                            listing.lng = float(obj["lng"])
                    if has_coord and isinstance(coord, dict):
                        if not listing.lat:
                            listing.lat = float(coord.get("latitude") or coord.get("lat") or 0)
                        if not listing.lng:
                            listing.lng = float(coord.get("longitude") or coord.get("lng") or 0)

                    # Extract other fields if available
                    if not listing.host_name:
                        listing.host_name = (_extract_nested(obj, ["user", "firstName"]) or
                                            _extract_nested(obj, ["primaryHost", "firstName"]) or
                                            obj.get("hostName") or "")
                    if not listing.host_id:
                        listing.host_id = str(_extract_nested(obj, ["user", "id"]) or
                                            _extract_nested(obj, ["primaryHost", "id"]) or "")
                    if not listing.bedrooms and obj.get("bedrooms"):
                        listing.bedrooms = int(obj["bedrooms"])
                    if not listing.bathrooms and obj.get("bathrooms"):
                        listing.bathrooms = int(obj["bathrooms"])
                    if not listing.guests and (obj.get("personCapacity") or obj.get("guestCapacity")):
                        listing.guests = int(obj.get("personCapacity") or obj.get("guestCapacity"))
                    if not listing.neighborhood:
                        nb = obj.get("neighborhood")
                        if isinstance(nb, dict) and nb.get("name"):
                            listing.neighborhood = nb["name"]
                        elif isinstance(nb, str) and nb:
                            listing.neighborhood = nb
                        elif obj.get("publicAddress"):
                            listing.neighborhood = obj["publicAddress"]
                    if not listing.title and obj.get("name") and len(str(obj["name"])) > 5:
                        listing.title = obj["name"]

                for v in obj.values():
                    if isinstance(v, dict):
                        _find(v, depth + 1)
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, dict):
                                _find(item, depth + 1)

            _find(data)

        # Fallback: regex extraction from page HTML
        try:
            html = page.content()

            # Coordinates
            if listing.lat == 0 and listing.lng == 0:
                lat_match = re.search(r'"lat(?:itude)?":\s*([-\d.]+)', html)
                lng_match = re.search(r'"l(?:on|ng|ongitude)":\s*([-\d.]+)', html)
                if lat_match and lng_match:
                    listing.lat = float(lat_match.group(1))
                    listing.lng = float(lng_match.group(1))

            # Title from <title> tag or og:title
            if not listing.title:
                title_match = (re.search(r'<meta property="og:title" content="([^"]+)"', html) or
                               re.search(r'<title>([^<]+)</title>', html))
                if title_match:
                    t = title_match.group(1).split(" - Airbnb")[0].split(" | ")[0].strip()
                    # Filter out generic Airbnb page titles
                    junk = ("airbnb", "vacation rental", "cabins", "beach house", "unique homes")
                    if t and not any(j in t.lower() for j in junk):
                        listing.title = t

            # Host name
            if not listing.host_name:
                host_match = (re.search(r'"firstName"\s*:\s*"([^"]+)"', html) or
                              re.search(r'Hosted by\s+</[^>]+>\s*<[^>]+>([^<]+)', html) or
                              re.search(r'Hosted by\s+([A-Z][a-z]+)', html))
                if host_match:
                    listing.host_name = host_match.group(1).strip()

            # Neighborhood / location from page text
            if not listing.neighborhood:
                loc_match = (re.search(r'"neighborhood"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', html) or
                             re.search(r'"locationTitle"\s*:\s*"([^"]+)"', html) or
                             re.search(r'"publicAddress"\s*:\s*"([^"]+)"', html))
                if loc_match:
                    val = loc_match.group(1).strip()
                    # Filter out non-neighborhood values (amenities, property features)
                    amenity_words = ("pool", "wifi", "kitchen", "parking", "gym", "view",
                                     "balcony", "patio", "garden", "beach", "laundry")
                    if val and not any(val.lower() == a for a in amenity_words) and len(val) > 2:
                        listing.neighborhood = val

            # Property type
            if not listing.property_type:
                pt_match = re.search(r'"roomTypeCategory"\s*:\s*"([^"]+)"', html)
                if pt_match:
                    listing.property_type = pt_match.group(1)

            # Bedrooms, bathrooms, guests
            if not listing.bedrooms:
                bed_match = re.search(r'"bedrooms"\s*:\s*(\d+)', html)
                if bed_match:
                    listing.bedrooms = int(bed_match.group(1))
            if not listing.bathrooms:
                bath_match = re.search(r'"bathrooms"\s*:\s*(\d+)', html)
                if bath_match:
                    listing.bathrooms = int(bath_match.group(1))
            if not listing.guests:
                guest_match = re.search(r'"personCapacity"\s*:\s*(\d+)', html)
                if guest_match:
                    listing.guests = int(guest_match.group(1))

            # Parse bedrooms/bathrooms/guests from title if not found in JSON
            # Title format: "Rental unit in Geneva · ★4.86 · 1 bedroom · 2 beds · 1 bath"
            if listing.title:
                if not listing.bedrooms:
                    t_bed = re.search(r'(\d+)\s+bedroom', listing.title, re.I)
                    if t_bed:
                        listing.bedrooms = int(t_bed.group(1))
                if not listing.bathrooms:
                    t_bath = re.search(r'(\d+)\s+bath', listing.title, re.I)
                    if t_bath:
                        listing.bathrooms = int(t_bath.group(1))
                if not listing.guests:
                    t_guest = re.search(r'(\d+)\s+guest', listing.title, re.I)
                    if t_guest:
                        listing.guests = int(t_guest.group(1))
                if not listing.rating:
                    t_rat = re.search(r'★\s*([\d.]+)', listing.title)
                    if t_rat:
                        listing.rating = float(t_rat.group(1))
                # Extract property type from title: "Rental unit in Geneva"
                if not listing.property_type:
                    t_prop = re.match(r'([A-Za-z\s]+?)\s+in\s+', listing.title)
                    if t_prop:
                        listing.property_type = t_prop.group(1).strip()

                # Clean title: strip metadata parts ("· ★4.86 · 1 bedroom · 2 beds · 1 bath")
                # Keep only the descriptive name, not the stats
                if "·" in listing.title:
                    listing.title = listing.title.split("·")[0].strip()
                # Remove " - Airbnb" suffix
                listing.title = listing.title.split(" - Airbnb")[0].strip()

            # Rating
            if not listing.rating:
                rating_match = re.search(r'"avgRating(?:Localized)?"\s*:\s*"?([\d.]+)"?', html)
                if rating_match:
                    listing.rating = float(rating_match.group(1))

            # Reviews count
            if not listing.reviews_count:
                rev_match = re.search(r'"(?:visible)?[Rr]eview(?:s)?Count"\s*:\s*(\d+)', html)
                if rev_match:
                    listing.reviews_count = int(rev_match.group(1))

            # ── Host profile discovery ──

            # Host profile URL
            if not listing.host_profile_url:
                # Airbnb links to /users/show/{host_id}
                host_id = listing.host_id
                if not host_id:
                    hid_match = re.search(r'"hostId"\s*:\s*"?(\d+)"?', html)
                    if hid_match:
                        host_id = hid_match.group(1)
                        listing.host_id = host_id
                if host_id:
                    listing.host_profile_url = f"https://www.airbnb.com/users/show/{host_id}"

            # Superhost status
            if not listing.host_superhost:
                if re.search(r'"isSuperhost"\s*:\s*true', html, re.IGNORECASE):
                    listing.host_superhost = True
                elif "superhost" in html.lower()[:50000]:
                    listing.host_superhost = True

            # Response rate
            if not listing.host_response_rate:
                rr_match = re.search(r'"response(?:Rate|_rate)"\s*:\s*"?(\d+)%?"?', html)
                if rr_match:
                    listing.host_response_rate = f"{rr_match.group(1)}%"

            # External links — website, IG, email
            # These appear in host profile sections or "about the host" areas
            if not listing.host_website:
                web_match = re.search(
                    r'"(?:website|url)"\s*:\s*"(https?://(?!(?:www\.)?airbnb)[^"]+)"', html)
                if web_match:
                    listing.host_website = web_match.group(1)

            # Instagram handle — look for IG links or @mentions
            if not listing.host_instagram:
                ig_match = (re.search(r'instagram\.com/([a-zA-Z0-9_.]+)', html) or
                            re.search(r'@([a-zA-Z0-9_.]{3,30})\b(?=[^@]*(?:instagram|insta|ig))',
                                      html, re.IGNORECASE))
                if ig_match:
                    handle = ig_match.group(1).strip().rstrip('/')
                    if handle.lower() not in ("airbnb", "p", "reel", "explore"):
                        listing.host_instagram = handle

            # Email — look for email patterns NOT from airbnb
            if not listing.host_email:
                emails = re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', html[:100000])
                for email in emails:
                    if not any(d in email.lower() for d in
                              ("airbnb", "example", "test", "noreply", "support", "info@")):
                        listing.host_email = email
                        break

        except Exception:
            pass

    except Exception as e:
        print(f"    Error enriching {listing.listing_id}: {e}")

    return listing


# ═══════════════════════════════════════════════════════════════
# PAGINATION — click Next button instead of URL manipulation
# ═══════════════════════════════════════════════════════════════

def _click_next_page(page: Page) -> bool:
    """Click the Next/pagination button. Returns True if successful."""
    # Airbnb pagination: aria-label="Next", or nav > a with "Next"
    selectors = [
        'a[aria-label="Next"]',
        'button[aria-label="Next"]',
        'nav a:has-text("Next")',
        'nav button:has-text("Next")',
        # Pagination dots — try the next numbered page
        'nav[aria-label="Search results pagination"] a[aria-current] + a',
        'nav a:not([aria-current]):last-child',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                btn.scroll_into_view_if_needed()
                time.sleep(0.5)
                btn.click()
                # Wait for content to change
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                time.sleep(2)
                return True
        except Exception:
            continue
    return False


# ═══════════════════════════════════════════════════════════════
# NEIGHBORHOOD DETECTION — reverse geocode from coordinates
# ═══════════════════════════════════════════════════════════════

# Approximate centers of known neighborhoods for distance matching
NEIGHBORHOOD_COORDS = {
    # Colombia
    "Medellín": {
        "El Poblado": (6.2086, -75.5695),
        "Laureles": (6.2467, -75.5920),
        "Envigado": (6.1712, -75.5876),
        "Sabaneta": (6.1515, -75.6165),
        "Belén": (6.2328, -75.6062),
        "La Candelaria": (6.2518, -75.5636),
    },
    "Bogotá": {
        "Chapinero": (4.6392, -74.0628),
        "Zona Rosa": (4.6660, -74.0524),
        "La Candelaria": (4.5964, -74.0739),
        "Usaquén": (4.6952, -74.0320),
        "Zona T": (4.6682, -74.0517),
        "Chicó": (4.6763, -74.0470),
    },
    # US — Florida
    "Miami": {
        "South Beach": (25.7826, -80.1341),
        "Wynwood": (25.8010, -80.1990),
        "Brickell": (25.7617, -80.1918),
        "Downtown": (25.7751, -80.1948),
        "Little Havana": (25.7653, -80.2195),
        "Coconut Grove": (25.7270, -80.2410),
        "Design District": (25.8130, -80.1926),
        "Coral Gables": (25.7215, -80.2684),
    },
    "Orlando": {
        "International Drive": (28.4295, -81.4695),
        "Kissimmee": (28.2920, -81.4076),
        "Lake Buena Vista": (28.3747, -81.5224),
        "Downtown Orlando": (28.5383, -81.3792),
        "Winter Park": (28.5999, -81.3392),
        "Dr. Phillips": (28.4500, -81.5050),
    },
    "Tampa": {
        "Ybor City": (27.9600, -82.4380),
        "Downtown Tampa": (27.9506, -82.4572),
        "South Tampa": (27.9200, -82.4850),
        "Channelside": (27.9420, -82.4490),
        "Seminole Heights": (27.9920, -82.4580),
        "Hyde Park": (27.9350, -82.4700),
        "Westshore": (27.9530, -82.5250),
    },
    "Destin": {
        "Crystal Beach": (30.3940, -86.4750),
        "Holiday Isle": (30.3880, -86.4980),
        "Destin Harbor": (30.3930, -86.5100),
        "Miramar Beach": (30.3780, -86.3700),
        "Henderson Park": (30.3760, -86.4500),
    },
    # US — Texas
    "Austin": {
        "Downtown": (30.2672, -97.7431),
        "South Congress": (30.2480, -97.7487),
        "East Austin": (30.2620, -97.7200),
        "Zilker": (30.2650, -97.7730),
        "Rainey Street": (30.2560, -97.7400),
        "Domain": (30.4020, -97.7250),
        "South Lamar": (30.2450, -97.7700),
        "Barton Hills": (30.2520, -97.7750),
    },
    # US — Arizona
    "Scottsdale": {
        "Old Town": (33.4942, -111.9261),
        "North Scottsdale": (33.6200, -111.8850),
        "South Scottsdale": (33.4600, -111.9200),
        "McCormick Ranch": (33.5500, -111.9100),
        "Gainey Ranch": (33.5700, -111.9100),
        "DC Ranch": (33.6300, -111.8600),
    },
    # US — Tennessee
    "Nashville": {
        "Downtown / Broadway": (36.1627, -86.7816),
        "The Gulch": (36.1520, -86.7870),
        "East Nashville": (36.1800, -86.7500),
        "Germantown": (36.1800, -86.7830),
        "12 South": (36.1270, -86.7870),
        "Music Row": (36.1520, -86.7960),
        "Midtown": (36.1530, -86.8050),
        "Hillsboro Village": (36.1340, -86.7980),
    },
    # US — Georgia
    "Savannah": {
        "Historic District": (32.0809, -81.0912),
        "Victorian District": (32.0680, -81.0930),
        "Forsyth Park": (32.0680, -81.0961),
        "Starland District": (32.0600, -81.0960),
        "Midtown": (32.0500, -81.1000),
    },
}


def _detect_neighborhood(lat: float, lng: float, city: str) -> str:
    """Find the closest known neighborhood for a coordinate."""
    if lat == 0 or lng == 0:
        return ""
    neighborhoods = NEIGHBORHOOD_COORDS.get(city, {})
    if not neighborhoods:
        return ""

    from math import radians, cos, sin, asin, sqrt

    def _haversine(lat1, lon1, lat2, lon2):
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        return 2 * 6371000 * asin(sqrt(a))

    closest = None
    min_dist = float("inf")
    for name, (nlat, nlng) in neighborhoods.items():
        d = _haversine(lat, lng, nlat, nlng)
        if d < min_dist:
            min_dist = d
            closest = name

    # Only assign if within ~5km of the center
    if min_dist < 5000:
        return closest
    return ""


# ═══════════════════════════════════════════════════════════════
# MAIN SCRAPER — Playwright pipeline
# ═══════════════════════════════════════════════════════════════

def scrape_city(city_config: dict, max_pages: int = 5, enrich_details: bool = True,
                max_detail_enrichments: int = 20) -> list[Listing]:
    """Scrape Airbnb listings for a city using Playwright.

    Pipeline:
    1. Open search page in real Chrome with persistent profile
    2. Close popups, warm scroll to load listings
    3. Extract dehydrated state data (preferred) or DOM fallback
    4. Paginate via "Next" button or URL offset
    5. Optionally visit detail pages for lat/lng enrichment
    """
    city_name = city_config["name"]
    search_url = city_config.get("airbnb_url",
        f"https://www.airbnb.com/s/{city_name.replace(' ', '-')}/homes")

    print(f"\nScraping {city_name} via Playwright...")
    print(f"  Profile: {PROFILE_DIR}")
    print(f"  Headless: {HEADLESS}")
    print(f"  URL: {search_url}")

    all_listings: list[Listing] = []
    prev_ids: set[str] = set()

    with sync_playwright() as p:
        # Use Playwright's bundled Chromium (not system Chrome which may be in use)
        # channel="chrome" conflicts with running Chrome — use bundled Chromium instead
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=HEADLESS,
            viewport={"width": 1400, "height": 900},
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        attach_nav_monitor(page)

        # Load first page
        print(f"\n  Page 1/{max_pages}: {search_url}")
        goto_with_retry(page, search_url)
        time.sleep(PAGE_LOAD_PAUSE)

        if is_blocked(page):
            wait_for_human_solve(page)
        close_popups(page)
        time.sleep(1)

        for page_num in range(max_pages):
            # Scroll to load all cards on current page
            warm_scroll(page)
            time.sleep(1)

            # Extract listings — try dehydrated state first
            page_listings = []
            data = extract_dehydrated_data(page)
            if data:
                page_listings = extract_listings_from_dehydrated(data, city_name)
                if page_listings:
                    print(f"    Dehydrated state: {len(page_listings)} listings")

            # Fallback to DOM
            if not page_listings:
                page_listings = extract_listings_from_dom(page, city_name)
                print(f"    DOM extraction: {len(page_listings)} listings")

            if not page_listings:
                print(f"    No listings found on page {page_num + 1}")
                break

            # Check for overlap (same pattern as Dubai scraper)
            curr_ids = {l.listing_id for l in page_listings}
            if prev_ids:
                overlap = len(prev_ids & curr_ids) / max(1, len(curr_ids))
                if overlap > 0.85:
                    print(f"    Overlap {overlap:.0%} — pagination stuck, stopping")
                    break

            prev_ids = curr_ids
            all_listings.extend(page_listings)
            print(f"    Running total: {len(all_listings)} listings")

            # Navigate to next page via button click (URL offset doesn't work)
            if page_num + 1 < max_pages:
                next_clicked = _click_next_page(page)
                if not next_clicked:
                    print(f"    No more pages (Next button not found)")
                    break
                print(f"\n  Page {page_num + 2}/{max_pages}: {page.url}")
                time.sleep(PAGE_LOAD_PAUSE)

                if is_blocked(page):
                    wait_for_human_solve(page)
                close_popups(page)

        # Deduplicate
        seen = set()
        unique = []
        for l in all_listings:
            if l.listing_id not in seen:
                seen.add(l.listing_id)
                unique.append(l)

        print(f"\n  {len(unique)} unique listings found")

        # Enrich detail pages for listings missing coordinates
        if enrich_details:
            needs_enrichment = [l for l in unique if l.lat == 0 or l.lng == 0]
            to_enrich = needs_enrichment[:max_detail_enrichments]
            if to_enrich:
                print(f"\n  Enriching {len(to_enrich)} listings from detail pages...")
                for i, listing in enumerate(to_enrich):
                    print(f"    [{i+1}/{len(to_enrich)}] {listing.listing_id}")
                    enrich_listing_from_detail(page, listing)
                    time.sleep(random.uniform(1.5, 3.0))

        # Assign neighborhoods from coordinates where missing
        for l in unique:
            if not l.neighborhood and l.lat != 0:
                l.neighborhood = _detect_neighborhood(l.lat, l.lng, city_name)

        context.close()

    print(f"\n  Final: {len(unique)} listings for {city_name}")
    return unique


def save_listings(listings: list[Listing], output_path: str):
    """Save listings to JSON."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump([asdict(l) for l in listings], f, indent=2, ensure_ascii=False)
    print(f"Saved {len(listings)} listings to {output_path}")


def load_listings(path: str) -> list[Listing]:
    """Load listings from JSON."""
    with open(path) as f:
        data = json.load(f)
    return [Listing(**d) for d in data]
