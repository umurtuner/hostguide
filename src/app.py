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

app = Flask(__name__, static_folder=str(Path(__file__).parent.parent / "static"))
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
    # Check expiry (single guides expire in 24h, pack credits don't)
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
    # Don't downgrade tier: single purchase shouldn't overwrite active pack
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


# ── Admin seed: ensure owner account always has credits ──
_ADMIN_EMAIL = "umurtuner@gmail.com"
_admin = _get_user_credits(_ADMIN_EMAIL)
if _admin["credits"] < 100:
    _admin["credits"] = 1000
    _admin["tier"] = "starter"
    _save_user_credits(_ADMIN_EMAIL, _admin)
    print(f"[seed] Admin {_ADMIN_EMAIL} seeded with 1000 credits")


# ═══════════════════════════════════════════════════════════════
# QR CODE INJECTION
# ═══════════════════════════════════════════════════════════════

def _inject_qr_code(html: str, url: str) -> str:
    """Inject a QR code into the guide HTML linking to the digital version."""
    try:
        import qrcode
        import qrcode.image.svg
        import io
        import base64

        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        svg_b64 = base64.b64encode(buf.getvalue()).decode()

        qr_block = f'''
        <div style="text-align:center;margin:24px 0 12px;page-break-inside:avoid;">
            <img src="data:image/svg+xml;base64,{svg_b64}" style="width:100px;height:100px;" alt="QR Code">
            <p style="font-size:10px;color:#666;margin:6px 0 0;">Scan for the digital version of this guide</p>
        </div>'''

        # Insert before </body>
        if "</body>" in html:
            html = html.replace("</body>", f"{qr_block}\n</body>", 1)
        return html
    except ImportError:
        print("[qr] qrcode library not installed, skipping QR injection")
        return html
    except Exception as e:
        print(f"[qr] QR generation failed: {e}")
        return html


# ═══════════════════════════════════════════════════════════════
# PDF GENERATION (WeasyPrint - no browser needed)
# ═══════════════════════════════════════════════════════════════

