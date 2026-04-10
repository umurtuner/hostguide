"""HostGuide — production web app.

Flow:
1. Landing page: host pastes Airbnb listing URL
2. Stripe Checkout: $5 payment
3. On success: scrape listing → enrich → generate guide
4. Serve guide at unique token URL (24h expiry)

Run:
    cd hostguide && python -m src.app
    # Requires: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET env vars
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

import requests as http_requests
import stripe
from flask import (Flask, abort, jsonify, redirect, render_template_string,
                   request, send_file)
from flask_cors import CORS

# ── Config ──
BASE = Path(__file__).parent.parent
OUTPUT = BASE / "output"
ORDERS_FILE = BASE / "data" / "orders.json"
ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)

STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DOMAIN = os.environ.get("HOSTGUIDE_DOMAIN", "http://localhost:5555")
ADMIN_SECRET = os.environ.get("HOSTGUIDE_ADMIN_SECRET", "dev-admin-secret")
REDIS_URL = os.environ.get("REDIS_URL", "")

# ── Redis (persistent storage for Render) ──
_redis = None
if REDIS_URL:
    try:
        import redis as _redis_lib
        _pool_kwargs = dict(decode_responses=True, socket_timeout=5,
                            socket_connect_timeout=3, retry_on_timeout=True,
                            max_connections=5)
        if REDIS_URL.startswith("rediss://"):
            _pool_kwargs["ssl_cert_reqs"] = None
        _redis = _redis_lib.Redis.from_url(REDIS_URL, **_pool_kwargs)
        _redis.ping()
        print("[storage] Redis connected — using persistent storage")
    except Exception as e:
        print(f"[storage] Redis unavailable ({e}) — falling back to JSON files")
        _redis = None
else:
    print("[storage] No REDIS_URL — using JSON file storage (local dev)")

if STRIPE_SECRET:
    stripe.api_key = STRIPE_SECRET

app = Flask(__name__)
CORS(app)


# ── Dashboard auth (HMAC-signed email links) ──
def _sign_email(email: str) -> str:
    """Create HMAC signature for a dashboard email link."""
    key = (ADMIN_SECRET or "fallback-key").encode()
    return hmac.new(key, email.lower().strip().encode(), hashlib.sha256).hexdigest()[:16]


def _dashboard_url(email: str, **params) -> str:
    """Build a signed dashboard URL."""
    email = email.lower().strip()
    sig = _sign_email(email)
    qs = f"email={email}&sig={sig}"
    for k, v in params.items():
        qs += f"&{k}={v}"
    return f"/dashboard?{qs}"


def _verify_dashboard_sig(email: str, sig: str) -> bool:
    """Verify HMAC signature on dashboard access."""
    return hmac.compare_digest(sig, _sign_email(email))


# ═══════════════════════════════════════════════════════════════
# ORDER STORAGE (Redis if available, JSON file fallback)
# ═══════════════════════════════════════════════════════════════

def _redis_order_key(token: str) -> str:
    return f"hg:order:{token}"


def _load_orders() -> dict:
    """Load all orders. Redis: scan keys. File: read JSON."""
    if _redis:
        orders = {}
        for key in _redis.scan_iter("hg:order:*"):
            token = key.replace("hg:order:", "")
            raw = _redis.get(key)
            if raw:
                orders[token] = json.loads(raw)
        return orders
    if ORDERS_FILE.exists():
        return json.loads(ORDERS_FILE.read_text())
    return {}


def _save_orders(orders: dict):
    """Save all orders. Only used for JSON fallback."""
    if not _redis:
        ORDERS_FILE.write_text(json.dumps(orders, indent=2))


def _create_order(airbnb_url: str, email: str, city: str = "") -> str:
    """Create a pending order, return order token."""
    token = secrets.token_urlsafe(24)
    order = {
        "airbnb_url": airbnb_url,
        "email": email,
        "city": city,
        "status": "pending",  # pending → paid → generating → generated → expired
        "created": datetime.utcnow().isoformat(),
        "expires": None,  # Set when guide is generated
        "guide_path": None,
        "stripe_session_id": None,
    }
    if _redis:
        _redis.set(_redis_order_key(token), json.dumps(order), ex=86400 * 7)  # 7 day TTL
    else:
        orders = _load_orders()
        orders[token] = order
        _save_orders(orders)
    return token


def _get_order(token: str) -> dict | None:
    if _redis:
        raw = _redis.get(_redis_order_key(token))
        if not raw:
            return None
        order = json.loads(raw)
    else:
        orders = _load_orders()
        order = orders.get(token)
        if not order:
            return None
    # Check expiry (only for single-purchase guides, not subscription users)
    if order["status"] == "generated" and order.get("expires"):
        if datetime.utcnow() > datetime.fromisoformat(order["expires"]):
            order["status"] = "expired"
            _update_order(token, status="expired")
    return order


def _update_order(token: str, **kwargs):
    if _redis:
        raw = _redis.get(_redis_order_key(token))
        if raw:
            order = json.loads(raw)
            order.update(kwargs)
            _redis.set(_redis_order_key(token), json.dumps(order), ex=86400 * 7)
    else:
        orders = _load_orders()
        if token in orders:
            orders[token].update(kwargs)
            _save_orders(orders)


# ═══════════════════════════════════════════════════════════════
# CREDITS SYSTEM (Redis if available, JSON file fallback)
# ═══════════════════════════════════════════════════════════════

CREDITS_FILE = BASE / "data" / "credits.json"


def _redis_credits_key(email: str) -> str:
    return f"hg:credits:{email.lower().strip()}"


def _load_credits() -> dict:
    if _redis:
        credits = {}
        for key in _redis.scan_iter("hg:credits:*"):
            email = key.replace("hg:credits:", "")
            raw = _redis.get(key)
            if raw:
                credits[email] = json.loads(raw)
        return credits
    if CREDITS_FILE.exists():
        return json.loads(CREDITS_FILE.read_text())
    return {}


def _save_credits(credits: dict):
    if not _redis:
        CREDITS_FILE.write_text(json.dumps(credits, indent=2))


def _get_user_credits(email: str) -> dict:
    """Get or create a user's credit record."""
    email = email.lower().strip()
    if _redis:
        raw = _redis.get(_redis_credits_key(email))
        if raw:
            return json.loads(raw)
        user = {"credits": 0, "tier": "none", "guides_generated": [],
                "stripe_customer_id": None}
        _redis.set(_redis_credits_key(email), json.dumps(user))
        return user
    credits = _load_credits()
    if email not in credits:
        credits[email] = {
            "credits": 0,
            "tier": "none",
            "guides_generated": [],
            "stripe_customer_id": None,
        }
        _save_credits(credits)
    return credits[email]


def _save_user_credits(email: str, user: dict):
    """Save a single user's credit record."""
    email = email.lower().strip()
    if _redis:
        _redis.set(_redis_credits_key(email), json.dumps(user))
    else:
        credits = _load_credits()
        credits[email] = user
        _save_credits(credits)


def _add_credits(email: str, amount: int, tier: str = "single",
                  stripe_customer_id: str | None = None,
                  dedup_key: str | None = None):
    """Add guide credits to a user. dedup_key prevents double-adding."""
    email = email.lower().strip()
    user = _get_user_credits(email)
    if "processed_payments" not in user:
        user["processed_payments"] = []
    # Dedup: skip if this payment was already processed
    if dedup_key:
        if dedup_key in user.get("processed_payments", []):
            return
        user["processed_payments"].append(dedup_key)
    user["credits"] += amount
    # Don't downgrade tier: single purchase shouldn't overwrite active subscription
    current_tier = user.get("tier", "none")
    tier_priority = {"none": 0, "single": 1, "starter": 2, "pro": 3}
    if tier_priority.get(tier, 0) >= tier_priority.get(current_tier, 0):
        user["tier"] = tier
    if stripe_customer_id:
        user["stripe_customer_id"] = stripe_customer_id
    _save_user_credits(email, user)


def _use_credit(email: str, token: str) -> bool:
    """Use 1 credit for a guide. Returns False if no credits."""
    email = email.lower().strip()
    user = _get_user_credits(email)
    if user["credits"] <= 0:
        return False
    user["credits"] -= 1
    user["guides_generated"].append(token)
    _save_user_credits(email, user)
    return True


# ═══════════════════════════════════════════════════════════════
# EMAIL SUBSCRIBERS (CRM list)
# ═══════════════════════════════════════════════════════════════

SUBSCRIBERS_FILE = BASE / "data" / "subscribers.json"


def _save_email_subscriber(email: str):
    """Save consenting email to subscriber list (Redis or JSON)."""
    email = email.lower().strip()
    if not email:
        return
    if _redis:
        _redis.hset("hg:subscribers", email, json.dumps({
            "email": email,
            "subscribed_at": datetime.utcnow().isoformat(),
            "source": "guide_form",
        }))
    else:
        subs = {}
        if SUBSCRIBERS_FILE.exists():
            subs = json.loads(SUBSCRIBERS_FILE.read_text())
        if email not in subs:
            subs[email] = {
                "subscribed_at": datetime.utcnow().isoformat(),
                "source": "guide_form",
            }
            SUBSCRIBERS_FILE.write_text(json.dumps(subs, indent=2))


def _get_all_subscribers() -> dict:
    """Get all subscribers for export."""
    if _redis:
        raw = _redis.hgetall("hg:subscribers")
        return {k: json.loads(v) for k, v in raw.items()}
    if SUBSCRIBERS_FILE.exists():
        return json.loads(SUBSCRIBERS_FILE.read_text())
    return {}


# ═══════════════════════════════════════════════════════════════
# GUIDE GENERATION PIPELINE
# ═══════════════════════════════════════════════════════════════

def _extract_listing_id(url: str) -> str:
    """Extract Airbnb listing ID from URL."""
    import re
    # Handles: /rooms/123456, /h/listing-name (resolves to rooms/ID)
    match = re.search(r'/rooms/(\d+)', url)
    if match:
        return match.group(1)
    # Handle /h/ vanity URLs — extract the slug
    match = re.search(r'/h/([\w-]+)', url)
    if match:
        return match.group(1)
    return ""


def _geocode_city(city: str) -> tuple[float, float]:
    """Geocode a city name to lat/lng using Nominatim (free, no key)."""
    try:
        resp = http_requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": city, "format": "json", "limit": 1},
            headers={"User-Agent": "HostGuide/1.0 (hello@host-guide.net)"},
            timeout=10,
        )
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        print(f"Geocode error for '{city}': {e}")
    return 0.0, 0.0


def _reverse_geocode(lat: float, lng: float) -> dict:
    """Reverse geocode lat/lng to city + neighborhood using Nominatim."""
    result = {"city": "", "neighborhood": "", "country": "", "country_code": ""}
    try:
        resp = http_requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lng, "format": "json", "zoom": 16},
            headers={"User-Agent": "HostGuide/1.0 (hello@host-guide.net)"},
            timeout=10,
        )
        data = resp.json()
        addr = data.get("address", {})
        result["city"] = (addr.get("city") or addr.get("town") or
                         addr.get("village") or addr.get("municipality") or "")
        result["neighborhood"] = (addr.get("suburb") or addr.get("neighbourhood") or
                                  addr.get("quarter") or addr.get("city_district") or "")
        result["country"] = addr.get("country", "")
        result["country_code"] = addr.get("country_code", "").upper()
        print(f"Reverse geocode {lat},{lng} → city={result['city']}, "
              f"neighborhood={result['neighborhood']}, country={result['country_code']}")
    except Exception as e:
        print(f"Reverse geocode error: {e}")
    return result


# Known city configs for the guide generator
CITY_CONFIGS = {
    "miami": {"name": "Miami", "country": "US"},
    "dublin": {"name": "Dublin", "country": "IE"},
    "lisbon": {"name": "Lisbon", "country": "PT"},
    "madrid": {"name": "Madrid", "country": "ES"},
    "medellin": {"name": "Medellín", "country": "CO"},
    "bogota": {"name": "Bogotá", "country": "CO"},
    "rochester": {"name": "Rochester", "country": "US"},
    "orlando": {"name": "Orlando", "country": "US"},
    "tampa": {"name": "Tampa", "country": "US"},
    "destin": {"name": "Destin", "country": "US"},
    "austin": {"name": "Austin", "country": "US"},
    "nashville": {"name": "Nashville", "country": "US"},
    "savannah": {"name": "Savannah", "country": "US"},
    "scottsdale": {"name": "Scottsdale", "country": "US"},
}


def _get_city_config(city: str) -> dict:
    """Get city config, or build a generic one."""
    key = city.lower().strip()
    if key in CITY_CONFIGS:
        return CITY_CONFIGS[key]
    # Generic config — works for any city
    return {"name": city.strip(), "country": ""}


