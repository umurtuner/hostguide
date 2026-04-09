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

import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

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

if STRIPE_SECRET:
    stripe.api_key = STRIPE_SECRET

app = Flask(__name__)
CORS(app)


# ═══════════════════════════════════════════════════════════════
# ORDER STORAGE (JSON file — swap for DB later)
# ═══════════════════════════════════════════════════════════════

def _load_orders() -> dict:
    if ORDERS_FILE.exists():
        return json.loads(ORDERS_FILE.read_text())
    return {}


def _save_orders(orders: dict):
    ORDERS_FILE.write_text(json.dumps(orders, indent=2))


def _create_order(airbnb_url: str, email: str) -> str:
    """Create a pending order, return order token."""
    token = secrets.token_urlsafe(24)
    orders = _load_orders()
    orders[token] = {
        "airbnb_url": airbnb_url,
        "email": email,
        "status": "pending",  # pending → paid → generated → expired
        "created": datetime.utcnow().isoformat(),
        "expires": (datetime.utcnow() + timedelta(hours=24)).isoformat(),
        "guide_path": None,
        "stripe_session_id": None,
    }
    _save_orders(orders)
    return token


def _get_order(token: str) -> dict | None:
    orders = _load_orders()
    order = orders.get(token)
    if not order:
        return None
    # Check expiry
    if order["status"] == "generated":
        if datetime.utcnow() > datetime.fromisoformat(order["expires"]):
            order["status"] = "expired"
            _save_orders(orders)
    return order


def _update_order(token: str, **kwargs):
    orders = _load_orders()
    if token in orders:
        orders[token].update(kwargs)
        _save_orders(orders)


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


def _generate_guide_for_order(token: str) -> bool:
    """Run the full pipeline: scrape → enrich → generate HTML + PDF."""
    order = _get_order(token)
    if not order or order["status"] != "paid":
        return False

    airbnb_url = order["airbnb_url"]
    listing_id = _extract_listing_id(airbnb_url)
    if not listing_id:
        return False

    try:
        from hostguide.src.scraper import Listing
        from hostguide.src.enricher import enrich_without_api, enrich_activities
        from hostguide.src.guide_generator import generate_guide

        # For now: create a minimal listing from the URL
        # Full scraping requires Playwright + browser — do async later
        listing = Listing(
            listing_id=listing_id,
            title="",
            url=airbnb_url,
            city="",
            neighborhood="",
        )

        # TODO: scrape listing details (lat/lng, host name, city)
        # For MVP: return False and generate manually
        # This will be wired up once we have the async pipeline

        return False  # Manual generation for now

    except Exception as e:
        print(f"Generation error: {e}")
        return False


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
  Something went wrong with the payment. Please try again or contact hello@host-guide.net.
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
    <form id="guideForm" action="/checkout" method="POST">
      <div class="mb-4">
        <label for="airbnb_url" class="block text-xs font-semibold text-gray-600 mb-1.5">Airbnb Listing URL</label>
        <input type="url" id="airbnb_url" name="airbnb_url" required
               placeholder="https://www.airbnb.com/rooms/123456..."
               class="w-full px-4 py-3 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-teal-500/30 focus:border-teal-500 transition placeholder:text-gray-400">
      </div>
      <div class="mb-5">
        <label for="email" class="block text-xs font-semibold text-gray-600 mb-1.5">Your Email</label>
        <input type="email" id="email" name="email" required
               placeholder="you@example.com"
               class="w-full px-4 py-3 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-teal-500/30 focus:border-teal-500 transition placeholder:text-gray-400">
      </div>
      <button type="submit" id="submitBtn"
              class="cta-btn w-full py-3.5 bg-gradient-to-r from-teal-600 to-teal-800 text-white rounded-xl font-semibold text-base">
        Get Your Guide &mdash; $4.99
      </button>
      <p id="errorMsg" class="text-red-500 text-xs text-center mt-2 hidden"></p>
      <p class="text-center text-xs text-gray-400 mt-3">One-time payment &middot; No subscription &middot; Delivered instantly</p>
    </form>
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
      <p class="text-sm text-gray-500">Drop your Airbnb listing URL. We pull the exact GPS coordinates automatically.</p>
    </div>
    <div class="text-center">
      <div class="w-14 h-14 bg-teal-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
        <svg class="w-6 h-6 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/></svg>
      </div>
      <h3 class="font-semibold mb-1">We find everything nearby</h3>
      <p class="text-sm text-gray-500">Restaurants, grocery stores, pharmacies, landmarks, transit — all within walking distance.</p>
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
      <p class="text-xs text-gray-500 leading-relaxed">Top-rated spots with cuisine types, ratings, and walking/driving times from your front door.</p>
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
      <p class="text-xs text-gray-500 leading-relaxed">Insider tips a local would share — hidden gems, transit hacks, neighborhood culture.</p>
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
    <h2 class="text-2xl font-bold text-center mb-10">Trusted by Hosts Worldwide</h2>
    <div class="flex justify-center gap-12 md:gap-20 flex-wrap">
      <div class="text-center">
        <div class="text-3xl font-extrabold text-teal-600">{{ total_guides }}+</div>
        <div class="text-xs text-gray-500 mt-1">Guides Generated</div>
      </div>
      <div class="text-center">
        <div class="text-3xl font-extrabold text-teal-600">{{ total_cities }}</div>
        <div class="text-xs text-gray-500 mt-1">Cities Covered</div>
      </div>
      <div class="text-center">
        <div class="text-3xl font-extrabold text-teal-600">Any</div>
        <div class="text-xs text-gray-500 mt-1">City Worldwide</div>
      </div>
    </div>
    <div class="mt-12 grid md:grid-cols-2 gap-6 max-w-2xl mx-auto">
      <div class="bg-gray-50 rounded-xl p-5">
        <p class="text-sm text-gray-600 italic mb-3">"Finally stopped getting 'where's the grocery store?' messages at 11pm. The guide pays for itself after one booking."</p>
        <p class="text-xs font-semibold text-gray-800">Miami Superhost</p>
      </div>
      <div class="bg-gray-50 rounded-xl p-5">
        <p class="text-sm text-gray-600 italic mb-3">"My guests loved it. One of them said it was the most thoughtful touch they've seen in any Airbnb."</p>
        <p class="text-xs font-semibold text-gray-800">Dublin Host</p>
      </div>
    </div>
  </div>
