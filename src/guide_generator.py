"""Guest guide generator -creates personalized PDF/HTML guides per listing.

Uses Claude API to write engaging, localized guide content.
Falls back to template-based generation if no API key.

Usage:
    from hostguide.src.guide_generator import generate_guide
    guide = generate_guide(listing, enriched_location, city_config)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Optional

try:
    from hostguide.src.scraper import Listing
    from hostguide.src.enricher import EnrichedLocation, Place
except ImportError:
    from src.scraper import Listing
    from src.enricher import EnrichedLocation, Place


@dataclass
class GuestGuide:
    """A complete guest guide for a listing."""
    listing_id: str
    listing_title: str
    host_name: str
    city: str
    neighborhood: str
    generated_date: str
    content_md: str  # Full guide in markdown
    content_html: str  # Full guide in HTML


def _format_place(p: Place) -> str:
    """Format a single place for the guide."""
    stars = f" ({'★' * int(p.rating)}{'☆' * (5 - int(p.rating))} {p.rating})" if p.rating else ""
    price = " · " + "$" * p.price_level if p.price_level else ""
    if p.distance_m > 0:
        return f"- **{p.name}**{stars}{price} -{p.walking_min} min walk ({p.distance_m}m)"
    elif p.address:
        return f"- **{p.name}**{stars}{price} -{p.address}"
    else:
        return f"- **{p.name}**{stars}{price}"


COUNTRY_CONTEXT = {
    "PT": {
        "transit_card": "Viva Viagem rechargeable card (€0.50 from any Metro machine, then load €1.65 per ride or €6.80 for 24h unlimited)",
        "ride_apps": "Bolt is dominant (cheaper than Uber). Uber works too. Free Now for taxis.",
        "payment": "Multibanco network covers nearly every shop. Contactless cards everywhere. Carry €20 cash for small cafés.",
        "tipping": "Not expected. Round up the bill or leave 5-10% if service was excellent.",
        "water": "Tap water is safe and fine to drink across mainland Portugal.",
        "plug": "Type F (same as most of EU). 230V.",
        "greeting": "Olá (hello), Bom dia (morning), Obrigado/Obrigada (thank you, m/f).",
        "emergency": "112 for everything (police/medical/fire). Tourist police 21 342 1623.",
    },
    "ES": {
        "transit_card": "T-Casual 10-trip card in Barcelona (€12.55), Metrobus pass in Madrid (€12.20).",
        "ride_apps": "Cabify and Bolt work well. Uber in major cities. Hailing taxis is fine and metered.",
        "payment": "Cards everywhere. Carry €20 for small bars and markets.",
        "tipping": "Not expected. Round up or 5-10% for excellent service.",
        "water": "Tap water is safe in most cities (Madrid, Barcelona, San Sebastián). Some southern coast cities locals prefer bottled.",
        "plug": "Type F. 230V.",
        "greeting": "Hola, Buenos días, Gracias.",
        "emergency": "112.",
    },
    "GB": {
        "transit_card": "Contactless card or Apple/Google Pay tap on/off works on London Underground, buses, and most rail. Daily cap auto-applied.",
        "ride_apps": "Uber, Bolt, FreeNow. Black cabs with metered fare; you can also hail.",
        "payment": "Contactless universal. Few places refuse cards. Pubs sometimes minimum £5.",
        "tipping": "10-12.5% at restaurants if service charge isn't included. Not expected at pubs or cafés.",
        "water": "Tap water safe nationwide.",
        "plug": "Type G (three flat pins). 230V.",
        "greeting": "Hello, Thanks, Cheers (informal thanks).",
        "emergency": "999 or 112.",
    },
    "US": {
        "transit_card": "City-specific (CharlieCard Boston, MetroCard NYC, Clipper SF, EasyPark Miami). Most accept contactless tap.",
        "ride_apps": "Uber and Lyft are the standard. Add 15-20% tip in-app.",
        "payment": "Cards everywhere. Tap your phone. Tipping changes the math, see below.",
        "tipping": "Restaurants 18-22% (sit-down). Bars $1-2/drink. Rideshare 15-20%. Hotel housekeeping $3-5/day.",
        "water": "Tap water safe nationwide; locally Floridians often prefer filtered for taste.",
        "plug": "Type A/B (two flat pins, optional ground). 120V.",
        "greeting": "Hi, Thanks, How's it going.",
        "emergency": "911.",
    },
    "FR": {
        "transit_card": "Navigo Easy card in Paris (€2 + €2.15/ride or €8.65/day). t+ tickets are being phased out.",
        "ride_apps": "Uber and Bolt. Heetch in Paris. Taxis G7 app for traditional taxis.",
        "payment": "Cards universal. Carry €10-20 for cafés and bakeries.",
        "tipping": "Service is included. Leave €1-2 if happy. 5-10% for excellent service.",
        "water": "Tap water safe nationwide. Restaurants will bring a free carafe (\"une carafe d'eau, s'il vous plaît\").",
        "plug": "Type E. 230V.",
        "greeting": "Bonjour (always say it when entering shops), Merci, S'il vous plaît.",
        "emergency": "112 or 17 (police), 15 (medical).",
    },
    "IT": {
        "transit_card": "Single rides ~€1.50, daily passes city-specific. Validate paper tickets in machines on board.",
        "ride_apps": "FreeNow and Uber Black (no UberX in most Italian cities). Taxis are metered.",
        "payment": "Cards widely accepted but small bars/markets are still cash. Carry €20.",
        "tipping": "Coperto (cover charge) is on the bill. No tipping expected. Round up if happy.",
        "water": "Tap water is safe and good. Public fountains in Rome, Milan are drinkable.",
        "plug": "Type F/L. 230V.",
        "greeting": "Ciao (informal), Buongiorno, Grazie, Prego (you're welcome).",
        "emergency": "112.",
    },
    "CO": {
        "transit_card": "TuLlave card in Bogotá (TransMilenio), Cívica card in Medellín (Metro). ~3000 COP per ride.",
        "ride_apps": "Uber, InDriver (cheapest, you negotiate fare), DiDi. Avoid hailing taxis at night.",
        "payment": "Cash for street vendors and small spots. Cards in supermarkets/restaurants. Bring small bills.",
        "tipping": "10% propina voluntaria — usually added to the bill, you can decline. Round up taxis.",
        "water": "Tap water safe in Bogotá and Medellín. Outside major cities, drink bottled.",
        "plug": "Type A/B (US-style). 110V.",
        "greeting": "Hola, Buenos días, Gracias, ¿Cómo está?",
        "emergency": "123.",
    },
    "MX": {
        "transit_card": "MetroBus card in CDMX (~$10 MXN to buy, $6 MXN/ride).",
        "ride_apps": "Uber and DiDi everywhere. Don't hail street taxis in CDMX.",
        "payment": "Cards in chains and tourist areas. Cash for everything else. ATMs at HSBC/Santander/BBVA.",
        "tipping": "10-15% restaurants. 10-20 pesos for hotel staff. Round up taxis.",
        "water": "Drink bottled or filtered. Tap is not safe.",
        "plug": "Type A/B. 127V.",
        "greeting": "Hola, Buenos días, Gracias, Por favor.",
        "emergency": "911.",
    },
    "AE": {
        "transit_card": "Nol card for Dubai Metro and buses (AED 25 buy + load).",
        "ride_apps": "Careem (local) and Uber. Both ~AED 15-30 per short ride.",
        "payment": "Cards universal. Bring some cash for souks and small shops.",
        "tipping": "10-15% restaurants if no service charge. AED 5-10 for hotel staff.",
        "water": "Tap is technically safe but locals/expats drink bottled for taste.",
        "plug": "Type G (UK-style). 230V.",
        "greeting": "As-salamu alaykum (peace be upon you), Shukran (thanks), Marhaba (hello).",
        "emergency": "999 (police), 998 (ambulance), 997 (fire).",
    },
}


def _generate_with_claude(listing: Listing, enriched: EnrichedLocation,
                          city_config: dict) -> str:
    """Use Claude to write an engaging, personalized guide."""
    try:
        import anthropic
        client = anthropic.Anthropic()
    except Exception:
        return ""

    places_summary = ""
    poi_total = 0
    for category in ["transit", "grocery", "restaurant", "landmark", "nightlife", "health"]:
        places = getattr(enriched, category, [])
        if places:
            poi_total += len(places)
            places_summary += f"\n{category.upper()} ({len(places)} found):\n"
            for p in places:
                places_summary += f"  - {p.name} ({p.distance_m}m, {p.walking_min} min walk)"
                if getattr(p, "rating", 0):
                    places_summary += f" — rated {p.rating}/5"
                if getattr(p, "address", ""):
                    places_summary += f" @ {p.address}"
                places_summary += "\n"
        else:
            places_summary += f"\n{category.upper()}: (no data — call this out and reason about it)\n"

    country = (city_config.get("country") or "").upper()
    ctx = COUNTRY_CONTEXT.get(country, {})
    country_block = ""
    if ctx:
        country_block = (
            "\nCOUNTRY CONTEXT (use these as ground truth — don't invent new versions):\n"
            f"  Transit ticketing: {ctx['transit_card']}\n"
            f"  Ride apps: {ctx['ride_apps']}\n"
            f"  Payment: {ctx['payment']}\n"
            f"  Tipping: {ctx['tipping']}\n"
            f"  Tap water: {ctx['water']}\n"
            f"  Plug: {ctx['plug']}\n"
            f"  Greeting/phrases: {ctx['greeting']}\n"
            f"  Emergency: {ctx['emergency']}\n"
        )

    listing_facts = (
        f"LISTING: {listing.title or 'this apartment'}\n"
        f"CITY: {city_config['name']}\n"
        f"NEIGHBORHOOD: {listing.neighborhood or '(not detected)'}\n"
        f"HOST: {listing.host_name or 'Your Host'}\n"
        f"PROPERTY TYPE: {listing.property_type or '(not detected)'}\n"
        f"BEDS / BATHS / GUESTS: {listing.bedrooms} / {listing.bathrooms} / {listing.guests}\n"
        f"COORDS: {listing.lat}, {listing.lng}\n"
    )

    prompt = f"""You are writing the printed welcome book a guest will read in this exact apartment, in {city_config['name']}, {country}.

