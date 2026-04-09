"""Just open the Playwright browser with the FB profile. User restores tabs manually."""
from playwright.sync_api import sync_playwright
import time
import os

SIGNAL = os.path.join(os.path.dirname(__file__), "..", ".go_signal")

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir="chrome_profile_fb",
        headless=False,
        viewport={"width": 1200, "height": 900},
        args=["--restore-last-session"],
    )
    print("Browser open. Restore your tabs, then tell Claude 'go'.")

    # Wait for go signal
    while not os.path.exists(SIGNAL):
        time.sleep(1)
    os.remove(SIGNAL)

    # Give extra time for tabs to fully load after restore
    print("Signal received, waiting 5s for tabs to load...")
    time.sleep(5)

    # Collect all open page URLs — check ALL contexts/pages
    pages = browser.pages
    print(f"\nFound {len(pages)} tabs:")
    for pg in pages:
        try:
            pg.wait_for_load_state("domcontentloaded", timeout=5000)
        except:
            pass
        print(f"  {pg.url}")

    # Save URLs for the poster to use
    import json
    urls = [pg.url for pg in pages if "facebook.com/groups" in pg.url]
    with open("output/active_fb_tabs.json", "w") as f:
        json.dump(urls, f, indent=2)
    print(f"\nSaved {len(urls)} group URLs. Now posting...\n")

    # Post in each tab
    results = {}

    POST_HOST = (
        "Got tired of the same guest messages: \"where's the nearest grocery store?\", "
        "\"best coffee?\", \"how do I get to the beach?\" \u2014 so I built a tool that "
        "generates a neighborhood guide based on your listing's exact GPS. Restaurants, "
        "groceries, pharmacies, landmarks, local tips, all with driving/walking times. "
        "First one free \u2014 drop your Airbnb link if you want one."
    )

    POST_SIDEHUSTLE = (
        "Built a micro-SaaS that auto-generates neighborhood guides for Airbnb hosts. "
        "Host pastes their listing link, gets a printable guide with nearby restaurants, "
        "groceries, landmarks, local tips \u2014 all based on GPS. Testing across 10+ cities. "
        "Anyone here hosting on Airbnb? First guide is free, drop your link."
    )

    POST_EXPAT_EN = (
        "Anyone here hosting on Airbnb? I built a tool that generates "
        "a neighborhood guide for your guests based on your listing's exact location "
        "\u2014 restaurants, groceries, transit, local tips. First one is free, "
        "drop your Airbnb link if you want one."
    )

    POST_EXPAT_ES = (
        "Hosting en Airbnb? Hice una herramienta que genera una guia "
        "del barrio para tus huespedes \u2014 restaurantes, supermercados, transporte, "
        "tips locales. La primera es gratis, deja tu link de Airbnb."
    )

    host_kw = ["hostcommunity", "professionalhosts", "helpinghosts", "airhostacademy", "545990332638815"]
    hustle_kw = ["sidehustle", "themoneyhustlers", "saasproducts"]
    es_kw = ["madrid", "medellin", "bogota", "colombia", "anfitriones"]

    for pg in pages:
        url = pg.url
        if "facebook.com/groups" not in url:
            continue

        url_lower = url.lower()
        if any(k in url_lower for k in host_kw):
            text = POST_HOST
        elif any(k in url_lower for k in hustle_kw):
            text = POST_SIDEHUSTLE
        elif any(k in url_lower for k in es_kw):
            text = POST_EXPAT_ES
        else:
            text = POST_EXPAT_EN

        print(f"--- Posting to: {url} ---")
        pg.bring_to_front()
        time.sleep(2)

        try:
            # Find composer
            clicked = False
            spans = pg.locator("span").all()
            for span in spans:
                try:
                    t = span.text_content()
                    if t and ("Write something" in t or "What" in t and "mind" in t or "Escribe algo" in t):
                        span.click(timeout=3000)
                        clicked = True
                        print("  Composer opened")
                        time.sleep(3)
                        break
                except:
                    continue

            if not clicked:
                print("  No composer - skipped")
                results[url] = "no_composer"
                continue

            # Type
            time.sleep(2)
            editors = pg.locator('div[contenteditable="true"]').all()
            typed = False
            for editor in editors:
                try:
                    if editor.is_visible(timeout=2000):
                        editor.click()
                        time.sleep(1)
                        editor.type(text, delay=20)
                        typed = True
                        print("  Text entered")
                        time.sleep(2)
                        break
                except:
                    continue

            if not typed:
                print("  Could not type")
                results[url] = "no_editor"
                continue

            # Click Post
            time.sleep(2)
            posted = False
            for sel in ['[aria-label="Post"]', '[aria-label="Publicar"]']:
                try:
                    btn = pg.locator(sel).first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        posted = True
                        print("  POSTED!")
                        time.sleep(5)
                        break
                except:
                    continue

            if not posted:
                btns = pg.locator('div[role="button"]').all()
                for btn in btns:
                    try:
                        t = btn.text_content()
                        if t and t.strip() in ("Post", "Publicar"):
                            btn.click()
                            posted = True
                            print("  POSTED! (text)")
                            time.sleep(5)
                            break
                    except:
                        continue

            results[url] = "posted" if posted else "no_post_btn"

        except Exception as e:
            print(f"  Error: {e}")
            results[url] = "error"

    print("\n=== RESULTS ===")
    for url, status in results.items():
        print(f"  {status:15} | {url}")

    posted_count = sum(1 for s in results.values() if s == "posted")
    print(f"\nPosted: {posted_count}/{len(results)}")

    with open("output/fb_expat_group_results.json", "w") as f:
        json.dump(results, f, indent=2)

    browser.close()