</section>

<!-- ════════ PRICING ════════ -->
<section id="pricing" class="max-w-md mx-auto px-6 mb-24 text-center">
  <h2 class="text-2xl font-bold mb-3">Simple Pricing</h2>
  <p class="text-sm text-gray-500 mb-8">No subscriptions. No hidden fees. Pay per guide.</p>
  <div class="bg-white rounded-2xl shadow-lg p-8 border border-gray-100">
    <div class="text-4xl font-extrabold text-teal-700 mb-1">$4.99</div>
    <div class="text-sm text-gray-500 mb-6">per guide</div>
    <ul class="text-left text-sm text-gray-600 space-y-3 mb-8">
      <li class="flex items-start gap-2"><svg class="w-4 h-4 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Full neighborhood guide with 30+ places</li>
      <li class="flex items-start gap-2"><svg class="w-4 h-4 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Print-ready PDF + digital version</li>
      <li class="flex items-start gap-2"><svg class="w-4 h-4 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Walking & driving times included</li>
      <li class="flex items-start gap-2"><svg class="w-4 h-4 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Personalized with your host name</li>
      <li class="flex items-start gap-2"><svg class="w-4 h-4 text-teal-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg> Works for any city worldwide</li>
    </ul>
    <a href="#" onclick="document.getElementById('airbnb_url').focus();window.scrollTo({top:0,behavior:'smooth'});return false;"
       class="cta-btn block w-full py-3.5 bg-gradient-to-r from-teal-600 to-teal-800 text-white rounded-xl font-semibold text-base text-center">
      Get Your Guide
    </a>
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
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">Most guides are ready in 1-2 minutes. We scrape your listing, find all nearby points of interest, and format everything into a clean guide automatically.</p></div>
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
      <div class="faq-answer px-6 text-sm text-gray-500 leading-relaxed"><p class="pb-4">Not yet, but we're working on multi-listing packages. For now, each guide is $4.99. Reach out if you have 10+ listings and we'll work something out.</p></div>
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
    <p class="text-xs text-gray-400">hello@host-guide.net</p>
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
    btn.textContent = 'Redirecting to checkout...';
    btn.style.opacity = '0.7';
    btn.style.pointerEvents = 'none';
    err.classList.add('hidden');
});
</script>

</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def landing():
    """Landing page."""
    cities = set()
    guides = 0
    if OUTPUT.exists():
        for city_dir in OUTPUT.iterdir():
            if city_dir.is_dir() and (city_dir / "guides").is_dir():
                city_guides = list((city_dir / "guides").glob("*_guide.html"))
                if city_guides:
                    cities.add(city_dir.name)
                    guides += len(city_guides)
    return render_template_string(
        LANDING_PAGE,
        total_cities=len(cities),
        total_guides=guides,
    )


