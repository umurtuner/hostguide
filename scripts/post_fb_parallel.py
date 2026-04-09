"""Open all FB groups as tabs, post in each one sequentially."""
from playwright.sync_api import sync_playwright
import time
import json
import os

GROUPS = [
    # Expat / Digital Nomad groups
    ("Dublin", "en", "https://www.facebook.com/groups/expatsindublin/"),
    ("Dublin", "en", "https://www.facebook.com/groups/americansindublin/"),
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
    # Host communities
    ("General", "en", "https://www.facebook.com/groups/545990332638815/"),
    ("General", "en", "https://www.facebook.com/groups/airhostacademy/"),
    ("General", "en", "https://www.facebook.com/groups/airbnbhostcommunity/"),
    ("General", "en", "https://www.facebook.com/groups/airbnbhostshelpinghosts/"),
    ("General", "en", "https://www.facebook.com/groups/professionalhosts/"),
    # SaaS / Side hustle
    ("General", "en", "https://www.facebook.com/groups/saasproductsandmarketing/"),
    ("General", "en", "https://www.facebook.com/groups/sidehustlenation/"),
    ("General", "en", "https://www.facebook.com/groups/thesidehustlemovement"),
    ("General", "en", "https://www.facebook.com/groups/sidehustleheroes/"),
    ("General", "en", "https://www.facebook.com/groups/themoneyhustlers/"),
]

POST_EXPAT_EN = (
    "Living in {city} and hosting on Airbnb? I built a tool that generates "
    "a neighborhood guide for your guests based on your listing's exact location "
    "\u2014 restaurants, groceries, transit, local tips. First one is free, "
    "drop your Airbnb link if you want one."
)

POST_EXPAT_ES = (
    "Hosting en Airbnb en {city}? Hice una herramienta que genera una guia "
    "del barrio para tus huespedes \u2014 restaurantes, supermercados, transporte, "
    "tips locales. La primera es gratis, deja tu link de Airbnb."
)

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


def post_in_tab(page, text, city, url):
    """Post in a single tab that's already loaded."""
    try:
        page.bring_to_front()
        time.sleep(1)

        # Find "Write something" span and click it
        clicked = False
        try:
            spans = page.locator("span").all()
            for span in spans:
                try:
                    t = span.text_content()
                    if t and ("Write something" in t or "What" in t and "mind" in t
                              or "Escribe algo" in t):
                        span.click(timeout=3000)
                        clicked = True
                        print(f"  [{city}] Composer opened")
                        time.sleep(3)
                        break
                except:
                    continue
        except:
            pass

        if not clicked:
            print(f"  [{city}] No composer found - skipped")
            return "no_composer"

        # Type into editor
        time.sleep(2)
        editors = page.locator('div[contenteditable="true"]').all()
        typed = False
        for editor in editors:
            try:
                if editor.is_visible(timeout=2000):
                    editor.click()
                    time.sleep(1)
                    editor.type(text, delay=20)
                    typed = True
                    print(f"  [{city}] Text entered")
                    time.sleep(2)
                    break
            except:
                continue

        if not typed:
            print(f"  [{city}] Could not type")
            return "no_editor"

        # Click Post
        time.sleep(2)
        for sel in ['[aria-label="Post"]', '[aria-label="Publicar"]']:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    print(f"  [{city}] POSTED!")
                    time.sleep(4)
                    return "posted"
            except:
                continue

        # Fallback: find button by text
        try:
            btns = page.locator('div[role="button"]').all()
            for btn in btns:
                try:
                    t = btn.text_content()
                    if t and t.strip() in ("Post", "Publicar"):
                        btn.click()
                        print(f"  [{city}] POSTED! (text match)")
                        time.sleep(4)
                        return "posted"
                except:
                    continue
        except:
            pass

        print(f"  [{city}] No Post button found")
        return "no_post_btn"

    except Exception as e:
        print(f"  [{city}] Error: {e}")
        return "error"


def main():
    print("Opening all 14 groups as tabs...\n")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir="chrome_profile_fb",
            headless=False,
            viewport={"width": 1200, "height": 900},
        )

        # Open ALL groups as separate tabs at once
        pages = []
        for city, lang, url in GROUPS:
            pg = browser.new_page()
            try:
                pg.goto(url, timeout=20000, wait_until="domcontentloaded")
                print(f"  Opened: {city} - {url}")
            except Exception as e:
                print(f"  Failed: {city} - {e}")
            pages.append((pg, city, lang, url))
            time.sleep(1)  # stagger slightly to avoid FB rate limit

        print(f"\nAll {len(pages)} tabs loaded. Waiting 5s for pages to settle...\n")
        time.sleep(5)

        # Now post in each tab
        results = {}
        host_urls = {"545990332638815", "airhostacademy", "airbnbhostcommunity",
                     "airbnbhostshelpinghosts", "professionalhosts"}
        hustle_urls = {"saasproductsandmarketing", "sidehustlenation",
                       "thesidehustlemovement", "sidehustleheroes", "themoneyhustlers"}

        for pg, city, lang, url in pages:
            # Pick the right post template
            url_lower = url.lower()
            if any(h in url_lower for h in host_urls):
                text = POST_HOST
            elif any(h in url_lower for h in hustle_urls):
                text = POST_SIDEHUSTLE
            elif lang == "es":
                text = POST_EXPAT_ES.format(city=city)
            else:
                text = POST_EXPAT_EN.format(city=city)

            status = post_in_tab(pg, text, city, url)
            results[url] = {"city": city, "status": status}
            time.sleep(2)

        browser.close()

    # Summary
    print("\n=== RESULTS ===")
    posted = 0
    for url, r in results.items():
        print(f"  {r['city']:12} | {r['status']}")
        if r["status"] == "posted":
            posted += 1
    print(f"\nPosted: {posted}/{len(GROUPS)}")

    with open("output/fb_expat_group_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Saved to output/fb_expat_group_results.json")


if __name__ == "__main__":
    main()
