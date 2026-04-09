#!/usr/bin/env python3
"""HostGuide CLI — scrape, enrich, generate guides, prep outreach.

Usage:
    python run.py medellin              # Full pipeline: scrape → enrich → generate
    python run.py bogota --max-pages 3  # Limit scraping pages
    python run.py medellin --skip-scrape # Use cached listings, regenerate guides
    python run.py medellin --outreach   # Also generate outreach messages

Requires:
    pip install requests pyyaml
    Optional: GOOGLE_MAPS_API_KEY (for Google Places enrichment)
    Optional: ANTHROPIC_API_KEY (for Claude-powered guide writing)
    Falls back to OpenStreetMap + templates if no API keys set.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from hostguide.src.scraper import scrape_city, save_listings, load_listings, Listing, _detect_neighborhood
from hostguide.src.enricher import (enrich_listing, enrich_without_api, enrich_with_foursquare,
                                    enrich_activities, _merge_enriched, EnrichedLocation)
from hostguide.src.guide_generator import generate_guide, save_guide
from hostguide.src.outreach import generate_dm, generate_fb_post
from hostguide.src.host_discovery import HostDiscovery


def load_city_config(city_key: str) -> dict:
    """Load city config from YAML."""
    config_path = Path(__file__).parent / "config" / "cities.yaml"
    with open(config_path) as f:
        cities = yaml.safe_load(f)
    if city_key not in cities:
        print(f"ERROR: City '{city_key}' not found. Available: {list(cities.keys())}")
        sys.exit(1)
    return cities[city_key]


def main():
    parser = argparse.ArgumentParser(description="HostGuide — Guest guides for Airbnb hosts")
    parser.add_argument("city", help="City key (e.g., medellin, bogota)")
    parser.add_argument("--max-pages", type=int, default=3, help="Max scraping pages (default: 3)")
    parser.add_argument("--max-guides", type=int, default=10, help="Max guides to generate (default: 10)")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip scraping, use cached listings")
    parser.add_argument("--outreach", action="store_true", help="Also generate outreach messages")
    parser.add_argument("--send", action="store_true", help="Actually send outreach (FB posts, emails)")
    parser.add_argument("--send-dry-run", action="store_true", help="Preview outreach without sending")
    parser.add_argument("--no-claude", action="store_true", help="Don't use Claude API for guides")
    parser.add_argument("--discover", action="store_true", help="Discover host external profiles (IG, email, website)")
    parser.add_argument("--max-discover", type=int, default=20, help="Max hosts to discover (default: 20)")
    args = parser.parse_args()

    city_config = load_city_config(args.city)
    city_name = city_config["name"]
    output_dir = Path(__file__).parent / "output" / args.city
    output_dir.mkdir(parents=True, exist_ok=True)
    listings_path = output_dir / "listings.json"

    print(f"{'='*60}")
    print(f"HOSTGUIDE — {city_name}")
    print(f"{'='*60}")

    # ── Step 1: Scrape ──
    if args.skip_scrape and listings_path.exists():
        print(f"\nLoading cached listings from {listings_path}")
        listings = load_listings(str(listings_path))
    else:
        listings = scrape_city(city_config, max_pages=args.max_pages)
        if listings:
            save_listings(listings, str(listings_path))
        else:
            print("No listings found. Check your network or try --max-pages 1")
            sys.exit(1)

    # Fill in missing neighborhoods from coordinates
    for l in listings:
        if not l.neighborhood and l.lat != 0:
            l.neighborhood = _detect_neighborhood(l.lat, l.lng, city_name)

    # Filter to listings with coordinates (needed for enrichment)
    geo_listings = [l for l in listings if l.lat != 0 and l.lng != 0]
    print(f"\n{len(geo_listings)} listings with coordinates (of {len(listings)} total)")

    if not geo_listings:
        print("No geolocated listings. Using first listings with placeholder coordinates.")
        # Use city center as fallback
        center_lat = (city_config["bounds"]["lat_min"] + city_config["bounds"]["lat_max"]) / 2
        center_lng = (city_config["bounds"]["lon_min"] + city_config["bounds"]["lon_max"]) / 2
        for l in listings[:args.max_guides]:
            l.lat = center_lat
            l.lng = center_lng
        geo_listings = listings[:args.max_guides]

    # ── Step 1.5: Host Discovery (optional) ──
    if args.discover:
        print(f"\n{'='*60}")
        print("HOST DISCOVERY")
        print(f"{'='*60}")
        discovery = HostDiscovery(headless=False)
        host_profiles = discovery.discover_all(
            str(listings_path), max_hosts=args.max_discover
        )
        if host_profiles:
            profiles_path = output_dir / "host_profiles.json"
            discovery.save_profiles(host_profiles, str(profiles_path))

            # Merge discovered data back into listings
            profile_map = {hp.host_id: hp for hp in host_profiles}
            for l in listings:
                hid = l.host_id or ""
                if hid in profile_map:
                    hp = profile_map[hid]
                    if hp.instagram and not l.host_instagram:
                        l.host_instagram = hp.instagram
                    if hp.email and not l.host_email:
                        l.host_email = hp.email
                    if hp.website and not l.host_website:
                        l.host_website = hp.website
                    if hp.facebook and not l.host_facebook:
                        l.host_facebook = hp.facebook
                    if hp.superhost:
                        l.host_superhost = True
                    if hp.response_rate and not l.host_response_rate:
                        l.host_response_rate = hp.response_rate

            # Re-save listings with enriched host data
            save_listings(listings, str(listings_path))
            print(f"  Updated listings with discovered host data")

    # ── Step 2: Enrich + Generate ──
    use_google = bool(os.environ.get("GOOGLE_MAPS_API_KEY"))
    use_foursquare = bool(os.environ.get("FOURSQUARE_API_KEY"))
    use_claude = not args.no_claude and bool(os.environ.get("ANTHROPIC_API_KEY"))

    sources = []
    if use_google: sources.append("Google Places")
    if use_foursquare: sources.append("Foursquare")
    sources.append("OpenStreetMap")
    print(f"\nEnrichment: {' + '.join(sources)}")
    print(f"Guide writing: {'Claude API' if use_claude else 'Template-based'}")

    # Fetch city-level activities once (GetYourGuide scrape)
    print(f"\nFetching activities/tours for {city_name}...")
    city_activities = enrich_activities(city_name, max_results=5)
    if city_activities:
        print(f"  Found {len(city_activities)} activities/tours")
    else:
        print(f"  No activities found (GetYourGuide may be blocked)")

    guides = []
    for i, listing in enumerate(geo_listings[:args.max_guides]):
        print(f"\n[{i+1}/{min(len(geo_listings), args.max_guides)}] "
              f"{listing.title or listing.listing_id} ({listing.neighborhood or 'unknown area'})")

        # Enrich — layer sources, merge results
        if use_google:
            enriched = enrich_listing(listing.lat, listing.lng, city_config)
        else:
            enriched = enrich_without_api(listing.lat, listing.lng, city_config)

        # Supplement with Foursquare (better restaurant/cafe coverage in LATAM)
        if use_foursquare:
            fsq = enrich_with_foursquare(listing.lat, listing.lng, city_config)
            enriched = _merge_enriched(enriched, fsq)

        # Add city-level activities to landmarks
        if city_activities:
            existing = {p.name.lower() for p in enriched.landmark}
            for act in city_activities:
                if act.name.lower() not in existing:
                    enriched.landmark.append(act)

        place_count = sum(len(getattr(enriched, cat, []))
                         for cat in ["transit", "grocery", "restaurant", "landmark", "nightlife", "health"])
        print(f"  Found {place_count} nearby places")

        # Generate guide
        guide = generate_guide(listing, enriched, city_config, use_claude=use_claude)
        md_path, html_path = save_guide(guide, str(output_dir / "guides"))
        guides.append((listing, guide))

    # ── Step 3: Outreach (optional) ──
    if args.outreach and guides:
        print(f"\n{'='*60}")
        print("OUTREACH TEMPLATES")
        print(f"{'='*60}")

        # FB post
        fb_post = generate_fb_post(city_name, sample_count=min(5, len(guides)))
        fb_path = output_dir / "fb_post.txt"
        fb_path.write_text(fb_post)
        print(f"\nFB group post saved to: {fb_path}")
        print(f"Target groups: {', '.join(city_config.get('fb_groups', []))}")

        # DMs
        dm_dir = output_dir / "dms"
        dm_dir.mkdir(exist_ok=True)
        for listing, guide in guides[:5]:
            dm = generate_dm(listing, guide)
            dm_path = dm_dir / f"dm_{listing.listing_id}.txt"
            dm_path.write_text(dm)
        print(f"DM templates saved to: {dm_dir}/ ({min(5, len(guides))} messages)")

    # ── Step 4: Send outreach (optional) ──
    if args.send or args.send_dry_run:
        from hostguide.src.outreach_automation import run_outreach
        run_outreach(args.city, city_config, dry_run=not args.send)

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"DONE — {city_name}")
    print(f"{'='*60}")
    print(f"Listings scraped: {len(listings)}")
    print(f"Guides generated: {len(guides)}")
    with_email = sum(1 for l in listings if l.host_email)
    with_ig = sum(1 for l in listings if l.host_instagram)
    with_fb = sum(1 for l in listings if l.host_facebook)
    with_web = sum(1 for l in listings if l.host_website)
    if any([with_email, with_ig, with_fb, with_web]):
        print(f"Host contacts: {with_email} email, {with_ig} IG, {with_fb} FB, {with_web} website")
    print(f"Output directory: {output_dir}")
    if guides:
        print(f"\nSample guide: {output_dir}/guides/")
    print(f"\nNext steps:")
    print(f"  1. Review the generated guides in output/{args.city}/guides/")
    if not args.discover:
        print(f"  2. Run with --discover to find host emails/IG/websites")
    print(f"  3. Run with --outreach to generate FB posts and DMs")
    print(f"  4. Run with --send-dry-run to preview automated outreach")
    print(f"  5. Run with --send to actually post to FB groups and send emails")
    print(f"  Target groups: {', '.join(city_config.get('fb_groups', [])[:2])}")


if __name__ == "__main__":
    main()
