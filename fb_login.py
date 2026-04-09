#!/usr/bin/env python3
"""Open Chromium for Facebook login with cookie banner auto-dismiss.

Usage:
    python fb_login.py              # Opens browser, dismisses cookie banner, waits for login
    python fb_login.py --check      # Just check if already logged in
    python fb_login.py --profile X  # Use custom profile dir
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_PROFILE = str(Path(__file__).parent / "chrome_profile_fb")


def _dismiss_cookie_banner(page):
    """Try to click through Facebook's cookie consent banner."""
    cookie_selectors = [
        'button[data-cookiebanner="accept_button"]',
        'button[data-testid="cookie-policy-manage-dialog-accept-button"]',
        'button:has-text("Allow all cookies")',
        'button:has-text("Accept All")',
        'button:has-text("Accept all")',
        'button:has-text("Allow essential and optional cookies")',
        'button:has-text("Only allow essential cookies")',  # fallback — at least dismiss it
        '[aria-label="Allow all cookies"]',
        '[aria-label="Accept All"]',
        'div[role="dialog"] button:first-of-type',
    ]
    for sel in cookie_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                print(f"  Dismissing cookie banner: {sel[:50]}")
                btn.click()
                time.sleep(2)
                return True
        except Exception:
            continue
    return False


def _is_logged_in(page) -> bool:
    """Check for logged-in indicators."""
    try:
        # These elements only exist when logged in
        logged_in_selectors = [
            '[aria-label="Your profile"]',
            '[aria-label="Messenger"]',
            '[aria-label="Notifications"]',
            '[aria-label="Account"]',
            'a[href*="/me/"]',
            '[role="feed"]',
        ]
        for sel in logged_in_selectors:
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue

        # If login form is gone, we're probably logged in
        try:
            email_visible = page.locator('input[name="email"]').is_visible(timeout=1000)
            if not email_visible:
                # Confirm with a navigation element
                has_nav = page.locator('[role="navigation"], [role="banner"]').count() > 0
                has_feed = page.locator('[role="main"]').count() > 0
                if has_nav and has_feed:
                    return True
        except Exception:
            pass

    except Exception:
        pass
    return False


def main():
    check_only = "--check" in sys.argv
    profile_dir = DEFAULT_PROFILE

    # Custom profile
    if "--profile" in sys.argv:
        idx = sys.argv.index("--profile")
        if idx + 1 < len(sys.argv):
            profile_dir = sys.argv[idx + 1]

    print(f"Profile: {profile_dir}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        print("Loading facebook.com...")
        page.goto("https://www.facebook.com/", wait_until="networkidle", timeout=30000)
        time.sleep(3)

        # Auto-dismiss cookie banner
        _dismiss_cookie_banner(page)
        time.sleep(1)

        if _is_logged_in(page):
            print("\nAlready logged in! Session is valid.")
            context.close()
            return

        if check_only:
            print("NOT logged in.")
            context.close()
            return

        # Show page state for debugging
        print(f"\nPage URL: {page.url}")
        print(f"Page title: {page.title()}")
        has_email = page.locator('input[name="email"]').count()
        has_pass = page.locator('input[name="pass"]').count()
        print(f"Login form: email={has_email}, password={has_pass}")

        print()
        print("=" * 55)
        print("  LOG INTO FACEBOOK in the Chromium window.")
        print()
        print("  If cookie banner is still showing, click")
        print("  'Allow all cookies' or 'Accept' first.")
        print()
        print("  Then enter your email + password.")
        print("  Complete 2FA if prompted.")
        print()
        print("  Max wait: 10 minutes. No page refreshing.")
        print("=" * 55)

        max_wait = 600
        elapsed = 0
        while elapsed < max_wait:
            time.sleep(10)
            elapsed += 10
            try:
                if _is_logged_in(page):
                    print(f"\n  LOGGED IN successfully! ({elapsed}s)")
                    print(f"  Session saved to: {profile_dir}")
                    print(f"\n  Ready to post! Run:")
                    print(f"  python run.py miami --skip-scrape --outreach --send")
                    context.close()
                    return
            except Exception:
                pass

            if elapsed % 60 == 0:
                url = page.url[:60] if page else "?"
                print(f"  [{elapsed}s] Still waiting... (url: {url})")

        print(f"\n  Timed out. Session may still be saved if you logged in.")
        context.close()


if __name__ == "__main__":
    main()
