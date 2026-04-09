"""Find actual FB group URLs by searching and capturing links.

FB search for groups is flaky with exact name matching.
This script searches broader terms and collects all group URLs found.

Run: python scripts/find_fb_groups.py
"""
import json
import time
import random
import yaml
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent.parent
FB_PROFILE = str(BASE / "chrome_profile_fb")
RESULTS_FILE = BASE / "output" / "fb_group_urls_found.json"

# Broader search terms per city — more likely to return results
SEARCHES = {
    "medellin": ["airbnb hosts medellin", "short term rental medellin", "digital nomads medellin"],
    "bogota": ["airbnb hosts bogota", "short term rental bogota", "digital nomads bogota"],
    "orlando": ["airbnb hosts orlando", "short term rental orlando florida", "vacation rental orlando"],
    "austin": ["airbnb hosts austin texas", "short term rental austin", "str owners texas"],
    "scottsdale": ["airbnb hosts scottsdale", "short term rental arizona", "vacation rental phoenix"],
    "tampa": ["airbnb hosts tampa", "short term rental tampa bay", "vacation rental florida gulf"],
    "nashville": ["airbnb hosts nashville", "short term rental nashville", "str owners tennessee"],
    "savannah": ["airbnb hosts savannah", "short term rental savannah georgia"],
    "destin": ["airbnb hosts destin", "vacation rental emerald coast", "vrbo hosts florida panhandle"],
    "lisbon": ["airbnb hosts lisbon", "short term rental lisbon portugal", "digital nomads lisbon"],
    "dublin": ["airbnb hosts dublin", "short term rental ireland"],
    "madrid": ["airbnb hosts madrid", "short term rental madrid spain"],
}


def main():
    found = {}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            FB_PROFILE,
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        )
        page = context.new_page()

        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        for city, queries in SEARCHES.items():
            print(f"\n  {city.upper()}")
            city_groups = []

            for query in queries:
                search_url = f"https://www.facebook.com/search/groups/?q={query.replace(' ', '%20')}"
                page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(random.uniform(3, 5))

                # Scroll to load more results
                page.evaluate("window.scrollBy(0, 1500)")
                time.sleep(2)

                # Collect all group links
                links = page.query_selector_all('a[href*="/groups/"]')
                for link in links:
                    href = link.get_attribute("href") or ""
                    text = ""
                    try:
                        text = link.inner_text().strip()[:100]
                    except Exception:
                        pass

                    if "/groups/" in href and text and len(text) > 3:
                        # Clean URL
                        if href.startswith("/"):
                            href = f"https://www.facebook.com{href}"
                        # Remove query params
                        href = href.split("?")[0].rstrip("/")

                        if href not in [g["url"] for g in city_groups]:
                            city_groups.append({"url": href, "name": text, "query": query})
                            print(f"    Found: {text[:60]} -> {href}")

                time.sleep(random.uniform(2, 4))

            found[city] = city_groups

        context.close()

    # Save
    with open(RESULTS_FILE, "w") as f:
        json.dump(found, f, indent=2, ensure_ascii=False)

    total = sum(len(g) for g in found.values())
    print(f"\n  Found {total} groups across {len(found)} cities")
    print(f"  Saved to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