def _generate_pdf(html_path: Path, pdf_path: Path):
    """Generate PDF from HTML file using WeasyPrint with print-optimized styles."""
    from weasyprint import HTML

    html_content = html_path.read_text(encoding="utf-8")

    # Inject print-optimized CSS before </head>
    print_css = """
<style>
@page { size: A4; margin: 15mm 12mm; }

/* Hide raw link URLs that WeasyPrint exposes */
a { text-decoration: none !important; color: #1a1a1a !important; }
a::after { content: none !important; }
a[href]::after { content: none !important; }

/* Fix table layout — prevent column wrapping */
table { width: 100% !important; table-layout: fixed !important; }
.place-row td { vertical-align: top; padding: 8px 0; border-bottom: 1px solid #eee; }
.place-name { width: 60% !important; font-weight: 600; font-size: 13px; word-wrap: break-word; }
.place-detail { width: 40% !important; text-align: right; font-size: 12px; color: #666; white-space: nowrap; }
.place-link { color: #1a1a1a !important; text-decoration: none !important; }
.place-addr { font-size: 11px; color: #888; }
.place-dist { font-size: 12px; color: #555; }
.rating { font-size: 11px; color: #e6a117; font-weight: 500; }

/* Compact sections */
.section { page-break-inside: avoid; margin-bottom: 16px; }
h2 { font-size: 18px; margin-bottom: 8px; }
h3 { font-size: 14px; margin-bottom: 4px; }

/* Hero header */
.hero { padding: 24px 28px !important; }
.hero h1 { font-size: 26px !important; }

/* Tips and info tables */
.tip-row td { padding: 6px 8px; font-size: 12px; }
.info-table td { padding: 4px 8px; font-size: 12px; }
.safety-list li { font-size: 12px; margin-bottom: 4px; }

/* Map embed — hide in PDF (not renderable) */
iframe { display: none !important; }
.map-container { display: none !important; }

/* Footer */
.footer { font-size: 10px; color: #999; text-align: center; margin-top: 20px; }
</style>
"""
    html_content = html_content.replace("</head>", f"{print_css}\n</head>", 1)

    HTML(string=html_content, base_url=str(html_path.parent)).write_pdf(str(pdf_path))


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
        from src.enricher import enrich_without_api, enrich_with_google_places, _merge_enriched
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

        # Step 2b: Reverse geocode to fill city/neighborhood/country
        geo = _reverse_geocode(listing.lat, listing.lng)
        if not listing.city and geo["city"]:
            listing.city = geo["city"]
        if not listing.neighborhood and geo["neighborhood"]:
            listing.neighborhood = geo["neighborhood"]
        geo_country = geo.get("country_code", "")

        # Step 3: Enrich with OSM Overpass (free, no API key)
        # Prefer reverse geocode city (reliable) over order city (may contain listing subtitle junk)
        city_name = listing.city or geo.get("city", "") or ""
        if not city_name:
            # Last resort: try order city but filter out listing subtitles
            raw = order.get("city", "")
            if raw and not any(w in raw.lower() for w in ("bed", "bath", "entire", "private", "shared")):
                city_name = raw.split(",")[-1].strip()
        city_config = _get_city_config(city_name or "Unknown")
        # Set country from reverse geocode if not already in config
        if not city_config.get("country") and geo_country:
            city_config["country"] = geo_country
        if not listing.city:
            listing.city = city_config["name"]

        print(f"Enriching {listing.city} at {listing.lat},{listing.lng}...")
        # Primary: Google Places (paid, reliable, high coverage). Fall back to
        # OSM Overpass when the Google key is missing or returns nothing useful.
        # Then merge so OSM can fill any holes Google left empty.
        enriched = enrich_with_google_places(listing.lat, listing.lng)
        google_total = sum(len(getattr(enriched, c, []) or []) for c in
                           ("transit", "grocery", "restaurant", "landmark", "nightlife", "health"))
        print(f"[google_places] returned {google_total} POIs total")

        if google_total < 8:
            print(f"[fallback] Google returned {google_total} POIs — running OSM fallback")
            osm_enriched = enrich_without_api(listing.lat, listing.lng, city_config)
            enriched = _merge_enriched(enriched, osm_enriched) if google_total > 0 else osm_enriched

        # Hard quality gate: refuse to ship a guide with no place data. Refund
        # the credit and surface the failure rather than ship an empty PDF
        # (this is what burned Joao on the v1 send).
        poi_total = sum(len(getattr(enriched, c, []) or []) for c in
                        ("transit", "grocery", "restaurant", "landmark", "nightlife", "health"))
        if poi_total < 8:
            print(f"[QUALITY_GATE_FAIL] token={token} listing={listing_id} "
                  f"city={listing.city} lat={listing.lat} lng={listing.lng} "
                  f"total={poi_total} — refusing to ship empty guide, refunding credit")
            return False

        # Step 4: Generate guide (HTML). Use Claude when ANTHROPIC_API_KEY is set.
        print(f"Generating guide for listing {listing_id} ({poi_total} POIs)...")
        guide = generate_guide(listing, enriched, city_config, use_claude=True)

        # Step 5: Inject QR code and save guide HTML
        guide_url = f"{DOMAIN}/download/{token}"
        html_with_qr = _inject_qr_code(guide.content_html, guide_url)
        guide_dir = OUTPUT / listing.city.lower() / "guides"
        guide_dir.mkdir(parents=True, exist_ok=True)
        html_path = guide_dir / f"{listing_id}_guide.html"
        html_path.write_text(html_with_qr, encoding="utf-8")

        # Step 6: Generate PDF using WeasyPrint (pure Python, no browser needed)
        pdf_path = html_path.with_suffix(".pdf")
        try:
            _generate_pdf(html_path, pdf_path)
            print(f"PDF generated via WeasyPrint: {pdf_path}")
        except Exception as e:
            import traceback
            print(f"WeasyPrint PDF failed: {e}\n{traceback.format_exc()}")
            pdf_path.unlink(missing_ok=True)

        # Update order — single purchases expire in 24h, pack credits don't
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

        # Parse title: "Guest house in Lisbon · ★4.33 · 1 bedroom · 1 private bathroom"
        # Extract city from the FIRST "in <Place>" segment (parts[-1] grabs junk
        # like "1 private bathroom" which then propagates to title/header/taxi
        # copy throughout the guide).
        if meta["title"] and "·" in meta["title"]:
            parts = meta["title"].split("·")
            meta["title"] = parts[0].strip()
            in_match = re.match(r'([\w\s]+?)\s+in\s+(.+)', meta["title"])
            if in_match:
                meta["property_type"] = in_match.group(1).strip()
                meta["city"] = in_match.group(2).split(",")[0].strip()
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
<title>HostGuide - Neighborhood Guides for Airbnb Hosts | Auto-Generated from Your Listing</title>
<meta name="description" content="Generate personalized neighborhood guides for Airbnb guests in minutes. Real ratings, walking distances, local tips - just paste your listing URL.">
<meta name="robots" content="index, follow">
<meta name="google-site-verification" content="qg0cPYAQg0Zr4Ozt6sD4d4SU0_6l5cFbA1EsgVJxJvc">
<meta name="theme-color" content="#004d40">
<link rel="canonical" href="https://www.host-guide.net">
<link rel="alternate" hreflang="en" href="https://www.host-guide.net">
<link rel="alternate" hreflang="x-default" href="https://www.host-guide.net">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='35' r='20' fill='%23004d40'/><path d='M50 75 L30 45 Q30 15 50 15 Q70 15 70 45 Z' fill='%23004d40'/><circle cx='50' cy='35' r='8' fill='white'/></svg>">
<!-- Open Graph -->
<meta property="og:title" content="HostGuide - Neighborhood Guides for Airbnb Hosts">
<meta property="og:description" content="Auto-generate personalized neighborhood guides with real ratings, walking distances, and local tips. Just paste your Airbnb listing URL.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://www.host-guide.net">
<meta property="og:site_name" content="HostGuide">
<meta property="og:image" content="https://www.host-guide.net/static/og-image.png">
<meta property="og:locale" content="en_US">
<!-- Twitter Card -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="HostGuide - Neighborhood Guides for Airbnb Hosts">
<meta name="twitter:description" content="Auto-generate personalized neighborhood guides with real ratings, walking distances, and local tips. Just paste your Airbnb listing URL.">
<meta name="twitter:image" content="https://www.host-guide.net/static/og-image.png">
<!-- Structured Data -->
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "name": "HostGuide",
  "description": "Auto-generate personalized neighborhood guides for Airbnb guests with real ratings, walking distances, and local tips.",
  "url": "https://www.host-guide.net",
  "applicationCategory": "BusinessApplication",
  "operatingSystem": "Web",
  "offers": {
    "@type": "Offer",
    "price": "4.99",
    "priceCurrency": "USD"
  }
}
</script>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {
      "@type": "Question",
      "name": "How long does it take to generate a guide?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "Most guides are ready in 1-2 minutes. We analyze your listing location, find all nearby points of interest, and format everything into a clean guide automatically."
      }
    },
    {
      "@type": "Question",
      "name": "Does this work for my city?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "If Google Maps covers it, we cover it. We have generated guides across Europe, North America, the Middle East, and Asia."
      }
    },
    {
      "@type": "Question",
      "name": "How is this better than my own Google Doc?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "Real ratings, verified walking distances, safety data, and a layout guests actually read. Most hosts spend 1-2 hours writing what we generate in a minute."
      }
    },
    {
      "@type": "Question",
      "name": "Will my guests actually use this?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "The #1 complaint in Airbnb reviews is lack of local recommendations. A printed guide on the kitchen counter gets picked up. A paragraph buried in your listing description does not."
      }
    },
    {
      "@type": "Question",
      "name": "What if I manage multiple properties?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "Each guide is tied to a specific location. Our 5 Guide Pack is $14.99 (launch price) - that is $3.00 per guide instead of $4.99. Credits never expire."
      }
    }
  ]
}
</script>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  "itemListElement": [
    {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://www.host-guide.net/"}
  ]
}
</script>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@700;800&display=swap" as="style">
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
  .gradient-hero {
    background: linear-gradient(135deg, #004d40 0%, #00695c 40%, #00897b 100%);
    position: relative;
    overflow: hidden;
  }
  .gradient-hero::before {
    content: '';
    position: absolute;
    top: -50%;
    right: -20%;
    width: 600px;
    height: 600px;
    background: radial-gradient(circle, rgba(77,182,172,0.3) 0%, transparent 70%);
    pointer-events: none;
  }
  .gradient-hero::after {
    content: '';
    position: absolute;
    bottom: -30%;
    left: -10%;
    width: 400px;
    height: 400px;
    background: radial-gradient(circle, rgba(0,105,92,0.4) 0%, transparent 70%);
    pointer-events: none;
  }
  .glass-card { background: rgba(255,255,255,0.95); backdrop-filter: blur(20px); }
  .fade-in { animation: fadeIn 0.6s ease-out; }
  .fade-in-delay { animation: fadeIn 0.8s ease-out 0.2s both; }
  @keyframes fadeIn { from { opacity:0; transform:translateY(16px); } to { opacity:1; transform:translateY(0); } }
  @keyframes float { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }
  .float-card { animation: float 4s ease-in-out infinite; }
  .float-card-delay { animation: float 4s ease-in-out 1s infinite; }
  .faq-answer { max-height:0; overflow:hidden; transition: max-height 0.3s ease; }
  .faq-answer.open { max-height: 200px; }
  .feature-card:hover { transform: translateY(-2px); box-shadow: 0 12px 40px rgba(0,0,0,0.08); }
  .cta-btn { transition: all 0.2s; }
  .cta-btn:hover { transform: translateY(-1px); box-shadow: 0 8px 24px rgba(0,105,92,0.35); }
  .stat-pill { background: rgba(255,255,255,0.12); backdrop-filter: blur(8px); border: 1px solid rgba(255,255,255,0.15); }
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
<nav class="gradient-hero" style="position:relative;z-index:10;" role="navigation" aria-label="Main navigation">
  <div class="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
    <div class="flex items-center gap-2.5">
      <svg width="32" height="32" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect width="32" height="32" rx="8" fill="white" fill-opacity="0.2"/>
        <!-- Open book / guide -->
        <path d="M7 10c3-1 5.5-.5 9 1v13c-3.5-1.5-6-2-9-1V10z" fill="white" fill-opacity="0.9"/>
        <path d="M25 10c-3-1-5.5-.5-9 1v13c3.5-1.5 6-2 9-1V10z" fill="white" fill-opacity="0.7"/>
        <!-- Map pin -->
        <circle cx="20" cy="9" r="4" fill="#4DB6AC"/>
        <circle cx="20" cy="8.5" r="1.5" fill="white"/>
        <path d="M20 13l-1.5-2.5h3L20 13z" fill="#4DB6AC"/>
      </svg>
      <span class="text-white font-semibold text-lg tracking-tight">HostGuide</span>
    </div>
    <a href="#pricing" class="text-white/80 hover:text-white text-sm font-medium transition">Pricing</a>
  </div>
</nav>

<main>
<!-- ════════ HERO ════════ -->
<section class="gradient-hero pb-36 pt-20 px-6 text-white">
  <div class="max-w-5xl mx-auto flex flex-col md:flex-row items-center gap-12 relative" style="z-index:2;">
    <!-- Left: Copy -->
    <div class="flex-1 text-center md:text-left fade-in">
      <div class="inline-flex items-center gap-2 mb-6 px-4 py-1.5 stat-pill rounded-full text-sm font-medium">
        <span class="w-2 h-2 bg-emerald-400 rounded-full animate-pulse"></span>
        Used by hosts in 10+ cities
      </div>
      <h1 class="text-4xl md:text-5xl lg:text-[3.4rem] font-extrabold leading-[1.1] mb-5 tracking-tight">
        Your guests deserve<br>better than a<br>
        <span class="relative inline-block">
          <span class="relative z-10">Google Doc</span>
          <span class="absolute bottom-1 left-0 w-full h-3 bg-red-400/30 -rotate-1 rounded-sm"></span>
        </span>
      </h1>
      <p class="text-lg md:text-xl text-white/80 max-w-lg leading-relaxed mb-8">
        Paste your Airbnb link. Get a polished, printable neighborhood guide with real ratings, walking distances, and local tips - built for your exact location in 60 seconds.
      </p>
      <div class="flex flex-wrap items-center gap-4 justify-center md:justify-start text-sm text-white/70">
        <span class="flex items-center gap-1.5"><svg class="w-4 h-4 text-emerald-400" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/></svg>No signup required</span>
        <span class="flex items-center gap-1.5"><svg class="w-4 h-4 text-emerald-400" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/></svg>Any city worldwide</span>
        <span class="flex items-center gap-1.5"><svg class="w-4 h-4 text-emerald-400" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/></svg>Print or share digitally</span>
      </div>
    </div>
    <!-- Right: Floating guide mockup -->
    <div class="flex-1 hidden md:flex justify-center relative" style="min-height:320px;">
      <!-- Background glow -->
      <div class="absolute inset-0 bg-gradient-to-br from-teal-400/20 to-transparent rounded-full blur-3xl"></div>
      <!-- Main card -->
      <div class="float-card relative bg-white rounded-2xl shadow-2xl p-5 w-72 text-gray-900" role="img" aria-label="Sample neighborhood guide showing nearby restaurants, groceries, and landmarks" style="transform-origin:center;">
        <div class="bg-gradient-to-r from-teal-600 to-teal-800 rounded-xl p-4 mb-3">
          <p class="text-white text-[10px] font-medium uppercase tracking-wider opacity-80">Neighborhood Guide</p>
          <p class="text-white font-bold text-base mt-0.5">Eaux-Vives, Geneva</p>
          <p class="text-white/70 text-[10px] mt-1">Prepared by your host</p>
        </div>
        <div class="space-y-2">
          <div class="flex items-center gap-2 text-xs">
            <span class="w-6 h-6 bg-teal-50 rounded-lg flex items-center justify-center flex-shrink-0">
              <svg class="w-3.5 h-3.5 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8.25v-1.5m0 1.5c-1.355 0-2.697.056-4.024.166C6.845 8.51 6 9.473 6 10.608v2.513m6-4.871c1.355 0 2.697.056 4.024.166C17.155 8.51 18 9.473 18 10.608v2.513"/></svg>
            </span>
            <div class="flex-1">
              <p class="font-semibold text-gray-800">Cafe du Soleil</p>
              <p class="text-gray-400 text-[10px]">4.6 stars - 3 min walk</p>
            </div>
          </div>
          <div class="flex items-center gap-2 text-xs">
            <span class="w-6 h-6 bg-amber-50 rounded-lg flex items-center justify-center flex-shrink-0">
              <svg class="w-3.5 h-3.5 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 3h1.386c.51 0 .955.343 1.087.835l.383 1.437M7.5 14.25a3 3 0 00-3 3h15.75m-12.75-3h11.218c1.121-2.3 2.1-4.684 2.924-7.138a60.114 60.114 0 00-16.536-1.84M7.5 14.25L5.106 5.272M6 20.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm12.75 0a.75.75 0 11-1.5 0 .75.75 0 011.5 0z"/></svg>
            </span>
            <div class="flex-1">
              <p class="font-semibold text-gray-800">Migros Eaux-Vives</p>
              <p class="text-gray-400 text-[10px]">Grocery - 4 min walk</p>
            </div>
          </div>
          <div class="flex items-center gap-2 text-xs">
            <span class="w-6 h-6 bg-blue-50 rounded-lg flex items-center justify-center flex-shrink-0">
              <svg class="w-3.5 h-3.5 text-blue-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15 10.5a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 10.5c0 7.142-7.5 11.25-7.5 11.25S4.5 17.642 4.5 10.5a7.5 7.5 0 1115 0z"/></svg>
            </span>
            <div class="flex-1">
              <p class="font-semibold text-gray-800">Jet d'Eau</p>
              <p class="text-gray-400 text-[10px]">Landmark - 8 min walk</p>
            </div>
          </div>
        </div>
      </div>
      <!-- Floating accent card -->
      <div class="float-card-delay absolute -bottom-2 -left-4 bg-white rounded-xl shadow-lg p-3 w-48" style="z-index:3;">
        <div class="flex items-center gap-2">
          <div class="w-8 h-8 bg-emerald-100 rounded-lg flex items-center justify-center">
            <svg class="w-4 h-4 text-emerald-600" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/></svg>
          </div>
          <div>
            <p class="text-[11px] font-bold text-gray-800">Ready in 60s</p>
            <p class="text-[9px] text-gray-400">PDF + digital link</p>
          </div>
        </div>
      </div>
      <!-- Rating badge -->
      <div class="float-card absolute -top-2 -right-2 bg-white rounded-xl shadow-lg p-2.5 w-36" style="z-index:3;animation-delay:0.5s;">
        <div class="flex items-center gap-1.5">
          <span class="text-amber-400 text-sm">&#9733;&#9733;&#9733;&#9733;&#9733;</span>
          <span class="text-[10px] font-bold text-gray-700">4.8 avg</span>
        </div>
        <p class="text-[9px] text-gray-400 mt-0.5">Real Google ratings</p>
      </div>
    </div>
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
        Preview My Guide - Free
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
  <h2 class="text-2xl font-bold text-center mb-12">Three steps. Done before your coffee gets cold.</h2>
  <div class="grid md:grid-cols-3 gap-8">
    <div class="text-center">
      <div class="w-14 h-14 bg-teal-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
        <svg class="w-6 h-6 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244"/></svg>
      </div>
      <h3 class="font-semibold mb-1">Paste your Airbnb link</h3>
      <p class="text-sm text-gray-500">That's all we need. No signup wall, no address lookup.</p>
    </div>
    <div class="text-center">
      <div class="w-14 h-14 bg-teal-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
        <svg class="w-6 h-6 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/></svg>
      </div>
      <h3 class="font-semibold mb-1">We scan your neighborhood</h3>
      <p class="text-sm text-gray-500">Real Google Maps data. Restaurants, groceries, pharmacies, transit - ranked by rating and distance from your door.</p>
    </div>
    <div class="text-center">
      <div class="w-14 h-14 bg-teal-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
        <svg class="w-6 h-6 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg>
      </div>
      <h3 class="font-semibold mb-1">Download your guide</h3>
      <p class="text-sm text-gray-500">A clean, branded PDF your guests will actually use. Share digitally or leave a printed copy at the property.</p>
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
  <h2 class="text-2xl font-bold mb-3">What your guests will see</h2>
  <p class="text-sm text-gray-500 mb-10">Not a sloppy Google Doc. A guide that looks like you hired a local concierge.</p>
  <div class="relative bg-white rounded-2xl shadow-lg overflow-hidden border border-gray-100 text-left" style="max-height:420px;">
    <div class="bg-gradient-to-r from-teal-600 to-teal-800 px-8 py-6 text-white">
      <p class="text-xs uppercase tracking-widest opacity-70 mb-1">Neighborhood Guide</p>
      <h3 class="text-xl font-bold">Downtown Miami</h3>
      <p class="text-sm opacity-80 mt-1">Hosted by Kevin &middot; 3-bedroom apartment</p>
    </div>
    <div class="p-8 grid sm:grid-cols-2 gap-6">
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Top Restaurants</h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>Zuma - <span class="text-yellow-500">&#9733;</span> 4.6 <span class="text-gray-400">&middot; 4 min walk</span></li>
          <li>Cipriani - <span class="text-yellow-500">&#9733;</span> 4.4 <span class="text-gray-400">&middot; 6 min walk</span></li>
          <li>La Mar by Gaston - <span class="text-yellow-500">&#9733;</span> 4.5 <span class="text-gray-400">&middot; 3 min walk</span></li>
        </ul>
      </div>
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Groceries &amp; Essentials</h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>Whole Foods Brickell - <span class="text-yellow-500">&#9733;</span> 4.3 <span class="text-gray-400">&middot; 7 min walk</span></li>
          <li>Publix Downtown - <span class="text-yellow-500">&#9733;</span> 4.2 <span class="text-gray-400">&middot; 5 min drive</span></li>
        </ul>
      </div>
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Landmarks</h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>Bayfront Park - <span class="text-yellow-500">&#9733;</span> 4.7 <span class="text-gray-400">&middot; 2 min walk</span></li>
          <li>Perez Art Museum - <span class="text-yellow-500">&#9733;</span> 4.6 <span class="text-gray-400">&middot; 8 min walk</span></li>
        </ul>
      </div>
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Local Tips</h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>Free Metromover covers downtown</li>
          <li>Best coffee: Per'La on Brickell</li>
          <li>Uber works everywhere - skip rental</li>
        </ul>
      </div>
    </div>
    <!-- Fade overlay to crop effect -->
    <div class="absolute bottom-0 left-0 right-0 h-32 bg-gradient-to-t from-gray-50 to-transparent pointer-events-none"></div>
    <div class="absolute bottom-4 left-0 right-0 text-center pointer-events-none">
      <span class="inline-block bg-white/90 backdrop-blur-sm text-teal-700 text-sm font-semibold px-5 py-2 rounded-full shadow-sm border border-teal-100 pointer-events-auto">
        + Safety tips, transit, useful info &amp; more &darr;
      </span>
    </div>
  </div>
</section>

<!-- ════════ SOCIAL PROOF ════════ -->
<section class="bg-white border-y border-gray-100 py-16 mb-24">
  <div class="max-w-4xl mx-auto px-6">
    <h2 class="text-2xl font-bold text-center mb-10">Hosts in 10 cities already stopped answering "where should we eat?"</h2>
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
<section id="pricing" class="max-w-3xl mx-auto px-6 mb-24">
  <h2 class="text-2xl font-bold text-center mb-3">Pay once per listing. Use it forever.</h2>
  <p class="text-sm text-gray-500 text-center mb-10">No subscriptions. No per-guest fees. One guide, unlimited prints.</p>
  <div class="grid md:grid-cols-2 gap-5 max-w-2xl mx-auto items-start">

    <!-- Single -->
    <div class="bg-white rounded-2xl shadow-md p-7 border border-gray-100 text-center">
      <h3 class="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-4">Single Guide</h3>
      <div class="text-3xl font-extrabold text-gray-800 mb-1"><span class="text-lg line-through text-gray-300 mr-1">$19.99</span>$4.99</div>
      <div class="text-xs text-gray-500 mb-1">one-time</div>
      <div class="text-xs text-teal-600 font-medium mb-5">Launch pricing - 75% off</div>
      <ul class="text-left text-sm text-gray-600 space-y-2 mb-7">
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> 1 personalized guide</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> 30+ nearby places with ratings</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> PDF + web version</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Safety tips + local info</li>
      </ul>
      <a href="#" onclick="document.getElementById('airbnb_url').focus();window.scrollTo({top:0,behavior:'smooth'});return false;"
         class="block w-full py-2.5 bg-white border-2 border-gray-200 text-gray-700 rounded-xl font-semibold text-sm text-center hover:border-teal-400 transition">
        Get One Guide
      </a>
    </div>

    <!-- 5 Guide Pack -->
    <div class="bg-white rounded-2xl shadow-xl p-7 border-2 border-teal-500 text-center relative md:-mt-2 md:mb-0">
      <div class="absolute -top-3 left-1/2 -translate-x-1/2 bg-orange-500 text-white text-xs font-semibold px-4 py-1 rounded-full">83% off + save 40% per guide</div>
      <h3 class="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-4 mt-1">5 Guide Pack</h3>
      <div class="text-3xl font-extrabold text-teal-700 mb-1"><span class="text-lg line-through text-gray-300 mr-1">$89.99</span>$14.99</div>
      <div class="text-xs text-gray-500 mb-1">$3.00/guide instead of $4.99</div>
      <div class="text-xs text-teal-600 font-medium mb-5">Launch pricing</div>
      <ul class="text-left text-sm text-gray-600 space-y-2 mb-7">
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> 5 personalized guides</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> 30+ nearby places with ratings</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> PDF + web version</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Safety tips + local info</li>
        <li class="flex items-start gap-2"><svg class="w-3.5 h-3.5 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Use anytime - credits never expire</li>
      </ul>
      <a href="#" onclick="document.getElementById('airbnb_url').focus();window.scrollTo({top:0,behavior:'smooth'});return false;"
         class="cta-btn block w-full py-2.5 bg-gradient-to-r from-teal-600 to-teal-800 text-white rounded-xl font-semibold text-sm text-center">
        Get 5 Guides
      </a>
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
        Does this work for my city?
        <span class="text-gray-400">+</span>
      </button>
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">If Google Maps covers it, we cover it. We have generated guides across Europe, North America, the Middle East, and Asia.</p></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm overflow-hidden">
      <button onclick="this.nextElementSibling.classList.toggle('open')" class="w-full text-left px-6 py-4 flex items-center justify-between text-sm font-semibold hover:bg-gray-50 transition">
        How is this better than my own Google Doc?
        <span class="text-gray-400">+</span>
      </button>
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">Real ratings, verified walking distances, safety data, and a layout guests actually read. Most hosts spend 1-2 hours writing what we generate in a minute. Airbnb's own <a href="https://www.airbnb.com/resources/hosting-homes/a/how-to-write-a-great-guidebook-for-your-guests-21" target="_blank" rel="noopener" class="text-teal-600 hover:underline">guidebook tips</a> confirm that local recommendations are what guests value most.</p></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm overflow-hidden">
      <button onclick="this.nextElementSibling.classList.toggle('open')" class="w-full text-left px-6 py-4 flex items-center justify-between text-sm font-semibold hover:bg-gray-50 transition">
        Will my guests actually use this?
        <span class="text-gray-400">+</span>
      </button>
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">The #1 complaint in Airbnb reviews is lack of local recommendations. A printed guide on the kitchen counter gets picked up. A paragraph buried in your listing description does not. That's why Airbnb highlights <a href="https://www.airbnb.com/resources/hosting-homes/a/what-guests-really-want-43" target="_blank" rel="noopener" class="text-teal-600 hover:underline">guest communication</a> as a key factor for <a href="https://www.airbnb.com/d/superhost" target="_blank" rel="noopener" class="text-teal-600 hover:underline">Superhost status</a>.</p></div>
    </div>
    <div class="bg-white rounded-xl shadow-sm overflow-hidden">
      <button onclick="this.nextElementSibling.classList.toggle('open')" class="w-full text-left px-6 py-4 flex items-center justify-between text-sm font-semibold hover:bg-gray-50 transition">
        What if I manage multiple properties?
        <span class="text-gray-400">+</span>
      </button>
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">Each guide is tied to a specific location. Our 5 Guide Pack is $14.99 (launch price) - that's $3.00 per guide instead of $4.99. Credits never expire.</p></div>
    </div>
  </div>
</section>

</main>
<!-- ════════ FOOTER ════════ -->
<footer class="border-t border-gray-100 py-10 text-center" role="contentinfo">
  <div class="max-w-4xl mx-auto px-6">
    <div class="flex items-center justify-center gap-2 mb-3">
      <svg width="24" height="24" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect width="32" height="32" rx="8" fill="#00897b"/>
        <path d="M7 10c3-1 5.5-.5 9 1v13c-3.5-1.5-6-2-9-1V10z" fill="white" fill-opacity="0.9"/>
        <path d="M25 10c-3-1-5.5-.5-9 1v13c3.5-1.5 6-2 9-1V10z" fill="white" fill-opacity="0.7"/>
        <circle cx="20" cy="9" r="4" fill="#4DB6AC"/>
        <circle cx="20" cy="8.5" r="1.5" fill="white"/>
        <path d="M20 13l-1.5-2.5h3L20 13z" fill="#4DB6AC"/>
      </svg>
      <span class="font-semibold text-sm">HostGuide</span>
    </div>
    <p class="text-xs text-gray-400 mb-4">Made for Airbnb hosts, by an Airbnb host.</p>
    <div class="flex flex-wrap items-center justify-center gap-4 mb-4 text-xs text-gray-400">
      <span class="font-medium text-gray-500">Hosting resources:</span>
      <a href="https://www.airbnb.com/d/superhost" target="_blank" rel="noopener" class="hover:text-teal-600 transition">Superhost Program</a>
      <a href="https://www.airbnb.com/resources/hosting-homes" target="_blank" rel="noopener" class="hover:text-teal-600 transition">Airbnb Host Resources</a>
      <a href="https://www.airbnb.com/help/article/2895" target="_blank" rel="noopener" class="hover:text-teal-600 transition">Review Guidelines</a>
    </div>
    <div class="flex flex-wrap items-center justify-center gap-3 mb-4 text-xs text-gray-400">
      <a href="/guides/geneva" class="hover:text-teal-600 transition">Geneva</a>
      <a href="/guides/dubai" class="hover:text-teal-600 transition">Dubai</a>
      <a href="/guides/miami" class="hover:text-teal-600 transition">Miami</a>
      <a href="/guides/lisbon" class="hover:text-teal-600 transition">Lisbon</a>
      <a href="/guides/barcelona" class="hover:text-teal-600 transition">Barcelona</a>
      <a href="/guides/paris" class="hover:text-teal-600 transition">Paris</a>
      <a href="/guides/london" class="hover:text-teal-600 transition">London</a>
      <a href="/guides/new-york" class="hover:text-teal-600 transition">New York</a>
      <a href="/guides/bali" class="hover:text-teal-600 transition">Bali</a>
      <a href="/guides/bangkok" class="hover:text-teal-600 transition">Bangkok</a>
    </div>
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
      <svg width="28" height="28" viewBox="0 0 32 32" fill="none"><rect width="32" height="32" rx="8" fill="white" fill-opacity="0.15"/><path d="M7 10c3-1 5.5-.5 9 1v13c-3.5-1.5-6-2-9-1V10z" fill="white" fill-opacity="0.9"/><path d="M25 10c-3-1-5.5-.5-9 1v13c3.5-1.5 6-2 9-1V10z" fill="white" fill-opacity="0.7"/><circle cx="20" cy="9" r="4" fill="#4DB6AC"/><circle cx="20" cy="8.5" r="1.5" fill="white"/><path d="M20 13l-1.5-2.5h3L20 13z" fill="#4DB6AC"/></svg>
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
          <li>{{ restaurants[0] if restaurants else 'Loading...' }} - <span class="text-gray-400">{{ distances[0] if distances else '' }}</span></li>
          <li>{{ restaurants[1] if restaurants|length > 1 else '...' }} - <span class="text-gray-400">{{ distances[1] if distances|length > 1 else '' }}</span></li>
          <li class="blur-light text-gray-400">{{ restaurants[2] if restaurants|length > 2 else '...' }} - <span>{{ distances[2] if distances|length > 2 else '' }}</span></li>
        </ul>
      </div>
      <div>
        <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3 flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 3h1.386c.51 0 .955.343 1.087.835l.383 1.437M7.5 14.25a3 3 0 00-3 3h15.75m-12.75-3h11.218c1.121-2.3 2.1-4.684 2.924-7.138a60.114 60.114 0 00-16.536-1.84M7.5 14.25L5.106 5.272M6 20.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm12.75 0a.75.75 0 11-1.5 0 .75.75 0 011.5 0z"/></svg>
          Groceries
        </h4>
        <ul class="space-y-2 text-sm text-gray-600">
          <li>{{ groceries[0] if groceries else 'Loading...' }} - <span class="text-gray-400">{{ gdistances[0] if gdistances else '' }}</span></li>
          <li class="blur-light text-gray-400">{{ groceries[1] if groceries|length > 1 else '...' }} - <span>{{ gdistances[1] if gdistances|length > 1 else '' }}</span></li>
        </ul>
      </div>
    </div>

    <!-- Bottom section: HEAVILY BLURRED -->
    <div class="p-8 blur-zone relative">
      <div class="grid sm:grid-cols-2 gap-6">
        <div>
          <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Transit & Transport</h4>
          <ul class="space-y-2 text-sm text-gray-600">
            <li>Metro Station Central - <span class="text-gray-400">4 min walk</span></li>
            <li>Bus Line 42 Stop - <span class="text-gray-400">2 min walk</span></li>
            <li>Taxi Rank - <span class="text-gray-400">6 min walk</span></li>
          </ul>
        </div>
        <div>
          <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Landmarks & Parks</h4>
          <ul class="space-y-2 text-sm text-gray-600">
            <li>Central Park - <span class="text-gray-400">8 min walk</span></li>
            <li>Art Museum - <span class="text-gray-400">12 min walk</span></li>
            <li>Historic District - <span class="text-gray-400">5 min walk</span></li>
          </ul>
        </div>
        <div>
          <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Nightlife</h4>
          <ul class="space-y-2 text-sm text-gray-600">
            <li>Rooftop Bar - <span class="text-gray-400">3 min walk</span></li>
            <li>Jazz Club - <span class="text-gray-400">7 min walk</span></li>
          </ul>
        </div>
        <div>
          <h4 class="text-xs font-bold text-teal-700 uppercase tracking-wide mb-3">Health & Safety</h4>
          <ul class="space-y-2 text-sm text-gray-600">
            <li>Pharmacy - <span class="text-gray-400">3 min walk</span></li>
            <li>Hospital - <span class="text-gray-400">10 min drive</span></li>
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
    <div class="grid grid-cols-2 gap-4 max-w-lg mx-auto">

      <!-- Single -->
      <form action="/checkout" method="POST" class="text-center">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="tier" value="single">
        <div class="bg-white rounded-xl border border-gray-200 p-4 hover:border-teal-400 transition h-full flex flex-col relative">
          <div class="absolute -top-2.5 left-1/2 -translate-x-1/2 bg-orange-500 text-white text-xs font-semibold px-3 py-0.5 rounded-full">50% off</div>
          <div class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2 mt-1">Single Guide</div>
          <div class="text-2xl font-extrabold text-gray-800"><span class="text-sm line-through text-gray-300">$19.99</span> $4.99</div>
          <div class="text-xs text-teal-600 font-medium mb-3">Launch pricing</div>
          <ul class="text-xs text-gray-500 text-left space-y-1 mb-4 flex-grow">
            <li>&#10003; This guide only</li>
            <li>&#10003; PDF + web version</li>
            <li>&#10003; 30+ places with ratings</li>
          </ul>
          <button type="submit" class="w-full py-2.5 bg-white border-2 border-gray-200 text-gray-700 rounded-lg font-semibold text-sm hover:border-teal-400 transition">
            Get This Guide
          </button>
        </div>
      </form>

      <!-- 5 Guide Pack -->
      <form action="/checkout" method="POST" class="text-center">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="tier" value="starter">
        <div class="bg-white rounded-xl border-2 border-teal-500 p-4 relative h-full flex flex-col shadow-md">
          <div class="absolute -top-2.5 left-1/2 -translate-x-1/2 bg-orange-500 text-white text-xs font-semibold px-3 py-0.5 rounded-full">83% off + save 40%/guide</div>
          <div class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2 mt-1">5 Guide Pack</div>
          <div class="text-2xl font-extrabold text-teal-700"><span class="text-sm line-through text-gray-300 mr-1">$89.99</span>$14.99</div>
          <div class="text-xs text-teal-600 font-medium mb-3">$3.00/guide instead of $4.99</div>
          <ul class="text-xs text-gray-500 text-left space-y-1 mb-4 flex-grow">
            <li>&#10003; <strong>5 personalized guides</strong></li>
            <li>&#10003; PDF + web version</li>
            <li>&#10003; 30+ places with ratings</li>
            <li>&#10003; Credits never expire</li>
          </ul>
          <button type="submit" class="pulse-cta w-full py-2.5 bg-gradient-to-r from-teal-600 to-teal-800 text-white rounded-lg font-semibold text-sm">
            Get 5 Guides
          </button>
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

    # Use form city if meta didn't get one — filter out listing subtitle junk
    listing_title = meta.get("title", "")
    raw_city = city or meta.get("city", "")
    if raw_city and any(w in raw_city.lower() for w in ("bed", "bath", "entire", "private", "shared", "room")):
        raw_city = ""
    city = raw_city
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
        domain=DOMAIN,
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
      <svg width="28" height="28" viewBox="0 0 32 32" fill="none"><rect width="32" height="32" rx="8" fill="white" fill-opacity="0.15"/><path d="M7 10c3-1 5.5-.5 9 1v13c-3.5-1.5-6-2-9-1V10z" fill="white" fill-opacity="0.9"/><path d="M25 10c-3-1-5.5-.5-9 1v13c3.5-1.5 6-2 9-1V10z" fill="white" fill-opacity="0.7"/><circle cx="20" cy="9" r="4" fill="#4DB6AC"/><circle cx="20" cy="8.5" r="1.5" fill="white"/><path d="M20 13l-1.5-2.5h3L20 13z" fill="#4DB6AC"/></svg>
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
        <div class="flex items-center gap-3">
          <button onclick="navigator.clipboard.writeText('{{ domain }}/download/{{ g.token }}');this.textContent='Copied!';setTimeout(()=>this.textContent='Share',1500)"
            class="text-sm text-gray-500 hover:text-teal-600 font-medium cursor-pointer bg-transparent border-0">Share</button>
          <a href="/download/{{ g.token }}" class="text-sm text-teal-600 font-semibold hover:underline">View</a>
        </div>
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
        "name": "HostGuide - Single Guide",
        "description": "One personalized neighborhood guide for your Airbnb listing",
        "amount": 499,  # $4.99 (launch, normal $9.99)
        "mode": "payment",
        "guides": 1,
    },
    "starter": {
        "name": "HostGuide - 5 Guide Pack",
        "description": "5 personalized neighborhood guides for your Airbnb listings",
        "amount": 1499,  # $14.99 (launch, normal $29.99)
        "mode": "payment",
        "guides": 5,
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
        session_kwargs = dict(
            payment_method_types=["card"],
            line_items=[{"price_data": price_data, "quantity": 1}],
            mode="payment",
            customer_email=email,
            success_url=f"{DOMAIN}{_dashboard_url(email, welcome='1')}" if tier in ("starter", "pro") else f"{DOMAIN}/generating/{token}",
            cancel_url=f"{DOMAIN}/preview/{token}",
            metadata={"order_token": token, "tier": tier},
        )
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

    # Build dashboard link for multi-guide pack users
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
      <div style="display:flex;gap:10px;align-items:center;">
        <button onclick="navigator.clipboard.writeText(window.location.href);this.textContent='Copied!';setTimeout(()=>this.textContent='Share Link',1500)"
           style="font-size:12px;padding:6px 14px;background:#e0f2f1;color:#00796b;border:1px solid #b2dfdb;
           border-radius:6px;cursor:pointer;font-weight:500;">Share Link</button>
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
            _generate_pdf(guide_path, pdf_path)
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

# ---------------------------------------------------------------------------
# SEO: City landing page data & template
# ---------------------------------------------------------------------------

CITY_SEO_DATA = {
    "geneva": {"name": "Geneva", "country": "Switzerland", "transit": "TPG trams and buses", "currency": "CHF", "tip": "Most shops close on Sundays - the Aeroport Coop is a lifesaver for late arrivals",
               "q1": "Guests ask about the free public transit pass - Geneva gives every hotel/Airbnb guest a free TPG card for their whole stay, but most hosts forget to mention it.",
               "q2": "Tap water is from the Alps and safe to drink. Tell your guests - it saves them buying bottles.",
               "q3": "Sunday closures, Lac Leman swimming spots, and CERN visit slots are the top three questions in almost every check-in message."},
    "dubai": {"name": "Dubai", "country": "UAE", "transit": "Dubai Metro and Careem/Uber", "currency": "AED", "tip": "Friday is the traditional weekend day - many attractions open later",
              "q1": "Dress codes for malls and Jumeirah Mosque catch guests off guard. A one-line note in the welcome book prevents awkward moments.",
              "q2": "Careem is usually cheaper than Uber in Dubai. Most first-time visitors only know Uber.",
              "q3": "Ramadan hours, alcohol licenses at restaurants, and the best beach club day passes come up in almost every message thread."},
    "miami": {"name": "Miami", "country": "USA", "transit": "Uber and the Metromover (free in Downtown/Brickell)", "currency": "USD", "tip": "Tipping 18-20% is expected at every sit-down restaurant",
              "q1": "The Metromover is free and most guests have no idea. Adding it to a welcome book saves them on Ubers around Downtown and Brickell.",
              "q2": "Hurricane season (June-November) questions spike after any weather alert. Having a one-page emergency section is a Superhost move.",
              "q3": "Beach parking, which Publix is closest, and the best Cuban coffee spot are the three most repeated questions."},
    "lisbon": {"name": "Lisbon", "country": "Portugal", "transit": "Metro, historic trams, and Bolt", "currency": "EUR", "tip": "Most family-run restaurants close between 3-7pm between lunch and dinner",
               "q1": "Tram 28 is famous but pickpockets are aggressive. Warning guests is kinder than recovering a stolen wallet.",
               "q2": "Bolt is cheaper and more common than Uber in Lisbon. Locals almost never use Uber.",
               "q3": "Hills, cobblestone shoes, and how late dinner actually starts are the three most repeated guest questions."},
    "barcelona": {"name": "Barcelona", "country": "Spain", "transit": "Metro, Bicing bikes, and Cabify/Uber", "currency": "EUR", "tip": "Dinner starts at 9pm - restaurants that open at 7pm are almost always tourist traps",
                  "q1": "Sagrada Familia timed tickets sell out weeks ahead. Including the booking link in the welcome book is worth 5-star gold.",
                  "q2": "The TMB 10-trip ticket (T-casual) is the single best transit buy most guests don't know about.",
                  "q3": "Siesta hours, pickpocket hotspots, and Sunday museum hours come up in nearly every guest thread."},
    "paris": {"name": "Paris", "country": "France", "transit": "Metro, RER trains, and Velib bikes", "currency": "EUR", "tip": "Most boulangeries close on Mondays - plan breakfast around it",
              "q1": "The Navigo Easy pass is cheaper than T+ tickets for any guest staying more than a day. Almost no welcome book mentions it.",
              "q2": "Paris restaurants stop serving between 2pm and 7pm. Guests arriving at 4pm and finding nothing open is the #1 complaint.",
              "q3": "Louvre late-night Wednesdays, bakery closures, and RER vs Metro confusion are the three repeated questions."},
    "london": {"name": "London", "country": "UK", "transit": "Tube, buses, Overground, and Bolt", "currency": "GBP", "tip": "Contactless payment works on all public transit - no Oyster card needed",
               "q1": "Guests buying Oyster cards is outdated - contactless bank cards work identically now. Telling them saves 7 and one trip.",
               "q2": "Pub food ordering is non-obvious: order at the bar, not the table. First-time guests get stuck waiting.",
               "q3": "The Heathrow Express vs Piccadilly line trade-off is the most repeated airport question in guest threads."},
    "new-york": {"name": "New York", "country": "USA", "transit": "Subway and Uber/Lyft", "currency": "USD", "tip": "Tipping 18-20% is expected everywhere - including takeaway counters",
                 "q1": "OMNY tap-to-pay on the subway works with any contactless card. Guests still buy MetroCards and pay 2x.",
                 "q2": "The 7-day unlimited on OMNY auto-caps at $34 - most guests don't know it exists.",
                 "q3": "Which Trader Joe's is closest, laundromat hours, and subway late-night reroutes are the three repeated questions."},
    "bali": {"name": "Bali", "country": "Indonesia", "transit": "Grab, GoJek, and private drivers", "currency": "IDR", "tip": "Negotiate taxi prices before getting in - Grab and GoJek are fixed-price and safer",
             "q1": "Bluebird Taxi is the only trusted metered brand. All others will overcharge - guests need to hear this on Day 1.",
             "q2": "ATM skimming is common in Kuta and Seminyak. Point guests at bank-branch ATMs only.",
             "q3": "Temple dress codes, the one-way Canggu traffic rules, and water safety are the top repeated questions."},
    "bangkok": {"name": "Bangkok", "country": "Thailand", "transit": "BTS Skytrain, MRT, and Grab", "currency": "THB", "tip": "Street food from busy stalls is safer than empty-looking restaurants",
                "q1": "The Rabbit Card for BTS saves guests from queuing every single ride - most welcome books miss this.",
                "q2": "Tuk-tuks quote tourist prices 4x over Grab. Telling guests the exact Grab price upfront is a 5-star move.",
                "q3": "Temple dress codes, scam-free taxi queues, and the best night market are the three most repeated questions."},
    "amsterdam": {"name": "Amsterdam", "country": "Netherlands", "transit": "Trams, metro, and OV-chipkaart (or tap-to-pay)", "currency": "EUR", "tip": "Never walk in the red bike lanes - locals will yell, tourists get hit daily",
                  "q1": "Contactless bank cards now work on all GVB transit. Guests still buy paper tickets and pay double.",
                  "q2": "Renting a bike is the #1 thing guests ask about. Pre-booking via MacBike or Black Bikes saves them 30 min of queuing.",
                  "q3": "Red light district etiquette, coffeeshop rules, and which canal cruise is least touristy come up in almost every thread."},
    "rome": {"name": "Rome", "country": "Italy", "transit": "Metro lines A/B, ATAC buses, and Free Now", "currency": "EUR", "tip": "Restaurants near major sights charge 3x - walk 5 blocks for real Roman food",
             "q1": "The Roma Pass covers transit plus 2 museums. For guests staying 2+ days it's always cheaper than individual tickets.",
             "q2": "Colosseum tickets MUST be pre-booked online now. Same-day queues are 3+ hours.",
             "q3": "Where to find coffee without the tourist markup, Vatican dress codes, and Sunday lunch hours are the top repeated questions."},
    "berlin": {"name": "Berlin", "country": "Germany", "transit": "U-Bahn, S-Bahn, trams, and FreeNow", "currency": "EUR", "tip": "Sundays are almost fully closed - grocery-shop on Saturday or head to a Spaeti",
               "q1": "The BVG app is the only transit source guests should trust - Google Maps is often wrong on weekend closures.",
               "q2": "Spaetis (corner shops) are the only thing open late on Sundays. Tell guests the nearest one - it's a Berlin rite of passage.",
               "q3": "Club entry policies, which bakery has the best pretzel, and the difference between a Doner and a Durum come up constantly."},
    "prague": {"name": "Prague", "country": "Czech Republic", "transit": "Metro, trams, and Bolt", "currency": "CZK", "tip": "Never exchange money in tourist-area kiosks - ATMs from major banks give the real rate",
               "q1": "The 24/72-hour transit pass is unbeatable value. Most guests default to single tickets and overpay 4x.",
               "q2": "Charles Bridge at sunrise (before 7am) is the only way to avoid the crush. Tell guests or they'll blame you for the crowds.",
               "q3": "Restaurant tipping (10% rounded up), which pilsner is authentic, and Old Town pickpocket zones are the top repeat questions."},
    "budapest": {"name": "Budapest", "country": "Hungary", "transit": "Metro M1-M4, trams, and Bolt", "currency": "HUF", "tip": "Use cards - Hungarian cash has awkward denominations and some shops reject 10k bills",
                 "q1": "Thermal bath etiquette (bring a swim cap for Szechenyi lap pool) trips up every first-time visitor.",
                 "q2": "Ruin bars on the Pest side are the nightlife. Szimpla is famous but Instant-Fogas is where locals go.",
                 "q3": "How to get to the airport (100E bus), Danube dinner cruise scams, and thermal bath timings are the repeat questions."},
    "porto": {"name": "Porto", "country": "Portugal", "transit": "Metro, STCP buses, and Bolt", "currency": "EUR", "tip": "The Andante card covers bus+metro+urban trains - buy one at the station, not on the bus",
              "q1": "Port wine cellar tours across the river in Gaia need booking - Sandeman and Graham's sell out by 11am.",
              "q2": "Francesinha restaurants are everywhere but Cafe Santiago and Brasao are the two locals actually rate.",
              "q3": "Sao Bento station tiles, the free Livraria Lello entry trick, and the best ocean-beach tram route are the top questions."},
    "madrid": {"name": "Madrid", "country": "Spain", "transit": "Metro, EMT buses, and Cabify", "currency": "EUR", "tip": "Lunch is the big meal (2-4pm) - menu del dia is the best value meal in Europe",
               "q1": "The 10-trip Metrobus ticket is half-price vs singles. Guests almost never know it exists.",
               "q2": "Prado Museum is free 6-8pm daily. Going earlier means paying and still waiting in line.",
               "q3": "Siesta hours, vermut tradition, and which churros-chocolate spot isn't a tourist trap are the repeat questions."},
    "vienna": {"name": "Vienna", "country": "Austria", "transit": "U-Bahn, trams, and Bolt", "currency": "EUR", "tip": "The coffee house culture is sitting, not takeaway - budget an hour, not five minutes",
               "q1": "The 24/48/72-hour transit pass covers all zones and is always cheaper than singles for multi-day stays.",
               "q2": "Schnitzel in touristy spots is frozen pork. Send guests to Figlmueller or Gasthaus Poschl for the real thing.",
               "q3": "Opera standing-room tickets, Naschmarkt scams, and which Kaffeehaus has the best Sachertorte are the top questions."},
    "istanbul": {"name": "Istanbul", "country": "Turkey", "transit": "Metro, trams, ferries, and BiTaksi", "currency": "TRY", "tip": "Istanbulkart covers every transit type including ferries - pick one up at any metro entrance",
                 "q1": "Hagia Sophia is now a mosque so no timed tickets but closed during prayer times. Guests arriving midday often lose an hour.",
                 "q2": "BiTaksi replaces Uber here. Standard taxis still quote 3x prices off the meter at tourist spots.",
                 "q3": "Bosphorus ferry vs tourist cruise, bazaar haggling, and Asian-side neighborhoods are the top repeat questions."},
    "marrakech": {"name": "Marrakech", "country": "Morocco", "transit": "Petit taxis (metered) and InDrive", "currency": "MAD", "tip": "Petit taxis MUST use the meter - insist or walk away, otherwise expect a 3x tourist fare",
                  "q1": "The Jemaa el-Fnaa square at sunset is the one must-do experience most guests don't realize peaks at 7pm.",
                  "q2": "Riad vs hotel is the #1 first-time visitor confusion. If your place is a riad, explain the no-shoes rule upfront.",
                  "q3": "Haggling in the souks, Atlas day-trip logistics, and which hammam isn't tourist-only are the most repeated questions."},
    "tokyo": {"name": "Tokyo", "country": "Japan", "transit": "JR lines, Tokyo Metro, Toei, and Suica IC card", "currency": "JPY", "tip": "Cash is still king outside convenience stores - always keep 10,000 yen in small bills",
              "q1": "Suica on Apple Wallet works on every train, bus, and vending machine. Guests still buy paper tickets and pay 2x.",
              "q2": "Tipping is an insult in Japan. Telling guests upfront prevents awkward restaurant moments.",
              "q3": "Convenience store meals, bathhouse tattoo rules, and how to ride a shinkansen for cheap are the top repeat questions."},
    "singapore": {"name": "Singapore", "country": "Singapore", "transit": "MRT, buses, and Grab", "currency": "SGD", "tip": "Durian is banned on public transit - the fine is S$500 and locals take it seriously",
                  "q1": "Contactless bank cards replace the EZ-Link card now on all MRT/buses. Guests still buy plastic cards.",
                  "q2": "Hawker centers (Maxwell, Lau Pa Sat) are where locals eat - Michelin-rated stalls cost S$5.",
                  "q3": "Chewing gum laws, Sentosa day-pass pricing, and which hawker center is open late are the top repeat questions."},
    "mexico-city": {"name": "Mexico City", "country": "Mexico", "transit": "Metro, Metrobus, and Uber/DiDi", "currency": "MXN", "tip": "Altitude is real - drink water the first 24 hours and skip alcohol day 1",
                    "q1": "Uber is dramatically cheaper and safer than street taxis. Tell guests never to flag one down.",
                    "q2": "Tap water is NOT safe to drink - mention it on page 1 or risk a bad stomach review.",
                    "q3": "Which neighborhoods to avoid at night, tacos al pastor spots, and Teotihuacan logistics are the top repeat questions."},
    "rio": {"name": "Rio de Janeiro", "country": "Brazil", "transit": "Metro, buses, and Uber/99", "currency": "BRL", "tip": "Never walk with a visible phone or camera after dark - locals know this instinctively",
            "q1": "99 app is often cheaper than Uber in Rio. Most first-time visitors only know Uber.",
            "q2": "Beaches have 'postos' (numbered posts) - telling guests which posto is closest saves 20 min of wandering.",
            "q3": "Safe neighborhoods, Christ the Redeemer timing, and which favela tours are ethical are the top repeat questions."},
    "toronto": {"name": "Toronto", "country": "Canada", "transit": "TTC subway, streetcars, and Uber", "currency": "CAD", "tip": "Tipping 18-20% is expected everywhere - Canadian tipping is closer to US than European norms",
                "q1": "Presto card is still worth it for multi-day stays - saves $1 per ride vs cash fares.",
                "q2": "Pearson UP Express train to downtown is $12.35 and 25 min - cheaper than Uber and always faster.",
                "q3": "Which CN Tower experience is worth it, Kensington Market timing, and St Lawrence Market Saturday tips are the repeat questions."},
}

CITY_GUIDE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Airbnb Guest Guide for {name}, {country} - HostGuide</title>
<meta name="description" content="Create a beautiful digital guest guide for your Airbnb in {name}, {country}. Walking distances, {transit}, top cafes, local tips your guests actually need on Day 1.">
<meta name="robots" content="index, follow">
<meta property="og:title" content="Airbnb Guest Guide for {name} - HostGuide">
<meta property="og:description" content="Everything your guests need to know about staying in {name}. Transit, currency, local tips and more.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://www.host-guide.net/guides/{slug}">
<meta property="og:image" content="https://www.host-guide.net/og/city/{slug}.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Airbnb Guest Guide for {name} - HostGuide">
<meta name="twitter:description" content="Auto-generate a {name} guest guide from your Airbnb listing in 60 seconds.">
<meta name="twitter:image" content="https://www.host-guide.net/og/city/{slug}.png">
<link rel="canonical" href="https://www.host-guide.net/guides/{slug}">
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>body{{font-family:'Inter',sans-serif}}</style>
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"TouristDestination","name":"{name}","description":"Airbnb guest guide for {name}, {country}","url":"https://www.host-guide.net/guides/{slug}"}}
</script>
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[
  {{"@type":"ListItem","position":1,"name":"Home","item":"https://www.host-guide.net/"}},
  {{"@type":"ListItem","position":2,"name":"Guides","item":"https://www.host-guide.net/"}},
  {{"@type":"ListItem","position":3,"name":"{name}","item":"https://www.host-guide.net/guides/{slug}"}}
]}}
</script>
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[
  {{"@type":"Question","name":"What transit should I mention for {name} guests?","acceptedAnswer":{{"@type":"Answer","text":"The main options are {transit}. {q1}"}}}},
  {{"@type":"Question","name":"What is the top local tip for hosting in {name}?","acceptedAnswer":{{"@type":"Answer","text":"{tip}. {q2}"}}}},
  {{"@type":"Question","name":"What do guests most often ask about {name}?","acceptedAnswer":{{"@type":"Answer","text":"{q3}"}}}}
]}}
</script>
</head>
<body class="bg-gray-50 text-gray-800">
<nav class="bg-white shadow-sm py-4 px-6 flex justify-between items-center">
  <a href="/" class="text-xl font-bold" style="color:#00897B;">HostGuide</a>
  <div class="text-sm space-x-4">
    <a href="/blog" style="color:#00897B;">Blog</a>
    <a href="/#pricing" class="font-semibold" style="color:#00897B;">Pricing</a>
  </div>