def _generate_guide_for_order(token: str) -> bool:
    """Run the full pipeline: Playwright scrape → OSM enrich → generate HTML guide.
    Falls back to geocode-only if Playwright fails."""
    order = _get_order(token)
    if not order or order["status"] not in ("paid", "generating"):
        return False

    airbnb_url = order["airbnb_url"]
    listing_id = _extract_listing_id(airbnb_url)
    if not listing_id:
        print(f"Could not extract listing ID from {airbnb_url}")
        return False

    try:
        import sys
        sys.path.insert(0, str(BASE))
        from src.scraper import Listing, enrich_listing_from_detail
        from src.enricher import enrich_without_api
        from src.guide_generator import generate_guide

        # Step 0: Use cached meta from preview, or fetch fresh
        cached_meta = order.get("meta_cache")
        if cached_meta:
            try:
                meta = json.loads(cached_meta)
                meta.setdefault("lat", 0.0)
                meta.setdefault("lng", 0.0)
                meta.setdefault("host_name", "")
                meta.setdefault("neighborhood", "")
                meta.setdefault("bedrooms", 0)
                meta.setdefault("bathrooms", 0)
                meta.setdefault("guests", 0)
                meta.setdefault("amenities", [])
                meta.setdefault("photos", [])
                print(f"Using cached meta for {listing_id}")
            except Exception:
                meta = _fetch_listing_meta(airbnb_url)
        else:
            meta = _fetch_listing_meta(airbnb_url)

        listing = Listing(
            listing_id=listing_id,
            title=meta.get("title", ""),
            url=airbnb_url,
            city=order.get("city", "") or meta.get("city", ""),
            neighborhood=meta.get("neighborhood", ""),
            lat=meta.get("lat", 0.0),
            lng=meta.get("lng", 0.0),
            host_name=meta.get("host_name", ""),
            property_type=meta.get("property_type", ""),
            bedrooms=meta.get("bedrooms", 0),
            bathrooms=meta.get("bathrooms", 0),
            guests=meta.get("guests", 0),
            rating=meta.get("rating", 0.0),
            reviews_count=meta.get("reviews_count", 0),
            host_superhost=meta.get("host_superhost", False),
            host_response_rate=meta.get("host_response_rate", ""),
            amenities=meta.get("amenities", []),
            photos=meta.get("photos", []),
        )
        print(f"HTTP meta populated listing: lat={listing.lat}, lng={listing.lng}, "
              f"host={listing.host_name}, neighborhood={listing.neighborhood}")

        # Step 1: Try Playwright to enrich further (may get more precise coords)
        try:
            from playwright.sync_api import sync_playwright
            print(f"Launching Playwright for listing {listing_id}...")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                page = browser.new_page()
                listing = enrich_listing_from_detail(page, listing)
                browser.close()
            print(f"Playwright got: lat={listing.lat}, lng={listing.lng}, "
                  f"city={listing.city}, host={listing.host_name}")
        except Exception as e:
            print(f"Playwright failed, falling back to geocode: {e}")

        # Step 2: If Playwright didn't get coords, fall back to geocoding
        if listing.lat == 0 and listing.lng == 0:
            city_input = order.get("city", "") or listing.city
            if city_input:
                # Geocode full input (e.g. "Eaux-Vives, Geneva") for precise location
                listing.lat, listing.lng = _geocode_city(city_input)
                # Parse neighborhood and city from input like "Eaux-Vives, Geneva"
                parts = [p.strip() for p in city_input.split(",")]
                if len(parts) >= 2:
                    listing.neighborhood = parts[0]
                    listing.city = parts[-1]
                elif not listing.city:
                    listing.city = city_input
                print(f"Geocoded '{city_input}' → {listing.lat},{listing.lng} "
                      f"(neighborhood={listing.neighborhood})")

        if listing.lat == 0 and listing.lng == 0:
            print(f"No coordinates for listing {listing_id}")
            return False

        # Step 2b: Reverse geocode to fill city/neighborhood if still missing
        if not listing.city or not listing.neighborhood:
            geo = _reverse_geocode(listing.lat, listing.lng)
            if not listing.city and geo["city"]:
                listing.city = geo["city"]
            if not listing.neighborhood and geo["neighborhood"]:
                listing.neighborhood = geo["neighborhood"]

        # Step 3: Enrich with OSM Overpass (free, no API key)
        city_name = listing.city or order.get("city", "").split(",")[-1].strip()
        city_config = _get_city_config(city_name)
        if not listing.city:
            listing.city = city_config["name"]

        print(f"Enriching {listing.city} at {listing.lat},{listing.lng}...")
        enriched = enrich_without_api(listing.lat, listing.lng, city_config)

        # Step 4: Generate guide (HTML)
        print(f"Generating guide for listing {listing_id}...")
        guide = generate_guide(listing, enriched, city_config, use_claude=False)

        # Step 5: Save guide HTML
        guide_dir = OUTPUT / listing.city.lower() / "guides"
        guide_dir.mkdir(parents=True, exist_ok=True)
        html_path = guide_dir / f"{listing_id}_guide.html"
        html_path.write_text(guide.content_html, encoding="utf-8")

        # Step 6: Generate PDF using Playwright (renders full styled HTML)
        pdf_path = html_path.with_suffix(".pdf")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                page = browser.new_page()
                page.goto(f"file://{html_path.resolve()}", wait_until="networkidle", timeout=30000)
                page.pdf(path=str(pdf_path), format="A4",
                         margin={"top": "15mm", "bottom": "15mm",
                                 "left": "12mm", "right": "12mm"},
                         print_background=True)
                browser.close()
            print(f"PDF generated via Playwright: {pdf_path}")
        except Exception as e:
            import traceback
            print(f"Playwright PDF failed: {e}\n{traceback.format_exc()}")
            pdf_path.unlink(missing_ok=True)

        # Update order — single purchases expire in 24h, subscriptions don't
        tier = order.get("tier", "single")
        expires = (datetime.utcnow() + timedelta(hours=24)).isoformat() if tier == "single" else None
        _update_order(token, status="generated", guide_path=str(html_path), expires=expires)
        print(f"Guide ready for token {token}: {html_path}")
        return True

    except Exception as e:
        print(f"Generation error for token {token}: {e}")
        import traceback
        traceback.print_exc()
        return False


def _refund_credit(token: str):
    """Refund 1 credit if the guide generation failed."""
    order = _get_order(token)
    if not order:
        return
    email = order.get("email", "").lower().strip()
    if not email:
        return
    user = _get_user_credits(email)
    if user:
        user["credits"] += 1
        # Remove the token from guides_generated
        if token in user.get("guides_generated", []):
            user["guides_generated"].remove(token)
        _save_user_credits(email, user)
        print(f"[refund] Restored 1 credit to {email} (failed generation {token})")


def _generate_in_background(token: str):
    """Run guide generation in a background thread."""
    try:
        success = _generate_guide_for_order(token)
        if not success:
            _update_order(token, status="failed")
            _refund_credit(token)
            print(f"Guide generation failed for {token}")
    except Exception as e:
        _update_order(token, status="failed")
        _refund_credit(token)
        print(f"Background generation failed for {token}: {e}")


def _fetch_listing_meta(airbnb_url: str) -> dict:
    """Fetch rich listing data from Airbnb via HTTP — no Playwright needed.

    Extracts OG tags + embedded JSON data for: lat/lng, host name, bedrooms,
    bathrooms, guests, neighborhood, property type, rating, reviews, amenities, photos.
    """
    meta = {
        "title": "", "city": "", "image": "", "description": "",
        "lat": 0.0, "lng": 0.0, "host_name": "", "neighborhood": "",
        "property_type": "", "bedrooms": 0, "bathrooms": 0, "guests": 0,
        "rating": 0.0, "reviews_count": 0, "amenities": [], "photos": [],
        "host_superhost": False, "host_response_rate": "",
    }
    try:
        resp = http_requests.get(airbnb_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }, timeout=15, allow_redirects=True)
        html = resp.text

        # ── OG tags (always available) ──
        og_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
        og_desc = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html)
        og_image = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', html)

        if og_title:
            meta["title"] = og_title.group(1)
        if og_desc:
            meta["description"] = og_desc.group(1)
        if og_image:
            meta["image"] = og_image.group(1)

        # Parse title: "Rental unit in Geneva · ★4.33 · 1 bedroom..." → extract parts
        if meta["title"] and "·" in meta["title"]:
            parts = meta["title"].split("·")
            meta["title"] = parts[0].strip()
            if len(parts) >= 2:
                meta["city"] = parts[-1].strip().split(",")[0].strip()
            # Extract property type from title like "Rental unit in Geneva"
            type_match = re.match(r'([\w\s]+?)\s+in\s+', meta["title"])
            if type_match:
                meta["property_type"] = type_match.group(1).strip()
        if not meta["city"] and meta["description"]:
            desc_parts = meta["description"].split(" in ")
            if len(desc_parts) >= 2:
                meta["city"] = desc_parts[-1].split(",")[0].split(".")[0].strip()

        # ── Parse OG title metadata parts (bedrooms, guests, etc.) ──
        if og_title:
            full_title = og_title.group(1)
            bed_m = re.search(r'(\d+)\s*bed(?:room)?s?', full_title, re.I)
            bath_m = re.search(r'(\d+)\s*bath', full_title, re.I)
            guest_m = re.search(r'(\d+)\s*guest', full_title, re.I)
            rating_m = re.search(r'★\s*([\d.]+)', full_title)
            if bed_m:
                meta["bedrooms"] = int(bed_m.group(1))
            if bath_m:
                meta["bathrooms"] = int(bath_m.group(1))
            if guest_m:
                meta["guests"] = int(guest_m.group(1))
            if rating_m:
                meta["rating"] = float(rating_m.group(1))

        # ── Extract embedded JSON data from page HTML ──
        # Airbnb embeds rich listing data in script tags and deferred state

        # Method 1: data-deferred-state (most common on Airbnb)
        deferred_match = re.search(
            r'<script\s+id="data-deferred-state(?:-\d+)?"\s+type="application/json"[^>]*>(.*?)</script>',
            html, re.DOTALL)

        # Method 2: bootstrapData or inline JSON with listing data
        if not deferred_match:
            deferred_match = re.search(
                r'<script[^>]*>\s*window\.__NEXT_DATA__\s*=\s*(\{.*?\})\s*;?\s*</script>',
                html, re.DOTALL)

        if deferred_match:
            try:
                raw_json = deferred_match.group(1)
                # Truncate at 500KB to avoid parsing massive payloads
                if len(raw_json) < 500_000:
                    data = json.loads(raw_json)
                    _extract_deep_listing_data(data, meta)
            except (json.JSONDecodeError, Exception) as e:
                print(f"  JSON parse from deferred state failed: {e}")

        # ── Regex fallback for key fields from raw HTML ──
        if not meta["lat"]:
            lat_m = re.search(r'"lat(?:itude)?":\s*([-\d.]+)', html)
            lng_m = re.search(r'"l(?:on|ng|ongitude)":\s*([-\d.]+)', html)
            if lat_m and lng_m:
                meta["lat"] = float(lat_m.group(1))
                meta["lng"] = float(lng_m.group(1))

        if not meta["host_name"]:
            host_m = (re.search(r'"firstName"\s*:\s*"([^"]+)"', html) or
                      re.search(r'Hosted by\s+([A-Z][a-z]+)', html))
            if host_m:
                meta["host_name"] = host_m.group(1).strip()

        if not meta["neighborhood"]:
            loc_m = (re.search(r'"neighborhood"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', html) or
                     re.search(r'"locationTitle"\s*:\s*"([^"]+)"', html) or
                     re.search(r'"publicAddress"\s*:\s*"([^"]+)"', html))
            if loc_m:
                meta["neighborhood"] = loc_m.group(1).strip()

        if not meta["bedrooms"]:
            bed_m = re.search(r'"bedrooms"\s*:\s*(\d+)', html)
            if bed_m:
                meta["bedrooms"] = int(bed_m.group(1))
        if not meta["bathrooms"]:
            bath_m = re.search(r'"bathrooms"\s*:\s*(\d+)', html)
            if bath_m:
                meta["bathrooms"] = int(bath_m.group(1))
        if not meta["guests"]:
            guest_m = re.search(r'"personCapacity"\s*:\s*(\d+)', html)
            if guest_m:
                meta["guests"] = int(guest_m.group(1))

        if not meta["rating"]:
            rat_m = re.search(r'"avgRating(?:Localized)?"\s*:\s*"?([\d.]+)"?', html)
            if rat_m:
                meta["rating"] = float(rat_m.group(1))
        if not meta["reviews_count"]:
            rev_m = re.search(r'"(?:visible)?[Rr]eview(?:s)?Count"\s*:\s*(\d+)', html)
            if rev_m:
                meta["reviews_count"] = int(rev_m.group(1))

        if not meta["host_superhost"]:
            if re.search(r'"isSuperhost"\s*:\s*true', html, re.I):
                meta["host_superhost"] = True

        if not meta["host_response_rate"]:
            rr_m = re.search(r'"response(?:Rate|_rate)"\s*:\s*"?(\d+)%?"?', html)
            if rr_m:
                meta["host_response_rate"] = f"{rr_m.group(1)}%"

        # Extract amenities
        if not meta["amenities"]:
            amenity_matches = re.findall(r'"localizedName"\s*:\s*"([^"]{2,50})"', html)
            if amenity_matches:
                # Deduplicate while preserving order
                seen = set()
                for a in amenity_matches:
                    if a not in seen and not a.startswith('{'):
                        seen.add(a)
                        meta["amenities"].append(a)

        # Extract photo URLs
        if not meta["photos"] and meta["image"]:
            meta["photos"].append(meta["image"])
        photo_matches = re.findall(r'"baseUrl"\s*:\s*"(https://a0\.muscache\.com/[^"]+)"', html)
        if photo_matches:
            meta["photos"] = list(dict.fromkeys(photo_matches))[:10]  # Top 10 unique

        print(f"  Meta extracted: lat={meta['lat']}, lng={meta['lng']}, "
              f"host={meta['host_name']}, neighborhood={meta['neighborhood']}, "
              f"beds={meta['bedrooms']}, baths={meta['bathrooms']}, "
              f"guests={meta['guests']}, amenities={len(meta['amenities'])}, "
              f"photos={len(meta['photos'])}")

    except Exception as e:
        print(f"Meta fetch error: {e}")

    return meta


