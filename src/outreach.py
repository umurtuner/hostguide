"""Outreach message generator — personalized DMs for Airbnb hosts.

Generates FB group posts and direct messages to hosts,
including a sample guide preview for their specific listing.

Usage:
    from hostguide.src.outreach import generate_dm, generate_fb_post
"""
from __future__ import annotations

from hostguide.src.scraper import Listing
from hostguide.src.guide_generator import GuestGuide


def generate_dm(listing: Listing, guide: GuestGuide, pricing: str = "$9.99/month") -> str:
    """Generate a personalized DM to a host about their specific listing."""
    host = listing.host_name or "there"
    city = listing.city
    neighborhood = listing.neighborhood or city

    return f"""Hi {host}! 👋

I came across your listing in {neighborhood} — looks like a great place!

I built a service that creates **personalized guest guides** for Airbnb hosts. Instead of writing your own guidebook or answering the same "where's the nearest grocery store?" message every time, we generate a beautiful, branded guide specific to YOUR apartment's exact location.

Here's what I made for your listing (free sample):
→ [Link to sample guide]

It includes:
✅ Walking directions to nearest metro/bus stops
✅ Best restaurants & cafes within 10 min walk
✅ Grocery stores, pharmacies, ATMs nearby
✅ Local tips (safety, tipping, taxi apps)
✅ Things to see & do in {neighborhood}

Your guests get a better experience, you get fewer repetitive messages, and your reviews improve. Win-win.

**{pricing}/listing** — one-time setup, we keep it updated.

Want me to send you the full guide for your listing? It's already done — just need your OK to share it.

Best,
Umur"""


def generate_fb_post(city: str, **_kwargs) -> str:
    """Generate a FB group post to attract hosts — conversational, not salesy."""
    return f"""Hey everyone — I've been hosting in {city} for a while and got tired of answering the same guest questions: "where's the nearest grocery store?", "best coffee nearby?", "how do I get to the beach?"

So I built a little tool that generates a neighborhood guide specific to your apartment's exact location. It pulls nearby restaurants, transit, groceries, landmarks — and formats it into something you can actually send to guests.

Here's what it looks like (screenshot attached).

I'm testing it out right now and happy to make one for your listing for free — just drop your Airbnb link and I'll send it over. Takes about 2 minutes to generate.

Figured it might save some of you the same headache."""


def generate_instagram_dm(listing: Listing) -> str:
    """Generate a short Instagram DM for hosts who post their listings on IG."""
    host = listing.host_name or "there"
    return f"""Hi {host}! Love your place in {listing.city} 🏡

I make personalized guest guides for Airbnb hosts — walking distances to restaurants, transit, groceries etc. specific to your apartment's location.

Made a free sample for your listing — want me to send it?"""


def generate_email_template(listing: Listing, guide_url: str) -> dict:
    """Generate email subject + body for hosts found via Airbnb messaging or email."""
    host = listing.host_name or "Host"
    city = listing.city
    neighborhood = listing.neighborhood or city

    return {
        "subject": f"Free guest guide for your {neighborhood} listing ✨",
        "body": f"""Hi {host},

I'm reaching out because I noticed your Airbnb listing in {neighborhood}, {city}. Great place!

I've created a personalized neighborhood guide specifically for YOUR listing's location. It includes walking directions to the nearest transit, restaurants, groceries, landmarks, and local tips — everything a guest needs on Day 1.

Here's the preview: {guide_url}

This is completely free — I'm building a guest guide service for Airbnb hosts and would love your feedback on the guide.

If you like it, I can set up a hosted version with a short link you can include in your check-in message to guests. Your guests get a better experience, you get fewer "where is the nearest...?" messages.

Let me know what you think!

Best,
Umur
HostGuide · hostguide.co""",
    }