{listing_facts}
NEARBY PLACES (from OpenStreetMap, walking distances calculated from the listing's exact lat/lng):
{places_summary}
{country_block}

WRITE THE GUIDE WITH THESE EIGHT SECTIONS, IN ORDER:

1. **Welcome from {listing.host_name or 'Your host'}** — 2-3 sentences in first person, warm but not gushy. Mention the neighborhood by name.
2. **Getting Around** — Open with the closest transit option BY NAME from the data. Then the country's transit ticketing fact verbatim. Then 1-2 lines on ride apps for this country. If TRANSIT data is empty: say "the closest station should be checked locally — your host can confirm" and recommend the country's ride app.
3. **Eating & Drinking** — Pick 3-4 specific named restaurants/cafes from the data (closest first, mix cafe + sit-down if both present). For each, one short reason to go (cuisine type, vibe, time of day). Include walking time. End with one country-specific food tip ("try a *pastel de nata*", "the prato do dia is the lunchtime move", etc.).
4. **Groceries & Essentials** — Name the 2 closest groceries with walking time. Name the closest pharmacy. One line about opening hours norms in this country.
5. **Things to See & Do** — 3-4 named landmarks from the data with walking time. If everything is >25 min walk: be honest ("the neighborhood is residential — for sights, take Metro/bus to..."). Don't pad.
6. **Nightlife** — If data is empty or you only have 1 entry: skip this section entirely (don't write a placeholder). If you have 2+: 2-3 named spots with vibe.
7. **Safety & Practicalities** — 2-3 specific notes about THIS neighborhood and city. Avoid clichés. Include the country's emergency number.
8. **Useful Info** — Bullet list: tap water, tipping, plug type, greeting phrases, emergency number — pulled from country context above.

HARD RULES:
- Every section must reference specific named places from the data. If a category is empty, say so honestly — don't invent place names.
- Voice: a host who lives in the building writing for a guest who arrived 20 minutes ago. First person. No "experience the vibrant culture of...", no "discover hidden gems".
- Walking times come from the data, never round to "about 10 min" — use the exact minute count.
- ZERO emojis anywhere in the output. The HTML adds its own SVG icons.
- Output clean markdown. No HTML, no preamble, no closing apology, just the guide starting with the H1 welcome header.
- Maximum 1200 words total."""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def _generate_template(listing: Listing, enriched: EnrichedLocation,
                       city_config: dict) -> str:
    """Template-based guide generation (no API needed)."""
    city = city_config["name"]
    country = city_config.get("country", "")
    neighborhood = listing.neighborhood or "your neighborhood"
    host = listing.host_name or "Your Host"

    sections = []

    # Header
    sections.append(f"# Welcome to {city}\n")
    sections.append(f"**Your host {host}** has prepared this guide to help you "
                    f"make the most of your stay in **{neighborhood}**, {city}.\n")

    # Transit
    sections.append("## Getting Around\n")
    if enriched.transit:
        for p in enriched.transit[:3]:
            sections.append(_format_place(p))
        sections.append("")
    if country == "CO":
        sections.append(f"**Taxi apps:** Uber, InDriver, DiDi all work in {city}. "
                        f"Always confirm the fare before getting in.")
        sections.append("**Tip:** Taxis are cheap but use apps -avoid hailing on the street at night.\n")
    elif country == "US":
        sections.append(f"**Ride apps:** Uber and Lyft work everywhere in {city}.")
        if city in ("Miami", "Orlando", "Tampa", "Destin", "Scottsdale", "Austin", "Nashville"):
            sections.append("**Tip:** You'll probably want a rental car. Public transit is limited outside downtown.\n")
        else:
            sections.append("**Tip:** Downtown is walkable but a car is handy for exploring.\n")
    else:
        sections.append(f"**Taxi apps:** Uber and Bolt work in {city}. Check locally for other options.\n")

    # Restaurants
    sections.append("## Eating & Drinking\n")
    if enriched.restaurant:
        sections.append("**Nearby favorites:**")
        for p in enriched.restaurant[:5]:
            sections.append(_format_place(p))
        sections.append("")
    if country == "CO":
        sections.append("**Local tip:** Try a *corrientazo* (set lunch) at any local restaurant -"
                        "full meal for ~10,000 COP ($2.50). *Bandeja paisa* is the must-try dish.\n")
    elif city == "Miami":
        sections.append("**Local tip:** Try Cuban coffee (*colada*) from a ventanita window. "
                        "Calle Ocho in Little Havana has the best. Tip 18-20% at restaurants.\n")
    elif city == "Austin":
        sections.append("**Local tip:** BBQ is king -Franklin, la Barbecue, or Micklethwait. "
                        "Lines can be 2+ hours so arrive early or use pickup. Tip 20%.\n")
    elif city == "Nashville":
        sections.append("**Local tip:** Hot chicken is a must -try Hattie B's, Prince's, or Bolton's. "
                        "Start with 'medium' if you're not sure. Broadway honky-tonks are free to enter.\n")
    elif city == "Savannah":
        sections.append("**Local tip:** Open container is legal in the Historic District (in a plastic cup). "
                        "River Street and City Market have the best food and drinks.\n")
    elif city == "Scottsdale":
        sections.append("**Local tip:** Old Town has great restaurants and nightlife. "
                        "In summer it's 110°F+ so pool time is essential. Tip 20% at restaurants.\n")
    elif city == "Orlando":
        sections.append("**Local tip:** Theme park tickets are cheaper on reseller sites. "
                        "Eat at Disney Springs -no park ticket needed. Tip 18-20%.\n")
    elif city == "Tampa":
        sections.append("**Local tip:** Ybor City has the best nightlife and Cuban sandwiches. "
                        "Bayshore Boulevard is great for a waterfront walk. Tip 18-20%.\n")
    elif city == "Destin":
        sections.append("**Local tip:** Harbor Boardwalk has great seafood restaurants. "
                        "Rent a pontoon boat at Crab Island -the locals' favorite. Tip 18-20%.\n")

    # Groceries
    sections.append("## Groceries & Essentials\n")
    if enriched.grocery:
        for p in enriched.grocery[:3]:
            sections.append(_format_place(p))
        sections.append("")
    if enriched.health:
        sections.append("**Pharmacy / Health:**")
        for p in enriched.health[:2]:
            sections.append(_format_place(p))
        sections.append("")

    # Landmarks
    sections.append("## Things to See & Do\n")
    if enriched.landmark:
        for p in enriched.landmark[:5]:
            sections.append(_format_place(p))
        sections.append("")

    # Nightlife
    if enriched.nightlife:
        sections.append("## Nightlife\n")
        for p in enriched.nightlife[:3]:
            sections.append(_format_place(p))
        sections.append("")

    # Safety
    sections.append("## Safety Tips\n")
    if city == "Medellín":
        sections.append("- El Poblado and Laureles are very safe day and night")
        sections.append("- Don't flash expensive phones/jewelry in crowded areas")
        sections.append("- Use Uber/InDriver at night instead of walking")
        sections.append("- Don't accept drinks from strangers (scopolamine risk)")
        sections.append("- Keep a photocopy of your passport, leave the original at home\n")
    elif city == "Bogotá":
        sections.append("- Chapinero, Zona Rosa, Usaquén are safe neighborhoods")
        sections.append("- Avoid La Candelaria after dark unless in a group")
        sections.append("- Use Uber/InDriver, not street taxis")
        sections.append("- Don't walk with headphones in quiet streets")
        sections.append("- Altitude (2,640m) -take it easy the first day, drink lots of water\n")
    elif country == "US":
        sections.append(f"- {neighborhood} is generally safe -use common sense")
        sections.append("- Lock your car and don't leave valuables visible")
        sections.append("- Use Uber/Lyft late at night, especially after drinking")
        if city == "Miami":
            sections.append("- Avoid walking in Overtown or Liberty City")
            sections.append("- Beach safety: watch for rip currents, red flags mean no swimming")
        elif city == "Nashville":
            sections.append("- Lower Broadway is safe but very crowded on weekends")
            sections.append("- Avoid walking alone in North Nashville late at night")
        elif city == "Austin":
            sections.append("- 6th Street gets rowdy late on weekends -stick to Rainey or East Austin")
            sections.append("- Drink lots of water -Texas heat is serious")
        elif city == "Scottsdale":
            sections.append("- Hydrate constantly in summer -heat stroke is a real risk")
            sections.append("- Watch for rattlesnakes on desert hiking trails")
        elif city in ("Orlando", "Tampa", "Destin"):
            sections.append("- Afternoon thunderstorms are daily in summer -they pass quickly")
            sections.append("- Use sunscreen -Florida sun is stronger than you think")
        sections.append("- Dial 911 for any emergency\n")
    else:
        sections.append("- Use ride-hailing apps at night")
        sections.append("- Keep valuables out of sight")
        sections.append("- Ask your host about areas to avoid\n")

    # Useful info
    sections.append("## Useful Info\n")
    sections.append("| | |")
    sections.append("|---|---|")
    if country == "US":
        sections.append("| **Emergency** | 911 (police, fire, ambulance) |")
        sections.append("| **Currency** | US Dollar (USD) |")
        sections.append("| **Tipping** | 18-20% at restaurants, $1-2 per drink at bars |")
        sections.append("| **Tax** | Prices don't include sales tax (6-10% added at checkout) |")
        sections.append("| **Water** | Tap water is safe everywhere |")
        sections.append("| **SIM card** | T-Mobile, AT&T, or Mint Mobile at any Walmart/Target |")
        sections.append("| **WiFi password** | [Ask your host] |")
    else:
        sections.append("| **Emergency** | Check local emergency numbers (112 in EU, 999 in UK) |")
    if country == "CO":
        sections.append("| **Currency** | Colombian Peso (COP). ~4,000 COP = $1 USD |")
        sections.append("| **Tipping** | 10% at restaurants (often included as *propina voluntaria*) |")
        sections.append("| **Language** | Spanish. English is limited outside tourist areas |")
        sections.append("| **Water** | Tap water is safe in Medellín and Bogotá |")
        sections.append("| **SIM card** | Claro or Movistar. Buy at any *Éxito* supermarket |")
        sections.append("| **WiFi password** | [Ask your host] |")

    sections.append(f"\n---\n*Guide prepared for {host}'s guests · {date.today().isoformat()} · "
                    f"Powered by HostGuide*")

    return "\n".join(sections)


# Inline SVG icons used in section headers. Heroicons-style (24x24, line, 1.6
# stroke). Monochrome — color is set in CSS via stroke="currentColor".
ICONS = {
    "transit": '<svg class="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="3" width="12" height="14" rx="3"/><circle cx="9" cy="13" r="0.8"/><circle cx="15" cy="13" r="0.8"/><path d="M9 17l-2 4M15 17l2 4M8 7h8"/></svg>',
    "eating": '<svg class="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3v8a3 3 0 003 3v7M9 3v8M14 14V3c2 0 4 2 4 5s-2 5-4 5z"/></svg>',
    "grocery": '<svg class="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h2l2.5 11h11l2-8H7"/><circle cx="9" cy="20" r="1.4"/><circle cx="17" cy="20" r="1.4"/></svg>',
    "landmark": '<svg class="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18M5 21V10l7-5 7 5v11M9 21v-5h6v5M9 13h.01M15 13h.01"/></svg>',
    "nightlife": '<svg class="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h14l-7 9zM12 13v8M8 21h8"/></svg>',
    "health": '<svg class="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-7-4.5-7-10a4 4 0 017-2.6A4 4 0 0119 11c0 5.5-7 10-7 10z"/><path d="M9 11h6M12 8v6"/></svg>',
    "safety": '<svg class="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 3v5c0 5-3.5 9-8 10-4.5-1-8-5-8-10V6l8-3z"/><path d="M9 12l2 2 4-4"/></svg>',
    "info": '<svg class="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v.01M11 12h1v5h1"/></svg>',
    "tips": '<svg class="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 016 6c0 2.5-1.5 4-3 5v2h-6v-2c-1.5-1-3-2.5-3-5a6 6 0 016-6z"/></svg>',
    "narrative": '<svg class="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v11H7l-3 3z"/><path d="M8 9h8M8 12h5"/></svg>',
}


def _build_html_guide(listing: Listing, enriched: EnrichedLocation,
                      city_config: dict, narrative_md: str = "") -> str:
    """Build a print-first, guest-ready HTML guide.

    Design principles:
    - Print-first: works laminated in a kitchen, no clicks needed
    - WiFi + address + host contact = above the fold
    - Real addresses on every recommendation (not just map links)
    - No emoji as content; clean text labels with small icons
    - QR code links to digital version for phone users
    - Mobile-responsive for guests viewing on phone
    - Dual @media print stylesheet for clean A4/Letter output

    `narrative_md` is the Claude-generated markdown narrative. When provided,
    it renders as an opening section above the structured place lists, giving
    the guide a host's voice on top of the data spine.
    """
    raw_city = city_config["name"]
    # Filter out listing subtitle junk that leaked into city name
    junk_words = ("bed", "bath", "entire", "private", "shared", "room", "guest")
    if any(w in raw_city.lower() for w in junk_words):
        raw_city = ""
    city = raw_city or listing.city or "your neighborhood"
    country = city_config.get("country", "")
    emergency_text = COUNTRY_CONTEXT.get(country, {}).get("emergency", "112 (EU) / 911 (US) / 999 (UK)")
    safety_icon = ICONS["safety"]
    info_icon = ICONS["info"]
    narrative_icon = ICONS["narrative"]
    lat, lng = listing.lat, listing.lng

    # Hero background: prefer the listing's first photo (Airbnb og:image, ~1080w).
    # If we have one, swap the gradient for a darkening overlay over the photo
    # so the hero feels like a real welcome book cover, not a banner.
    hero_photo_class = ""
    hero_style = ""
    if listing.photos:
        # Sanitize: only allow https and reasonable URL chars (defense against
        # CSS injection via a malicious og:image).
        photo_url = listing.photos[0]
        if photo_url.startswith("https://") and '"' not in photo_url and "<" not in photo_url:
            hero_photo_class = "hero-with-photo"
            hero_style = f"background-image: linear-gradient(rgba(20,30,40,0.55), rgba(15,25,35,0.78)), url('{photo_url}');"

    # Define neighborhood up front so downstream blocks (map banner alt text,
    # narrative templates) can reference it. Previous regression: map banner
    # built at line ~467 referenced `neighborhood` before it was assigned a
    # few lines down -> UnboundLocalError on every guide.
    neighborhood = listing.neighborhood or city

    # Static map banner: 600x260 Google Static Maps, centered on the listing,
    # with a marker. Only render when we have the API key + valid coords.
    map_banner_html = ""
    google_map_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if google_map_key and lat and lng:
        static_map_url = (
            "https://maps.googleapis.com/maps/api/staticmap"
            f"?center={lat},{lng}&zoom=15&size=720x260&scale=2"
            "&maptype=roadmap"
            "&style=feature:poi|element:labels|visibility:off"
            f"&markers=color:0x37474F%7C{lat},{lng}"
            f"&key={google_map_key}"
        )
        map_banner_html = f'''<div class="map-banner">
            <img src="{static_map_url}" alt="Map of {neighborhood}, {city}" loading="lazy"/>
        </div>'''
    host = listing.host_name or "Your Host"
    today = date.today().strftime("%B %d, %Y")

    # Deduplicate places by name within each category
    def _dedup_places(places: list) -> list:
        seen = set()
        result = []
        for p in places:
            key = p.name.strip().lower()
            if key not in seen:
                seen.add(key)
                result.append(p)
        return result

    # Filter out generic chains from restaurants
    CHAIN_FILTER = {"starbucks", "mcdonald's", "burger king", "subway", "dunkin'",
                    "p.f. chang's", "chili's", "applebee's", "taco bell", "wendy's",
                    "pizza hut", "domino's", "kfc", "popeyes", "five guys",
                    "taco bell", "arby's", "sonic", "jack in the box", "panda express",
                    "chipotle", "panera bread", "jimmy john's", "jersey mike's"}

    # Zoo/aquarium exhibits and other non-place entries to filter from landmarks
    ZOO_ANIMAL_FILTER = {
        "african elephant", "olive baboon", "lion", "polar bear", "sea lion",
        "penguin", "giraffe", "gorilla", "cheetah", "zebra", "tiger", "leopard",
        "rhino", "rhinoceros", "hippo", "hippopotamus", "flamingo", "orangutan",
        "bear", "wolf", "eagle", "parrot", "snake", "crocodile", "alligator",
        "monkey", "chimpanzee", "bison", "elk", "moose", "otter", "seal",
        "dolphin", "whale", "shark", "jellyfish", "octopus", "turtle", "tortoise",
    }

    for attr in ["transit", "grocery", "restaurant", "landmark", "nightlife", "health"]:
        places = _dedup_places(getattr(enriched, attr, []))
        if attr == "restaurant":
            places = [p for p in places if p.name.strip().lower() not in CHAIN_FILTER]
        if attr == "landmark":
            places = [p for p in places if p.name.strip().lower() not in ZOO_ANIMAL_FILTER]
        setattr(enriched, attr, places)

    # Build place rows -print-friendly with real addresses
    # US = driving distances for anything > 1km, walkable cities = walking
    is_driving_city = country == "US"

    def place_row(p: Place) -> str:
        addr = p.address or ""
        dist_html = ""
        if p.distance_m > 0:
            if is_driving_city and p.distance_m > 1000:
                drive_min = max(1, round(p.distance_m / 500))
                dist_mi = round(p.distance_m / 1609, 1)
                dist_text = f"{drive_min} min drive · {dist_mi} mi"
                badge_class = "far"
            elif p.distance_m > 1500:
                drive_min = max(1, round(p.distance_m / 500))
                dist_text = f"{drive_min} min drive · {round(p.distance_m/1000, 1)} km"
                badge_class = "far"
            else:
                dist_text = f"{p.walking_min} min walk · {p.distance_m}m"
                # Color-code: <=5min teal "near", 6-15 amber "mid", >15 gray "far"
                if p.walking_min <= 5:
                    badge_class = "near"
                elif p.walking_min <= 15:
                    badge_class = "mid"
                else:
                    badge_class = "far"
            dist_html = f'<span class="dist-badge {badge_class}">{dist_text}</span>'
        if p.rating and p.total_ratings:
            rating_html = f' <span class="rating">★ {p.rating} ({p.total_ratings:,})</span>'
        elif p.rating:
            rating_html = f' <span class="rating">★ {p.rating}</span>'
        else:
            rating_html = ""
        maps_url = f"https://www.google.com/maps/dir/?api=1&destination={p.lat},{p.lng}" if p.lat else ""
        # Show address as text (print-friendly), name as link (digital bonus)
        name_html = f'<a href="{maps_url}" target="_blank" class="place-link">{p.name}</a>' if maps_url else p.name
        addr_line = f'<span class="place-addr">{addr}</span>' if addr else ""
        return f'''<tr class="place-row">
            <td class="place-name">{name_html}{rating_html}</td>
            <td class="place-detail">{addr_line}{" &middot; " if addr and dist_html else ""}{dist_html}</td>
        </tr>'''

    # ── Section data builders ──

    local_tips = {
        "Miami": [
            ("Coffee", "Cuban coffee (colada) from a ventanita window. Calle Ocho in Little Havana has the best."),
            ("Happy hour", "Brickell has $5-8 happy hour deals Mon-Fri 4-7pm. Try Area 31 or Batch."),
            ("Beach", "South Beach is touristy. Locals go to Crandon Park (Key Biscayne) or Bill Baggs."),
            ("Getting around", "Take the free Metromover downtown. Trolleys are also free in Brickell and Wynwood."),
            ("Tipping", "18-20% at sit-down restaurants. $1-2 per drink at bars. Valet is $2-5."),
        ],
        "Austin": [
            ("BBQ", "Franklin, la Barbecue, or Micklethwait. Lines can be 2+ hours -arrive early or order pickup."),
            ("Live music", "6th Street for tourists, Rainey Street for locals. Most venues have no cover."),
            ("Tacos", "Breakfast tacos are religion here. Veracruz All Natural and Torchy's are top picks."),
            ("Swimming", "Barton Springs Pool ($5) stays 68F year-round. Best on a hot afternoon."),
            ("Tipping", "20% at restaurants. Austin is a tipping-heavy town."),
        ],
        "Nashville": [
            ("Hot chicken", "Hattie B's, Prince's, or Bolton's. Start with 'medium' if you're not sure."),
            ("Broadway", "Honky-tonks are free to enter. Bands play all day. Don't skip Robert's Western World."),
            ("Brunch", "Biscuit Love, Pancake Pantry, or The Loveless Cafe (worth the drive)."),
            ("Getting around", "Broadway is walkable. Uber/Lyft for anything beyond. Skip the scooters at night."),
            ("Tipping", "20% at restaurants. Tip the bands on Broadway ($5-10 per song request)."),
        ],
        "Savannah": [
            ("Open container", "Legal in the Historic District -but must be in a plastic cup (16oz max)."),
            ("Food", "River Street and City Market have the best restaurants. Try Mrs. Wilkes for family-style."),
            ("Squares", "Walk the 22 historic squares. Each one is different. Forsyth Park is the best."),
            ("Ghost tours", "Savannah is 'America's most haunted city'. Tours run nightly from City Market."),
        ],
        "Scottsdale": [
            ("Old Town", "Great restaurants and nightlife concentrated here. Walkable in the evening."),
            ("Heat", "In summer it's 110F+. Pool time is essential. Hike only before 8am."),
            ("Spa", "Scottsdale has world-class spas. Many offer day passes for $50-100."),
            ("Tipping", "20% at restaurants. Tip poolside servers $2-3 per drink."),
        ],
        "Orlando": [
            ("Theme parks", "Tickets are cheaper on reseller sites (Undercover Tourist, Park Savers)."),
            ("Food", "Eat at Disney Springs or Universal CityWalk -no park ticket needed."),
            ("Outlets", "Orlando Premium Outlets (Vineland) has the best deals. Go on weekdays."),
            ("Weather", "Afternoon thunderstorms daily in summer. They pass in 30 min -just wait it out."),
        ],
        "Tampa": [
            ("Ybor City", "Best nightlife and Cuban sandwiches. Columbia Restaurant is a must."),
            ("Bayshore", "Bayshore Blvd is the world's longest continuous sidewalk. Great for a walk or run."),
            ("Beaches", "Clearwater Beach is 30 min west. St. Pete Beach is less crowded and just as nice."),
            ("Cuban sandwich", "Tampa invented it. Try La Segunda (oldest Cuban bakery in the US)."),
        ],
        "Destin": [
            ("Seafood", "Harbor Boardwalk has the best restaurants. AJ's is the classic."),
            ("Crab Island", "Rent a pontoon boat and anchor at Crab Island -the locals' #1 thing to do."),
            ("Beaches", "Crystal Beach and Henderson Park are less crowded than the main strip."),
            ("Fishing", "Book a charter from Destin Harbor. It's called 'the world's luckiest fishing village'."),
        ],
        "CO": [
            ("Lunch deal", "Try a corrientazo (set lunch) at any local restaurant -full meal for ~10,000 COP ($2.50)."),
            ("Must-try", "Bandeja paisa is the national dish. Arepas for breakfast."),
            ("Coffee", "Colombia has the best coffee in the world. Order a tinto (black coffee) anywhere."),
            ("Bargaining", "Fine in markets and with taxi drivers. Not appropriate in restaurants or stores."),
        ],
        "Lisbon": [
            ("Pasteis", "Pasteis de nata from Manteigaria or Time Out Market. Skip Pasteis de Belem -same recipe, longer line."),
            ("Transit", "Get a Viva Viagem card at any metro station. Works on trams, buses, ferries, and metro."),
            ("Tram 28", "Iconic but packed with tourists and pickpockets. Take it early morning or skip for the 12E."),
            ("Food", "Lunch menus (menu do dia) at local tascas are 8-12 EUR for soup + main + drink."),
            ("Tipping", "Not expected. Round up or leave 5-10% for great service."),
        ],
        "Dublin": [
            ("Pubs", "Temple Bar is touristy and expensive. Locals drink in Stoneybatter, Portobello, or Wexford Street."),
            ("Guinness", "Must try at the source. Storehouse tour is good but any local pub pours it perfectly."),
            ("Getting around", "Leap Card works on all buses, trams (Luas), and DART trains. Taxis are expensive."),
            ("Weather", "Rain is constant but brief. Layers + waterproof jacket is the move. No umbrella needed."),
            ("Tipping", "10% at restaurants if service isn't included. Not expected in pubs."),
        ],
        "Madrid": [
            ("Meal times", "Lunch 2-4pm, dinner 9-11pm. Restaurants are empty before these times."),
            ("Tapas", "Free tapas with drinks in La Latina and Lavapies. Bar-hop -one tapa per bar."),
            ("Churros", "Chocolateria San Gines (open since 1894) for churros con chocolate. Go after midnight."),
            ("Metro", "10-trip Metrobus ticket saves money. Madrid metro is fast and covers everything."),
            ("Tipping", "Not expected. Round up the bill or leave small change."),
        ],
        "Geneva": [
            ("Fondue", "Cafe du Soleil in Petit-Saconnex serves the best fondue in Geneva. Reservation essential."),
            ("Lake", "Bains des Paquis is the local beach -swim in the lake, sauna in winter, fondue in their cafe."),
            ("Transit", "Get a TPG day pass (CHF 10) for unlimited trams, buses, and boats. Tap your card."),
            ("Sunday", "Almost everything is closed on Sundays. Stock up on Saturday. Gare Cornavin shops stay open."),
            ("Tipping", "Not expected -service is included. Round up the bill for good service."),
            ("Water", "Geneva tap water is excellent. The free fountains everywhere are safe to drink."),
        ],
        "Zürich": [
            ("Swimming", "Locals swim in the Limmat river and Lake Zürich in summer. Seebad Utoquai is the spot."),
            ("Transit", "ZVV day pass covers trains, trams, buses, boats. Buy at any machine or use SBB app."),
            ("Sunday", "Shops closed on Sunday. Main station (HB) and airport shops are the exception."),
            ("Tipping", "Not expected -service included. Round up for good service."),
            ("Cheap eats", "Coop and Migros hot food counters are the local hack for cheap meals (CHF 8-12)."),
        ],
        "CH": [
            ("Supermarkets", "Migros and Coop are the main chains. Denner and Aldi for budget shopping."),
            ("Transit", "Swiss public transport is world-class. SBB app for all trains, buses, and boats."),
            ("Sunday", "Most shops closed on Sunday. Gas station shops and train station shops are the exception."),
            ("Tipping", "Service is included in prices. Rounding up the bill is a nice gesture but not expected."),
            ("Water", "Tap water is safe and excellent everywhere. Fountains in cities are drinkable."),
        ],
        "PT": [
            ("Pastel de nata", "The national pastry -find it at any pastelaria. Best warm from the oven."),
            ("Transit", "Get a Viva Viagem card for metro/bus/tram. Costs EUR 0.50 and load rides on it."),
            ("Lunch menu", "Menu do dia at local tascas: soup + main + drink for EUR 8-12. Best value."),
            ("Tipping", "Not expected. Leave 5-10% for great service, or just round up."),
            ("Coffee", "Order a 'bica' (espresso) or 'meia de leite' (latte). Coffee culture is strong."),
        ],
        "ES": [
            ("Meal times", "Lunch 2-4pm, dinner 9-11pm. Restaurants are empty outside these hours."),
            ("Tapas", "Many bars serve free tapas with drinks. Bar-hop for variety -one tapa per stop."),
            ("Siesta", "Small shops close 2-5pm. Plan errands for morning or evening."),
            ("Tipping", "Not expected. Round up or leave small change at restaurants."),
        ],
        "FR": [
            ("Boulangerie", "Fresh bread twice daily. Go before 8am for the best croissants."),
            ("Lunch", "Formule/menu du jour at restaurants: starter + main or main + dessert for EUR 12-18."),
            ("Sunday", "Most shops closed. Boulangeries open Sunday morning. Markets are great on Sundays."),
            ("Tipping", "Service included (service compris). Leave small change for exceptional service."),
        ],
        "IT": [
            ("Coffee", "Stand at the bar for cheaper coffee. A cappuccino after 11am marks you as a tourist."),
            ("Coperto", "Cover charge (EUR 1-3) at restaurants is normal, not a scam."),
            ("Lunch", "Pranzo (lunch) menus are much cheaper than dinner. Eat your big meal at lunch."),
            ("Tipping", "Not expected. Round up the bill or leave EUR 1-2 for good service."),
        ],
        "DE": [
            ("Cash", "Germany is surprisingly cash-heavy. Many restaurants don't take cards. ATMs everywhere."),
            ("Pfand", "Bottle deposit system -return bottles at supermarkets for EUR 0.08-0.25 back."),
            ("Sunday", "Everything closed on Sunday except bakeries, gas stations, and restaurants."),
            ("Tipping", "5-10% at restaurants. Say the total you want to pay when handing cash."),
        ],
        "GB": [
            ("Pub culture", "Order at the bar, not at your table. No tipping at pubs."),
            ("Transit", "Get an Oyster card or use contactless for Tube/bus. Always tap in AND out."),
            ("Tipping", "10-12.5% at restaurants if service isn't included. Check the bill first."),
            ("Queuing", "The British queue for everything. Cutting the line is a serious social offense."),
        ],
        "AE": [
            ("Metro", "Dubai Metro is cheap (AED 3-7.5) and covers most tourist areas. Get a Nol card."),
            ("Dress code", "Cover shoulders and knees in malls and public areas. Beachwear only at the beach."),
            ("Tipping", "10-15% at restaurants. Round up taxi fares."),
            ("Alcohol", "Only at licensed restaurants, hotels, and bars. Not in public."),
            ("Friday", "Friday is the weekend. Friday brunch is a Dubai institution -book ahead."),
        ],
        "TH": [
            ("Street food", "Safe and delicious. Follow the crowds -busy stalls have the freshest food."),
            ("Temples", "Cover knees and shoulders. Remove shoes before entering. Don't point feet at Buddha."),
            ("Tipping", "Not expected but appreciated. Round up or leave 20-50 THB at restaurants."),
            ("Bargaining", "Expected at markets and tuk-tuks. Not at malls, 7-Elevens, or restaurants."),
        ],
        "JP": [
            ("Cash", "Japan is cash-heavy. 7-Eleven ATMs accept foreign cards. Carry cash always."),
            ("Tipping", "Never tip -it can be considered rude."),
            ("Transit", "Get a Suica/Pasmo IC card for trains, buses, and convenience store purchases."),
            ("Shoes", "Remove shoes when entering homes, temples, and many restaurants (look for shoe racks)."),
        ],
    }
    city_tips = local_tips.get(city, local_tips.get(country, []))

    # Generic fallback tips if no city/country match
    if not city_tips:
        city_tips = [
            ("Transit", f"Check local transit apps for {city}. Ride-hailing (Uber/Bolt/local apps) usually works."),
            ("Tipping", "Check local customs -tipping norms vary widely by country."),
            ("Cash vs card", "Carry some local cash. Not all small shops and restaurants accept cards."),
            ("Water", "Check if tap water is safe to drink. When in doubt, buy bottled."),
        ]

    # ── Helper: build a place table from a list of places ──
    def _place_table(places: list) -> str:
        rows = "\n".join(place_row(p) for p in places)
        return f'<table class="place-table">{rows}</table>'

    # ── Apartment details section (guest-relevant info only) ──
    apartment_details_html = ""
    detail_items = []
    # Only show property type if it's specific (not generic Airbnb labels)
    generic_types = {"rental unit", "entire home", "entire place", "room", "place"}
    if listing.property_type and listing.property_type.lower() not in generic_types:
        detail_items.append(f'<span class="apt-tag">{listing.property_type}</span>')
    if listing.bedrooms:
        detail_items.append(f'<span class="apt-tag">{listing.bedrooms} bedroom{"s" if listing.bedrooms > 1 else ""}</span>')
    if listing.bathrooms:
        detail_items.append(f'<span class="apt-tag">{listing.bathrooms} bathroom{"s" if listing.bathrooms > 1 else ""}</span>')
    if listing.guests:
        detail_items.append(f'<span class="apt-tag">Up to {listing.guests} guests</span>')

    # Only show real amenities. The Airbnb scrape sometimes leaks review-category
    # names (Cleanliness, Communication, Location, Check-in, etc.) into the
    # amenities list — filter those out so the welcome book doesn't read like
    # a review form.
    REVIEW_CATEGORIES = {
        "cleanliness", "communication", "location", "check-in", "checkin",
        "accuracy", "value", "noise", "sleep quality", "shared spaces",
        "getting around", "kitchen", "amenities",
    }
    amenity_tags = ""
    if listing.amenities:
        clean = [a for a in listing.amenities
                 if a and a.strip().lower() not in REVIEW_CATEGORIES]
        top_amenities = clean[:12]
        if top_amenities:
            amenity_tags = '<div class="amenity-list">' + \
                " ".join(f'<span class="amenity-tag">{a}</span>' for a in top_amenities) + \
                '</div>'

    if detail_items:
        apartment_details_html = f'''<div class="apartment-details">
            <h3 class="apt-heading">Your Apartment</h3>
            <div class="apt-tags">{"".join(detail_items)}</div>
            {amenity_tags}
        </div>'''

    # ── Section HTML builders (icon + label headers, no emojis) ──
    restaurants_html = ""
    if enriched.restaurant:
        restaurants_html = f'''<section class="section">
            <h2>{ICONS["eating"]}<span>Eating &amp; Drinking</span></h2>
            {_place_table(enriched.restaurant[:6])}
        </section>'''

    groceries_html = ""
    grocery_rows = enriched.grocery[:3]
    health_rows = enriched.health[:2]
    if grocery_rows or health_rows:
        grocery_tbl = _place_table(grocery_rows) if grocery_rows else ""
        health_tbl = f'<h3>Pharmacy / Health</h3>{_place_table(health_rows)}' if health_rows else ""
        groceries_html = f'''<section class="section">
            <h2>{ICONS["grocery"]}<span>Groceries &amp; Essentials</span></h2>
            {grocery_tbl}
            {health_tbl}
        </section>'''

    # Transit
    ride_info = ""
    taxi_tip = ""
    if country == "CO":
        ride_info = f"<strong>Taxi apps:</strong> Uber, InDriver, DiDi all work in {city}. Always confirm the fare."
        taxi_tip = "<p>Taxis are cheap but use apps -avoid hailing on the street at night.</p>"
    elif country == "US":
        ride_info = f"<strong>Ride apps:</strong> Uber and Lyft work everywhere in {city}."
        if city in ("Miami", "Orlando", "Tampa", "Destin", "Scottsdale", "Austin", "Nashville"):
            taxi_tip = "<p>You'll probably want a rental car. Public transit is limited outside downtown.</p>"
        else:
            taxi_tip = "<p>Downtown is walkable but a car is handy for exploring.</p>"
    elif country in ("AE", "SA", "BH", "QA", "KW", "OM"):
        ride_info = f"<strong>Taxi apps:</strong> Uber and Careem work in {city}."
    elif country in ("TH", "VN", "MY", "SG", "PH", "ID", "KH", "MM"):
        ride_info = f"<strong>Taxi apps:</strong> Grab and Bolt work in {city}."
    elif country == "JP":
        ride_info = f"<strong>Taxi apps:</strong> Uber and GO Taxi work in {city}. Taxis are safe and metered."
    elif country == "KR":
        ride_info = f"<strong>Taxi apps:</strong> KakaoT is the main ride app in {city}. Uber also works."
    elif country in ("BR",):
        ride_info = f"<strong>Taxi apps:</strong> Uber and 99 work in {city}."
    elif country in ("MX", "AR", "CL", "PE"):
        ride_info = f"<strong>Taxi apps:</strong> Uber and DiDi work in {city}."
    elif country in ("IN",):
        ride_info = f"<strong>Taxi apps:</strong> Uber and Ola work in {city}."
    elif country in ("AU", "NZ"):
        ride_info = f"<strong>Taxi apps:</strong> Uber and Didi work in {city}."
    elif country in ("TR",):
        ride_info = f"<strong>Taxi apps:</strong> Uber and BiTaksi work in {city}."
    elif country in ("CN",):
        ride_info = f"<strong>Taxi apps:</strong> DiDi is the main ride app in {city}. Uber does not operate here."
    elif country in ("RU",):
        ride_info = f"<strong>Taxi apps:</strong> Yandex Go is the main ride app in {city}."
    elif country in ("NG", "KE", "ZA", "GH", "TZ"):
        ride_info = f"<strong>Taxi apps:</strong> Uber and Bolt work in {city}."
    else:
        # Europe and all other countries -Uber + Bolt is the safest generic combo
        ride_info = f"<strong>Taxi apps:</strong> Uber and Bolt work in {city}. Check locally for other options."
    transit_tbl = _place_table(enriched.transit[:3]) if enriched.transit else ""
    transit_html = f'''<section class="section">
        <h2>{ICONS["transit"]}<span>Getting Around</span></h2>
        {transit_tbl}
        <div class="note">{ride_info}{taxi_tip}</div>
    </section>'''

    # Landmarks
    landmarks_html = ""
    if enriched.landmark:
        landmarks_html = f'''<section class="section">
            <h2>{ICONS["landmark"]}<span>Things to See &amp; Do</span></h2>
            {_place_table(enriched.landmark[:5])}
        </section>'''

    # Nightlife
    nightlife_html = ""
    if enriched.nightlife:
        nightlife_html = f'''<section class="section">
            <h2>{ICONS["nightlife"]}<span>Nightlife</span></h2>
            {_place_table(enriched.nightlife[:3])}
        </section>'''

    # Local tips section
    tips_html = ""
    if city_tips:
        tip_items = "\n".join(
            f'<div class="tip-item"><span class="tip-label">{label}</span><span class="tip-text">{text}</span></div>'
            for label, text in city_tips
        )
        tips_html = f'''<section class="section tips-section">
            <h2>{ICONS["tips"]}<span>Local Tips from {city}</span></h2>
            <div class="tips-grid">{tip_items}</div>
        </section>'''

    # Safety tips (plain text, no HTML entities)
    if city == "Medell\u00edn":
        safety_items = [
            "El Poblado and Laureles are very safe day and night",
            "Don't flash expensive phones/jewelry in crowded areas",
            "Use Uber/InDriver at night instead of walking",
            "Don't accept drinks from strangers (scopolamine risk)",
            "Keep a photocopy of your passport, leave the original at home",
        ]
    elif city == "Bogot\u00e1":
        safety_items = [
            "Chapinero, Zona Rosa, Usaquen are safe neighborhoods",
            "Avoid La Candelaria after dark unless in a group",
            "Use Uber/InDriver, not street taxis",
            "Don't walk with headphones in quiet streets",
            "Altitude (2,640m) -take it easy the first day, drink lots of water",
        ]
    elif country == "US":
        safety_items = [
            f"{neighborhood} is generally safe -use common sense",
            "Lock your car and don't leave valuables visible",
            "Use Uber/Lyft late at night, especially after drinking",
        ]
        if city == "Miami":
            safety_items += ["Avoid walking in Overtown or Liberty City",
                             "Beach safety: watch for rip currents, red flags mean no swimming"]
        elif city == "Nashville":
            safety_items += ["Lower Broadway is safe but very crowded on weekends",
                             "Avoid walking alone in North Nashville late at night"]
        elif city == "Austin":
            safety_items += ["6th Street gets rowdy late on weekends -try Rainey or East Austin",
                             "Drink lots of water -Texas heat is serious"]
        elif city == "Scottsdale":
            safety_items += ["Hydrate constantly in summer -heat stroke is a real risk",
                             "Watch for rattlesnakes on desert hiking trails"]
        elif city in ("Orlando", "Tampa", "Destin"):
            safety_items += ["Afternoon thunderstorms are daily in summer -they pass quickly",
                             "Use sunscreen -Florida sun is stronger than you think"]
        safety_items.append("Dial 911 for any emergency")
    elif city == "Lisbon":
        safety_items = [
            "Lisbon is very safe -one of the safest capitals in Europe",
            "Watch for pickpockets on Tram 28 and in Baixa/Rossio",
            "Cobblestone streets are slippery -wear flat shoes",
            "Dial 112 for any emergency",
        ]
    elif city == "Dublin":
        safety_items = [
            "Dublin is generally safe -use common sense at night",
            "Avoid walking alone through Phoenix Park after dark",
            "O'Connell Street can be rowdy late on weekends",
            "Dial 112 or 999 for any emergency",
        ]
    elif city == "Madrid":
        safety_items = [
            "Madrid is very safe -locals are out until 2-3am regularly",
            "Watch for pickpockets on Gran Via and in the metro",
            "Avoid Lavapies late at night if alone",
            "Dial 112 for any emergency",
        ]
    elif country == "CH":
        safety_items = [
            f"{city} is extremely safe -one of the safest cities in the world",
            "Pickpockets are rare but be aware at train stations and tourist spots",
            "Swiss police are helpful and most speak English",
            "Dial 112 for any emergency, 117 for police, 144 for ambulance",
        ]
    elif country in ("FR", "DE", "IT", "GB", "NL", "AT", "BE"):
        safety_items = [
            f"{city} is generally very safe",
            "Watch for pickpockets in tourist areas and on public transport",
            "Dial 112 for any emergency",
            "Keep a copy of your passport -leave the original at your accommodation",
        ]
    elif country == "AE":
        safety_items = [
            f"{city} is extremely safe -very low crime rate",
            "Public displays of affection are frowned upon",
            "Photographing government buildings or people without consent is not allowed",
            "Dial 999 for police, 998 for ambulance",
        ]
    elif country in ("TH", "JP", "SG", "KR"):
        safety_items = [
            f"{city} is very safe, even late at night",
            "Respect local customs and dress codes at temples",
            "Keep valuables secure on public transport",
        ]
    else:
        safety_items = [
            "Use ride-hailing apps at night",
            "Keep valuables out of sight",
            "Ask your host about areas to avoid",
        ]
    safety_list = "\n".join(f"<li>{s}</li>" for s in safety_items)

    # Useful info table (clean text)
    if country == "US":
        info_rows = [
            ("Emergency", "911 (police, fire, ambulance)"),
            ("Tipping", "18-20% at restaurants, $1-2 per drink at bars"),
            ("Tax", "Prices don't include sales tax (6-10% added at checkout)"),
            ("Water", "Tap water is safe everywhere"),
            ("SIM card", "T-Mobile, AT&T, or Mint Mobile at any Walmart/Target"),
        ]
    else:
        info_rows = [("Emergency", "123 (police), 125 (fire), 132 (ambulance)")]
    if country == "CO":
        info_rows += [
            ("Currency", "Colombian Peso (COP). ~4,000 COP = $1 USD"),
            ("Tipping", "10% at restaurants (often included as propina voluntaria)"),
            ("Language", "Spanish. English is limited outside tourist areas"),
            ("Water", "Tap water is safe in Medellin and Bogota"),
            ("SIM card", "Claro or Movistar. Buy at any Exito supermarket"),
        ]
    elif country == "CH":
        info_rows = [
            ("Emergency", "112 (general), 117 (police), 144 (ambulance), 118 (fire)"),
            ("Currency", "Swiss Franc (CHF). Cards accepted almost everywhere."),
            ("Tipping", "Service is included. Rounding up is a nice gesture."),
            ("Language", "Varies by region -French (Geneva), German (Zurich), Italian (Ticino). English widely spoken."),
            ("Water", "Tap water is excellent. Public fountains are all drinkable."),
            ("SIM card", "Swisscom, Sunrise, or Salt. Buy at any train station or electronics store."),
        ]
    elif country in ("PT", "IE", "ES", "FR", "IT", "DE", "NL", "AT", "BE", "GB"):
        info_rows = [
            ("Emergency", "112 (all services)" if country != "GB" else "999 or 112"),
            ("Currency", "British Pound (GBP)" if country == "GB" else "Euro (EUR)"),
            ("Tipping", "Not expected. Round up or 5-10% for great service."),
            ("Water", "Tap water is safe everywhere"),
        ]
        if country == "PT":
            info_rows.append(("Language", "Portuguese. English widely spoken in tourist areas."))
            info_rows.append(("SIM card", "Vodafone or NOS. Buy at any airport or electronics store."))
        elif country == "IE":
            info_rows.append(("Language", "English"))
            info_rows.append(("SIM card", "Three or Vodafone. Available at any Tesco or newsagent."))
        elif country == "ES":
            info_rows.append(("Language", "Spanish. English spoken in tourist areas, limited elsewhere."))
            info_rows.append(("SIM card", "Vodafone or Orange. Buy at any phone shop or El Corte Ingles."))
        elif country == "FR":
            info_rows.append(("Language", "French. English spoken in tourist areas and hotels."))
            info_rows.append(("SIM card", "Free Mobile, Orange, or SFR. Buy at any tabac or phone shop."))
        elif country == "IT":
            info_rows.append(("Language", "Italian. English varies -better in tourist areas."))
            info_rows.append(("SIM card", "TIM, Vodafone, or Wind. Buy at any tabacchi."))
        elif country == "DE":
            info_rows.append(("Language", "German. English widely spoken in cities."))
            info_rows.append(("SIM card", "Aldi Talk or Lidl Connect -cheapest. Buy at any supermarket."))
        elif country == "GB":
            info_rows.append(("Language", "English"))
            info_rows.append(("SIM card", "Three, EE, or Giffgaff. Buy at any convenience store."))
    info_table = "\n".join(f'<tr><td class="info-label">{k}</td><td>{v}</td></tr>' for k, v in info_rows)

    # Render Claude narrative (if any) to a styled HTML block. Strip any leading
    # H1 — the page already has its own hero header.
    narrative_html = ""
    if narrative_md and narrative_md.strip():
        cleaned = re.sub(r"^\s*#\s+.+?\n", "", narrative_md, count=1)
        try:
            import markdown as _md
            body = _md.markdown(cleaned, extensions=["tables", "fenced_code"])
        except ImportError:
            body = "<p>" + cleaned.replace("\n\n", "</p><p>") + "</p>"
        narrative_html = f'''<section class="section narrative">
            <h2 class="narrative-heading">{narrative_icon}<span>From Your Host</span></h2>
            <div class="narrative-body">{body}</div>
        </section>'''

    # Map section
    map_html = ""
    if lat and lng and lat != 0:
        gmaps_link = f"https://www.google.com/maps/@{lat},{lng},16z"
        map_html = f'''<section class="section map-section">
            <h2>Your Location</h2>
            <div class="map-container">
                <a href="{gmaps_link}" target="_blank" style="display: block;">
                    <iframe
                        width="100%" height="260" frameborder="0" scrolling="no"
                        src="https://www.openstreetmap.org/export/embed.html?bbox={lng-0.006}%2C{lat-0.004}%2C{lng+0.006}%2C{lat+0.004}&amp;layer=mapnik&amp;marker={lat}%2C{lng}"
                        style="border-radius: 8px; pointer-events: none;">
                    </iframe>
                </a>
                <p class="map-link"><a href="{gmaps_link}" target="_blank">Open in Google Maps</a></p>
            </div>
            <p class="print-only map-coords">GPS: {lat}, {lng} -search in Google Maps or scan QR code on back page</p>
        </section>'''

    # ── City color themes (primary, dark, light bg) ──
    _themes = {
        "Miami": ("#00897B", "#00695C", "#E0F2F1"),
        "Austin": ("#E65100", "#BF360C", "#FBE9E7"),
        "Nashville": ("#1565C0", "#0D47A1", "#E3F2FD"),
        "Savannah": ("#2E7D32", "#1B5E20", "#E8F5E9"),
        "Scottsdale": ("#F57F17", "#E65100", "#FFF8E1"),
        "Orlando": ("#7B1FA2", "#4A148C", "#F3E5F5"),
        "Tampa": ("#00838F", "#006064", "#E0F7FA"),
        "Destin": ("#0277BD", "#01579B", "#E1F5FE"),
        "Medellin": ("#D84315", "#BF360C", "#FBE9E7"),
        "Bogota": ("#4E342E", "#3E2723", "#EFEBE9"),
        "Lisbon": ("#1565C0", "#0D47A1", "#E3F2FD"),
        "Dublin": ("#2E7D32", "#1B5E20", "#E8F5E9"),
        "Madrid": ("#C62828", "#B71C1C", "#FFEBEE"),
    }
    primary, dark, bg = _themes.get(city, ("#37474F", "#263238", "#ECEFF1"))

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guest Guide -{neighborhood}, {city}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Playfair+Display:wght@600;700&display=swap" rel="stylesheet">
<style>
:root {{
    --primary: {primary};
    --primary-dark: {dark};
    --primary-bg: {bg};
    --text: #1a1a1a;
    --text-secondary: #555;
    --border: #e0e0e0;
    --surface: #f8f8f8;
    --white: #fff;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    color: var(--text);
    background: #f0f0f0;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
}}

/* ── Page container ── */
.page {{
    max-width: 700px;
    margin: 0 auto;
    background: var(--white);
}}

/* ── Hero ── */
.hero {{
    background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
    color: white;
    padding: 0;
    position: relative;
    min-height: 220px;
    display: flex;
    align-items: flex-end;
}}
.hero.hero-with-photo {{
    background-size: cover;
    background-position: center;
    min-height: 280px;
}}
.hero-overlay {{
    padding: 44px 36px 32px;
    width: 100%;
}}
.hero h1 {{
    font-family: 'Playfair Display', Georgia, serif;
    font-size: 34px;
    font-weight: 700;
    line-height: 1.15;
    margin-bottom: 8px;
    letter-spacing: -0.5px;
}}
.hero-sub {{
    font-size: 16px;
    opacity: 0.92;
    letter-spacing: 0.3px;
}}
.hero-host {{
    margin-top: 16px;
    font-size: 13px;
    opacity: 0.82;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    font-weight: 500;
}}

/* ── Static map banner ── */
.map-banner {{
    line-height: 0;
    border-bottom: 2px solid var(--primary);
    background: #f0f0f0;
}}
.map-banner img {{
    display: block;
    width: 100%;
    height: auto;
    max-height: 260px;
    object-fit: cover;
}}

/* ── Section header icons ── */
.sec-icon {{
    width: 22px;
    height: 22px;
    color: var(--primary);
    flex-shrink: 0;
}}
.section h2 {{
    display: flex;
    align-items: center;
    gap: 10px;
}}
.section h2 span {{ display: inline-block; }}
.narrative-heading {{
    color: var(--primary-dark);
    margin-bottom: 14px !important;
    border-bottom: none !important;
    padding-bottom: 0 !important;
}}
.narrative-heading .sec-icon {{ color: var(--primary-dark); }}

/* ── Distance badge color coding (near = teal, mid = amber, far = gray) ── */
.dist-badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.2px;
    background: #eceff1;
    color: #455a64;
}}
.dist-badge.near {{ background: #b2dfdb; color: #00695c; }}
.dist-badge.mid {{ background: #ffe0b2; color: #bf6b00; }}
.dist-badge.far {{ background: #cfd8dc; color: #455a64; }}

/* ── Essentials card (WiFi, address, emergency) ── */
.essentials {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1px;
    background: var(--border);
    border-bottom: 2px solid var(--primary);
}}
.apartment-details {{
    padding: 20px 24px;
    background: #f0faf9;
    border-bottom: 1px solid var(--border);
}}
.apt-heading {{
    font-size: 14px;
    font-weight: 700;
    color: var(--primary);
    margin: 0 0 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}
.apt-tags {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 8px;
}}
.apt-tag {{
    display: inline-block;
    padding: 4px 12px;
    background: white;
    border: 1px solid #b2dfdb;
    border-radius: 20px;
    font-size: 13px;
    color: #004d40;
    font-weight: 500;
}}
.amenity-list {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
}}
.amenity-tag {{
    display: inline-block;
    padding: 3px 10px;
    background: white;
    border: 1px solid #e0e0e0;
    border-radius: 12px;
    font-size: 11px;
    color: #555;
}}
.essentials .cell {{
    background: var(--white);
    padding: 16px 20px;
}}
.essentials .cell.full {{
    grid-column: 1 / -1;
}}
.essentials .label {{
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--primary);
    margin-bottom: 4px;
}}
.essentials .value {{
    font-size: 15px;
    font-weight: 500;
    color: var(--text);
}}
.essentials .blank {{
    font-size: 14px;
    color: #999;
    border-bottom: 1.5px dashed #ccc;
    display: inline-block;
    min-width: 180px;
    height: 22px;
}}

/* ── Content wrapper ── */
.content {{
    padding: 28px 36px 40px;
}}

/* ── Sections ── */
.section {{
    margin-bottom: 28px;
}}
.section h2 {{
    font-size: 18px;
    font-weight: 700;
    color: var(--text);
    padding-bottom: 8px;
    border-bottom: 2px solid var(--border);
    margin-bottom: 12px;
}}
.section h3 {{
    font-size: 14px;
    font-weight: 600;
    color: var(--text-secondary);
    margin: 14px 0 6px;
}}

/* ── Narrative (Claude-generated voice block) ── */
.narrative {{
    background: #fafbfb;
    border-left: 3px solid var(--primary);
    padding: 22px 26px;
    margin-bottom: 28px;
    border-radius: 4px;
}}
.narrative-body h2 {{
    font-family: 'Playfair Display', Georgia, serif;
    font-size: 19px;
    margin: 22px 0 8px;
    color: var(--primary-dark);
    border-bottom: none;
    padding-bottom: 0;
}}
.narrative-body h2:first-child {{ margin-top: 0; }}
.narrative-body p {{ margin: 0 0 12px; line-height: 1.65; color: var(--text); }}
.narrative-body ul {{ padding-left: 20px; margin: 8px 0 14px; }}
.narrative-body li {{ margin-bottom: 4px; }}
.narrative-body strong {{ color: var(--primary-dark); }}
.narrative-body em {{ color: var(--text-secondary); }}

/* ── Place tables ── */
.place-table {{
    width: 100%;
    border-collapse: collapse;
}}
.place-row td {{
    padding: 9px 0;
    border-bottom: 1px solid #f0f0f0;
    font-size: 14px;
    vertical-align: top;
}}
.place-row:last-child td {{
    border-bottom: none;
}}
.place-name {{
    font-weight: 600;
    white-space: nowrap;
    padding-right: 16px;
    width: 40%;
}}
.place-name a.place-link {{
    color: var(--text);
    text-decoration: none;
}}
.place-name a.place-link:hover {{
    color: var(--primary);
}}
.place-detail {{
    color: var(--text-secondary);
}}
.place-addr {{
    font-size: 13px;
}}
.place-dist {{
    font-size: 13px;
    color: #888;
}}
.rating {{
    font-size: 12px;
    color: #e6a117;
    margin-left: 6px;
    font-weight: 500;
}}

/* ── Notes and tips ── */
.note {{
    background: var(--surface);
    border-radius: 6px;
    padding: 14px 18px;
    margin-top: 10px;
    font-size: 14px;
    color: var(--text-secondary);
    line-height: 1.6;
}}
.note p {{ margin-top: 6px; }}
/* ── Local tips section ── */
.tips-section {{
    background: var(--primary-bg);
    margin-left: -36px;
    margin-right: -36px;
    padding: 24px 36px;
}}
.tips-section h2 {{
    border-bottom-color: var(--primary);
    opacity: 0.9;
}}
.tips-grid {{
    display: flex;
    flex-direction: column;
    gap: 0;
}}
.tip-item {{
    display: flex;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid rgba(0,0,0,0.06);
    font-size: 14px;
    line-height: 1.5;
}}
.tip-item:last-child {{ border-bottom: none; }}
.tip-label {{
    font-weight: 700;
    color: var(--primary-dark);
    min-width: 90px;
    flex-shrink: 0;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    padding-top: 1px;
}}

/* ── Safety list ── */
.safety-list {{
    list-style: none;
    padding: 0;
}}
.safety-list li {{
    padding: 7px 0 7px 24px;
    font-size: 14px;
    color: var(--text);
    border-bottom: 1px solid #f0f0f0;
    position: relative;
}}
.safety-list li::before {{
    content: '';
    position: absolute;
    left: 0;
    top: 12px;
    width: 8px;
    height: 8px;
    background: var(--primary);
    border-radius: 50%;
    opacity: 0.7;
}}
.safety-list li:last-child {{ border-bottom: none; }}

/* ── Info table ── */
.info-table {{
    width: 100%;
    border-collapse: collapse;
}}
.info-table td {{
    padding: 10px 12px;
    font-size: 14px;
    border-bottom: 1px solid #f0f0f0;
    vertical-align: top;
}}
.info-table tr:last-child td {{ border-bottom: none; }}
.info-label {{
    font-weight: 600;
    color: var(--text);
    white-space: nowrap;
    width: 90px;
}}

/* ── Map ── */
.map-container {{
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid var(--border);
}}
.map-link {{
    text-align: center;
    margin-top: 6px;
    font-size: 13px;
}}
.map-link a {{
    color: var(--primary);
    text-decoration: none;
    font-weight: 500;
}}

/* ── Footer ── */
.footer {{
    text-align: center;
    padding: 20px 36px 28px;
    border-top: 1px solid var(--border);
    font-size: 12px;
    color: #999;
}}

/* ── Print-only elements ── */
.print-only {{ display: none; }}

/* ── Mobile ── */
@media (max-width: 600px) {{
    .hero {{ padding: 32px 20px 28px; }}
    .hero h1 {{ font-size: 26px; }}
    .content {{ padding: 20px 20px 32px; }}
    .essentials {{ grid-template-columns: 1fr; }}
}}

/* ── Print / PDF stylesheet ── */
@media print {{
    body {{ background: white; }}
    .page {{ max-width: none; box-shadow: none; }}
    .map-container {{ display: none; }}
    .map-link {{ display: none; }}
    .map-section {{ display: none; }}
    .print-only {{ display: block; }}
    .section {{ page-break-inside: avoid; }}
    .hero {{
        -webkit-print-color-adjust: exact !important;
        print-color-adjust: exact !important;
        background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%) !important;
        color: white !important;
    }}
    .essentials .label {{ color: var(--primary) !important; -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
    .tips-section {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; background: var(--primary-bg) !important; }}
    .safety-list li::before {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; background: var(--primary) !important; }}
    a {{ color: inherit; text-decoration: none; }}
    a.place-link::after {{ content: none; }}
}}
</style>
</head>
<body>

<div class="page">

    <!-- Hero -->
    <div class="hero {hero_photo_class}" style="{hero_style}">
        <div class="hero-overlay">
            <h1>Your Guide to {neighborhood}</h1>
            <p class="hero-sub">{city} &middot; prepared by your host</p>
            <p class="hero-host">Hosted by {host}</p>
        </div>
    </div>

    {map_banner_html}

    <!-- Essentials (above the fold, print-critical) -->
    <div class="essentials">
        <div class="cell">
            <div class="label">WiFi Network</div>
            <div class="blank"></div>
        </div>
        <div class="cell">
            <div class="label">WiFi Password</div>
            <div class="blank"></div>
        </div>
        <div class="cell">
            <div class="label">Property Address</div>
            <div class="value">{neighborhood}, {city}</div>
        </div>
        <div class="cell">
            <div class="label">Emergency</div>
            <div class="value">{emergency_text}</div>
        </div>
        <div class="cell">
            <div class="label">Check-in / Check-out</div>
            <div class="blank"></div>
        </div>
        <div class="cell">
            <div class="label">Host Contact</div>
            <div class="blank"></div>
        </div>
    </div>

    <!-- Apartment details (if available) -->
    {apartment_details_html}

    <!-- Main content -->
    <div class="content">

        {narrative_html}
        {map_html}
        {transit_html}
        {restaurants_html}
        {tips_html}
        {groceries_html}
        {landmarks_html}
        {nightlife_html}

        <!-- Safety -->
        <section class="section">
            <h2>{safety_icon}<span>Safety Tips</span></h2>
            <ul class="safety-list">
                {safety_list}
            </ul>
        </section>

        <!-- Useful Info -->
        <section class="section">
            <h2>{info_icon}<span>Useful Info</span></h2>
            <table class="info-table">
                {info_table}
            </table>
        </section>

    </div>

    <!-- Footer -->
    <div class="footer">
        Guide for {host}'s guests &middot; {today} &middot; Powered by HostGuide
    </div>

</div>

</body>
</html>'''


def _md_to_html(md_content: str) -> str:
    """Legacy fallback: convert markdown to basic styled HTML."""
    try:
        import markdown
        body = markdown.markdown(md_content, extensions=["tables", "fenced_code"])
    except ImportError:
        body = md_content.replace("\n", "<br>")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guest Guide</title>
<style>
  body {{ font-family: 'Inter', system-ui, sans-serif; max-width: 700px; margin: 40px auto; padding: 0 20px; color: #333; line-height: 1.6; }}
  h1 {{ color: #FF5A5F; border-bottom: 2px solid #FF5A5F; padding-bottom: 8px; }}
  h2 {{ color: #484848; margin-top: 28px; }}
  ul {{ padding-left: 20px; }}
  li {{ margin-bottom: 6px; }}
  strong {{ color: #484848; }}
  table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
  td, th {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
  hr {{ border: none; border-top: 1px solid #eee; margin: 30px 0; }}
  em {{ color: #767676; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


def generate_guide(listing: Listing, enriched: EnrichedLocation,
                   city_config: dict, use_claude: bool = True) -> GuestGuide:
    """Generate a complete guest guide."""
    md_content = ""
    claude_md = ""  # only inject the Claude voice — never the bland template — into the HTML

    if use_claude and os.environ.get("ANTHROPIC_API_KEY"):
        claude_md = _generate_with_claude(listing, enriched, city_config)
        md_content = claude_md

    if not md_content:
        md_content = _generate_template(listing, enriched, city_config)

    # HTML: structured place data + (when available) Claude narrative on top.
    html_content = _build_html_guide(listing, enriched, city_config, narrative_md=claude_md)

    return GuestGuide(
        listing_id=listing.listing_id,
        listing_title=listing.title,
        host_name=listing.host_name,
        city=listing.city,
        neighborhood=listing.neighborhood,
        generated_date=date.today().isoformat(),
        content_md=md_content,
        content_html=html_content,
    )


def save_guide(guide: GuestGuide, output_dir: str = "output"):
    """Save guide as both .md and .html files."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    slug = f"{guide.city}_{guide.listing_id}"
    md_path = out / f"{slug}_guide.md"
    html_path = out / f"{slug}_guide.html"

    md_path.write_text(guide.content_md, encoding="utf-8")
    html_path.write_text(guide.content_html, encoding="utf-8")

    print(f"  Saved: {md_path}")
    print(f"  Saved: {html_path}")
    return str(md_path), str(html_path)