def _extract_deep_listing_data(data: dict, meta: dict, depth: int = 0):
    """Walk Airbnb's embedded JSON tree to extract listing details."""
    if depth > 12 or not isinstance(data, dict):
        return

    # Check if this dict looks like a listing object
    lid = data.get("listingId") or data.get("id") or data.get("listing_id")
    if lid and (data.get("name") or data.get("title") or data.get("roomTypeCategory")):
        # Coordinates
        coord = data.get("coordinate") or data.get("location") or {}
        if isinstance(coord, dict):
            lat = coord.get("latitude") or coord.get("lat")
            lng = coord.get("longitude") or coord.get("lng") or coord.get("lon")
            if lat and lng:
                meta["lat"] = float(lat)
                meta["lng"] = float(lng)

        # Host
        host_first = (data.get("user", {}).get("firstName") if isinstance(data.get("user"), dict) else None) or \
                     (data.get("primaryHost", {}).get("firstName") if isinstance(data.get("primaryHost"), dict) else None)
        if host_first and not meta["host_name"]:
            meta["host_name"] = host_first

        # Property details
        if not meta["bedrooms"] and data.get("bedrooms"):
            meta["bedrooms"] = int(data["bedrooms"])
        if not meta["bathrooms"] and data.get("bathrooms"):
            meta["bathrooms"] = int(data["bathrooms"])
        if not meta["guests"] and (data.get("personCapacity") or data.get("guestCapacity")):
            meta["guests"] = int(data.get("personCapacity") or data.get("guestCapacity"))
        if not meta["property_type"] and (data.get("roomTypeCategory") or data.get("roomType")):
            meta["property_type"] = data.get("roomTypeCategory") or data.get("roomType")

        # Neighborhood
        nb = data.get("neighborhood")
        if isinstance(nb, dict) and nb.get("name") and not meta["neighborhood"]:
            meta["neighborhood"] = nb["name"]
        elif isinstance(nb, str) and nb and not meta["neighborhood"]:
            meta["neighborhood"] = nb
        if not meta["neighborhood"] and data.get("publicAddress"):
            meta["neighborhood"] = data["publicAddress"]

        # Rating & reviews
        if not meta["rating"]:
            r = data.get("avgRating") or data.get("avgRatingLocalized")
            if r:
                meta["rating"] = float(r)
        if not meta["reviews_count"]:
            rc = data.get("reviewsCount") or data.get("visibleReviewCount")
            if rc:
                meta["reviews_count"] = int(rc)

        # Superhost
        if data.get("isSuperhost"):
            meta["host_superhost"] = True

    # Recurse into values
    for v in data.values():
        if isinstance(v, dict):
            _extract_deep_listing_data(v, meta, depth + 1)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    _extract_deep_listing_data(item, meta, depth + 1)


# ═══════════════════════════════════════════════════════════════
# LANDING PAGE
# ═══════════════════════════════════════════════════════════════