</nav>
<nav class="bg-white border-b text-xs text-gray-500 px-6 py-2">
  <a href="/" style="color:#00897B;">Home</a> &rsaquo; <a href="/" style="color:#00897B;">Guides</a> &rsaquo; <span>{name}</span>
</nav>
<section class="text-center py-20 px-4" style="background:linear-gradient(135deg,#00897B,#00695C);">
  <h1 class="text-4xl md:text-5xl font-bold text-white mb-4">Airbnb Guest Guide for {name}</h1>
  <p class="text-lg text-teal-100 max-w-2xl mx-auto">Your guests in {name} deserve a local's guide, not a generic PDF.</p>
</section>
<section class="max-w-3xl mx-auto py-16 px-6">
  <h2 class="text-2xl font-bold mb-4">What your {name} guide includes</h2>
  <p class="mb-4 leading-relaxed">A HostGuide for {name} gives your guests everything they need from the moment they land. It covers getting around with <strong>{transit}</strong>, paying in <strong>{currency}</strong>, and the kind of local knowledge that turns a good trip into a great one.</p>
  <p class="mb-4 leading-relaxed">Your guide is auto-generated from your Airbnb listing and enriched with local data specific to {name}, {country}. It includes check-in instructions, neighbourhood highlights, restaurant recommendations, emergency contacts, and house rules, all in a beautiful mobile-friendly page.</p>

  <h2 class="text-2xl font-bold mb-4 mt-10">Transit and getting around in {name}</h2>
  <p class="mb-4 leading-relaxed">{q1}</p>

  <h2 class="text-2xl font-bold mb-4 mt-10">Local tip most hosts in {name} miss</h2>
  <p class="mb-4 leading-relaxed"><strong>{tip}.</strong> {q2}</p>

  <h2 class="text-2xl font-bold mb-4 mt-10">The three questions your {name} guests will ask</h2>
  <p class="mb-4 leading-relaxed">{q3}</p>
  <p class="mb-4 leading-relaxed">These are the questions that drop into your inbox at 9pm when you were hoping for a quiet evening. A one-page welcome section answers them once instead of fifty times.</p>

  <h2 class="text-2xl font-bold mb-4 mt-10">Related reading for {name} hosts</h2>
  <ul class="list-disc pl-6 leading-relaxed mb-6">
    <li><a href="/blog/welcome-book-guests-read" style="color:#00897B;">How to write an Airbnb welcome book your guests will actually read</a></li>
    <li><a href="/blog/five-questions-every-guest-asks" style="color:#00897B;">The 5 questions every Airbnb guest asks (and how to answer them once)</a></li>
    <li><a href="/blog/superhost-welcome-book-upgrades" style="color:#00897B;">Superhost upgrades: how I moved my rating from 4.6 to 4.9</a></li>
  </ul>

  <p class="mb-4 leading-relaxed text-sm text-gray-500">Want to learn more about hosting in {name}? Check out <a href="https://www.airbnb.com/s/{name}/homes" target="_blank" rel="noopener" style="color:#00897B;">Airbnb listings in {name}</a> and Airbnb's <a href="https://www.airbnb.com/resources/hosting-homes" target="_blank" rel="noopener" style="color:#00897B;">hosting resources</a> for tips on becoming a <a href="https://www.airbnb.com/d/superhost" target="_blank" rel="noopener" style="color:#00897B;">Superhost</a>.</p>
  <div class="mt-10 text-center">
    <a href="/#guideForm" class="inline-block px-8 py-4 text-white font-semibold rounded-lg text-lg" style="background:#00897B;">Generate Your {name} Guide Now</a>
  </div>
