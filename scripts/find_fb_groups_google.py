"""Find FB group URLs via Google search (FB's own search is blocked).

Run: python scripts/find_fb_groups_google.py
"""
import json
import time
import random
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent.parent
FB_PROFILE = str(BASE / "chrome_profile_fb")
RESULTS_FILE = BASE / "output" / "fb_group_urls_found.json"

CITIES = {
    "orlando": "orlando florida",
    "tampa": "tampa bay florida",
    "destin": "destin florida",
    "medellin": "medellin colombia",
    "bogota": "bogota colombia",
    "lisbon": "lisbon portugal",
    "dublin": "dublin ireland",
    "madrid": "madrid spain",
}


def main():
    found = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        for city, location in CITIES.items():
            print(f"\n  {city.upper()}")
            city_groups = []

            for query in [
                f'site:facebook.com/groups airbnb hosts {location}',
                f'site:facebook.com/groups short term rental {location}',
            ]:
                url = f"https://www.google.com/search?q={query.replace(' ', '+')}&num=10"
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(random.uniform(2, 4))

                # Check for captcha
                html = page.content().lower()
                if "captcha" in html or "unusual traffic" in html:
                    print("    Google captcha detected — waiting 30s for you to solve...")
                    for _ in range(30):
                        time.sleep(2)
                        html = page.content().lower()
                        if "captcha" not in html and "unusual traffic" not in html:
                            break

                # Extract FB group links
                links = page.query_selector_all('a[href*="facebook.com/groups/"]')
                for link in links:
                    href = link.get_attribute("href") or ""
                    # Extract clean FB URL from Google redirect
                    match = re.search(r'(https?://(?:www\.)?facebook\.com/groups/[^&?/\s"]+)', href)
                    if not match:
                        # Try from text/cite elements
                        try:
                            text = link.inner_text()
                            match = re.search(r'facebook\.com/groups/(\w+)', text)
                            if match:
                                href = f"https://www.facebook.com/groups/{match.group(1)}"
                        except:
                            continue
                    else:
                        href = match.group(1)

                    if href and href not in [g["url"] for g in city_groups]:
                        # Get the title from search result
                        title = ""
                        try:
                            parent = link.query_selector("h3")
                            if parent:
                                title = parent.inner_text().strip()
                            elif link.inner_text().strip():
                                title = link.inner_text().strip()[:80]
                        except:
                            pass
                        city_groups.append({"url": href, "name": title or href})
                        print(f"    {title[:60] or href}")

                time.sleep(random.uniform(2, 4))

            found[city] = city_groups

        browser.close()

    with open(RESULTS_FILE, "w") as f:
        json.dump(found, f, indent=2, ensure_ascii=False)

    total = sum(len(g) for g in found.values())
    print(f"\n  Found {total} groups across {len(found)} cities")
    print(f"  Saved to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