LANDING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HostGuide — Neighborhood Guides for Airbnb Hosts</title>
<meta name="description" content="Auto-generate beautiful neighborhood guides for your Airbnb guests. Restaurants, groceries, transit, local tips — based on your listing's exact location.">
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<script>
tailwind.config = {
  theme: {
    extend: {
      colors: {
        teal: { 50:'#e0f2f1', 100:'#b2dfdb', 200:'#80cbc4', 300:'#4db6ac',
                400:'#26a69a', 500:'#009688', 600:'#00897b', 700:'#00796b',
                800:'#00695c', 900:'#004d40' },
      },
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
    }
  }
}
</script>
<style>
  .gradient-hero { background: linear-gradient(135deg, #00897b 0%, #004d40 100%); }
  .glass-card { background: rgba(255,255,255,0.95); backdrop-filter: blur(20px); }
  .fade-in { animation: fadeIn 0.6s ease-out; }
  @keyframes fadeIn { from { opacity:0; transform:translateY(16px); } to { opacity:1; transform:translateY(0); } }
  .faq-answer { max-height:0; overflow:hidden; transition: max-height 0.3s ease; }
  .faq-answer.open { max-height: 200px; }
  .feature-card:hover { transform: translateY(-2px); box-shadow: 0 12px 40px rgba(0,0,0,0.08); }
  .cta-btn { transition: all 0.2s; }
  .cta-btn:hover { transform: translateY(-1px); box-shadow: 0 8px 24px rgba(0,105,92,0.35); }
</style>
</head>
<body class="font-sans text-gray-900 bg-gray-50 antialiased">

<!-- ════════ BANNERS ════════ -->
<div id="cancelBanner" class="hidden bg-amber-50 border-b border-amber-200 px-6 py-3 text-center text-sm text-amber-800">
  Payment was cancelled. No charge was made. You can try again whenever you're ready.
  <button onclick="this.parentElement.classList.add('hidden')" class="ml-3 text-amber-600 hover:text-amber-800 font-semibold">&times;</button>
</div>
<div id="errorBanner" class="hidden bg-red-50 border-b border-red-200 px-6 py-3 text-center text-sm text-red-800">
  Something went wrong with the payment. Please try again.
  <button onclick="this.parentElement.classList.add('hidden')" class="ml-3 text-red-600 hover:text-red-800 font-semibold">&times;</button>
</div>
<script>
if (location.search.includes('cancelled=1')) document.getElementById('cancelBanner').classList.remove('hidden');
if (location.search.includes('error=payment')) document.getElementById('errorBanner').classList.remove('hidden');
</script>

<!-- ════════ NAV ════════ -->
<nav class="gradient-hero">
  <div class="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
    <div class="flex items-center gap-2.5">
      <svg width="32" height="32" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect width="32" height="32" rx="8" fill="white" fill-opacity="0.2"/>
        <path d="M10 8v16M22 8v16M10 16h12" stroke="white" stroke-width="2.5" stroke-linecap="round"/>
        <circle cx="22" cy="10" r="3" fill="#4DB6AC"/>
        <path d="M21 9.5l1 1 2-2" stroke="white" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span class="text-white font-semibold text-lg tracking-tight">HostGuide</span>
    </div>
    <a href="#pricing" class="text-white/80 hover:text-white text-sm font-medium transition">Pricing</a>
  </div>
</nav>

<!-- ════════ HERO ════════ -->
<section class="gradient-hero pb-32 pt-16 px-6 text-center text-white">
  <div class="max-w-2xl mx-auto fade-in">
    <div class="inline-block mb-6 px-4 py-1.5 bg-white/15 rounded-full text-sm font-medium">
      Works for any city worldwide
    </div>
    <h1 class="text-4xl md:text-5xl font-extrabold leading-tight mb-5">
      Stop answering the same<br>guest questions
    </h1>
    <p class="text-lg md:text-xl text-white/85 max-w-xl mx-auto leading-relaxed">
      Paste your Airbnb link, get a printable neighborhood guide with restaurants, groceries, transit, and local tips — in minutes.
    </p>
  </div>
</section>

<!-- ════════ FORM CARD (overlapping hero) ════════ -->
<section class="max-w-lg mx-auto px-5 -mt-20 relative z-10 mb-20">
  <div class="glass-card rounded-2xl shadow-xl p-8 md:p-10 fade-in">
    <h2 class="text-xl font-bold text-center mb-1">Generate Your Guide</h2>
    <p class="text-sm text-gray-500 text-center mb-7">Paste your Airbnb link and we'll do the rest.</p>
    <form id="guideForm" action="/preview" method="POST">
      <div class="mb-4">
        <label for="airbnb_url" class="block text-xs font-semibold text-gray-600 mb-1.5">Airbnb Listing URL</label>
        <input type="url" id="airbnb_url" name="airbnb_url" required
               placeholder="https://www.airbnb.com/rooms/123456..."
               class="w-full px-4 py-3 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-teal-500/30 focus:border-teal-500 transition placeholder:text-gray-400">
      </div>
      <div class="mb-4">
        <label for="city" class="block text-xs font-semibold text-gray-600 mb-1.5">Neighborhood &amp; City <span class="text-gray-400 font-normal">(optional — auto-detected from listing)</span></label>
        <input type="text" id="city" name="city"
               placeholder="e.g. Eaux-Vives, Geneva"
               class="w-full px-4 py-3 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-teal-500/30 focus:border-teal-500 transition placeholder:text-gray-400">
      </div>
      <div class="mb-5">
        <label for="email" class="block text-xs font-semibold text-gray-600 mb-1.5">Your Email</label>
        <input type="email" id="email" name="email" required
               placeholder="you@example.com"
               class="w-full px-4 py-3 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-teal-500/30 focus:border-teal-500 transition placeholder:text-gray-400">
      </div>
      <div class="mb-5">
        <label class="flex items-start gap-2 cursor-pointer">
          <input type="checkbox" name="email_consent" value="yes" checked
                 class="mt-0.5 accent-teal-600 w-4 h-4 rounded">
          <span class="text-xs text-gray-500 leading-relaxed">Send me hosting tips, feature updates &amp; exclusive offers. Unsubscribe anytime.</span>
        </label>
      </div>
      <button type="submit" id="submitBtn"
              class="cta-btn w-full py-3.5 bg-gradient-to-r from-teal-600 to-teal-800 text-white rounded-xl font-semibold text-base">
        Preview My Guide &mdash; Free
      </button>
      <p id="errorMsg" class="text-red-500 text-xs text-center mt-2 hidden"></p>
      <p class="text-center text-xs text-gray-400 mt-3">See your personalized guide instantly &middot; Pay only if you want the full version</p>
    </form>
    <div class="text-center mt-4 pt-4 border-t border-gray-100">
      <p class="text-xs text-gray-400 mb-2">Already have a plan?</p>
      <form action="/dashboard/login" method="POST" class="inline">
        <input type="email" name="email" required placeholder="Enter your email"
               class="px-3 py-2 border border-gray-200 rounded-lg text-xs w-48 focus:outline-none focus:ring-2 focus:ring-teal-500/30">
        <button type="submit" class="px-4 py-2 text-xs font-semibold text-teal-700 bg-teal-50 rounded-lg hover:bg-teal-100 transition">
          Go to Dashboard
        </button>
      </form>
    </div>
  </div>
</section>

<!-- ════════ HOW IT WORKS ════════ -->
<section class="max-w-4xl mx-auto px-6 mb-24">
  <h2 class="text-2xl font-bold text-center mb-12">How It Works</h2>
  <div class="grid md:grid-cols-3 gap-8">
    <div class="text-center">
      <div class="w-14 h-14 bg-teal-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
        <svg class="w-6 h-6 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244"/></svg>
      </div>
      <h3 class="font-semibold mb-1">Paste your link</h3>
      <p class="text-sm text-gray-500">Drop your Airbnb listing URL. We detect the location automatically.</p>
    </div>
    <div class="text-center">
      <div class="w-14 h-14 bg-teal-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
        <svg class="w-6 h-6 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/></svg>
      </div>
      <h3 class="font-semibold mb-1">Preview for free</h3>
      <p class="text-sm text-gray-500">See a personalized preview with restaurants, groceries, landmarks, and more near your listing.</p>
    </div>
    <div class="text-center">
      <div class="w-14 h-14 bg-teal-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
        <svg class="w-6 h-6 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg>
      </div>
      <h3 class="font-semibold mb-1">Get your guide</h3>
      <p class="text-sm text-gray-500">Download a beautiful, print-ready PDF. Share digitally or leave a printed copy in your unit.</p>
    </div>
  </div>
</section>

<!-- ════════ FEATURES ════════ -->
<section class="max-w-4xl mx-auto px-6 mb-24">
  <h2 class="text-2xl font-bold text-center mb-3">Everything Your Guests Need</h2>
  <p class="text-sm text-gray-500 text-center mb-12 max-w-md mx-auto">Each guide is tailored to your listing's exact location with real, verified local data.</p>
  <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-5">
    <div class="feature-card bg-white rounded-xl p-6 shadow-sm transition-all duration-200">
      <div class="w-10 h-10 bg-teal-50 rounded-xl flex items-center justify-center mb-3">
        <svg class="w-5 h-5 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8.25v-1.5m0 1.5c-1.355 0-2.697.056-4.024.166C6.845 8.51 6 9.473 6 10.608v2.513m6-4.871c1.355 0 2.697.056 4.024.166C17.155 8.51 18 9.473 18 10.608v2.513M15 8.25v-1.5m-6 1.5v-1.5m12 9.75l-1.5.75a3.354 3.354 0 01-3 0 3.354 3.354 0 00-3 0 3.354 3.354 0 01-3 0 3.354 3.354 0 00-3 0 3.354 3.354 0 01-3 0L3 16.5m15-3.379a48.474 48.474 0 00-6-.371c-2.032 0-4.034.126-6 .371m12 0c.39.049.777.102 1.163.16 1.07.16 1.837 1.094 1.837 2.175v5.17c0 .62-.504 1.124-1.125 1.124H4.125A1.125 1.125 0 013 20.625v-5.17c0-1.08.768-2.014 1.837-2.174A47.78 47.78 0 016 13.12M12.265 3.11a.375.375 0 11-.53 0L12 2.845l.265.265zm-3 0a.375.375 0 11-.53 0L9 2.845l.265.265zm6 0a.375.375 0 11-.53 0L15 2.845l.265.265z"/></svg>
      </div>
      <h3 class="font-semibold text-sm mb-1">Restaurants & Cafes</h3>
      <p class="text-xs text-gray-500 leading-relaxed">Nearby spots with walking and driving times from your front door.</p>
    </div>
    <div class="feature-card bg-white rounded-xl p-6 shadow-sm transition-all duration-200">
      <div class="w-10 h-10 bg-teal-50 rounded-xl flex items-center justify-center mb-3">
        <svg class="w-5 h-5 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 3h1.386c.51 0 .955.343 1.087.835l.383 1.437M7.5 14.25a3 3 0 00-3 3h15.75m-12.75-3h11.218c1.121-2.3 2.1-4.684 2.924-7.138a60.114 60.114 0 00-16.536-1.84M7.5 14.25L5.106 5.272M6 20.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm12.75 0a.75.75 0 11-1.5 0 .75.75 0 011.5 0z"/></svg>
      </div>
      <h3 class="font-semibold text-sm mb-1">Grocery Stores</h3>
      <p class="text-xs text-gray-500 leading-relaxed">Nearest supermarkets and convenience stores so guests never have to ask.</p>
    </div>
    <div class="feature-card bg-white rounded-xl p-6 shadow-sm transition-all duration-200">
      <div class="w-10 h-10 bg-teal-50 rounded-xl flex items-center justify-center mb-3">
        <svg class="w-5 h-5 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z"/></svg>
      </div>
      <h3 class="font-semibold text-sm mb-1">Pharmacies</h3>
      <p class="text-xs text-gray-500 leading-relaxed">Closest pharmacies with addresses — essential info every guest appreciates.</p>
    </div>
    <div class="feature-card bg-white rounded-xl p-6 shadow-sm transition-all duration-200">
      <div class="w-10 h-10 bg-teal-50 rounded-xl flex items-center justify-center mb-3">
        <svg class="w-5 h-5 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M15 10.5a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 10.5c0 7.142-7.5 11.25-7.5 11.25S4.5 17.642 4.5 10.5a7.5 7.5 0 1115 0z"/></svg>
      </div>
      <h3 class="font-semibold text-sm mb-1">Landmarks & Activities</h3>
      <p class="text-xs text-gray-500 leading-relaxed">Must-see attractions, parks, museums, and local favorites within easy reach.</p>
    </div>
    <div class="feature-card bg-white rounded-xl p-6 shadow-sm transition-all duration-200">
      <div class="w-10 h-10 bg-teal-50 rounded-xl flex items-center justify-center mb-3">
        <svg class="w-5 h-5 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 18v-5.25m0 0a6.01 6.01 0 001.5-.189m-1.5.189a6.01 6.01 0 01-1.5-.189m3.75 7.478a12.06 12.06 0 01-4.5 0m3.75 2.383a14.406 14.406 0 01-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 10-7.517 0c.85.493 1.509 1.333 1.509 2.316V18"/></svg>
      </div>
      <h3 class="font-semibold text-sm mb-1">Local Tips</h3>
      <p class="text-xs text-gray-500 leading-relaxed">Insider tips like nearby transit, best coffee spots, and practical info every guest needs.</p>
    </div>
    <div class="feature-card bg-white rounded-xl p-6 shadow-sm transition-all duration-200">
      <div class="w-10 h-10 bg-teal-50 rounded-xl flex items-center justify-center mb-3">
        <svg class="w-5 h-5 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"/></svg>
      </div>
      <h3 class="font-semibold text-sm mb-1">Print-Ready PDF</h3>
      <p class="text-xs text-gray-500 leading-relaxed">Beautiful A4 layout. Print it, frame it, or share a digital link with guests.</p>
    </div>
  </div>
</section>

<!-- ════════ PREVIEW ════════ -->
<section class="max-w-3xl mx-auto px-6 mb-24 text-center">
  <h2 class="text-2xl font-bold mb-3">See What You Get</h2>
  <p class="text-sm text-gray-500 mb-10">Here's a real guide generated for a Miami listing.</p>
  <div class="bg-white rounded-2xl shadow-lg overflow-hidden border border-gray-100 text-left">
    <div class="bg-gradient-to-r from-teal-600 to-teal-800 px-8 py-6 text-white">
      <p class="text-xs uppercase tracking-widest opacity-70 mb-1">Neighborhood Guide</p>
      <h3 class="text-xl font-bold">Downtown Miami</h3>
      <p class="text-sm opacity-80 mt-1">Hosted by Kevin</p>
    </div>
    <div class="p-8 grid sm:grid-cols-2 gap-6">
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Restaurants</h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>Zuma &mdash; <span class="text-gray-400">4 min walk</span></li>
          <li>Cipriani &mdash; <span class="text-gray-400">6 min walk</span></li>
          <li>La Mar by Gaston &mdash; <span class="text-gray-400">3 min walk</span></li>
        </ul>
      </div>
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Groceries</h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>Whole Foods Brickell &mdash; <span class="text-gray-400">7 min walk</span></li>
          <li>Publix Downtown &mdash; <span class="text-gray-400">5 min drive</span></li>
        </ul>
      </div>
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Landmarks</h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>Bayfront Park &mdash; <span class="text-gray-400">2 min walk</span></li>
          <li>Perez Art Museum &mdash; <span class="text-gray-400">8 min walk</span></li>
        </ul>
      </div>
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Local Tips</h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>Free Metromover downtown</li>
          <li>Best coffee: Per'La</li>
        </ul>
      </div>
    </div>
    <div class="border-t border-gray-100 px-8 py-3 text-xs text-gray-400 text-center">This is a preview &mdash; your guide will be fully personalized to your listing</div>
  </div>
</section>

<!-- ════════ SOCIAL PROOF ════════ -->
<section class="bg-white border-y border-gray-100 py-16 mb-24">
  <div class="max-w-4xl mx-auto px-6">
    <h2 class="text-2xl font-bold text-center mb-10">Built for Hosts, Tested Across 10 Cities</h2>
    <div class="flex justify-center gap-12 md:gap-20 flex-wrap">
      <div class="text-center">
        <div class="text-3xl font-extrabold text-teal-600">50+</div>
        <div class="text-xs text-gray-500 mt-1">Guides Generated</div>
      </div>
      <div class="text-center">
        <div class="text-3xl font-extrabold text-teal-600">10</div>
        <div class="text-xs text-gray-500 mt-1">Cities Tested</div>
      </div>
      <div class="text-center">
        <div class="text-3xl font-extrabold text-teal-600">Any</div>
        <div class="text-xs text-gray-500 mt-1">City Worldwide</div>
      </div>
    </div>
    <p class="text-center text-sm text-gray-500 mt-8 max-w-md mx-auto">Currently live in Miami, Dublin, Lisbon, Madrid, Medellin, Bogota, Rochester, Orlando, Tampa, and Destin. Works for any Airbnb listing worldwide.</p>
  </div>
</section>

<!-- ════════ PRICING ════════ -->
<section id="pricing" class="max-w-4xl mx-auto px-6 mb-24">
  <h2 class="text-2xl font-bold text-center mb-3">Simple Pricing</h2>
  <p class="text-sm text-gray-500 text-center mb-10">One guide or many — pick what fits.</p>
  <div class="grid md:grid-cols-3 gap-5 max-w-3xl mx-auto items-start">

    <!-- Single -->
    <div class="bg-white rounded-2xl shadow-md p-7 border border-gray-100 text-center">
      <h3 class="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-4">Single</h3>
      <div class="text-3xl font-extrabold text-gray-800 mb-1"><span class="text-lg line-through text-gray-300 mr-1">$4.99</span>$1.99</div>
      <div class="text-xs text-gray-500 mb-1">one-time</div>
      <div class="text-xs text-teal-600 font-medium mb-5">Launch pricing</div>
      <ul class="text-left text-sm text-gray-600 space-y-2 mb-7">
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> 1 guide</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> 30+ nearby places</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> PDF + web version</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-gray-300 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg><span class="text-gray-400"> Regeneration</span></li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-gray-300 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg><span class="text-gray-400"> Priority support</span></li>
      </ul>
      <a href="#" onclick="document.getElementById('airbnb_url').focus();window.scrollTo({top:0,behavior:'smooth'});return false;"
         class="block w-full py-2.5 bg-white border-2 border-gray-200 text-gray-700 rounded-xl font-semibold text-sm text-center hover:border-teal-400 transition">
        Get One Guide
      </a>
    </div>

    <!-- Starter (decoy target) -->
    <div class="bg-white rounded-2xl shadow-xl p-7 border-2 border-teal-500 text-center relative md:-mt-2 md:mb-0">
      <div class="absolute -top-3 left-1/2 -translate-x-1/2 bg-teal-600 text-white text-xs font-semibold px-4 py-1 rounded-full">Most Popular</div>
      <h3 class="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-4 mt-1">Starter</h3>
      <div class="text-3xl font-extrabold text-teal-700 mb-1">$4.99</div>
      <div class="text-xs text-gray-500 mb-1">per month</div>
      <div class="text-xs text-teal-600 font-medium mb-5">5 guides/month</div>
      <ul class="text-left text-sm text-gray-600 space-y-2 mb-7">
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> 5 guides per month</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> 30+ nearby places</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> PDF + web version</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Regenerate anytime</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-gray-300 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg><span class="text-gray-400"> Priority support</span></li>
      </ul>
      <a href="#" onclick="document.getElementById('airbnb_url').focus();window.scrollTo({top:0,behavior:'smooth'});return false;"
         class="cta-btn block w-full py-2.5 bg-gradient-to-r from-teal-600 to-teal-800 text-white rounded-xl font-semibold text-sm text-center">
        Get Starter
      </a>
      <p class="text-xs text-gray-400 mt-2">Cancel anytime</p>
    </div>

    <!-- Pro -->
    <div class="bg-white rounded-2xl shadow-md p-7 border border-gray-100 text-center">
      <h3 class="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-4">Pro</h3>
      <div class="text-3xl font-extrabold text-gray-800 mb-1">$14.99</div>
      <div class="text-xs text-gray-500 mb-1">per month</div>
      <div class="text-xs text-teal-600 font-medium mb-5">25 guides/month</div>
      <ul class="text-left text-sm text-gray-600 space-y-2 mb-7">
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> 25 guides per month</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> 30+ nearby places</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> PDF + web version</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Regenerate anytime</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Priority support</li>
      </ul>
      <a href="#" onclick="document.getElementById('airbnb_url').focus();window.scrollTo({top:0,behavior:'smooth'});return false;"
         class="block w-full py-2.5 bg-white border-2 border-gray-200 text-gray-700 rounded-xl font-semibold text-sm text-center hover:border-teal-400 transition">
        Go Pro
      </a>
      <p class="text-xs text-gray-400 mt-2">Cancel anytime</p>
    </div>

  </div>
</section>

<!-- ════════ FAQ ════════ -->
<section class="max-w-2xl mx-auto px-6 mb-24">
  <h2 class="text-2xl font-bold text-center mb-10">Frequently Asked Questions</h2>
  <div class="space-y-3">
    <div class="bg-white rounded-xl shadow-sm overflow-hidden">
      <button onclick="this.nextElementSibling.classList.toggle('open')" class="w-full text-left px-6 py-4 flex items-center justify-between text-sm font-semibold hover:bg-gray-50 transition">
        How long does it take to generate a guide?
        <span class="text-gray-400">+</span>
      </button>
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">Most guides are ready in 1-2 minutes. We analyze your listing location, find all nearby points of interest, and format everything into a clean guide automatically.</p></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm overflow-hidden">
      <button onclick="this.nextElementSibling.classList.toggle('open')" class="w-full text-left px-6 py-4 flex items-center justify-between text-sm font-semibold hover:bg-gray-50 transition">
        What cities do you cover?
        <span class="text-gray-400">+</span>
      </button>
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">Any city worldwide. We use location data from your Airbnb listing, so as long as your listing has an address, we can generate a guide for it.</p></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm overflow-hidden">
      <button onclick="this.nextElementSibling.classList.toggle('open')" class="w-full text-left px-6 py-4 flex items-center justify-between text-sm font-semibold hover:bg-gray-50 transition">
        Can I customize the guide?
        <span class="text-gray-400">+</span>
      </button>
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">The guide is auto-generated with your host name and listing location. Custom branding and editable sections are coming soon.</p></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm overflow-hidden">
      <button onclick="this.nextElementSibling.classList.toggle('open')" class="w-full text-left px-6 py-4 flex items-center justify-between text-sm font-semibold hover:bg-gray-50 transition">
        What format is the guide?
        <span class="text-gray-400">+</span>
      </button>
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">You get both a web version (shareable link) and a print-ready PDF. Perfect for leaving a physical copy in your unit or sending to guests before check-in.</p></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm overflow-hidden">
      <button onclick="this.nextElementSibling.classList.toggle('open')" class="w-full text-left px-6 py-4 flex items-center justify-between text-sm font-semibold hover:bg-gray-50 transition">
        I have multiple listings. Is there a bulk discount?
        <span class="text-gray-400">+</span>
      </button>
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">Yes! Our Starter plan ($4.99/mo) includes 5 guides, and Pro ($14.99/mo) gives you 25 guides per month — much cheaper than buying individually.</p></div>
    </div>
  </div>
</section>

<!-- ════════ FOOTER ════════ -->
<footer class="border-t border-gray-100 py-10 text-center">
  <div class="max-w-4xl mx-auto px-6">
    <div class="flex items-center justify-center gap-2 mb-3">
      <svg width="24" height="24" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect width="32" height="32" rx="8" fill="#00897b"/>
        <path d="M10 8v16M22 8v16M10 16h12" stroke="white" stroke-width="2.5" stroke-linecap="round"/>
        <circle cx="22" cy="10" r="3" fill="#4DB6AC"/>
        <path d="M21 9.5l1 1 2-2" stroke="white" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span class="font-semibold text-sm">HostGuide</span>
    </div>
    <p class="text-xs text-gray-400 mb-2">Made for Airbnb hosts, by an Airbnb host.</p>
    <p class="text-xs text-gray-400">&copy; 2026 HostGuide</p>
  </div>
</footer>

<script>
document.getElementById('guideForm').addEventListener('submit', function(e) {
    const btn = document.getElementById('submitBtn');
    const url = document.getElementById('airbnb_url').value;
    const err = document.getElementById('errorMsg');
    if (!/airbnb\.\w+\/(rooms|h)\//.test(url)) {
        e.preventDefault();
        err.textContent = 'Please paste a valid Airbnb listing URL (e.g. airbnb.com/rooms/123456)';
        err.classList.remove('hidden');
        return;
    }
    btn.textContent = 'Generating preview...';
    btn.style.opacity = '0.7';
    btn.style.pointerEvents = 'none';
    err.classList.add('hidden');
});
</script>

</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# PREVIEW PAGE (blurred, anti-screenshot)
# ═══════════════════════════════════════════════════════════════

PREVIEW_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Your Guide Preview — HostGuide</title>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<script>
tailwind.config = {
  theme: { extend: {
    colors: { teal: { 50:'#e0f2f1',100:'#b2dfdb',200:'#80cbc4',300:'#4db6ac',400:'#26a69a',500:'#009688',600:'#00897b',700:'#00796b',800:'#00695c',900:'#004d40' } },
    fontFamily: { sans: ['Inter','system-ui','sans-serif'] }
  }}
}
</script>
<style>
  /* Anti-screenshot: watermark overlay + selection block */
  .preview-guard {
    position: relative;
    user-select: none;
    -webkit-user-select: none;
    -webkit-touch-callout: none;
  }
  .preview-guard::after {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    pointer-events: none;
    background: repeating-linear-gradient(
      -45deg,
      transparent,
      transparent 80px,
      rgba(0,137,123,0.03) 80px,
      rgba(0,137,123,0.03) 82px
    );
    z-index: 10;
  }
  .preview-watermark {
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%) rotate(-30deg);
    font-size: 3rem;
    font-weight: 800;
    color: rgba(0,137,123,0.06);
    white-space: nowrap;
    pointer-events: none;
    z-index: 11;
    letter-spacing: 0.2em;
  }
  .blur-zone { filter: blur(6px); transition: filter 0.3s; }
  .blur-light { filter: blur(3px); }
  /* Block right-click */
  .preview-guard img { pointer-events: none; }
  /* Pulsing CTA */
  @keyframes pulse-glow {
    0%, 100% { box-shadow: 0 0 0 0 rgba(0,137,123,0.4); }
    50% { box-shadow: 0 0 20px 6px rgba(0,137,123,0.25); }
  }
  .pulse-cta { animation: pulse-glow 2s ease-in-out infinite; }
  .gradient-hero { background: linear-gradient(135deg, #00897b 0%, #004d40 100%); }
</style>
</head>
<body class="font-sans text-gray-900 bg-gray-50 antialiased" oncontextmenu="return false;">

<!-- Nav -->
<nav class="gradient-hero">
  <div class="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
    <a href="/" class="flex items-center gap-2">
      <svg width="28" height="28" viewBox="0 0 32 32" fill="none"><rect width="32" height="32" rx="8" fill="white" fill-opacity="0.15"/><path d="M10 8v16M22 8v16M10 16h12" stroke="white" stroke-width="2.5" stroke-linecap="round"/><circle cx="22" cy="10" r="3" fill="#4DB6AC"/></svg>
      <span class="font-bold text-white text-lg">HostGuide</span>
    </a>
  </div>
</nav>

<!-- Header -->
<div class="bg-gradient-to-b from-teal-50 to-gray-50 pt-10 pb-6 text-center">
  <p class="text-xs uppercase tracking-widest text-teal-600 font-semibold mb-2">Your Personalized Guide</p>
  <h1 class="text-2xl md:text-3xl font-bold text-gray-900 mb-1">{{ listing_title or 'Your Neighborhood Guide' }}</h1>
  {% if city %}<p class="text-sm text-gray-500">{{ city }}</p>{% endif %}
</div>

<!-- BLURRED PREVIEW -->
<section class="max-w-3xl mx-auto px-6 py-10">
  <div class="preview-guard bg-white rounded-2xl shadow-xl overflow-hidden border border-gray-100 relative">
    <div class="preview-watermark">PREVIEW</div>

    <!-- Guide header (visible) -->
    <div class="bg-gradient-to-r from-teal-600 to-teal-800 px-8 py-6 text-white">
      <p class="text-xs uppercase tracking-widest opacity-70 mb-1">Neighborhood Guide</p>
      <h2 class="text-xl font-bold">{{ neighborhood or city or 'Your Listing Area' }}{% if neighborhood and city %}, {{ city }}{% endif %}</h2>
      {% if preview_subtitle %}<p class="text-sm opacity-90 mt-1">{{ preview_subtitle }}</p>{% endif %}
      {% if host_name %}<p class="text-xs opacity-70 mt-1">Hosted by {{ host_name }}</p>{% endif %}
    </div>

    <!-- Top section: CLEAR (tease value) -->
    <div class="p-8 grid sm:grid-cols-2 gap-6 border-b border-gray-100">
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3 flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8.25v-1.5m0 1.5c-1.355 0-2.697.056-4.024.166C6.845 8.51 6 9.473 6 10.608v2.513m6-4.871c1.355 0 2.697.056 4.024.166C17.155 8.51 18 9.473 18 10.608v2.513M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z"/></svg>
          Restaurants Nearby
        </h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>{{ restaurants[0] if restaurants else 'Loading...' }} &mdash; <span class="text-gray-400">{{ distances[0] if distances else '' }}</span></li>
          <li>{{ restaurants[1] if restaurants|length > 1 else '...' }} &mdash; <span class="text-gray-400">{{ distances[1] if distances|length > 1 else '' }}</span></li>
          <li class="blur-light text-gray-400">{{ restaurants[2] if restaurants|length > 2 else '...' }} &mdash; <span>{{ distances[2] if distances|length > 2 else '' }}</span></li>
        </ul>
      </div>
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3 flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 3h1.386c.51 0 .955.343 1.087.835l.383 1.437M7.5 14.25a3 3 0 00-3 3h15.75m-12.75-3h11.218c1.121-2.3 2.1-4.684 2.924-7.138a60.114 60.114 0 00-16.536-1.84M7.5 14.25L5.106 5.272M6 20.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm12.75 0a.75.75 0 11-1.5 0 .75.75 0 011.5 0z"/></svg>
          Groceries
        </h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>{{ groceries[0] if groceries else 'Loading...' }} &mdash; <span class="text-gray-400">{{ gdistances[0] if gdistances else '' }}</span></li>
          <li class="blur-light text-gray-400">{{ groceries[1] if groceries|length > 1 else '...' }} &mdash; <span>{{ gdistances[1] if gdistances|length > 1 else '' }}</span></li>
        </ul>
      </div>
    </div>

    <!-- Bottom section: HEAVILY BLURRED -->
    <div class="p-8 blur-zone relative">
      <div class="grid sm:grid-cols-2 gap-6">
        <div>
          <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Transit & Transport</h4>
          <ul class="space-y-2 text-sm text-gray-600">
            <li>Metro Station Central &mdash; <span class="text-gray-400">4 min walk</span></li>
            <li>Bus Line 42 Stop &mdash; <span class="text-gray-400">2 min walk</span></li>
            <li>Taxi Rank &mdash; <span class="text-gray-400">6 min walk</span></li>
          </ul>
        </div>
        <div>
          <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Landmarks & Parks</h4>
          <ul class="space-y-2 text-sm text-gray-600">
            <li>Central Park &mdash; <span class="text-gray-400">8 min walk</span></li>
            <li>Art Museum &mdash; <span class="text-gray-400">12 min walk</span></li>
            <li>Historic District &mdash; <span class="text-gray-400">5 min walk</span></li>
          </ul>
        </div>
        <div>
          <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Nightlife</h4>
          <ul class="space-y-2 text-sm text-gray-600">
            <li>Rooftop Bar &mdash; <span class="text-gray-400">3 min walk</span></li>
            <li>Jazz Club &mdash; <span class="text-gray-400">7 min walk</span></li>
          </ul>
        </div>
        <div>
          <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Health & Safety</h4>
          <ul class="space-y-2 text-sm text-gray-600">
            <li>Pharmacy &mdash; <span class="text-gray-400">3 min walk</span></li>
            <li>Hospital &mdash; <span class="text-gray-400">10 min drive</span></li>
          </ul>
        </div>
      </div>
      <!-- Overlay on blurred section -->
      <div class="absolute inset-0 flex items-center justify-center z-20">
        <div class="bg-white/90 backdrop-blur-sm rounded-2xl shadow-lg px-8 py-6 text-center max-w-sm">
          <svg class="w-10 h-10 text-teal-600 mx-auto mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z"/></svg>
          <p class="text-sm font-semibold text-gray-800 mb-1">6 more categories hidden</p>
          <p class="text-xs text-gray-500">Transit, landmarks, nightlife, health &amp; more</p>
        </div>
      </div>
    </div>
  </div>

  <!-- UNLOCK CTA — TIER COMPARISON -->
  <div class="mt-8 max-w-2xl mx-auto">
    {% if has_credits %}
    <div class="bg-teal-50 border border-teal-200 rounded-xl p-5 mb-5 text-center">
      <p class="text-sm font-semibold text-teal-800">You have {{ user_credits }} credit{{ 's' if user_credits != 1 else '' }} remaining{% if user_tier not in ('none', 'single') %} ({{ user_tier | capitalize }} plan){% endif %}</p>
      <form action="/use-credit" method="POST" class="mt-3">
        <input type="hidden" name="token" value="{{ token }}">
        <button type="submit" class="px-6 py-3 bg-gradient-to-r from-teal-600 to-teal-800 text-white rounded-xl font-semibold text-sm hover:shadow-lg transition">
          Generate Guide — Use 1 Credit
        </button>
      </form>
    </div>
    <p class="text-xs text-gray-400 text-center mb-4">— or upgrade your plan —</p>
    {% endif %}
    <h3 class="text-lg font-bold text-center mb-5">Unlock Your Full Guide</h3>
    <div class="grid grid-cols-3 gap-3">

      <!-- Single -->
      <form action="/checkout" method="POST" class="text-center">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="tier" value="single">
        <div class="bg-white rounded-xl border border-gray-200 p-4 hover:border-teal-400 transition h-full flex flex-col">
          <div class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Single</div>
          <div class="text-2xl font-extrabold text-gray-800"><span class="text-sm line-through text-gray-300">$4.99</span> $1.99</div>
          <div class="text-xs text-gray-400 mb-3">one-time</div>
          <ul class="text-xs text-gray-500 text-left space-y-1 mb-4 flex-grow">
            <li>&#10003; This guide only</li>
            <li>&#10003; PDF + web version</li>
            <li>&#10003; 30+ places</li>
          </ul>
          <button type="submit" class="w-full py-2.5 bg-white border-2 border-gray-200 text-gray-700 rounded-lg font-semibold text-sm hover:border-teal-400 transition">
            Get This Guide
          </button>
        </div>
      </form>

      <!-- Starter -->
      <form action="/checkout" method="POST" class="text-center">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="tier" value="starter">
        <div class="bg-white rounded-xl border-2 border-teal-500 p-4 relative h-full flex flex-col shadow-md">
          <div class="absolute -top-2.5 left-1/2 -translate-x-1/2 bg-teal-600 text-white text-xs font-semibold px-3 py-0.5 rounded-full">Best Value</div>
          <div class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2 mt-1">Starter</div>
          <div class="text-2xl font-extrabold text-teal-700">$4.99</div>
          <div class="text-xs text-gray-400 mb-3">per month</div>
          <ul class="text-xs text-gray-500 text-left space-y-1 mb-4 flex-grow">
            <li>&#10003; <strong>5 guides/month</strong></li>
            <li>&#10003; PDF + web version</li>
            <li>&#10003; 30+ places each</li>
            <li>&#10003; Regenerate anytime</li>
          </ul>
          <button type="submit" class="pulse-cta w-full py-2.5 bg-gradient-to-r from-teal-600 to-teal-800 text-white rounded-lg font-semibold text-sm">
            Get Starter
          </button>
          <p class="text-xs text-gray-400 mt-1">Cancel anytime</p>
        </div>
      </form>

      <!-- Pro -->
      <form action="/checkout" method="POST" class="text-center">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="tier" value="pro">
        <div class="bg-white rounded-xl border border-gray-200 p-4 hover:border-teal-400 transition h-full flex flex-col">
          <div class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Pro</div>
          <div class="text-2xl font-extrabold text-gray-800">$14.99</div>
          <div class="text-xs text-gray-400 mb-3">per month</div>
          <ul class="text-xs text-gray-500 text-left space-y-1 mb-4 flex-grow">
            <li>&#10003; <strong>25 guides/month</strong></li>
            <li>&#10003; PDF + web version</li>
            <li>&#10003; 30+ places each</li>
            <li>&#10003; Regenerate anytime</li>
            <li>&#10003; Priority support</li>
          </ul>
          <button type="submit" class="w-full py-2.5 bg-white border-2 border-gray-200 text-gray-700 rounded-lg font-semibold text-sm hover:border-teal-400 transition">
            Go Pro
          </button>
          <p class="text-xs text-gray-400 mt-1">Cancel anytime</p>
        </div>
      </form>

    </div>
    <p class="text-xs text-gray-400 text-center mt-4">Secure payment via Stripe &middot; Instant access &middot; PDF + digital version</p>
    <div class="flex items-center justify-center gap-4 mt-4 text-xs text-gray-400">
      <span class="flex items-center gap-1">
        <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z"/></svg>
        Secure payment via Stripe
      </span>
      <span class="flex items-center gap-1">
        <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 5.25v13.5m-7.5-13.5v13.5"/></svg>
        30+ personalized places
      </span>
    </div>
  </div>
</section>

<!-- Disable print/save -->
<script>
// Block Ctrl+P / Cmd+P (print)
document.addEventListener('keydown', function(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'p') { e.preventDefault(); }
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); }
});
// Block drag
document.addEventListener('dragstart', function(e) { e.preventDefault(); });
</script>

