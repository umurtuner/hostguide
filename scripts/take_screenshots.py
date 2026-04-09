"""Take screenshots of sample guides for each city (for FB posts)."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT = Path(__file__).parent.parent / "output"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 1200})

        for city_dir in sorted(OUTPUT.iterdir()):
            guides_dir = city_dir / "guides"
            if not city_dir.is_dir() or not guides_dir.is_dir():
                continue

            # Pick first HTML guide
            html_files = sorted(guides_dir.glob("*_guide.html"))
            if not html_files:
                continue

            guide_path = html_files[0]
            screenshot_path = city_dir / "guide_preview.png"

            page.goto(f"file://{guide_path}")
            page.wait_for_timeout(1000)
            page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"  {city_dir.name}: {screenshot_path.name}")

        browser.close()
    print(f"\nDone — screenshots saved to each city's output folder.")

if __name__ == "__main__":
    main()