</section>
<footer class="text-center py-8 text-sm text-gray-500 border-t">
  <div class="flex flex-wrap justify-center gap-3 mb-4 text-xs text-gray-400">
    <a href="/guides/geneva" style="color:#999;">Geneva</a>
    <a href="/guides/dubai" style="color:#999;">Dubai</a>
    <a href="/guides/miami" style="color:#999;">Miami</a>
    <a href="/guides/lisbon" style="color:#999;">Lisbon</a>
    <a href="/guides/barcelona" style="color:#999;">Barcelona</a>
    <a href="/guides/paris" style="color:#999;">Paris</a>
    <a href="/guides/london" style="color:#999;">London</a>
    <a href="/guides/new-york" style="color:#999;">New York</a>
    <a href="/guides/bali" style="color:#999;">Bali</a>
    <a href="/guides/bangkok" style="color:#999;">Bangkok</a>
  </div>
  <p>&copy; 2026 HostGuide - <a href="mailto:hello@host-guide.net" style="color:#00897B;">hello@host-guide.net</a></p>
</footer>
</body>
</html>"""

BLOG_ARTICLES = {
    "welcome-book-guests-read": {
        "title": "How to write an Airbnb welcome book your guests will actually read",
        "description": "A short guide to building an Airbnb welcome book that guests open on Day 1 instead of ignoring. Real structure, real examples, no fluff.",
        "date": "2026-04-10",
        "body": """<p class="mb-4 leading-relaxed">Most Airbnb welcome books don't get read. Hosts spend three hours on a 12-page Canva PDF, and guests skim the first page and never open it again. After rebuilding mine three times, here's what finally worked.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Rule 1: one page per category, max</h2>