</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def landing():
    """Landing page."""
    return render_template_string(LANDING_PAGE)


@app.route("/preview", methods=["POST"])
def preview():
    """Fetch listing meta, show blurred personalized preview."""
    airbnb_url = request.form.get("airbnb_url", "").strip()
    email = request.form.get("email", "").strip()
    city = request.form.get("city", "").strip()

    if not airbnb_url or not re.search(r'airbnb\.\w+/(rooms|h)/', airbnb_url):
        return redirect("/")

    # Create order early (pending state) — includes city
    token = _create_order(airbnb_url, email, city=city)

    # Store email consent for CRM
    email_consent = request.form.get("email_consent", "") == "yes"
    if email_consent and email:
        _save_email_subscriber(email)

    # If user already has credits, skip preview — go straight to generation
    if email:
        user_rec = _get_user_credits(email)
        if user_rec["credits"] > 0:
            if _use_credit(email, token):
                _update_order(token, status="generating", tier=user_rec.get("tier", "single"))
                t = threading.Thread(target=_generate_in_background, args=(token,), daemon=True)
                t.start()
                return redirect(f"/generating/{token}")

    # Rich meta fetch (OG tags + embedded JSON — fast, no Playwright)
    meta = _fetch_listing_meta(airbnb_url)

    # Use form city if meta didn't get one
    listing_title = meta.get("title", "")
    city = city or meta.get("city", "")
    neighborhood = meta.get("neighborhood", "")

    # Store extracted meta in the order for generation (avoids double fetch)
    _update_order(token, meta_cache=json.dumps({
        k: v for k, v in meta.items()
        if k in ("lat", "lng", "host_name", "neighborhood", "property_type",
                 "bedrooms", "bathrooms", "guests", "rating", "reviews_count",
                 "amenities", "photos", "host_superhost", "host_response_rate")
    }))

    # Build preview subtitle from extracted data
    preview_details = []
    if meta.get("property_type"):
        preview_details.append(meta["property_type"])
    if meta.get("bedrooms"):
        preview_details.append(f"{meta['bedrooms']} bed{'s' if meta['bedrooms'] > 1 else ''}")
    if meta.get("bathrooms"):
        preview_details.append(f"{meta['bathrooms']} bath{'s' if meta['bathrooms'] > 1 else ''}")
    if meta.get("guests"):
        preview_details.append(f"up to {meta['guests']} guests")
    if meta.get("rating"):
        preview_details.append(f"★ {meta['rating']}")
    preview_subtitle = " · ".join(preview_details) if preview_details else ""

    # Provide a few real-looking (but generic) preview items
    restaurants = ["Popular Cafe Nearby", "Local Restaurant", "Bistro Around the Corner"]
    distances = ["3 min walk", "5 min walk", "7 min walk"]
    groceries = ["Supermarket", "Convenience Store"]
    gdistances = ["4 min walk", "6 min walk"]

    # Check existing credits for this email
    user_rec = _get_user_credits(email) if email else {"credits": 0, "tier": "none"}
    has_credits = user_rec["credits"] > 0
    user_tier = user_rec.get("tier", "none")

    return render_template_string(PREVIEW_PAGE,
        token=token,
        listing_title=listing_title,
        city=city,
        neighborhood=neighborhood,
        preview_subtitle=preview_subtitle,
        host_name=meta.get("host_name", ""),
        restaurants=restaurants,
        distances=distances,
        groceries=groceries,
        gdistances=gdistances,
        has_credits=has_credits,
        user_credits=user_rec["credits"],
        user_tier=user_tier,
        email=email,
    )


