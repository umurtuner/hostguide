"""Post to Indie Hackers via Playwright."""
from playwright.sync_api import sync_playwright
import time
import sys

TITLE = "HostGuide \u2014 auto-generates neighborhood guides for Airbnb hosts"
BODY = (
    'Got tired of the same guest messages: "where\'s the nearest grocery store?", '
    '"best coffee?", "how do I get to the beach?"\n\n'
    "Built a tool that takes your listing's GPS location and spits out a guide \u2014 "
    "restaurants, groceries, pharmacies, landmarks, local tips, all with walking/driving "
    "times. Formatted so you can print it or send a link.\n\n"
    "Working across 10+ cities right now (Miami, Orlando, Dublin, Lisbon, Madrid, "
    "Medellin, and more).\n\n"
    "Drop your Airbnb link and I'll make your first one free. What else would you "
    "want in a guide like this?"
)

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir="chrome_profile_fb",
        headless=False,
        viewport={"width": 1200, "height": 900},
    )
    page = browser.new_page()
    page.goto("https://www.indiehackers.com/new-post")
    time.sleep(3)

    # Dismiss cookie banner if present
    try:
        accept_btn = page.locator('button:has-text("ACCEPT ALL")').first
        if accept_btn.is_visible(timeout=3000):
            accept_btn.click()
            print("Cookie banner dismissed")
            time.sleep(2)
    except:
        pass

    # Wait for page to fully load
    time.sleep(5)

    if "sign-in" in page.url:
        print("NOT LOGGED IN - go log in first")
        browser.close()
        sys.exit(1)

    print(f"URL: {page.url}")

    # Debug: find all visible form elements
    elements = page.evaluate("""() => {
        const results = [];
        const all = document.querySelectorAll(
            'input, textarea, [contenteditable="true"], [role="textbox"], '
            + '.ProseMirror, .ql-editor, [data-placeholder]'
        );
        all.forEach(el => {
            results.push({
                tag: el.tagName,
                type: el.type || '',
                placeholder: el.placeholder || el.dataset?.placeholder || '',
                contentEditable: el.contentEditable,
                className: (el.className || '').substring(0, 120),
                visible: el.offsetParent !== null,
                id: el.id || '',
                role: el.getAttribute('role') || ''
            });
        });
        return results;
    }""")

    print("--- Visible form elements ---")
    for e in elements:
        if e["visible"]:
            print(e)

    page.screenshot(path="static/ih_form.png")
    print("Screenshot saved to static/ih_form.png")

    # Try to fill the form
    try:
        # Try common title selectors
        title_filled = False
        for sel in [
            'input[placeholder*="Title"]',
            'input[placeholder*="title"]',
            'input[type="text"]:visible',
            'input.ember-text-field:visible',
            '[data-placeholder*="title"]',
        ]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=2000):
                    el.fill(TITLE)
                    title_filled = True
                    print(f"Title filled via: {sel}")
                    break
            except:
                continue

        if not title_filled:
            # Try clicking on any heading-like area
            print("Could not find title input, trying keyboard approach...")
            page.keyboard.type(TITLE)
            page.keyboard.press("Tab")
            time.sleep(1)

        # Try common body selectors
        body_filled = False
        for sel in [
            ".ProseMirror",
            ".ql-editor",
            '[contenteditable="true"]',
            "textarea",
            '[role="textbox"]',
        ]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=2000):
                    el.click()
                    el.fill(BODY)
                    body_filled = True
                    print(f"Body filled via: {sel}")
                    break
            except:
                continue

        if not body_filled:
            print("Could not find body input, trying keyboard...")
            page.keyboard.type(BODY, delay=5)

        time.sleep(2)
        page.screenshot(path="static/ih_filled.png")
        print("Filled form screenshot saved")

        # Find and click post button
        for btn_text in ["Post", "Submit", "Publish", "Create Post", "Create"]:
            try:
                btn = page.locator(f'button:has-text("{btn_text}")').first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    print(f"Clicked: {btn_text}")
                    time.sleep(5)
                    break
            except:
                continue

        print(f"Final URL: {page.url}")
        page.screenshot(path="static/ih_result.png")

    except Exception as e:
        print(f"Error: {e}")
        page.screenshot(path="static/ih_error.png")

    browser.close()
