"""One-shot guide regeneration for QA.

Bypasses the order/credit system. Calls the same scrape → enrich → generate
pipeline as production, writes the resulting HTML + PDF to output/qa/.

Usage:
    python scripts/qa_guide.py https://www.airbnb.co.uk/rooms/29079488
    python scripts/qa_guide.py https://www.airbnb.com/rooms/<id> --no-pdf
    python scripts/qa_guide.py <url> --city "Brixton, London"  # override city
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT.parent))

from hostguide.src.app import (  # noqa: E402
    _fetch_listing_meta, _extract_listing_id, _geocode_city, _reverse_geocode,
    _get_city_config, _generate_pdf, _inject_qr_code, DOMAIN,
)
from hostguide.src.scraper import Listing  # noqa: E402
from hostguide.src.enricher import enrich_without_api  # noqa: E402
from hostguide.src.guide_generator import generate_guide  # noqa: E402


def regenerate(airbnb_url: str, city_override: str = "", make_pdf: bool = True) -> dict:
    listing_id = _extract_listing_id(airbnb_url)
    if not listing_id:
        raise SystemExit(f"Could not extract listing ID from {airbnb_url}")

    print(f"\n=== QA regeneration for listing {listing_id} ===")
    print(f"URL: {airbnb_url}")

    meta = _fetch_listing_meta(airbnb_url)
    print(f"Meta: lat={meta.get('lat')}, lng={meta.get('lng')}, "
          f"city={meta.get('city')!r}, host={meta.get('host_name')!r}, "
          f"neighborhood={meta.get('neighborhood')!r}")

    listing = Listing(
        listing_id=listing_id,
        title=meta.get("title", ""),
        url=airbnb_url,
        city=city_override or meta.get("city", ""),
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
        amenities=meta.get("amenities", []),
        photos=meta.get("photos", []),
    )

    if listing.lat == 0 and listing.lng == 0 and city_override:
        listing.lat, listing.lng = _geocode_city(city_override)
        print(f"Geocoded {city_override!r} → {listing.lat},{listing.lng}")

    if listing.lat == 0 and listing.lng == 0:
        raise SystemExit("No coordinates resolved. Pass --city to geocode manually.")

    geo = _reverse_geocode(listing.lat, listing.lng)
    junk_words = ("bed", "bath", "entire", "private", "shared", "guest")
    city_is_junk = listing.city and any(w in listing.city.lower() for w in junk_words)
    if (not listing.city or city_is_junk) and geo.get("city"):
        listing.city = geo["city"]
    if not listing.neighborhood and geo.get("neighborhood"):
        listing.neighborhood = geo["neighborhood"]
    country = geo.get("country_code", "")

    city_config = _get_city_config(listing.city or "Unknown")
    if not city_config.get("country") and country:
        city_config["country"] = country

    print(f"Resolved: {listing.city} / {listing.neighborhood} "
          f"(country={city_config.get('country')})")
    print(f"Enriching at {listing.lat},{listing.lng}...")
    enriched = enrich_without_api(listing.lat, listing.lng, city_config)

    counts = {c: len(getattr(enriched, c, []) or [])
              for c in ("transit", "grocery", "restaurant", "landmark", "nightlife", "health")}
    total = sum(counts.values())
    print(f"POI counts: {counts} (total={total})")
    if total < 8:
        print(f"[LOW_POI] guide will be sparse — total={total}")

    use_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
    print(f"Generating guide (claude={'yes' if use_claude else 'NO — template only'})...")
    guide = generate_guide(listing, enriched, city_config, use_claude=use_claude)

    out_dir = ROOT / "output" / "qa"
    out_dir.mkdir(parents=True, exist_ok=True)

    html_path = out_dir / f"{listing_id}.html"
    html_with_qr = _inject_qr_code(guide.content_html, f"{DOMAIN}/qa/{listing_id}")
    html_path.write_text(html_with_qr, encoding="utf-8")
    print(f"HTML: {html_path}")

    md_path = out_dir / f"{listing_id}.md"
    md_path.write_text(guide.content_md or "(template-only generation, no markdown produced)",
                       encoding="utf-8")
    print(f"MD:   {md_path}")

    pdf_path = None
    if make_pdf:
        pdf_path = html_path.with_suffix(".pdf")
        try:
            _generate_pdf(html_path, pdf_path)
            print(f"PDF:  {pdf_path}")
        except Exception as e:
            print(f"PDF generation failed: {e}")
            pdf_path = None

    return {
        "listing_id": listing_id,
        "city": listing.city,
        "neighborhood": listing.neighborhood,
        "country": city_config.get("country"),
        "lat": listing.lat,
        "lng": listing.lng,
        "poi_counts": counts,
        "poi_total": total,
        "use_claude": use_claude,
        "html": str(html_path),
        "md": str(md_path),
        "pdf": str(pdf_path) if pdf_path else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="Airbnb listing URL")
    parser.add_argument("--city", default="", help="Manual city override (e.g. 'Brixton, London')")
    parser.add_argument("--no-pdf", action="store_true", help="Skip PDF generation")
    args = parser.parse_args()
    result = regenerate(args.url, city_override=args.city, make_pdf=not args.no_pdf)
    print("\n=== Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
