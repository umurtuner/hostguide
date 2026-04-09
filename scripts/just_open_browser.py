"""Open browser and keep it open forever. User posts manually."""
from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir="chrome_profile_fb",
        headless=False,
        viewport={"width": 1200, "height": 900},
        args=["--restore-last-session"],
    )
    print("Browser open. Restore your tabs, post manually. Press Ctrl+C when done.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Closing browser.")
    browser.close()
