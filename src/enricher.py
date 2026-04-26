"""Places enricher — adds nearby transit, grocery, restaurants, landmarks to each listing.

Uses Google Maps Places API (Nearby Search).
Requires: GOOGLE_MAPS_API_KEY env var.

Usage:
    from hostguide.src.enricher import enrich_listing
    places = enrich_listing(lat=6.21, lng=-75.57, city_config=config)
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests

GOOGLE_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
FOURSQUARE_API_KEY = os.environ.get("FOURSQUARE_API_KEY", "")
PLACES_NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
PLACE_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
FOURSQUARE_SEARCH_URL = "https://api.foursquare.com/v3/places/search"


@dataclass
class Place:
    """A nearby place."""
    name: str
    type: str  # transit, grocery, restaurant, landmark, nightlife, health
    category: str  # specific google type
    lat: float
    lng: float
    distance_m: int  # meters from listing
    walking_min: int  # estimated walking time
    rating: float = 0.0
    total_ratings: int = 0
    address: str = ""
    price_level: int = 0  # 0-4
    open_now: Optional[bool] = None


@dataclass
class EnrichedLocation:
    """All nearby places for a listing."""
    lat: float
    lng: float
    transit: list[Place] = field(default_factory=list)
    grocery: list[Place] = field(default_factory=list)
    restaurant: list[Place] = field(default_factory=list)
    landmark: list[Place] = field(default_factory=list)
    nightlife: list[Place] = field(default_factory=list)
    health: list[Place] = field(default_factory=list)


def _haversine_m(lat1, lon1, lat2, lon2) -> int:
    """Distance in meters between two coordinates."""
    from math import radians, cos, sin, asin, sqrt
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return int(2 * 6371000 * asin(sqrt(a)))


def _walking_minutes(distance_m: int) -> int:
    """Estimate walking time at 5 km/h."""
    return max(1, round(distance_m / 83))  # 83 m/min ≈ 5 km/h


def _driving_minutes(distance_m: int) -> int:
    """Estimate driving time at ~30 km/h average (city driving)."""
    return max(1, round(distance_m / 500))  # 500 m/min ≈ 30 km/h


def _search_nearby(lat: float, lng: float, place_types: list[str],
                   radius: int = 1000, max_results: int = 5) -> list[dict]:
    """Call Google Places Nearby Search."""
    if not GOOGLE_API_KEY:
        return []

    results = []
    for ptype in place_types:
        params = {
            "location": f"{lat},{lng}",
            "radius": radius,
            "type": ptype,
            "key": GOOGLE_API_KEY,
            "language": "en",
        }
        try:
            resp = requests.get(PLACES_NEARBY_URL, params=params, timeout=10)
            data = resp.json()
            if data.get("status") == "OK":
                for place in data.get("results", [])[:max_results]:
                    loc = place.get("geometry", {}).get("location", {})
                    results.append({
                        "name": place.get("name", ""),
                        "category": ptype,
                        "lat": loc.get("lat", 0),
                        "lng": loc.get("lng", 0),
                        "rating": place.get("rating", 0),
                        "total_ratings": place.get("user_ratings_total", 0),
                        "address": place.get("vicinity", ""),
                        "price_level": place.get("price_level", 0),
                        "open_now": place.get("opening_hours", {}).get("open_now"),
                    })
            time.sleep(0.2)  # API rate limit
        except Exception:
            continue

    return results


def enrich_listing(lat: float, lng: float, city_config: dict) -> EnrichedLocation:
    """Enrich a listing location with all nearby places."""
    enriched = EnrichedLocation(lat=lat, lng=lng)
    places_types = city_config.get("places_types", {})

    for category, google_types in places_types.items():
        raw_places = _search_nearby(lat, lng, google_types,
                                     radius=1000 if category != "transit" else 1500,
                                     max_results=5)
        places = []
        for p in raw_places:
            dist = _haversine_m(lat, lng, p["lat"], p["lng"])
            places.append(Place(
                name=p["name"],
                type=category,
                category=p["category"],
                lat=p["lat"],
                lng=p["lng"],
                distance_m=dist,
                walking_min=_walking_minutes(dist),
                rating=p["rating"],
                total_ratings=p["total_ratings"],
                address=p["address"],
                price_level=p["price_level"],
                open_now=p["open_now"],
            ))

        # Sort by distance, keep top 5
        places.sort(key=lambda x: x.distance_m)
        setattr(enriched, category, places[:5])

    return enriched


def _foursquare_search(lat: float, lng: float, categories: list[str],
                       radius: int = 1000, limit: int = 5) -> list[dict]:
    """Search Foursquare Places API (free tier: 500 calls/day).

    Category IDs: https://docs.foursquare.com/data-products/docs/categories
    """
    if not FOURSQUARE_API_KEY:
        return []

    results = []
    for cat_id in categories:
        try:
            resp = requests.get(
                FOURSQUARE_SEARCH_URL,
                headers={
                    "Authorization": FOURSQUARE_API_KEY,
                    "Accept": "application/json",
                },
                params={
                    "ll": f"{lat},{lng}",
                    "radius": radius,
                    "categories": cat_id,
                    "limit": limit,
                    "sort": "DISTANCE",
                },
                timeout=10,
            )
            data = resp.json()
            for place in data.get("results", []):
                geo = place.get("geocodes", {}).get("main", {})
                results.append({
                    "name": place.get("name", ""),
                    "category": cat_id,
                    "lat": geo.get("latitude", 0),
                    "lng": geo.get("longitude", 0),
                    "address": place.get("location", {}).get("formatted_address", ""),
                })
            time.sleep(0.3)
        except Exception:
            continue
    return results


# Foursquare category IDs for our enrichment types
FOURSQUARE_CATEGORIES = {
    "transit": ["19042"],          # Bus Station, Train Station
    "grocery": ["17069", "17070"], # Grocery Store, Supermarket
    "restaurant": ["13065"],       # Restaurant
    "landmark": ["16000"],         # Landmarks & Outdoors
    "nightlife": ["10032"],        # Bar, Night Club
    "health": ["15014", "15026"], # Pharmacy, Hospital
}


def enrich_with_foursquare(lat: float, lng: float, city_config: dict) -> EnrichedLocation:
    """Enrich using Foursquare Places API (free tier, better restaurant coverage than OSM)."""
    enriched = EnrichedLocation(lat=lat, lng=lng)

    for category, fsq_cats in FOURSQUARE_CATEGORIES.items():
        radius = 1500 if category == "transit" else 1000
        raw_places = _foursquare_search(lat, lng, fsq_cats, radius=radius, limit=5)
        places = []
        for p in raw_places:
            if not p["name"]:
                continue
            dist = _haversine_m(lat, lng, p["lat"], p["lng"])
            places.append(Place(
                name=p["name"],
                type=category,
                category=p["category"],
                lat=p["lat"],
                lng=p["lng"],
                distance_m=dist,
                walking_min=_walking_minutes(dist),
                address=p["address"],
            ))
        places.sort(key=lambda x: x.distance_m)
        setattr(enriched, category, places[:5])

    return enriched


def _merge_enriched(primary: EnrichedLocation, secondary: EnrichedLocation) -> EnrichedLocation:
    """Merge two enrichment results, filling gaps in primary with secondary data."""
    for cat in ["transit", "grocery", "restaurant", "landmark", "nightlife", "health"]:
        primary_places = getattr(primary, cat, [])
        secondary_places = getattr(secondary, cat, [])
        if not primary_places and secondary_places:
            setattr(primary, cat, secondary_places)
        elif primary_places and secondary_places:
            # Supplement: add unique places from secondary up to 5 total
            existing_names = {p.name.lower() for p in primary_places}
            for sp in secondary_places:
                if sp.name.lower() not in existing_names and len(primary_places) < 5:
                    primary_places.append(sp)
                    existing_names.add(sp.name.lower())
            primary_places.sort(key=lambda x: x.distance_m)
            setattr(primary, cat, primary_places[:5])
    return primary


def enrich_activities(city_name: str, max_results: int = 5) -> list[Place]:
    """Fetch top attractions and activities for a city from Wikivoyage.

    Uses Wikivoyage MediaWiki API (free, no key needed, server-rendered).
    Extracts from the 'See' and 'Do' sections of the city's travel guide.
    Returns Place objects with type='landmark' to fill Things to See & Do.
    """
    import re

    activities = []
    headers = {"User-Agent": "HostGuide/1.0 (hostguide@example.com) Python/3"}

    try:
        # Get section list for the city page
        resp = requests.get("https://en.wikivoyage.org/w/api.php", params={
            "action": "parse", "page": city_name, "prop": "sections", "format": "json",
        }, timeout=10, headers=headers)
        if resp.status_code != 200:
            return []

        sections = resp.json().get("parse", {}).get("sections", [])

        # Find "See" and "Do" sections and their subsections
        target_indices = []
        in_see_do = False
        see_do_level = 0
        for s in sections:
            line = s.get("line", "").lower()
            level = int(s.get("level", 0))
            if line in ("see", "do") and level == 2:
                in_see_do = True
                see_do_level = level
                target_indices.append(s["index"])
            elif in_see_do and level > see_do_level:
                target_indices.append(s["index"])
            elif in_see_do and level <= see_do_level:
                in_see_do = False

        # Extract attraction names from each section
        seen = set()
        for idx in target_indices[:6]:
            resp = requests.get("https://en.wikivoyage.org/w/api.php", params={
                "action": "parse", "page": city_name, "prop": "wikitext",
                "section": idx, "format": "json",
            }, timeout=10, headers=headers)
            wikitext = resp.json().get("parse", {}).get("wikitext", {}).get("*", "")

            # Extract bold names at start of bullet points: * '''Name'''
            names = re.findall(r"^\*\s*'''([^']+)'''", wikitext, re.MULTILINE)
            for name in names:
                n = name.strip()
                if n.lower() not in seen and len(n) > 3:
                    seen.add(n.lower())
                    activities.append(Place(
                        name=n,
                        type="landmark",
                        category="attraction",
                        lat=0, lng=0,
                        distance_m=0,
                        walking_min=0,
                        address="City highlight",
                    ))
                    if len(activities) >= max_results:
                        return activities
            time.sleep(0.3)

    except Exception:
        pass

    return activities


def enrich_without_api(lat: float, lng: float, city_config: dict) -> EnrichedLocation:
    """Fallback enrichment using OpenStreetMap Overpass API (free, no key needed).

    Uses wider search radius for US cities (car-oriented, suburban sprawl)
    and tighter radius for walkable European/Latin American cities.
    """
    enriched = EnrichedLocation(lat=lat, lng=lng)
    country = city_config.get("country", "")

    # US suburbs need much wider radius (driving distance) vs walkable cities
    if country == "US":
        r_restaurant, r_cafe = 8000, 6000
        r_grocery, r_conv = 8000, 4000
        r_transit, r_bus = 5000, 3000
        r_landmark, r_park = 15000, 8000
        r_nightlife, r_club = 8000, 10000
        r_pharmacy, r_hospital = 8000, 15000
        max_restaurant, max_places = 30, 15
    else:
        r_restaurant, r_cafe = 1500, 1200
        r_grocery, r_conv = 1500, 1000
        r_transit, r_bus = 2000, 1500
        r_landmark, r_park = 3500, 2500
        r_nightlife, r_club = 2000, 2500
        r_pharmacy, r_hospital = 1500, 3000
        max_restaurant, max_places = 25, 12

    # Use node + way (not nwr which includes relations and is too heavy)
    # way with 'out center' gives centroid coords for building-mapped places
    osm_mapping = {
        "transit": f'[out:json][timeout:15];(node["railway"="station"](around:{r_transit},{{lat}},{{lng}});node["railway"="halt"](around:{r_transit},{{lat}},{{lng}});node["railway"="subway_entrance"](around:{r_transit},{{lat}},{{lng}});node["station"="subway"](around:{r_transit},{{lat}},{{lng}});node["public_transport"="station"](around:{r_transit},{{lat}},{{lng}});node["public_transport"="stop_position"]["train"="yes"](around:{r_transit},{{lat}},{{lng}});node["highway"="bus_stop"](around:{r_bus},{{lat}},{{lng}});node["railway"="tram_stop"](around:{r_transit},{{lat}},{{lng}}););out body {max_places};',
        "grocery": f'[out:json][timeout:15];(node["shop"="supermarket"](around:{r_grocery},{{lat}},{{lng}});way["shop"="supermarket"](around:{r_grocery},{{lat}},{{lng}});node["shop"="convenience"](around:{r_conv},{{lat}},{{lng}});node["shop"="bakery"](around:{r_conv},{{lat}},{{lng}}););out body center {max_places};',
        "restaurant": f'[out:json][timeout:15];(node["amenity"="restaurant"](around:{r_restaurant},{{lat}},{{lng}});way["amenity"="restaurant"](around:{r_restaurant},{{lat}},{{lng}});node["amenity"="cafe"](around:{r_cafe},{{lat}},{{lng}});way["amenity"="cafe"](around:{r_cafe},{{lat}},{{lng}});node["amenity"="fast_food"](around:{r_restaurant},{{lat}},{{lng}}););out body center {max_restaurant};',
        "landmark": f'[out:json][timeout:15];(node["tourism"="attraction"](around:{r_landmark},{{lat}},{{lng}});node["tourism"="museum"](around:{r_landmark},{{lat}},{{lng}});way["leisure"="park"](around:{r_park},{{lat}},{{lng}});node["tourism"="viewpoint"](around:{r_landmark},{{lat}},{{lng}});node["tourism"="gallery"](around:{r_landmark},{{lat}},{{lng}}););out body center {max_places};',
        "nightlife": f'[out:json][timeout:15];(node["amenity"="bar"](around:{r_nightlife},{{lat}},{{lng}});node["amenity"="pub"](around:{r_nightlife},{{lat}},{{lng}});node["amenity"="nightclub"](around:{r_club},{{lat}},{{lng}}););out body {max_places};',
        "health": f'[out:json][timeout:15];(node["amenity"="pharmacy"](around:{r_pharmacy},{{lat}},{{lng}});node["amenity"="hospital"](around:{r_hospital},{{lat}},{{lng}});way["amenity"="hospital"](around:{r_hospital},{{lat}},{{lng}}););out body center 10;',
    }

    overpass_urls = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]

    for category, query_template in osm_mapping.items():
        query = query_template.format(lat=lat, lng=lng)
        places = []
        for attempt in range(3):
            try:
                url = overpass_urls[0] if attempt < 2 else overpass_urls[1]
                resp = requests.post(url, data={"data": query}, timeout=20)
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(3 * (attempt + 1))
                    continue
                data = resp.json()
                for elem in data.get("elements", []):
                    tags = elem.get("tags", {})
                    name = tags.get("name", "")
                    if not name:
                        continue
                    # way elements have center coords from 'out body center'
                    plat = elem.get("lat") or (elem.get("center", {}).get("lat", 0))
                    plng = elem.get("lon") or (elem.get("center", {}).get("lon", 0))
                    dist = _haversine_m(lat, lng, plat, plng)
                    # Build full address: "123 Main Street"
                    street = tags.get("addr:street", "")
                    house = tags.get("addr:housenumber", "")
                    addr = f"{house} {street}".strip() if street else ""
                    places.append(Place(
                        name=name,
                        type=category,
                        category=list(tags.keys())[0] if tags else "",
                        lat=plat,
                        lng=plng,
                        distance_m=dist,
                        walking_min=_walking_minutes(dist),
                        address=addr,
                    ))
                break  # success
            except Exception:
                time.sleep(2 * (attempt + 1))
        places.sort(key=lambda x: x.distance_m)
        setattr(enriched, category, places[:8])
        time.sleep(4)  # Overpass per-IP throttling — keep generous

    # Adaptive retry: if any core category is thin, widen the radius. This used
    # to be US-only and used a non-existent `overpass_url` variable, so it was
    # silently failing. Now applies to all countries.
    if country == "US":
        # US: very wide retry (driving distance)
        retry_specs = [
            ("restaurant", 3, '[out:json][timeout:20];(node["amenity"="restaurant"](around:20000,{lat},{lng});node["amenity"="cafe"](around:15000,{lat},{lng});node["amenity"="fast_food"](around:10000,{lat},{lng}););out body 30;'),
            ("grocery", 2, '[out:json][timeout:20];(node["shop"="supermarket"](around:20000,{lat},{lng});node["shop"="convenience"](around:10000,{lat},{lng}););out body 15;'),
            ("health", 1, '[out:json][timeout:20];(node["amenity"="pharmacy"](around:20000,{lat},{lng});node["amenity"="hospital"](around:20000,{lat},{lng}););out body 10;'),
            ("transit", 1, '[out:json][timeout:20];(node["railway"="station"](around:25000,{lat},{lng});node["public_transport"="station"](around:25000,{lat},{lng}););out body 10;'),
            ("landmark", 2, '[out:json][timeout:20];(node["tourism"="attraction"](around:25000,{lat},{lng});node["tourism"="museum"](around:25000,{lat},{lng});way["leisure"="park"](around:15000,{lat},{lng}););out body center 15;'),
        ]
    else:
        # Walkable cities (EU/LATAM/UK): widen to 3-5km, still walking-tolerant
        retry_specs = [
            ("restaurant", 3, '[out:json][timeout:20];(node["amenity"="restaurant"](around:5000,{lat},{lng});node["amenity"="cafe"](around:5000,{lat},{lng});node["amenity"="fast_food"](around:5000,{lat},{lng}););out body 30;'),
            ("grocery", 2, '[out:json][timeout:20];(node["shop"="supermarket"](around:5000,{lat},{lng});node["shop"="convenience"](around:5000,{lat},{lng});node["shop"="bakery"](around:5000,{lat},{lng}););out body 20;'),
            ("health", 1, '[out:json][timeout:20];(node["amenity"="pharmacy"](around:5000,{lat},{lng});node["amenity"="hospital"](around:8000,{lat},{lng}););out body 10;'),
            ("transit", 1, '[out:json][timeout:20];(node["railway"="station"](around:5000,{lat},{lng});node["railway"="subway_entrance"](around:5000,{lat},{lng});node["station"="subway"](around:5000,{lat},{lng});node["public_transport"="station"](around:5000,{lat},{lng});node["highway"="bus_stop"](around:3000,{lat},{lng}););out body 10;'),
            ("landmark", 2, '[out:json][timeout:20];(node["tourism"="attraction"](around:8000,{lat},{lng});node["tourism"="museum"](around:8000,{lat},{lng});way["leisure"="park"](around:5000,{lat},{lng}););out body center 15;'),
        ]

    for cat, min_count, wider_query in retry_specs:
        current = getattr(enriched, cat, [])
        # Only widen when category is fully empty. If the initial query already
        # returned even 1 result, treat that as a signal that Overpass is up
        # and the area is just sparse — don't hammer the API for marginal gains.
        if len(current) > 0:
            continue
        query = wider_query.format(lat=lat, lng=lng)
        for attempt in range(2):
            try:
                time.sleep(5)
                url = overpass_urls[0] if attempt == 0 else overpass_urls[1]
                resp = requests.post(url, data={"data": query}, timeout=25)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                seen_names = {p.name.lower() for p in current}
                for elem in data.get("elements", []):
                    tags = elem.get("tags", {})
                    name = tags.get("name", "")
                    if not name or name.lower() in seen_names:
                        continue
                    seen_names.add(name.lower())
                    plat = elem.get("lat") or (elem.get("center", {}).get("lat", 0))
                    plng = elem.get("lon") or (elem.get("center", {}).get("lon", 0))
                    dist = _haversine_m(lat, lng, plat, plng)
                    street = tags.get("addr:street", "")
                    house = tags.get("addr:housenumber", "")
                    addr = f"{house} {street}".strip() if street else ""
                    current.append(Place(
                        name=name, type=cat,
                        category=tags.get("amenity", tags.get("shop", tags.get("tourism", ""))),
                        lat=plat, lng=plng,
                        distance_m=dist, walking_min=_walking_minutes(dist),
                        address=addr,
                    ))
                current.sort(key=lambda x: x.distance_m)
                setattr(enriched, cat, current[:10])
                break
            except Exception as e:
                print(f"[adaptive-retry] {cat} attempt {attempt+1} failed: {e}")

    # Post-enrichment: fetch Google ratings for top places if API key available
    if GOOGLE_API_KEY:
        enriched = _add_google_ratings(enriched)

    return enriched


def _add_google_ratings(enriched: EnrichedLocation) -> EnrichedLocation:
    """Look up Google ratings for all places found via Overpass.

    Uses Places API (New) - Text Search endpoint.
    Requests only rating + userRatingCount fields (cheapest SKU).
    """
    SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
    lookup_count = 0
    max_lookups = 50  # Budget cap per guide

    for category in ["restaurant", "grocery", "landmark", "nightlife", "health", "transit"]:
        places = getattr(enriched, category, [])
        for place in places:
            if place.rating > 0 or lookup_count >= max_lookups:
                continue
            try:
                resp = requests.post(SEARCH_URL, json={
                    "textQuery": place.name,
                    "locationBias": {
                        "circle": {
                            "center": {"latitude": place.lat, "longitude": place.lng},
                            "radius": 500.0,
                        }
                    },
                    "maxResultCount": 1,
                }, headers={
                    "X-Goog-Api-Key": GOOGLE_API_KEY,
                    "X-Goog-FieldMask": "places.rating,places.userRatingCount",
                }, timeout=5)
                lookup_count += 1
                data = resp.json()
                results = data.get("places", [])
                if results:
                    place.rating = results[0].get("rating", 0)
                    place.total_ratings = results[0].get("userRatingCount", 0)
                elif "error" in data:
                    err = data["error"]
                    print(f"[ratings] API error for '{place.name}': {err.get('status', '')} - {err.get('message', '')}")
                time.sleep(0.1)
            except Exception as e:
                print(f"[ratings] Exception for '{place.name}': {e}")
                continue

    rated = sum(1 for cat in ["restaurant", "grocery", "landmark", "nightlife"] for p in getattr(enriched, cat, []) if p.rating > 0)
    print(f"[ratings] Looked up {lookup_count} places, {rated} got ratings")

    # Filter: keep only 4.0+ stars with 100+ reviews (or unrated transit/health)
    for category in ["restaurant", "grocery", "landmark", "nightlife"]:
        places = getattr(enriched, category, [])
        filtered = [p for p in places if p.rating >= 4.0 and p.total_ratings >= 100]
        if len(filtered) < 2:
            # Relax: keep 3.8+ with 50+ reviews if strict filter is too harsh
            filtered = [p for p in places if p.rating >= 3.8 and p.total_ratings >= 50]
        if len(filtered) < 2:
            # Last resort: keep anything with a rating, sorted by rating
            filtered = [p for p in places if p.rating > 0]
            filtered.sort(key=lambda x: (-x.rating, -x.total_ratings))
        setattr(enriched, category, filtered[:8])

    # Health/transit: softer filter (keep 3.5+ or unrated — pharmacies often have few reviews)
    for category in ["health", "transit"]:
        places = getattr(enriched, category, [])
        filtered = [p for p in places if p.rating == 0 or p.rating >= 3.5]
        setattr(enriched, category, filtered[:8])

    return enriched