<p class="mb-4 leading-relaxed">Guests don't read long welcome books because they're on vacation, not studying. If your transit section is two pages long, nobody is getting past the first paragraph. Cut it to the three closest stops, with walking times, and stop there.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Rule 2: walking times, not addresses</h2>
<p class="mb-4 leading-relaxed">"Boulevard Helvetique 42" means nothing to a guest who just landed. "7 minute walk, turn right out the building, the bakery is on the corner" means everything. Every single recommendation in your welcome book should have a walking time on it.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Rule 3: answer the five questions that always come up</h2>
<p class="mb-4 leading-relaxed">Every guest asks the same things: where's the nearest grocery store, best coffee nearby, how do I get to the main attraction, is tap water safe, do I tip taxis. Answer them once, in the welcome book, and watch your inbox go quiet.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Rule 4: the "what to do if X" section is the most re-read page</h2>
<p class="mb-4 leading-relaxed">Wifi down, heating cold, washing machine locked, lockbox stuck, trash day. Write one paragraph per problem. Guests keep this page open the whole stay. It's the single biggest review-saving upgrade you can make.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Rule 5: refresh it every 90 days</h2>
<p class="mb-4 leading-relaxed">Restaurants close, ride apps change, metro ticket prices go up. An outdated welcome book is worse than no welcome book, because it signals "the host doesn't care." Set a calendar reminder. Better yet, use a tool that refreshes the data for you automatically.</p>
<p class="mb-4 leading-relaxed">If you want to skip the Canva rebuild entirely, <a href="/" style="color:#00897B;">HostGuide</a> generates a welcome book from your Airbnb listing URL in 60 seconds and keeps the data fresh for you.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Related reading</h2>
<ul class="list-disc pl-6 leading-relaxed">
  <li><a href="/blog/five-questions-every-guest-asks" style="color:#00897B;">The 5 questions every Airbnb guest asks</a></li>
  <li><a href="/blog/superhost-welcome-book-upgrades" style="color:#00897B;">Superhost upgrades: moving from 4.6 to 4.9</a></li>