@app.route("/preview/<token>")
def preview_by_token(token: str):
    """Show preview page for an existing order (e.g. after Stripe cancel)."""
    order = _get_order(token)
    if not order:
        return redirect("/")

    city = order.get("city", "")
    restaurants = ["Popular Cafe Nearby", "Local Restaurant", "Bistro Around the Corner"]
    distances = ["3 min walk", "5 min walk", "7 min walk"]
    groceries = ["Supermarket", "Convenience Store"]
    gdistances = ["4 min walk", "6 min walk"]

    return render_template_string(PREVIEW_PAGE,
        token=token,
        listing_title=city or "Your Listing",
        city=city,
        restaurants=restaurants,
        distances=distances,
        groceries=groceries,
        gdistances=gdistances,
    )


@app.route("/dashboard/login", methods=["POST"])
def dashboard_login():
    """Returning user enters email → redirect to signed dashboard URL."""
    email = request.form.get("email", "").strip().lower()
    if not email:
        return redirect("/")
    user = _get_user_credits(email)
    if user["tier"] == "none" and user["credits"] == 0:
        # No account — redirect back with message
        return redirect("/?error=no_account")
    return redirect(_dashboard_url(email))


@app.route("/dashboard")
def dashboard():
    """User dashboard — shows credits, past guides, generate new guide."""
    email = request.args.get("email", "").strip().lower()
    sig = request.args.get("sig", "")
    welcome = request.args.get("welcome", "")
    if not email:
        return redirect("/")
    if not sig or not _verify_dashboard_sig(email, sig):
        abort(403, "Invalid or missing dashboard link. Please use the link from your payment confirmation.")

    user = _get_user_credits(email)

    # If arriving from Stripe (welcome=1) and credits are 0, verify payment directly
    if welcome and user["credits"] == 0 and STRIPE_SECRET:
        # Find the most recent order for this email with a stripe session
        orders = _load_orders()
        for tok, ord_data in sorted(orders.items(),
                                     key=lambda x: x[1].get("created", ""), reverse=True):
            if (ord_data.get("email", "").lower() == email
                    and ord_data.get("stripe_session_id")
                    and ord_data.get("status") in ("pending", "paid")):
                try:
                    session = stripe.checkout.Session.retrieve(ord_data["stripe_session_id"])
                    if session.payment_status == "paid":
                        tier_name = ord_data.get("tier", "single")
                        tier_config = TIERS.get(tier_name, TIERS["single"])
                        _add_credits(email, tier_config["guides"], tier_name,
                                     stripe_customer_id=session.get("customer"),
                                     dedup_key=ord_data["stripe_session_id"])
                        _update_order(tok, status="paid", tier=tier_name)
                        user = _get_user_credits(email)  # Refresh
                        print(f"[dashboard] Verified Stripe payment for {email}: "
                              f"+{tier_config['guides']} credits ({tier_name})")
                except Exception as e:
                    print(f"[dashboard] Stripe verify failed: {e}")
                break

    # If arriving from Stripe with credits, auto-generate the first guide
    # using the Airbnb URL they already entered before payment
    if welcome and user["credits"] > 0:
        orders = _load_orders()
        for tok, ord_data in sorted(orders.items(),
                                     key=lambda x: x[1].get("created", ""), reverse=True):
            if (ord_data.get("email", "").lower() == email
                    and ord_data.get("airbnb_url")
                    and ord_data.get("status") == "paid"):
                # Use 1 credit and start generation
                _use_credit(email, tok)
                return redirect(f"/generating/{tok}")
        # No matching order found — fall through to dashboard

    credits = user["credits"]
    tier = user["tier"]

    # Get past guides for this user
    orders = _load_orders()
    past_guides = []
    for tok, order in orders.items():
        if order.get("email", "").lower() == email and order.get("status") == "generated":
            past_guides.append({"token": tok, "city": order.get("city", ""), "url": order.get("airbnb_url", "")})

    return render_template_string(DASHBOARD_PAGE,
        email=email,
        sig=sig,
        credits=credits,
        tier=tier,
        past_guides=past_guides,
        welcome=welcome,
    )


