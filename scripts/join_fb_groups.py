"""Join FB groups for all cities in cities.yaml.

Searches each group name on Facebook and clicks 'Join group'.
Uses persistent chrome_profile_fb (must be logged into FB already).

Run: python scripts/join_fb_groups.py
"""
import json
import time
import random
import yaml
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent.parent
CONFIG = BASE / "config" / "cities.yaml"
FB_PROFILE = str(BASE / "chrome_profile_fb")
RESULTS_FILE = BASE / "output" / "fb_group_join_results.json"


def main():
    with open(CONFIG) as f:
        cities = yaml.safe_load(f)

    results = {}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            FB_PROFILE,
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        )
        page = context.new_page()

        # Check if logged in
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if "login" in page.url.lower():
            print("ERROR: Not logged into Facebook. Log in manually first.")
            context.close()
            return

        print(f"Logged into Facebook. Processing {len(cities)} cities...\n")

        for city_key, cfg in cities.items():
            city_name = cfg["name"]
            groups = cfg.get("fb_groups", [])
            if not groups:
                continue

            print(f"\n{'='*50}")
            print(f"  {city_name} — {len(groups)} groups")
            print(f"{'='*50}")

            for group_query in groups:
                # If it's a URL, extract group name or just visit directly
                if "facebook.com" in group_query:
                    status = _join_by_url(page, group_query)
                else:
                    status = _join_by_search(page, group_query)

                results[f"{city_key}:{group_query}"] = status
                print(f"    [{status}] {group_query}")
                time.sleep(random.uniform(3, 6))

        context.close()

    # Save results
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)

    # Summary
    statuses = list(results.values())
    print(f"\n{'='*50}")
    print(f"  SUMMARY")
    print(f"  Total: {len(statuses)}")
    print(f"  Joined/requested: {statuses.count('join_requested')}")
    print(f"  Already member: {statuses.count('already_member')}")
    print(f"  Already pending: {statuses.count('already_pending')}")
    print(f"  Not found: {statuses.count('not_found')}")
    print(f"  Error: {statuses.count('error')}")
    print(f"{'='*50}")
    print(f"\nResults saved to {RESULTS_FILE}")


def _join_by_url(page, url: str) -> str:
    """Visit a FB group URL directly and try to join."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(random.uniform(2, 4))
        html = page.content().lower()

        if "joined" in html and "join group" not in html:
            return "already_member"
        if "pending" in html or "cancel request" in html.lower():
            return "already_pending"

        # Try clicking Join
        for selector in [
            'div[role="button"]:has-text("Join group")',
            'div[role="button"]:has-text("Join Group")',
            'span:has-text("Join group")',
        ]:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                time.sleep(2)
                # Answer membership questions if any
                _answer_questions(page)
                return "join_requested"

        return "not_found"
    except Exception as e:
        print(f"      Error: {str(e)[:60]}")
        return "error"


def _join_by_search(page, group_name: str) -> str:
    """Search FB for a group name and try to join."""
    try:
        search_url = f"https://www.facebook.com/search/groups/?q={group_name.replace(' ', '%20')}"
        page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(random.uniform(3, 5))

        # Find group links in search results
        links = page.query_selector_all('a[href*="/groups/"]')
        for link in links[:5]:
            text = link.inner_text().strip()
            # Loose match — group name should overlap with search query
            if any(word.lower() in text.lower() for word in group_name.split()[:3]):
                href = link.get_attribute("href")
                if href and "/groups/" in href:
                    return _join_by_url(page, f"https://www.facebook.com{href}" if href.startswith("/") else href)

        # If no match found in links, look for Join buttons on page
        for selector in [
            'div[role="button"]:has-text("Join")',
        ]:
            btns = page.query_selector_all(selector)
            for btn in btns[:3]:
                if btn.is_visible() and "join" in btn.inner_text().lower():
                    btn.click()
                    time.sleep(2)
                    _answer_questions(page)
                    return "join_requested"

        return "not_found"
    except Exception as e:
        print(f"      Error: {str(e)[:60]}")
        return "error"


def _answer_questions(page):
    """Handle FB group membership questions dialog."""
    try:
        time.sleep(1.5)
        # Check for question dialog
        textareas = page.query_selector_all('textarea')
        for ta in textareas:
            if ta.is_visible():
                ta.fill("I'm an Airbnb host looking to connect with other hosts and share resources.")
                time.sleep(0.5)

        # Click Submit / Join
        for sel in ['div[role="button"]:has-text("Submit")', 'div[role="button"]:has-text("Answer")']:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                time.sleep(1)
                break
    except Exception:
        pass


if __name__ == "__main__":
    main()
