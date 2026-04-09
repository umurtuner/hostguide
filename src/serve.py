"""HostGuide — simple web server to share guides via URL.

Serves guides at:
    /                           → index with all cities + guides
    /guide/<city>/<listing_id>  → individual HTML guide
    /api/cities                 → JSON list of cities with guide counts

Run:
    cd hostguide && python -m src.serve
    # or: python src/serve.py
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, send_file, abort, render_template_string, jsonify
from flask_cors import CORS

BASE = Path(__file__).parent.parent
OUTPUT = BASE / "output"

app = Flask(__name__)
CORS(app)


def _scan_guides() -> dict[str, list[dict]]:
    """Scan output dir and return {city: [{listing_id, host, neighborhood, path}, ...]}."""
    cities = {}
    if not OUTPUT.exists():
        return cities
    for city_dir in sorted(OUTPUT.iterdir()):
        if not city_dir.is_dir() or not (city_dir / "guides").is_dir():
            continue
        city = city_dir.name
        guides = []
        for html_file in sorted((city_dir / "guides").glob("*_guide.html")):
            # Parse listing ID from filename: City_LISTINGID_guide.html
            parts = html_file.stem.replace("_guide", "").split("_", 1)
            listing_id = parts[1] if len(parts) > 1 else parts[0]
            # Try to get host name from listings.json
            host = ""
            neighborhood = ""
            listings_path = city_dir / "listings.json"
            if listings_path.exists():
                try:
                    with open(listings_path) as f:
                        for l in json.load(f):
                            if l.get("listing_id") == listing_id:
                                host = l.get("host_name", "")
                                neighborhood = l.get("neighborhood", "")
                                break
                except Exception:
                    pass
            guides.append({
                "listing_id": listing_id,
                "host": host,
                "neighborhood": neighborhood,
                "file": str(html_file),
            })
        if guides:
            cities[city] = guides
    return cities


INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HostGuide — Guest Guides for Airbnb Hosts</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Inter', -apple-system, sans-serif;
    background: #f5f5f5;
    color: #1a1a1a;
    line-height: 1.5;
}
.header {
    background: linear-gradient(135deg, #00897B 0%, #00695C 100%);
    color: white;
    padding: 48px 24px;
    text-align: center;
}
.header h1 { font-size: 36px; font-weight: 700; }
.header p { font-size: 16px; opacity: 0.9; margin-top: 8px; }
.stats {
    display: flex; justify-content: center; gap: 32px; margin-top: 20px;
    font-size: 14px; opacity: 0.85;
}
.stats span { font-weight: 600; font-size: 20px; display: block; }
.container { max-width: 900px; margin: 0 auto; padding: 32px 24px; }
.city-card {
    background: white;
    border-radius: 12px;
    margin-bottom: 20px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.city-header {
    padding: 20px 24px;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid #f0f0f0;
}
.city-header:hover { background: #fafafa; }
.city-name { font-size: 20px; font-weight: 600; }
.city-count {
    font-size: 13px;
    color: #888;
    background: #f0f0f0;
    padding: 4px 12px;
    border-radius: 12px;
}
.guide-list { display: none; padding: 0; }
.city-card.open .guide-list { display: block; }
.guide-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 14px 24px;
    border-bottom: 1px solid #f8f8f8;
    font-size: 14px;
}
.guide-item:last-child { border-bottom: none; }
.guide-item:hover { background: #fafafa; }
.guide-info { color: #555; }
.guide-link {
    color: #00897B;
    text-decoration: none;
    font-weight: 500;
    padding: 6px 14px;
    border: 1px solid #00897B;
    border-radius: 6px;
    font-size: 13px;
}
.guide-link:hover { background: #00897B; color: white; }
.cta {
    text-align: center;
    margin-top: 40px;
    padding: 32px;
    background: white;
    border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.cta h2 { font-size: 20px; margin-bottom: 8px; }
.cta p { color: #666; font-size: 14px; }
.footer { text-align: center; padding: 24px; font-size: 12px; color: #999; }
</style>
</head>
<body>

<div class="header">
    <h1>HostGuide</h1>
    <p>Personalized neighborhood guides for Airbnb guests</p>
    <div class="stats">
        <div>{{ total_cities }}<span>Cities</span></div>
        <div>{{ total_guides }}<span>Guides</span></div>
    </div>
</div>

<div class="container">
    {% for city, guides in cities.items() %}
    <div class="city-card" onclick="this.classList.toggle('open')">
        <div class="city-header">
            <span class="city-name">{{ city | title }}</span>
            <span class="city-count">{{ guides | length }} guides</span>
        </div>
        <div class="guide-list">
            {% for g in guides %}
            <div class="guide-item">
                <div>
                    <strong>{{ g.neighborhood or 'Guide' }}</strong>
                    <span class="guide-info">{% if g.host %} — hosted by {{ g.host }}{% endif %}</span>
                </div>
                <a class="guide-link" href="/guide/{{ city }}/{{ g.listing_id }}">View Guide</a>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endfor %}

    <div class="cta">
        <h2>Want a guide for your listing?</h2>
        <p>Drop your Airbnb link and we'll generate a personalized neighborhood guide in 2 minutes.</p>
    </div>
</div>

<div class="footer">Powered by HostGuide</div>

</body>
</html>"""


@app.route("/")
def index():
    cities = _scan_guides()
    total_guides = sum(len(g) for g in cities.values())
    return render_template_string(
        INDEX_TEMPLATE,
        cities=cities,
        total_cities=len(cities),
        total_guides=total_guides,
    )


@app.route("/guide/<city>/<listing_id>")
def serve_guide(city: str, listing_id: str):
    guide_dir = OUTPUT / city / "guides"
    if not guide_dir.exists():
        abort(404, f"City '{city}' not found")
    # Find the matching HTML file
    for html_file in guide_dir.glob(f"*{listing_id}*_guide.html"):
        return send_file(html_file, mimetype="text/html")
    abort(404, f"Guide '{listing_id}' not found in {city}")


@app.route("/api/cities")
def api_cities():
    cities = _scan_guides()
    return jsonify({
        city: [{"listing_id": g["listing_id"], "host": g["host"],
                "neighborhood": g["neighborhood"],
                "url": f"/guide/{city}/{g['listing_id']}"}
               for g in guides]
        for city, guides in cities.items()
    })


if __name__ == "__main__":
    print(f"\n  HostGuide server starting...")
    print(f"  Output dir: {OUTPUT}")
    cities = _scan_guides()
    total = sum(len(g) for g in cities.values())
    print(f"  Found {total} guides across {len(cities)} cities\n")
    print(f"  Open: http://localhost:5555\n")
    app.run(host="0.0.0.0", port=5555, debug=True)