@app.route("/dashboard/generate", methods=["POST"])
def dashboard_generate():
    """Generate a new guide using credits."""
    email = request.form.get("email", "").strip().lower()
    sig = request.form.get("sig", "")
    airbnb_url = request.form.get("airbnb_url", "").strip()
    city = request.form.get("city", "").strip()

    if not email or not sig or not _verify_dashboard_sig(email, sig):
        abort(403, "Invalid dashboard session.")
    if not airbnb_url or not re.search(r'airbnb\.\w+/(rooms|h)/', airbnb_url):
        return redirect(_dashboard_url(email))

    # Check credits
    user = _get_user_credits(email)
    if user["credits"] <= 0:
        return redirect(_dashboard_url(email, error="no_credits"))

    # Create order and use credit
    token = _create_order(airbnb_url, email, city=city)
    _update_order(token, status="paid", tier=user["tier"])
    _use_credit(email, token)

    return redirect(f"/generating/{token}")


DASHBOARD_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard — HostGuide</title>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<script>
tailwind.config = {
  theme: { extend: {
    colors: { teal: { 50:'#e0f2f1',100:'#b2dfdb',200:'#80cbc4',300:'#4db6ac',400:'#26a69a',500:'#009688',600:'#00897b',700:'#00796b',800:'#00695c',900:'#004d40' } },
    fontFamily: { sans: ['Inter','system-ui','sans-serif'] }
  }}
}
</script>
<style>
  .gradient-hero { background: linear-gradient(135deg, #00897b 0%, #004d40 100%); }
</style>
</head>
<body class="font-sans text-gray-900 bg-gray-50 antialiased">

<!-- Nav -->
<nav class="gradient-hero">
  <div class="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
    <a href="/" class="flex items-center gap-2">
      <svg width="28" height="28" viewBox="0 0 32 32" fill="none"><rect width="32" height="32" rx="8" fill="white" fill-opacity="0.15"/><path d="M10 8v16M22 8v16M10 16h12" stroke="white" stroke-width="2.5" stroke-linecap="round"/><circle cx="22" cy="10" r="3" fill="#4DB6AC"/></svg>
      <span class="font-bold text-white text-lg">HostGuide</span>
    </a>
    <span class="text-white/70 text-sm">{{ email }}</span>
  </div>
</nav>

<div class="max-w-3xl mx-auto px-6 py-10">

  {% if welcome %}
  <div class="bg-teal-50 border border-teal-200 rounded-xl px-6 py-4 mb-8 text-sm text-teal-800">
    Welcome to your dashboard! You have <strong>{{ credits }} guide credits</strong> ready to use.
  </div>
  {% endif %}

  <!-- Credits summary -->
  <div class="flex items-center justify-between bg-white rounded-2xl shadow-sm p-6 mb-8 border border-gray-100">
    <div>
      <p class="text-xs text-gray-400 uppercase tracking-wide font-semibold">Your Plan</p>
      <p class="text-lg font-bold text-gray-900 capitalize">{{ tier }}</p>
    </div>
    <div class="text-right">
      <p class="text-xs text-gray-400 uppercase tracking-wide font-semibold">Credits Remaining</p>
      <p class="text-3xl font-extrabold text-teal-600">{{ credits }}</p>
    </div>
  </div>

  <!-- Generate new guide -->
  <div class="bg-white rounded-2xl shadow-sm p-6 mb-8 border border-gray-100">
    <h2 class="text-lg font-bold mb-4">Generate a New Guide</h2>
    {% if credits > 0 %}
    <form action="/dashboard/generate" method="POST">
      <input type="hidden" name="email" value="{{ email }}">
      <input type="hidden" name="sig" value="{{ sig }}">
      <div class="grid sm:grid-cols-2 gap-4 mb-4">
        <div>
          <label class="block text-xs font-semibold text-gray-600 mb-1">Airbnb Listing URL</label>
          <input type="url" name="airbnb_url" required placeholder="https://www.airbnb.com/rooms/123456..."
                 class="w-full px-4 py-3 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-teal-500/30 focus:border-teal-500">
        </div>
        <div>
          <label class="block text-xs font-semibold text-gray-600 mb-1">Neighborhood &amp; City <span class="text-gray-400 font-normal">(optional)</span></label>
          <input type="text" name="city" placeholder="e.g. Eaux-Vives, Geneva"
                 class="w-full px-4 py-3 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-teal-500/30 focus:border-teal-500">
        </div>
      </div>
      <button type="submit"
              class="px-6 py-3 bg-gradient-to-r from-teal-600 to-teal-800 text-white rounded-xl font-semibold text-sm hover:shadow-lg transition">
        Generate Guide (1 credit)
      </button>
    </form>
    {% else %}
      {% if tier in ('starter', 'pro') %}
    <p class="text-sm text-gray-500 mb-4">You've used all your credits for this billing cycle. They'll automatically refill on your next billing date.</p>
    <a href="/#pricing" class="inline-block px-4 py-2 text-teal-700 bg-teal-50 rounded-xl font-semibold text-sm hover:bg-teal-100 transition">
      Upgrade Plan
    </a>
      {% else %}
    <p class="text-sm text-gray-500 mb-4">You've used all your credits.</p>
    <a href="/#pricing" class="inline-block px-6 py-3 bg-teal-600 text-white rounded-xl font-semibold text-sm hover:bg-teal-700 transition">
      Get More Credits
    </a>
      {% endif %}
    {% endif %}
  </div>

  <!-- Past guides -->
  {% if past_guides %}
  <div class="bg-white rounded-2xl shadow-sm p-6 border border-gray-100">
    <h2 class="text-lg font-bold mb-4">Your Guides</h2>
    <div class="space-y-3">
      {% for g in past_guides %}
      <div class="flex items-center justify-between py-3 border-b border-gray-50 last:border-0">
        <div>
          <p class="font-medium text-sm">{{ g.city or 'Guide' }}</p>
          <p class="text-xs text-gray-400 truncate max-w-xs">{{ g.url }}</p>
        </div>
        <a href="/download/{{ g.token }}" class="text-sm text-teal-600 font-semibold hover:underline">View</a>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

</div>
</body>
</html>"""


# Stripe pricing tiers
TIERS = {
    "single": {
        "name": "HostGuide — Single Guide",
        "description": "One personalized neighborhood guide for your Airbnb listing",
        "amount": 199,  # $1.99
        "mode": "payment",
        "guides": 1,
    },
    "starter": {
        "name": "HostGuide — Starter Plan",
        "description": "5 neighborhood guides per month for your Airbnb listings. Cancel anytime.",
        "amount": 499,  # $4.99/mo
        "mode": "subscription",
        "interval": "month",
        "guides": 5,
    },
    "pro": {
        "name": "HostGuide — Pro Plan",
        "description": "25 neighborhood guides per month for your Airbnb listings. Cancel anytime.",
        "amount": 1499,  # $14.99/mo
        "mode": "subscription",
        "interval": "month",
        "guides": 25,
    },
}


@app.route("/use-credit", methods=["POST"])
def use_credit():
    """Use an existing credit to generate a guide (skips Stripe)."""
    token = request.form.get("token", "").strip()
    if not token:
        return redirect("/")
    order = _get_order(token)
    if not order:
        return redirect("/")
    email = order["email"]
    if _use_credit(email, token):
        _update_order(token, status="generating", tier=order.get("tier", "single"))
        t = threading.Thread(target=_generate_in_background, args=(token,), daemon=True)
        t.start()
        return redirect(f"/generating/{token}")
    return redirect(f"/preview/{token}")


@app.route("/checkout", methods=["POST"])
def checkout():
    """Create Stripe Checkout session for any tier."""
    token = request.form.get("token", "").strip()
    tier = request.form.get("tier", "single").strip()

    if tier not in TIERS:
        tier = "single"

    if token:
        order = _get_order(token)
        if not order:
            return redirect("/")
        email = order["email"]
    else:
        airbnb_url = request.form.get("airbnb_url", "").strip()
        email = request.form.get("email", "").strip()
        city = request.form.get("city", "").strip()
        if not airbnb_url or not re.search(r'airbnb\.\w+/(rooms|h)/', airbnb_url):
            return redirect("/")
        token = _create_order(airbnb_url, email, city=city)

    # Store tier on order
    _update_order(token, tier=tier)

    if not STRIPE_SECRET:
        _update_order(token, status="paid", tier=tier)
        tier_credits = TIERS[tier]["guides"]
        _add_credits(email, tier_credits, tier, dedup_key=f"dev-{token}")
        if tier in ("starter", "pro"):
            return redirect(_dashboard_url(email))
        return redirect(f"/generating/{token}")

    tier_config = TIERS[tier]

    try:
        # Build price_data based on tier
        price_data = {
            "currency": "usd",
            "product_data": {
                "name": tier_config["name"],
                "description": tier_config["description"],
            },
            "unit_amount": tier_config["amount"],
        }
        # Subscriptions need recurring interval
        if tier_config["mode"] == "subscription":
            price_data["recurring"] = {"interval": tier_config["interval"]}

        session_kwargs = dict(
            payment_method_types=["card"],
            line_items=[{"price_data": price_data, "quantity": 1}],
            mode=tier_config["mode"],
            customer_email=email,
            success_url=f"{DOMAIN}{_dashboard_url(email, welcome='1')}" if tier in ("starter", "pro") else f"{DOMAIN}/generating/{token}",
            cancel_url=f"{DOMAIN}/preview/{token}",
            metadata={"order_token": token, "tier": tier},
        )
        if tier_config["mode"] == "subscription":
            session_kwargs["custom_text"] = {
                "submit": {"message": "You can cancel your subscription anytime from your dashboard — no questions asked."}
            }
        session = stripe.checkout.Session.create(**session_kwargs)
        _update_order(token, stripe_session_id=session.id)
        return redirect(session.url, code=303)
    except Exception as e:
        print(f"Stripe error: {e}")
        return redirect("/?error=payment")


@app.route("/generating/<token>")
def generating(token: str):
    """Show 'generating your guide' page — polls for completion."""
    order = _get_order(token)
    if not order:
        abort(404)

    # If Stripe redirected here, payment succeeded even if webhook hasn't fired yet.
    # Accept "pending" status — the page polls /api/status until generation completes.
    if order["status"] not in ("pending", "paid", "generating", "generated"):
        abort(404)

    if order["status"] == "generated" and order.get("guide_path"):
        return redirect(f"/download/{token}")

    # If user arrives from Stripe success redirect and webhook hasn't fired yet,
    # verify payment via Stripe API and mark as paid immediately.
    if order["status"] == "pending" and order.get("stripe_session_id") and STRIPE_SECRET:
        try:
            session = stripe.checkout.Session.retrieve(order["stripe_session_id"])
            if session.payment_status == "paid":
                tier = order.get("tier", "single")
                _update_order(token, status="paid", tier=tier)
                tier_config = TIERS.get(tier, TIERS["single"])
                _add_credits(order["email"], tier_config["guides"], tier,
                             stripe_customer_id=session.get("customer"),
                             dedup_key=order["stripe_session_id"])
                order["status"] = "paid"
        except Exception as e:
            print(f"Stripe session check failed: {e}")

    # Kick off background generation if paid
    if order["status"] == "paid":
        _update_order(token, status="generating")
        t = threading.Thread(target=_generate_in_background, args=(token,), daemon=True)
        t.start()

    # Build dashboard link for subscription users
    dashboard_link = ""
    email = order.get("email", "")
    tier = order.get("tier", "single")
    if tier in ("starter", "pro") and email:
        dashboard_link = _dashboard_url(email)

    return render_template_string(GENERATING_PAGE, token=token, dashboard_link=dashboard_link)


@app.route("/api/status/<token>")
def order_status(token: str):
    """API endpoint for polling generation status."""
    order = _get_order(token)
    if not order:
        # Order not in memory — check if a guide was already generated on disk
        # (handles Render redeploy wiping orders.json mid-generation)
        guide_glob = list(OUTPUT.glob(f"*_{token[:8]}*/*.html")) if OUTPUT.exists() else []
        if guide_glob:
            return jsonify({"status": "generated", "ready": True, "recovered": True})
        return jsonify({"status": "not_found", "ready": False, "failed": False}), 404

    # If still pending, try to verify payment and kick off generation
    if order["status"] == "pending" and order.get("stripe_session_id") and STRIPE_SECRET:
        try:
            session = stripe.checkout.Session.retrieve(order["stripe_session_id"])
            if session.payment_status == "paid":
                tier = order.get("tier", "single")
                _update_order(token, status="paid", tier=tier)
                tier_config = TIERS.get(tier, TIERS["single"])
                _add_credits(order["email"], tier_config["guides"], tier,
                             stripe_customer_id=session.get("customer"),
                             dedup_key=order["stripe_session_id"])
                # Start generation immediately
                _update_order(token, status="generating")
                t = threading.Thread(target=_generate_in_background, args=(token,), daemon=True)
                t.start()
                return jsonify({"status": "generating", "ready": False})
        except Exception as e:
            print(f"Stripe status check failed: {e}")

    return jsonify({
        "status": order["status"],
        "ready": order["status"] == "generated" and order.get("guide_path") is not None,
        "failed": order["status"] == "failed",
    })


@app.route("/download/<token>")
def download(token: str):
    """Serve the generated guide with navigation bar."""
    order = _get_order(token)
    if not order:
        abort(404, "Order not found")
    if order["status"] == "expired":
        abort(410, "Download link has expired")
    if order["status"] != "generated" or not order.get("guide_path"):
        return redirect(f"/generating/{token}")

    guide_path = Path(order["guide_path"])
    if not guide_path.exists():
        abort(404, "Guide file not found")

    html = guide_path.read_text(encoding="utf-8")

    # Build dashboard link if we have the email
    email = order.get("email", "").lower().strip()
    dash_link = _dashboard_url(email) if email else "/"

    nav_bar = f'''<div id="hostguide-nav" style="position:fixed;top:0;left:0;right:0;z-index:9999;
        background:rgba(255,255,255,0.95);backdrop-filter:blur(8px);border-bottom:1px solid #e0e0e0;
        padding:10px 20px;display:flex;align-items:center;justify-content:space-between;font-family:Inter,-apple-system,sans-serif;">
      <a href="{dash_link}" style="font-size:13px;color:#00796b;text-decoration:none;font-weight:600;">
        &larr; Dashboard
      </a>
      <div style="display:flex;gap:10px;">
        <a href="/download/{token}/pdf" style="font-size:12px;padding:6px 14px;background:#00796b;color:#fff;
           border-radius:6px;text-decoration:none;font-weight:500;">Download PDF</a>
      </div>
    </div>
    <div style="height:48px;"></div>'''

    # Inject nav bar after <body> tag
    html = html.replace("<body>", f"<body>\n{nav_bar}", 1)

    return html


@app.route("/download/<token>/pdf")
def download_pdf(token: str):
    """Serve the PDF version — regenerate on-demand if missing."""
    order = _get_order(token)
    if not order or order["status"] != "generated":
        abort(404)

    guide_path = Path(order.get("guide_path", ""))
    if not guide_path.exists():
        abort(404, "Guide file not found")

    pdf_path = guide_path.with_suffix(".pdf")

    # Regenerate PDF on-demand if it's missing
    if not pdf_path.exists():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                page = browser.new_page()
                page.goto(f"file://{guide_path.resolve()}", wait_until="networkidle", timeout=30000)
                page.pdf(path=str(pdf_path), format="A4",
                         margin={"top": "15mm", "bottom": "15mm",
                                 "left": "12mm", "right": "12mm"},
                         print_background=True)
                browser.close()
            print(f"On-demand PDF generated: {pdf_path}")
        except Exception as e:
            import traceback
            print(f"On-demand PDF generation failed: {e}\n{traceback.format_exc()}")
            abort(500, "Could not generate PDF")

    return send_file(pdf_path, mimetype="application/pdf",
                    as_attachment=True,
                    download_name="HostGuide_Guest_Guide.pdf")


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhook events."""
    payload = request.get_data(as_text=True)
    sig = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        abort(400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        token = session.get("metadata", {}).get("order_token")
        tier = session.get("metadata", {}).get("tier", "single")
        customer_id = session.get("customer")  # Stripe customer ID
        if token:
            _update_order(token, status="paid", tier=tier)
            # Add credits based on tier
            order = _get_order(token)
            if order:
                tier_config = TIERS.get(tier, TIERS["single"])
                _add_credits(order["email"], tier_config["guides"], tier,
                             stripe_customer_id=customer_id,
                             dedup_key=session.get("id", token))
            # Generation triggers from the /generating page when user lands there

    elif event["type"] == "invoice.paid":
        # Monthly subscription renewal — refill credits
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        customer_email = invoice.get("customer_email", "").lower().strip()
        # Skip the first invoice (credits already added via checkout.session.completed)
        if invoice.get("billing_reason") == "subscription_cycle":
            # Look up user by email or customer ID
            if not customer_email and customer_id:
                all_credits = _load_credits()
                for em, rec in all_credits.items():
                    if rec.get("stripe_customer_id") == customer_id:
                        customer_email = em
                        break
            if customer_email:
                user_rec = _get_user_credits(customer_email)
                tier = user_rec.get("tier", "starter")
                tier_config = TIERS.get(tier, TIERS["starter"])
                _add_credits(customer_email, tier_config["guides"], tier,
                             stripe_customer_id=customer_id,
                             dedup_key=invoice.get("id"))
                print(f"[invoice.paid] Refilled {tier_config['guides']} credits for {customer_email} ({tier})")

    elif event["type"] == "customer.subscription.deleted":
        # Subscription cancelled — zero out credits and reset tier
        sub = event["data"]["object"]
        customer_id = sub.get("customer")
        # Find user by customer ID
        all_credits = _load_credits()
        for em, rec in all_credits.items():
            if rec.get("stripe_customer_id") == customer_id:
                rec["credits"] = 0
                rec["tier"] = "none"
                _save_user_credits(em, rec)
                print(f"[subscription.deleted] Cancelled subscription for {em}")
                break

    return jsonify({"received": True})


@app.route("/admin/complete/<token>", methods=["POST"])
def admin_complete(token: str):
    """Admin endpoint: mark order as generated with guide path.

    Usage: curl -X POST localhost:5555/admin/complete/TOKEN \
           -H 'Authorization: Bearer dev-admin-secret' \
           -d 'guide_path=/path/to/guide.html'
    """
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {ADMIN_SECRET}":
        abort(401, "Unauthorized")
    guide_path = request.form.get("guide_path", "")
    if not guide_path:
        return jsonify({"error": "guide_path required"}), 400
    _update_order(token, status="generated", guide_path=guide_path)
    return jsonify({"ok": True, "download_url": f"/download/{token}"})


@app.route("/admin/subscribers")
def admin_subscribers():
    """Export subscriber list as JSON. Requires admin auth."""
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {ADMIN_SECRET}":
        abort(401, "Unauthorized")
    subs = _get_all_subscribers()
    return jsonify({"count": len(subs), "subscribers": subs})


# Static files (preview image)
@app.route("/static/<path:filename>")
def static_files(filename: str):
    static_dir = BASE / "static"
    return send_file(static_dir / filename)


# ═══════════════════════════════════════════════════════════════
# GENERATING PAGE (polls for completion)
# ═══════════════════════════════════════════════════════════════

GENERATING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Generating Your Guide — HostGuide</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Inter', sans-serif;
    background: #f8faf9;
    color: #1a1a1a;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
}
.card {
    background: white;
    border-radius: 16px;
    padding: 48px 40px;
    max-width: 460px;
    text-align: center;
    box-shadow: 0 4px 24px rgba(0,0,0,0.08);
}
.spinner {
    width: 48px; height: 48px;
    border: 4px solid #E0F2F1;
    border-top: 4px solid #00897B;
    border-radius: 50%;
    animation: spin 1s linear infinite;
    margin: 0 auto 24px;
}
@keyframes spin { to { transform: rotate(360deg); } }
h1 { font-size: 22px; margin-bottom: 8px; }
p { font-size: 14px; color: #666; line-height: 1.6; }
.status { margin-top: 16px; font-size: 13px; color: #00897B; font-weight: 500; }
.ready { display: none; }
.ready a {
    display: inline-block;
    margin-top: 16px;
    padding: 14px 32px;
    background: linear-gradient(135deg, #00897B, #00695C);
    color: white;
    text-decoration: none;
    border-radius: 8px;
    font-weight: 600;
    font-size: 15px;
}
</style>
</head>
<body>
<div class="card">
    <div class="generating">
        <div class="spinner"></div>
        <h1>Generating Your Guide</h1>
        <p>We're building a personalized neighborhood guide for your listing. This usually takes 3-5 minutes.</p>
        <p class="status">Analyzing your listing...</p>
        <div class="progress-bar" style="margin-top:20px;background:#E0F2F1;border-radius:8px;height:6px;overflow:hidden;">
          <div id="progressFill" style="width:5%;height:100%;background:linear-gradient(90deg,#00897B,#00695C);border-radius:8px;transition:width 1s ease;"></div>
        </div>
    </div>
    <div class="ready" id="readySection">
        <h1>Your Guide is Ready!</h1>
        <p>Your personalized neighborhood guide has been generated.</p>
        <a href="/download/{{ token }}">View Your Guide</a>
        <br>
        <a href="/download/{{ token }}/pdf" style="margin-top:12px; display:inline-block; padding:10px 24px; font-size:14px; background:#e0f2f1; color:#004d40; border-radius:8px; font-weight:600; text-decoration:none;">Download PDF</a>
        {% if dashboard_link %}
        <br>
        <a href="{{ dashboard_link }}" style="margin-top:12px; display:inline-block; padding:8px 20px; font-size:13px; background:#f3f4f6; color:#374151; border-radius:8px; font-weight:500; text-decoration:none;">Back to Dashboard</a>
        {% endif %}
    </div>
</div>
<script>
const token = "{{ token }}";
let checks = 0;
let notFoundCount = 0;
const steps = [
    {msg: 'Analyzing your listing...', pct: 10, until: 3},
    {msg: 'Launching browser to scrape details...', pct: 20, until: 6},
    {msg: 'Extracting property information...', pct: 30, until: 9},
    {msg: 'Detecting neighborhood & city...', pct: 40, until: 12},
    {msg: 'Finding nearby restaurants & cafes...', pct: 50, until: 18},
    {msg: 'Mapping transit & grocery stores...', pct: 60, until: 24},
    {msg: 'Discovering landmarks & nightlife...', pct: 70, until: 30},
    {msg: 'Building your neighborhood guide...', pct: 80, until: 40},
    {msg: 'Generating PDF...', pct: 90, until: 50},
    {msg: 'Almost there — finalizing...', pct: 95, until: 999}
];
function updateProgress() {
    const step = steps.find(s => checks < s.until) || steps[steps.length - 1];
    document.querySelector('.status').textContent = step.msg;
    document.getElementById('progressFill').style.width = step.pct + '%';
}
function pollStatus() {
    fetch(`/api/status/${token}`)
        .then(r => {
            if (r.status === 404) { notFoundCount++; return r.json(); }
            notFoundCount = 0;
            return r.json();
        })
        .then(data => {
            if (data.ready) {
                document.getElementById('progressFill').style.width = '100%';
                setTimeout(() => {
                    document.querySelector('.generating').style.display = 'none';
                    document.getElementById('readySection').style.display = 'block';
                }, 500);
            } else if (data.failed) {
                document.querySelector('.generating').innerHTML =
                    '<h1 style="font-size:22px;margin-bottom:8px;color:#dc2626;">Generation Failed</h1>' +
                    '<p style="font-size:14px;color:#666;line-height:1.6;">We couldn\\'t generate your guide. Please make sure the Airbnb URL is valid and the city is correct.</p>' +
                    '<p style="margin-top:16px;"><a href="/" style="color:#00897B;font-weight:600;">Try Again</a></p>' +
                    '<p style="margin-top:8px;font-size:13px;color:#888;">Your credit has been refunded. Questions? hello@host-guide.net</p>';
            } else if (notFoundCount >= 8) {
                document.querySelector('.generating').innerHTML =
                    '<h1 style="font-size:22px;margin-bottom:8px;color:#dc2626;">Generation Interrupted</h1>' +
                    '<p style="font-size:14px;color:#666;line-height:1.6;">Our server restarted during generation. Your credit has not been charged — please try again.</p>' +
                    '<p style="margin-top:16px;"><a href="/" style="color:#00897B;font-weight:600;">Try Again</a></p>' +
                    '<p style="margin-top:8px;font-size:13px;color:#888;">Questions? hello@host-guide.net</p>';
            } else if (checks < 80) {
                checks++;
                updateProgress();
                setTimeout(pollStatus, 5000);
            } else {
                document.querySelector('.generating').innerHTML =
                    '<h1 style="font-size:22px;margin-bottom:8px;">Taking longer than expected</h1>' +
                    '<p style="font-size:14px;color:#666;line-height:1.6;">Your guide is still being prepared. Please refresh this page in a few minutes.</p>' +
                    '<p style="margin-top:16px;font-size:13px;color:#888;">Questions? hello@host-guide.net</p>';
            }
        })
        .catch(() => { checks++; setTimeout(pollStatus, 5000); });
}
updateProgress();
setTimeout(pollStatus, 3000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    print(f"\n  HostGuide App starting...")
    print(f"  Stripe: {'configured' if STRIPE_SECRET else 'DEV MODE (skipping payment)'}")
    print(f"  Domain: {DOMAIN}")
    print(f"  Open: http://localhost:5555\n")
    app.run(host="0.0.0.0", port=5555, debug=True)