</ul>""",
    },
    "five-questions-every-guest-asks": {
        "title": "The 5 questions every Airbnb guest asks (and how to answer them once)",
        "description": "The five questions every Airbnb guest asks by message, and the one-page welcome book section that answers all of them.",
        "date": "2026-04-08",
        "body": """<p class="mb-4 leading-relaxed">If you've hosted more than ten bookings, you've gotten the same five messages on repeat. Here's the full list, and the one welcome-book section that kills them forever.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">1. "Where's the nearest grocery store?"</h2>
<p class="mb-4 leading-relaxed">Name it, walking time, and hours. If it's closed on Sundays, say so in the same line. Bonus: mention what it's good for (cheap breakfast stuff, late-night wine).</p>
<h2 class="text-2xl font-bold mb-4 mt-8">2. "Best coffee/breakfast nearby?"</h2>
<p class="mb-4 leading-relaxed">Pick three. Not fifteen. Guests freeze when you give them twenty options - decision fatigue is real. Three walking times, three opening times, one sentence each on what makes it good.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">3. "How do I get to [main attraction]?"</h2>
<p class="mb-4 leading-relaxed">Which line, how many stops, which ticket to buy, and walking time from the stop. If it's a famous attraction, include the booking link too - guests always forget to pre-book.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">4. "Is tap water safe?"</h2>
<p class="mb-4 leading-relaxed">Yes/no answer in bold. Three words. In most of Europe, saving guests the cost of bottled water is a 5-star move. In places where it's not safe, saying so clearly on Day 1 avoids a stomach bug and a 3-star review.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">5. "Do I tip taxis / restaurants?"</h2>
<p class="mb-4 leading-relaxed">Every country is different. Guests have no idea and they're genuinely anxious about it. Two lines: "Taxis: X percent. Restaurants: Y percent. Service charge included: yes/no." Done.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">The one-page section</h2>
<p class="mb-4 leading-relaxed">Put these five answers on page 1 of your welcome book, before anything else. Not a "welcome to our home" paragraph. Not an "about your host" section. These five answers, in this order. Your message volume will drop by 70% in a month.</p>
<p class="mb-4 leading-relaxed"><a href="/" style="color:#00897B;">HostGuide</a> auto-generates exactly this page from any Airbnb URL.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Related reading</h2>
<ul class="list-disc pl-6 leading-relaxed">
  <li><a href="/blog/welcome-book-guests-read" style="color:#00897B;">How to write an Airbnb welcome book your guests will actually read</a></li>
  <li><a href="/blog/superhost-welcome-book-upgrades" style="color:#00897B;">Superhost upgrades: moving from 4.6 to 4.9</a></li>
