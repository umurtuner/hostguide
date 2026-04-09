"""Host discovery — find external profiles and contact info for Airbnb hosts.

Given a list of scraped listings with host IDs, discovers:
- Airbnb host profile details (response rate, superhost, listing count)
- Instagram handles (from Airbnb profile or Google search)
- Email addresses (from host websites or Google search)
- Facebook profiles (for DM outreach)

Usage:
    from hostguide.src.host_discovery import HostDiscovery
    hd = HostDiscovery()
    enriched = hd.discover_all("output/miami/listings.json")
"""
from __future__ import annotations

import json
import os
import re
import time
import random
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page

PROFILE_DIR = os.getenv("PROFILE_DIR", str(Path(__file__).parent.parent / "chrome_profile_airbnb"))


@dataclass
class HostProfile:
    """Enriched host profile with external contact info."""
    listing_id: str
    host_name: str
    host_id: str
    city: str
    airbnb_profile_url: str = ""
    superhost: bool = False
    response_rate: str = ""
    total_listings: int = 0
    member_since: str = ""
    # External presence
    website: str = ""
    instagram: str = ""
    facebook: str = ""
    email: str = ""
    # Discovery metadata
    discovery_source: str = ""  # how we found the external info


class HostDiscovery:
    """Discover host external profiles from Airbnb data."""

    def __init__(self, headless: bool = False):
        self.headless = headless

    def discover_all(self, listings_path: str, max_hosts: int = 20) -> list[HostProfile]:
        """Full discovery pipeline for all hosts in a listings file."""
        with open(listings_path) as f:
            listings = json.load(f)

        # Deduplicate by host_id
        seen_hosts = set()
        profiles = []
        for l in listings:
            hid = l.get("host_id", "")
            if not hid or hid in seen_hosts or hid == "":
                continue
            seen_hosts.add(hid)
            profiles.append(HostProfile(
                listing_id=l["listing_id"],
                host_name=l.get("host_name", ""),
                host_id=hid,
                city=l.get("city", ""),
                airbnb_profile_url=l.get("host_profile_url", "") or
                    (f"https://www.airbnb.com/users/show/{hid}" if hid else ""),
                superhost=l.get("host_superhost", False),
                response_rate=l.get("host_response_rate", ""),
                website=l.get("host_website", ""),
                instagram=l.get("host_instagram", ""),
                email=l.get("host_email", ""),
            ))

        print(f"\n  {len(profiles)} unique hosts found (from {len(listings)} listings)")

        # Visit Airbnb host profiles for additional data
        profiles_to_visit = [p for p in profiles if p.airbnb_profile_url][:max_hosts]
        if profiles_to_visit:
            print(f"  Visiting {len(profiles_to_visit)} host profiles on Airbnb...")
            self._enrich_from_airbnb_profiles(profiles_to_visit)

        # Try Google search for hosts with websites but no email/IG
        hosts_need_more = [p for p in profiles if p.host_name and not (p.email and p.instagram)]
        if hosts_need_more:
            print(f"  Searching Google for {min(len(hosts_need_more), 10)} hosts...")
            self._enrich_from_google(hosts_need_more[:10])

        # Summary
        with_email = sum(1 for p in profiles if p.email)
        # Post-discovery cleanup: remove false positive URLs
        JUNK_DOMAINS = ("muscache.com", "google.com", "googleapis", "gstatic",
                        "accounts.google", "apple-touch", "airbnb.com", "facebook.com/login")
        for p in profiles:
            if p.website and any(d in p.website.lower() for d in JUNK_DOMAINS):
                p.website = ""
            if p.instagram and p.instagram.lower() in (
                "media", "p", "reel", "explore", "airbnb", "accounts",
                "static", "about", "developer", "stories", "tv",
            ):
                p.instagram = ""

        with_email = sum(1 for p in profiles if p.email)
        with_ig = sum(1 for p in profiles if p.instagram)
        with_website = sum(1 for p in profiles if p.website)
        with_fb = sum(1 for p in profiles if p.facebook)
        print(f"\n  Discovery results:")
        print(f"    Email: {with_email}/{len(profiles)}")
        print(f"    Instagram: {with_ig}/{len(profiles)}")
        print(f"    Website: {with_website}/{len(profiles)}")
        print(f"    Facebook: {with_fb}/{len(profiles)}")

        return profiles

    def _enrich_from_airbnb_profiles(self, profiles: list[HostProfile]):
        """Visit Airbnb host profile pages to extract more data."""
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=self.headless,
                viewport={"width": 1400, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            )
            page = context.new_page()

            for i, profile in enumerate(profiles):
                print(f"    [{i+1}/{len(profiles)}] {profile.host_name or profile.host_id}")
                try:
                    page.goto(profile.airbnb_profile_url,
                              wait_until="domcontentloaded", timeout=30000)
                    time.sleep(random.uniform(2, 4))

                    html = page.content()

                    # Total listings count
                    if not profile.total_listings:
                        listings_match = re.search(r'(\d+)\s*listing', html, re.IGNORECASE)
                        if listings_match:
                            profile.total_listings = int(listings_match.group(1))

                    # Member since
                    if not profile.member_since:
                        since_match = re.search(
                            r'(?:Joined in|Member since)\s*(\w+\s*\d{4})', html, re.IGNORECASE)
                        if since_match:
                            profile.member_since = since_match.group(1)

                    # Superhost badge
                    if not profile.superhost:
                        if "superhost" in html.lower():
                            profile.superhost = True

                    # Response rate from profile
                    if not profile.response_rate:
                        rr_match = re.search(r'(\d+)%\s*response rate', html, re.IGNORECASE)
                        if rr_match:
                            profile.response_rate = f"{rr_match.group(1)}%"

                    # External links — Airbnb sometimes shows host website
                    if not profile.website:
                        web_match = re.search(
                            r'href="(https?://(?!(?:www\.)?(airbnb|facebook\.com|instagram\.com|muscache\.com|google\.com|googleapis|gstatic))[^"]+)"[^>]*>.*?(?:website|site)',
                            html, re.IGNORECASE)
                        if web_match:
                            url = web_match.group(1)
                            # Skip CDN/static asset URLs
                            if not any(d in url.lower() for d in (
                                "muscache.com", "google.com", "googleapis",
                                "gstatic", ".js", ".css", ".png", ".jpg",
                            )):
                                profile.website = url

                    # Instagram from profile page
                    if not profile.instagram:
                        ig_match = re.search(r'instagram\.com/([a-zA-Z0-9_.]{3,30})', html)
                        if ig_match:
                            handle = ig_match.group(1)
                            if handle.lower() not in (
                                "p", "reel", "explore", "airbnb", "media",
                                "accounts", "static", "about", "developer",
                            ):
                                profile.instagram = handle
                                profile.discovery_source = "airbnb_profile"

                    # Facebook from profile page
                    if not profile.facebook:
                        fb_match = re.search(r'facebook\.com/([a-zA-Z0-9.]+)', html)
                        if fb_match:
                            handle = fb_match.group(1)
                            if handle.lower() not in ("airbnb", "sharer", "dialog"):
                                profile.facebook = handle
                                profile.discovery_source = "airbnb_profile"

                    time.sleep(random.uniform(1.5, 3))

                except Exception as e:
                    print(f"      Error: {str(e)[:60]}")

            context.close()

    def _enrich_from_google(self, profiles: list[HostProfile]):
        """Google search for host external presence."""
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=self.headless,
                viewport={"width": 1400, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            )
            page = context.new_page()

            for profile in profiles:
                if not profile.host_name:
                    continue

                try:
                    query = f'"{profile.host_name}" airbnb host {profile.city}'
                    page.goto(f"https://www.google.com/search?q={query.replace(' ', '+')}",
                              wait_until="domcontentloaded", timeout=20000)
                    time.sleep(random.uniform(2, 4))

                    # Wait for captcha if present — give user 30s to solve
                    html = page.content()
                    if "captcha" in html.lower() or "unusual traffic" in html.lower():
                        print(f"      [CAPTCHA] Solve it in the browser window (30s)...")
                        for _ in range(15):
                            time.sleep(2)
                            html = page.content()
                            if "captcha" not in html.lower() and "unusual traffic" not in html.lower():
                                break

                    # Find Instagram
                    if not profile.instagram:
                        ig_match = re.search(r'instagram\.com/([a-zA-Z0-9_.]{3,30})', html)
                        if ig_match:
                            handle = ig_match.group(1)
                            if handle.lower() not in (
                                "p", "reel", "explore", "airbnb", "media",
                                "accounts", "static", "about", "developer",
                            ):
                                profile.instagram = handle
                                profile.discovery_source = "google_search"

                    # Find email
                    if not profile.email:
                        emails = re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', html)
                        for email in emails:
                            if not any(d in email.lower() for d in
                                       ("google", "airbnb", "example", "noreply", "support",
                                        "gstatic", "schema", "w3.org")):
                                profile.email = email
                                profile.discovery_source = "google_search"
                                break

                    # Find website
                    if not profile.website:
                        # Look for personal websites in search results
                        site_matches = re.findall(
                            r'href="(https?://(?!(?:www\.)?(google|airbnb|facebook|instagram|twitter|linkedin|youtube|yelp|tripadvisor|muscache|gstatic|googleapis)\b)[^"]+)"',
                            html
                        )
                        for url, _ in site_matches[:5]:
                            if profile.host_name.split()[0].lower() in url.lower():
                                profile.website = url
                                profile.discovery_source = "google_search"
                                break

                    time.sleep(random.uniform(3, 6))  # Longer delay for Google

                except Exception:
                    pass

            context.close()

    def save_profiles(self, profiles: list[HostProfile], output_path: str):
        """Save discovered profiles to JSON."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump([asdict(p) for p in profiles], f, indent=2, ensure_ascii=False)
        print(f"  Saved {len(profiles)} host profiles to {output_path}")

    def load_profiles(self, path: str) -> list[HostProfile]:
        """Load profiles from JSON."""
        with open(path) as f:
            data = json.load(f)
        return [HostProfile(**d) for d in data]
