"""Outreach message generator - personalized DMs for Airbnb hosts.

Generates Contact-Host messages, FB group posts, and email templates for hosts.

Usage:
    from hostguide.src.outreach import generate_contact_host, generate_fb_post
"""
from __future__ import annotations

try:
    from hostguide.src.scraper import Listing
    from hostguide.src.guide_generator import GuestGuide
except ImportError:
    from src.scraper import Listing
    from src.guide_generator import GuestGuide


SITE = "https://www.host-guide.net"


def _first_name(full: str) -> str:
    if not full:
        return ""
    return full.strip().split()[0]


def generate_contact_host(listing: Listing, guide_url: str = "") -> str:
    """Short Airbnb Contact-Host message. Must fit within Airbnb's message limits
    and read like a host-to-host note, not a sales pitch.
    """
    host = _first_name(listing.host_name) or "there"
    neighborhood = listing.neighborhood or listing.city

    return f"""Hey {host}, fellow host here. I run HostGuide (host-guide dot net) - it auto-generates a printable neighborhood welcome book from any Airbnb URL in 60s. Walking times, top cafes, transit, groceries, local tips. I use it for my own place in Geneva.

For {neighborhood} specifically I think you'd find the cafe + transit picks useful. $1.99 to test on yours, refund if it's not better than what you have now.

Cheers, Umur"""


def generate_dm(listing: Listing, guide: GuestGuide = None, guide_url: str = "",
                pricing: str = "$4.99 one-time") -> str:
    """Longer DM for FB/IG or email, with pricing mention.

    guide is optional and unused in the copy; kept for backwards compatibility.
    """
    host = _first_name(listing.host_name) or "there"
    neighborhood = listing.neighborhood or listing.city
    cta_link = guide_url or SITE

    return f"""Hi {host},

I came across your listing in {neighborhood} and it looks great. Quick note - I built a tool that generates personalized guest guides for Airbnb hosts. Instead of answering the same "where's the nearest grocery store?" message every week, hosts drop one branded PDF in their welcome book.

What's in it:
- Walking directions to nearest metro/bus
- Best cafes and restaurants within 10 min walk
- Groceries, pharmacies, ATMs
- Local taxi apps and tipping norms
- Things to do in {neighborhood}

Your guests get a better first day, you get fewer repetitive messages, reviews go up.

Pricing: {pricing} per listing (no subscription). Site: {cta_link}

Best,
Umur
HostGuide"""


def generate_fb_post(city: str, sample_url: str = "", **_kwargs) -> str:
    """Conversational FB group post. sample_url optional - a real host-guide.net link."""
    link_line = f"\n\nHere's a sample: {sample_url}" if sample_url else ""
    return f"""Hey everyone. I've been hosting in {city} for a while and got tired of answering the same guest questions: "where's the nearest grocery store?", "best coffee nearby?", "how do I get to the beach?".

So I built a little tool that generates a neighborhood guide specific to your apartment's exact location. It pulls nearby restaurants, transit, groceries, landmarks, and formats it into something you can actually send to guests.{link_line}

Site: https://www.host-guide.net - $1.99 to test on your listing, refund if it's not better than what you have now.

Figured it might save some of you the same headache."""


def generate_instagram_dm(listing: Listing, guide_url: str = "") -> str:
    """Short IG DM - fits the 1000-char limit and reads casual."""
    host = _first_name(listing.host_name) or "there"
    cta_link = guide_url or SITE
    return f"""Hi {host}! Love your place in {listing.city}.

I built a tool that auto-generates a printable welcome book for any Airbnb listing - walking distances to cafes, transit, groceries, all tailored to the exact spot.

Site: {cta_link} - $1.99 to test on your listing.

Cheers, Umur"""


def generate_email_template(listing: Listing, _guide_url: str = "") -> dict:
    """Subject + body for email outreach (when we have the host's email)."""
    host = _first_name(listing.host_name) or "there"
    city = listing.city
    neighborhood = listing.neighborhood or city

    return {
        "subject": f"Printable neighborhood guide for your {neighborhood} listing",
        "body": f"""Hi {host},

I noticed your Airbnb listing in {neighborhood}, {city}. Great place.

I built a tool that auto-generates a personalized printable neighborhood guide from any Airbnb URL. Walking directions to the nearest transit, top-rated cafes within 10 min, groceries, local tips, ride apps - everything a guest needs on Day 1.

Site: {SITE} - $1.99 per listing, no subscription.

If you'd like to see what one looks like for {neighborhood} specifically, reply and I'll send a preview. Your guests get a better first day, you get fewer repetitive messages.

Best,
Umur
HostGuide""",
    }