</ul>""",
    },
    "superhost-welcome-book-upgrades": {
        "title": "Superhost upgrades: how I moved my Airbnb rating from 4.6 to 4.9",
        "description": "The exact welcome-book upgrades that moved my Airbnb rating from 4.6 to 4.9 in one quarter. Hour by hour.",
        "date": "2026-04-05",
        "body": """<p class="mb-4 leading-relaxed">My rating was stuck at 4.6 for six months. Not bad, but not Superhost. I audited my last 30 reviews, found the pattern, and fixed five things in one weekend. The rating moved to 4.9 in the next quarter.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">1. I cut the welcome book from 12 pages to 4</h2>
<p class="mb-4 leading-relaxed">Every review that mentioned the welcome book called it "overwhelming" or "too much." I cut the city history, the 20-restaurant list, and the "about your hosts" paragraph. Kept: top 3 cafes, top 3 restaurants, top 3 groceries, each with walking time. That's it. Reviews immediately started mentioning the guide was "clear" and "useful."</p>
<h2 class="text-2xl font-bold mb-4 mt-8">2. I added a "what to do if X" emergency page</h2>
<p class="mb-4 leading-relaxed">Wifi password, router reboot instructions, lockbox jam procedure, heating pilot light, washing machine unlock, trash day. One page. Every single star-losing review in my history was a panicked guest who couldn't reach me at 10pm. This page ended that pattern.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">3. I added walking times to everything</h2>
<p class="mb-4 leading-relaxed">Addresses don't help guests. Walking times do. "7 min walk" is immediately mappable in a guest's head. Doing this across every recommendation took 45 minutes and showed up in the next three reviews by name.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">4. I put the local emergency numbers on the fridge</h2>
<p class="mb-4 leading-relaxed">Not 911. The actual country numbers. Police, ambulance, fire, non-emergency medical. Printed on a small card, taped to the inside of the kitchen cabinet. Took 10 minutes. A guest later mentioned this saved them when they had a late-night allergic reaction.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">5. I added a QR code that opens the digital guide</h2>
<p class="mb-4 leading-relaxed">Printed welcome books get left at home. A QR code on the cover opens the same content on the guest's phone. Half my guests scan it within the first hour. That single change meant guests had the walking directions with them on Day 3 at a cafe, not just on Day 1 at the apartment.</p>
<p class="mb-4 leading-relaxed">The total time investment across all five upgrades was one weekend. The rating moved from 4.6 to 4.9 in the quarter after. Superhost badge landed the same month.</p>
<p class="mb-4 leading-relaxed">If you want all five of these in a single PDF without the weekend of Canva work, <a href="/" style="color:#00897B;">HostGuide</a> generates it from your listing URL automatically.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Related reading</h2>
<ul class="list-disc pl-6 leading-relaxed">
  <li><a href="/blog/welcome-book-guests-read" style="color:#00897B;">How to write an Airbnb welcome book your guests will actually read</a></li>
  <li><a href="/blog/five-questions-every-guest-asks" style="color:#00897B;">The 5 questions every Airbnb guest asks</a></li>
</ul>""",
    },
    "handling-late-check-ins": {
        "title": "How to handle late Airbnb check-ins without losing sleep",
        "description": "A practical playbook for late check-ins: smart locks, QR-code access, and the one welcome-book page that prevents 90% of 2am messages.",
        "date": "2026-04-15",
        "body": """<p class="mb-4 leading-relaxed">Late check-ins are the single biggest source of "my wifi won't work" messages at 1am. Here's how I stopped getting them.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">1. Install a smart lock or a reliable lockbox</h2>
<p class="mb-4 leading-relaxed">Keys are the #1 source of check-in chaos. A basic smart lock (Yale, August, Nuki) lets you generate one-time codes per guest. Cheaper option: a Master Lock 5401D lockbox mounted outside. Either way, the guest never needs you present.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">2. Send check-in instructions 24 hours ahead</h2>
<p class="mb-4 leading-relaxed">Photo of the door. Photo of the lockbox. Step-by-step with numbers ("1. Approach the green door", "2. Enter code 4421 on keypad"). Most confusion happens because guests are tired and the instructions came too late. 24 hours gives them time to read.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">3. Put the wifi password in a place they'll find drunk</h2>
<p class="mb-4 leading-relaxed">On a card next to the front door. Printed big. Also in the welcome book, also stuck to the fridge. Redundancy prevents the 2am "what's the wifi?" message.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">4. Leave snacks + bottled water</h2>
<p class="mb-4 leading-relaxed">A red-eye flight guest arriving at 1am wants water and something salty. Total cost: $4. Review mentions: "so thoughtful". Best ROI on any hosting upgrade.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">5. Pre-write a "you're in" message</h2>
<p class="mb-4 leading-relaxed">Ask them to send one word ("in") when they're settled. Prevents you from lying awake wondering. Also: if they DON'T send it by 30 minutes after their expected time, you know to check in.</p>
<p class="mb-4 leading-relaxed">One welcome-book page covers 1-4. That page is the best investment you'll make. <a href="/" style="color:#00897B;">HostGuide</a> generates a late-check-in section automatically when you create your welcome book.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Related reading</h2>
<ul class="list-disc pl-6 leading-relaxed">
  <li><a href="/blog/welcome-book-guests-read" style="color:#00897B;">How to write an Airbnb welcome book your guests will actually read</a></li>
  <li><a href="/blog/house-rules-that-work" style="color:#00897B;">House rules that guests actually follow</a></li>
</ul>""",
    },
    "pricing-new-airbnb-listing": {
        "title": "How to price a new Airbnb listing without leaving money on the table",
        "description": "A pragmatic pricing strategy for new Airbnb listings: the first 10 bookings matter more than your nightly rate. Here's how to actually set it.",
        "date": "2026-04-17",
        "body": """<p class="mb-4 leading-relaxed">Every new host agonizes over the nightly rate. It's the wrong thing to agonize over. Here's what actually moves the needle in your first 90 days.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">1. Start 15-20% below the market rate</h2>
<p class="mb-4 leading-relaxed">Your first 10 reviews are worth more than any single booking revenue. Airbnb's algorithm amplifies listings with 8+ reviews hard. Lose $30/night on the first 10 bookings, gain $500+/week in visibility forever.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">2. Use Airbnb's Smart Pricing as a ceiling, not a floor</h2>
<p class="mb-4 leading-relaxed">Smart Pricing is aggressive on the upside and lazy on the downside. I use it to cap prices during peak events (F1, summits) but set my own floor. Never trust it to lower prices for slow weeks - it won't go low enough.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">3. The cleaning fee matters more than the nightly rate</h2>
<p class="mb-4 leading-relaxed">A $80 cleaning fee on a 2-night stay is a 40% add-on. On a 7-night stay it's 11%. Short-stay guests see the total and bounce. Either bake cleaning into the nightly rate or set a 3-night minimum.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">4. Compare against listings, not recommendations</h2>
<p class="mb-4 leading-relaxed">Open Airbnb in an incognito window, search your own neighborhood for your exact dates. See what the 5-star listings charge. Ignore the "market rate" dashboard - it includes empty weekends and bad listings.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">5. Raise prices after 8 reviews, not before</h2>
<p class="mb-4 leading-relaxed">Once you have 8+ reviews at 4.8+, Airbnb surfaces you to premium search traffic. That's when you move to market rate, then 10% above. Going early kills your booking velocity; going late leaves real money on the table.</p>
<p class="mb-4 leading-relaxed">Pricing is a review-count problem disguised as a revenue problem. Solve the first and the second takes care of itself. While you're waiting for those first 10 reviews, make sure the welcome book actually helps guests - that's where 5-star ratings come from. <a href="/" style="color:#00897B;">HostGuide</a> generates one from your listing URL.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Related reading</h2>
<ul class="list-disc pl-6 leading-relaxed">
  <li><a href="/blog/superhost-welcome-book-upgrades" style="color:#00897B;">Superhost upgrades: moving from 4.6 to 4.9</a></li>
  <li><a href="/blog/welcome-book-guests-read" style="color:#00897B;">How to write an Airbnb welcome book your guests will actually read</a></li>
</ul>""",
    },
    "house-rules-that-work": {
        "title": "House rules that Airbnb guests actually follow",
        "description": "The difference between house rules guests ignore and rules they follow is tone, placement, and brevity. A field guide from a Superhost.",
        "date": "2026-04-19",
        "body": """<p class="mb-4 leading-relaxed">A wall of 20 house rules is a wall guests skip. Five specific rules, phrased right, placed right, get followed. Here's what I learned in three years of tuning mine.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">1. Put the 3 most important rules at the top of the welcome book</h2>
<p class="mb-4 leading-relaxed">Not the listing. The welcome book. Guests accept listing rules without reading them. They actually read the welcome book on arrival. Mine are: "shoes off inside", "no parties (we live next door)", and "trash day is Thursday, the bag goes out Wednesday night".</p>
<h2 class="text-2xl font-bold mb-4 mt-8">2. Explain the "why" in one line</h2>
<p class="mb-4 leading-relaxed">"No parties" is a rule guests ignore. "No parties - we share a wall with a 3-month-old baby next door" is a rule guests respect. The why does the enforcement for you.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">3. Write rules as the guest, not as the host</h2>
<p class="mb-4 leading-relaxed">"NO SMOKING" reads like a sign at a gas station. "We kept this place smoke-free since 2021 - please help us keep it that way outside on the balcony" reads like a conversation. Same rule, different compliance.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">4. Quiet hours work. 10 rules about noise don't.</h2>
<p class="mb-4 leading-relaxed">One specific quiet-hours rule (10pm-8am) is enforceable, memorable, and something neighbors can point to. "Be respectful to neighbors" is noise no guest processes.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">5. Trash and check-out rules go on the fridge, not the welcome book</h2>
<p class="mb-4 leading-relaxed">Guests forget the welcome book on check-out day. A small card on the fridge ("Please empty the dishwasher, put trash in the hallway bin, and leave the keys on the counter") gets followed 95% of the time. This single change moved my "cleanliness" review score up 0.2 points.</p>
<p class="mb-4 leading-relaxed">Fewer rules, placed right, phrased human. That's the whole game. <a href="/" style="color:#00897B;">HostGuide</a> includes a house-rules block that follows this exact structure.</p>
<h2 class="text-2xl font-bold mb-4 mt-8">Related reading</h2>
<ul class="list-disc pl-6 leading-relaxed">
  <li><a href="/blog/handling-late-check-ins" style="color:#00897B;">How to handle late check-ins without losing sleep</a></li>
  <li><a href="/blog/welcome-book-guests-read" style="color:#00897B;">How to write an Airbnb welcome book your guests will actually read</a></li>
