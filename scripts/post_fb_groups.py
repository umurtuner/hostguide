"""Join and post to FB expat/nomad groups via Playwright.

Opens browser, waits for user to log in, then:
1. Visits each group
2. Clicks Join if not already a member
3. Posts the city-appropriate message
4. Moves to next group
"""
from playwright.sync_api import sync_playwright
import time
import sys

GROUPS = [
    # (city, language, group_url)
    ("Dublin", "en", "https://www.facebook.com/groups/expatsindublin/"),
    ("Dublin", "en", "https://www.facebook.com/groups/americansindublin/"),
    ("Dublin", "en", "https://www.facebook.com/groups/758647231366752"),
    ("Lisbon", "en", "https://www.facebook.com/groups/ExpatsClubLisbon/"),
    ("Lisbon", "en", "https://www.facebook.com/groups/lx.expats.accomodation/"),
    ("Lisbon", "en", "https://www.facebook.com/groups/941785005955634"),
    ("Medellin", "es", "https://www.facebook.com/groups/digitalnomadsmedellin/"),
    ("Medellin", "es", "https://www.facebook.com/groups/700671533664704/"),
    ("Bogota", "es", "https://www.facebook.com/groups/digitalnomadsbogota/"),
    ("Bogota", "es", "https://www.facebook.com/groups/digitalnomadscolombia/"),
    ("Bogota", "es", "https://www.facebook.com/groups/2019185021704156/"),
    ("Madrid", "es", "https://www.facebook.com/groups/Expats.in.Madrid/"),
    ("Madrid", "es", "https://www.facebook.com/groups/252529318788363/"),
    ("Miami", "en", "https://www.facebook.com/groups/expatsinmiamigroup/"),
]

POST_EN = (
    "Living in {city} and hosting on Airbnb? I built a tool that generates "
    "a neighborhood guide for your guests based on your listing's exact location "
    "\u2014 restaurants, groceries, transit, local tips. First one is free, "
    "drop your Airbnb link if you want one."
)

POST_ES = (
    "Hosting en Airbnb en {city}? Hice una herramienta que genera una guia "
    "del barrio para tus huespedes \u2014 restaurantes, supermercados, transporte, "
    "tips locales. La primera es gratis, deja tu link de Airbnb."
)


def try_join(page):
    """Try to click Join Group button if visible."""
    try:
        join_btn = page.locator('[aria-label*="Join"], [aria-label*="join"]').first
        if join_btn.is_visible(timeout=3000):
            join_btn.click()
            print("  -> Clicked Join")
            time.sleep(3)
            # Answer membership questions if they pop up
            for _ in range(3):
                try:
                    submit = page.locator(
                        'button:has-text("Submit"), button:has-text("Answer")'
                    ).first
                    if submit.is_visible(timeout=2000):
                        submit.click()
                        time.sleep(2)
                except:
                    break
            return "joined"
        return "already_member"
    except:
        return "unknown"


def try_post(page, text):
    """Try to create a post in the group."""
    try:
        # Step 1: Click ANY "Write something" area — try multiple approaches
        # FB uses spans inside nested divs, so we search broadly
        clicked = False

        # Approach A: find span with text and click its parent
        try:
            spans = page.locator('span').all()
            for span in spans:
                try:
                    txt = span.text_content()
                    if txt and ("Write something" in txt or "What's on your mind" in txt):
                        span.click(timeout=3000)
                        clicked = True
                        print("  -> Composer opened (span click)")
                        time.sleep(3)
                        break
                except:
                    continue
        except:
            pass

        if not clicked:
            print("  -> Could not find composer, skipping post")
            return "no_composer"

        # Step 2: Find the contenteditable textbox and type
        time.sleep(2)
        editors = page.locator('div[contenteditable="true"]').all()
        typed = False
        for editor in editors:
            try:
                if editor.is_visible(timeout=2000):
                    editor.click()
                    time.sleep(1)
                    # Type character by character for FB to register
                    editor.type(text, delay=20)
                    typed = True
                    print("  -> Text entered")
                    time.sleep(2)
                    break
            except:
                continue

        if not typed:
            print("  -> Could not type in editor")
            return "no_editor"

        # Step 3: Click Post button
        time.sleep(2)
        post_clicked = False
        # Try aria-label first
        for sel in [
            '[aria-label="Post"]',
            '[aria-label="Publicar"]',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    post_clicked = True
                    print("  -> POST CLICKED (aria-label)")
                    time.sleep(5)
                    break
            except:
                continue

        if not post_clicked:
            # Try finding button by text
            try:
                btns = page.locator('div[role="button"]').all()
                for btn in btns:
                    try:
                        t = btn.text_content()
                        if t and t.strip() in ("Post", "Publicar"):
                            btn.click()
                            post_clicked = True
                            print("  -> POST CLICKED (text match)")
                            time.sleep(5)
                            break
                    except:
                        continue
            except:
                pass

        if post_clicked:
            return "posted"
        else:
            print("  -> Post button not found")
            return "no_post_btn"

    except Exception as e:
        print(f"  -> Post error: {e}")
        return "error"


def main():
    print("Starting FB group poster...")
    print("Browser will open. Make sure you're logged into Facebook.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir="chrome_profile_fb",
            headless=False,
            viewport={"width": 1200, "height": 900},
        )
        page = browser.new_page()

        # Navigate to FB — user should already be logged in
        page.goto("https://www.facebook.com/")
        time.sleep(3)

        # Check for signal file to start
        import os
        signal_file = os.path.join(os.path.dirname(__file__), "..", ".go_signal")
        print("Waiting for go signal...")
        while not os.path.exists(signal_file):
            time.sleep(1)
        os.remove(signal_file)
        print("Signal received! Starting automated posting...\n")

        results = {}

        for i, (city, lang, url) in enumerate(GROUPS):
            print(f"\n--- [{i+1}/{len(GROUPS)}] {city}: {url} ---")
            try:
                # Use a fresh page per group to avoid FB page crashes
                if i > 0:
                    page.close()
                    page = browser.new_page()
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                print(f"  -> Nav error: {e}")
                results[url] = {"city": city, "join": "nav_error", "post": "skipped"}
                continue
            time.sleep(5)

            # Try joining
            status = try_join(page)
            print(f"  Join status: {status}")

            # Build post text
            template = POST_ES if lang == "es" else POST_EN
            text = template.format(city=city)

            # Try posting regardless — if we're already in, composer will be there
            post_status = try_post(page, text)
            results[url] = {"city": city, "join": status, "post": post_status}

            time.sleep(3)

        browser.close()

    # Print summary
    print("\n\n=== RESULTS ===")
    posted = 0
    joined = 0
    for url, r in results.items():
        print(f"  {r['city']:12} | join: {r['join']:15} | post: {r['post']}")
        if r["post"] == "posted":
            posted += 1
        if r["join"] in ("joined", "already_member"):
            joined += 1

    print(f"\nJoined/member: {joined}/{len(GROUPS)}")
    print(f"Posted: {posted}/{len(GROUPS)}")

    # Save results
    import json
    with open("output/fb_expat_group_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Results saved to output/fb_expat_group_results.json")


if __name__ == "__main__":
    main()