@app.route("/checkout", methods=["POST"])
def checkout():
    """Create Stripe Checkout session and redirect."""
    airbnb_url = request.form.get("airbnb_url", "").strip()
    email = request.form.get("email", "").strip()

    import re
    if not airbnb_url or not re.search(r'airbnb\.\w+/(rooms|h)/', airbnb_url):
        return redirect("/")

    # Create order
    token = _create_order(airbnb_url, email)

    if not STRIPE_SECRET:
        # Dev mode: skip payment, mark as paid
        _update_order(token, status="paid")
        return redirect(f"/generating/{token}")

    # Create Stripe Checkout session
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "HostGuide — Personalized Guest Guide",
                        "description": f"Neighborhood guide for your Airbnb listing",
                    },
                    "unit_amount": 499,  # $4.99
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=email,
            success_url=f"{DOMAIN}/generating/{token}",
            cancel_url=f"{DOMAIN}/?cancelled=1",
            metadata={"order_token": token},
        )
        _update_order(token, stripe_session_id=session.id)
        return redirect(session.url, code=303)
    except Exception as e:
        print(f"Stripe error: {e}")
        return redirect("/?error=payment")


@app.route("/generating/<token>")
def generating(token: str):
    """Show 'generating your guide' page — polls for completion."""
    order = _get_order(token)
    if not order or order["status"] not in ("paid", "generated"):
        abort(404)

    if order["status"] == "generated" and order.get("guide_path"):
        return redirect(f"/download/{token}")

    return render_template_string(GENERATING_PAGE, token=token)


@app.route("/api/status/<token>")
def order_status(token: str):
    """API endpoint for polling generation status."""
    order = _get_order(token)
    if not order:
        return jsonify({"status": "not_found"}), 404
    return jsonify({
        "status": order["status"],
        "ready": order["status"] == "generated" and order.get("guide_path") is not None,
    })


@app.route("/download/<token>")
def download(token: str):
    """Serve the generated guide."""
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

    return send_file(guide_path, mimetype="text/html")


@app.route("/download/<token>/pdf")
def download_pdf(token: str):
    """Serve the PDF version."""
    order = _get_order(token)
    if not order or order["status"] != "generated":
        abort(404)

    guide_path = Path(order.get("guide_path", ""))
    pdf_path = guide_path.with_suffix(".pdf") if guide_path.exists() else None
    if pdf_path and pdf_path.exists():
        return send_file(pdf_path, mimetype="application/pdf",
                        as_attachment=True,
                        download_name="HostGuide_Guest_Guide.pdf")
    abort(404, "PDF not found")


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
        if token:
            _update_order(token, status="paid")
            # Trigger async generation here
            # For MVP: manual generation, update order when done

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
        <p>We're building a personalized neighborhood guide for your listing. This usually takes 1-2 minutes.</p>
        <p class="status">Scraping listing details...</p>
    </div>
    <div class="ready" id="readySection">
        <h1>Your Guide is Ready!</h1>
        <p>Your personalized neighborhood guide has been generated.</p>
        <a href="/download/{{ token }}">View Your Guide</a>
        <br>
        <a href="/download/{{ token }}/pdf" style="margin-top:8px; display:inline-block; font-size:13px; color:#00897B;">Download PDF</a>
    </div>
</div>
<script>
const token = "{{ token }}";
let checks = 0;
function pollStatus() {
    fetch(`/api/status/${token}`)
        .then(r => r.json())
        .then(data => {
            if (data.ready) {
                document.querySelector('.generating').style.display = 'none';
                document.getElementById('readySection').style.display = 'block';
            } else if (checks < 60) {
                checks++;
                const msgs = ['Scraping listing details...', 'Finding nearby places...',
                              'Building your guide...', 'Almost there...'];
                document.querySelector('.status').textContent = msgs[Math.min(checks, msgs.length-1)];
                setTimeout(pollStatus, 5000);
            } else {
                document.querySelector('.generating').innerHTML =
                    '<h1 style="font-size:22px;margin-bottom:8px;">Taking longer than expected</h1>' +
                    '<p style="font-size:14px;color:#666;line-height:1.6;">Your guide is still being prepared. We\\'ll email it to you when it\\'s ready. You can close this page.</p>' +
                    '<p style="margin-top:16px;font-size:13px;color:#888;">Questions? hello@host-guide.net</p>';
            }
        })
        .catch(() => setTimeout(pollStatus, 5000));
}
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