</ul>""",
    },
}

BLOG_INDEX_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HostGuide Blog - Airbnb hosting tips for better welcome books</title>
<meta name="description" content="Practical Airbnb hosting tips: how to write a welcome book guests read, the 5 questions every guest asks, and Superhost upgrades that move your rating.">
<meta name="robots" content="index, follow">
<link rel="canonical" href="https://www.host-guide.net/blog">
<meta property="og:title" content="HostGuide Blog - Airbnb hosting tips">
<meta property="og:description" content="Practical Airbnb hosting tips: welcome books, guest questions, Superhost upgrades.">
<meta property="og:url" content="https://www.host-guide.net/blog">
<meta property="og:type" content="website">
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>body{font-family:'Inter',sans-serif}</style>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Blog","name":"HostGuide Blog","url":"https://www.host-guide.net/blog"}
</script>
</head>
<body class="bg-gray-50 text-gray-800">
<nav class="bg-white shadow-sm py-4 px-6 flex justify-between items-center">
  <a href="/" class="text-xl font-bold" style="color:#00897B;">HostGuide</a>
  <a href="/#pricing" class="text-sm font-semibold" style="color:#00897B;">Pricing</a>
</nav>
<section class="text-center py-16 px-4" style="background:linear-gradient(135deg,#00897B,#00695C);">
  <h1 class="text-4xl md:text-5xl font-bold text-white mb-3">HostGuide Blog</h1>
  <p class="text-lg text-teal-100">Airbnb hosting tips that move reviews from 4.6 to 4.9.</p>
</section>
<section class="max-w-3xl mx-auto py-16 px-6 space-y-8">
  <article class="border-b pb-8">
    <h2 class="text-2xl font-bold mb-2"><a href="/blog/house-rules-that-work" style="color:#00897B;">House rules that Airbnb guests actually follow</a></h2>
    <p class="text-sm text-gray-500 mb-2">2026-04-19</p>
    <p class="leading-relaxed">The difference between house rules guests ignore and rules they follow is tone, placement, and brevity. A field guide.</p>
  </article>
  <article class="border-b pb-8">
    <h2 class="text-2xl font-bold mb-2"><a href="/blog/pricing-new-airbnb-listing" style="color:#00897B;">How to price a new Airbnb listing without leaving money on the table</a></h2>
    <p class="text-sm text-gray-500 mb-2">2026-04-17</p>
    <p class="leading-relaxed">The first 10 bookings matter more than your nightly rate. Here's how to actually set pricing for a new listing.</p>
  </article>
  <article class="border-b pb-8">
    <h2 class="text-2xl font-bold mb-2"><a href="/blog/handling-late-check-ins" style="color:#00897B;">How to handle late Airbnb check-ins without losing sleep</a></h2>
    <p class="text-sm text-gray-500 mb-2">2026-04-15</p>
    <p class="leading-relaxed">A practical playbook for late check-ins: smart locks, QR-code access, and the welcome-book page that kills 90% of 2am messages.</p>
  </article>
  <article class="border-b pb-8">
    <h2 class="text-2xl font-bold mb-2"><a href="/blog/welcome-book-guests-read" style="color:#00897B;">How to write an Airbnb welcome book your guests will actually read</a></h2>
    <p class="text-sm text-gray-500 mb-2">2026-04-10</p>
    <p class="leading-relaxed">A short guide to building a welcome book guests open on Day 1 instead of ignoring. Real structure, real examples, no fluff.</p>
  </article>
  <article class="border-b pb-8">
    <h2 class="text-2xl font-bold mb-2"><a href="/blog/five-questions-every-guest-asks" style="color:#00897B;">The 5 questions every Airbnb guest asks (and how to answer them once)</a></h2>
    <p class="text-sm text-gray-500 mb-2">2026-04-08</p>
    <p class="leading-relaxed">The five messages every host sees on repeat, and the one-page welcome book section that kills them forever.</p>
  </article>
  <article class="border-b pb-8">
    <h2 class="text-2xl font-bold mb-2"><a href="/blog/superhost-welcome-book-upgrades" style="color:#00897B;">Superhost upgrades: how I moved my rating from 4.6 to 4.9</a></h2>
    <p class="text-sm text-gray-500 mb-2">2026-04-05</p>
    <p class="leading-relaxed">The exact welcome-book upgrades that moved my rating from 4.6 to 4.9 in one quarter. Hour by hour.</p>
  </article>
</section>
<footer class="text-center py-8 text-sm text-gray-500 border-t">
  <p>&copy; 2026 HostGuide - <a href="mailto:hello@host-guide.net" style="color:#00897B;">hello@host-guide.net</a></p>
</footer>
</body>
</html>"""

BLOG_ARTICLE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} - HostGuide</title>
<meta name="description" content="{description}">
<meta name="robots" content="index, follow">
<link rel="canonical" href="https://www.host-guide.net/blog/{slug}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="article">
<meta property="og:url" content="https://www.host-guide.net/blog/{slug}">
<meta property="og:image" content="https://www.host-guide.net/og/blog/{slug}.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{description}">
<meta name="twitter:image" content="https://www.host-guide.net/og/blog/{slug}.png">
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>body{{font-family:'Inter',sans-serif}}</style>
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"BlogPosting","headline":"{title}","description":"{description}","datePublished":"{date}","author":{{"@type":"Person","name":"Umur Tuner"}},"publisher":{{"@type":"Organization","name":"HostGuide","url":"https://www.host-guide.net/"}},"mainEntityOfPage":"https://www.host-guide.net/blog/{slug}"}}
</script>
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[
  {{"@type":"ListItem","position":1,"name":"Home","item":"https://www.host-guide.net/"}},
  {{"@type":"ListItem","position":2,"name":"Blog","item":"https://www.host-guide.net/blog"}},
  {{"@type":"ListItem","position":3,"name":"{title}","item":"https://www.host-guide.net/blog/{slug}"}}
]}}
</script>
</head>
<body class="bg-gray-50 text-gray-800">
<nav class="bg-white shadow-sm py-4 px-6 flex justify-between items-center">
  <a href="/" class="text-xl font-bold" style="color:#00897B;">HostGuide</a>
  <div class="text-sm space-x-4">
    <a href="/blog" style="color:#00897B;">Blog</a>
    <a href="/#pricing" class="font-semibold" style="color:#00897B;">Pricing</a>
  </div>
</nav>
<nav class="bg-white border-b text-xs text-gray-500 px-6 py-2">
  <a href="/" style="color:#00897B;">Home</a> &rsaquo; <a href="/blog" style="color:#00897B;">Blog</a> &rsaquo; <span>{title}</span>
</nav>
<article class="max-w-3xl mx-auto py-16 px-6">
  <h1 class="text-3xl md:text-4xl font-bold mb-3">{title}</h1>
  <p class="text-sm text-gray-500 mb-8">Published {date} by Umur Tuner</p>
  {body}
  <div class="mt-12 text-center">
    <a href="/#guideForm" class="inline-block px-8 py-4 text-white font-semibold rounded-lg text-lg" style="background:#00897B;">Generate a guide for your listing</a>
  </div>
</article>
<footer class="text-center py-8 text-sm text-gray-500 border-t">
  <p>&copy; 2026 HostGuide - <a href="mailto:hello@host-guide.net" style="color:#00897B;">hello@host-guide.net</a></p>
</footer>
</body>
</html>"""


# ---------------------------------------------------------------------------
# SEO routes: robots.txt, sitemap.xml, city landing pages
# ---------------------------------------------------------------------------

@app.route("/google775205a02f12530e.html")
def google_verification():
    return send_file(BASE / "static" / "google775205a02f12530e.html")


@app.route("/robots.txt")
def robots_txt():
    txt = "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /dashboard\nDisallow: /generating/\n\nSitemap: https://www.host-guide.net/sitemap.xml"
    return app.response_class(txt, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    from datetime import date
    today = date.today().isoformat()
    urls = [f'<url><loc>https://www.host-guide.net/</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>1.0</priority></url>']
    for c in CITY_SEO_DATA.keys():
        urls.append(f'<url><loc>https://www.host-guide.net/guides/{c}</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.8</priority></url>')
    urls.append(f'<url><loc>https://www.host-guide.net/blog</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>0.7</priority></url>')
    for slug, article in BLOG_ARTICLES.items():
        urls.append(f'<url><loc>https://www.host-guide.net/blog/{slug}</loc><lastmod>{article["date"]}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>')
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + '\n'.join(urls) + '\n</urlset>'
    return app.response_class(xml, mimetype="application/xml")


@app.route("/guides/<city_slug>")
def city_guide_page(city_slug):
    city = CITY_SEO_DATA.get(city_slug)
    if not city:
        return redirect("/")
    html = CITY_GUIDE_PAGE.format(
        name=city["name"],
        country=city["country"],
        transit=city["transit"],
        currency=city["currency"],
        tip=city["tip"],
        q1=city["q1"],
        q2=city["q2"],
        q3=city["q3"],
        slug=city_slug,
    )
    return app.response_class(html, mimetype="text/html")


@app.route("/blog")
def blog_index():
    return app.response_class(BLOG_INDEX_PAGE, mimetype="text/html")


@app.route("/blog/<slug>")
def blog_article(slug):
    article = BLOG_ARTICLES.get(slug)
    if not article:
        return redirect("/blog")
    html = BLOG_ARTICLE_PAGE.format(
        title=article["title"],
        description=article["description"],
        slug=slug,
        date=article["date"],
        body=article["body"],
    )
    return app.response_class(html, mimetype="text/html")


def _render_og_png(title: str, subtitle: str) -> bytes:
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1200, 630
    dark_teal = (0, 77, 64)
    med_teal = (0, 137, 123)
    accent = (77, 182, 172)
    img = Image.new("RGB", (W, H), dark_teal)
    draw = ImageDraw.Draw(img)
    for x in range(W):
        t = x / W
        r = int(dark_teal[0] + (med_teal[0] - dark_teal[0]) * t)
        g = int(dark_teal[1] + (med_teal[1] - dark_teal[1]) * t)
        b = int(dark_teal[2] + (med_teal[2] - dark_teal[2]) * t)
        draw.line([(x, 0), (x, H)], fill=(r, g, b))
    draw.rectangle([(0, 0), (W, 4)], fill=accent)
    try:
        font_brand = ImageFont.load_default(size=32)
        font_title = ImageFont.load_default(size=56)
        font_sub = ImageFont.load_default(size=26)
        font_url = ImageFont.load_default(size=20)
    except TypeError:
        font_brand = font_title = font_sub = font_url = ImageFont.load_default()
    draw.text((70, 70), "HostGuide", fill=(255, 255, 255), font=font_brand)
    max_w = W - 140
    words = title.split()
    lines, cur = [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font_title) < max_w:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lines = lines[:3]
    y = 180
    for line in lines:
        draw.text((70, y), line, fill=(255, 255, 255), font=font_title)
        y += 72
    y += 12
    draw.line([(70, y), (370, y)], fill=(255, 255, 255, 128), width=2)
    y += 20
    sub_lines, cur = [], ""
    for w in subtitle.split():
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font_sub) < max_w:
            cur = test
        else:
            sub_lines.append(cur)
            cur = w
    if cur:
        sub_lines.append(cur)
    for line in sub_lines[:2]:
        draw.text((70, y), line, fill=(220, 240, 240), font=font_sub)
        y += 34
    url_text = "host-guide.net"
    url_w = draw.textlength(url_text, font=font_url)
    draw.text(((W - url_w) // 2, H - 45), url_text, fill=(200, 230, 230), font=font_url)
    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


_OG_CACHE: dict[str, bytes] = {}


@app.route("/og/city/<slug>.png")
def og_city(slug):
    city = CITY_SEO_DATA.get(slug.replace(".png", ""))
    if not city:
        return redirect("/static/og-image.png")
    key = f"city:{slug}"
    if key not in _OG_CACHE:
        _OG_CACHE[key] = _render_og_png(
            f"Airbnb Guide for {city['name']}",
            f"Auto-generate a welcome book for your {city['name']} listing in 60 seconds."
        )
    return app.response_class(_OG_CACHE[key], mimetype="image/png")


@app.route("/og/blog/<slug>.png")
def og_blog(slug):
    article = BLOG_ARTICLES.get(slug.replace(".png", ""))
    if not article:
        return redirect("/static/og-image.png")
    key = f"blog:{slug}"
    if key not in _OG_CACHE:
        _OG_CACHE[key] = _render_og_png(article["title"], article["description"])
    return app.response_class(_OG_CACHE[key], mimetype="image/png")


if __name__ == "__main__":
    print(f"\n  HostGuide App starting...")
    print(f"  Stripe: {'configured' if STRIPE_SECRET else 'DEV MODE (skipping payment)'}")
    print(f"  Domain: {DOMAIN}")
    print(f"  Open: http://localhost:5555\n")
    app.run(host="0.0.0.0", port=5555, debug=True)
