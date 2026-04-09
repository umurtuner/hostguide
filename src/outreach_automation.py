"""Outreach automation — FB group posting, email discovery & sending, CRM tracking.

Automates the distribution of guest guides to Airbnb hosts via multiple channels.
Uses Playwright for FB (same persistent profile as scraper) and Gmail API for email.

Usage:
    from hostguide.src.outreach_automation import OutreachManager
    mgr = OutreachManager("miami")
    mgr.post_to_fb_groups()
    mgr.send_emails()
    mgr.show_stats()
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import smtplib
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page

# ── Config ──
PROFILE_DIR = os.getenv("PROFILE_DIR", str(Path(__file__).parent.parent / "chrome_profile_airbnb"))
FB_PROFILE_DIR = os.getenv("FB_PROFILE_DIR", str(Path(__file__).parent.parent / "chrome_profile_fb"))
CRM_DIR = Path(__file__).parent.parent / "outreach_crm"
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")  # Google App Password, not regular password


# ═══════════════════════════════════════════════════════════════
# CRM — Track who we contacted, when, via which channel
# ═══════════════════════════════════════════════════════════════

@dataclass
class Contact:
    """A host we've contacted or plan to contact."""
    listing_id: str
    host_name: str
    city: str
    channel: str  # fb_group, fb_dm, email, instagram
    status: str  # pending, sent, replied, converted, skipped
    contacted_at: str = ""
    guide_url: str = ""
    email: str = ""
    fb_profile: str = ""
    ig_handle: str = ""
    notes: str = ""


class OutreachCRM:
    """Simple CSV-based CRM for tracking outreach contacts."""

    def __init__(self, city: str):
        self.city = city
        CRM_DIR.mkdir(parents=True, exist_ok=True)
        self.csv_path = CRM_DIR / f"{city}_contacts.csv"
        self.contacts: list[Contact] = []
        self._load()

    def _load(self):
        if self.csv_path.exists():
            with open(self.csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self.contacts.append(Contact(**row))

    def save(self):
        with open(self.csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "listing_id", "host_name", "city", "channel", "status",
                "contacted_at", "guide_url", "email", "fb_profile", "ig_handle", "notes"
            ])
            writer.writeheader()
            for c in self.contacts:
                writer.writerow(asdict(c))

    def add(self, contact: Contact):
        # Don't duplicate
        for c in self.contacts:
            if c.listing_id == contact.listing_id and c.channel == contact.channel:
                return False
        self.contacts.append(contact)
        return True

    def mark_sent(self, listing_id: str, channel: str):
        for c in self.contacts:
            if c.listing_id == listing_id and c.channel == channel:
                c.status = "sent"
                c.contacted_at = datetime.now().isoformat()

    def was_contacted(self, listing_id: str, channel: str) -> bool:
        return any(c.listing_id == listing_id and c.channel == channel
                   and c.status in ("sent", "replied", "converted")
                   for c in self.contacts)

    def stats(self) -> dict:
        total = len(self.contacts)
        by_status = {}
        by_channel = {}
        for c in self.contacts:
            by_status[c.status] = by_status.get(c.status, 0) + 1
            by_channel[c.channel] = by_channel.get(c.channel, 0) + 1
        return {"total": total, "by_status": by_status, "by_channel": by_channel}


# ═══════════════════════════════════════════════════════════════
# FACEBOOK — Auto-post to groups and send DMs
# ═══════════════════════════════════════════════════════════════

class FacebookOutreach:
    """Automate FB group posts and DMs using Playwright.

    Uses the same persistent Chrome profile as the Airbnb scraper.
    First run requires manual FB login — after that, cookies persist.
    """

    # Cache of group name → URL so we don't re-search every time
    GROUP_CACHE_PATH = CRM_DIR / "fb_group_urls.json"

    def __init__(self, headless: bool = False):
        self.headless = headless
        self.group_urls = self._load_group_cache()

    def _load_group_cache(self) -> dict:
        if self.GROUP_CACHE_PATH.exists():
            with open(self.GROUP_CACHE_PATH) as f:
                return json.load(f)
        return {}

    def _save_group_cache(self):
        CRM_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.GROUP_CACHE_PATH, "w") as f:
            json.dump(self.group_urls, f, indent=2)

    def _ensure_logged_in(self, page: Page) -> bool:
        """Navigate to FB and check login status."""
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)

        current_url = page.url.lower()
        has_login_form = page.locator('input[name="email"]').count() > 0

        if "login" in current_url or has_login_form:
            print("\n  !! Not logged into Facebook.")
            print("  Run 'python fb_login.py' first to log in, then retry.")
            return False

        print("  Logged into Facebook ✓")
        return True

    def _find_group_url(self, page: Page, group_name: str) -> str:
        """Search for a group by name. Returns the group URL or empty string.

        If group_name is already a URL (starts with http), use it directly.
        Otherwise search Facebook and cache the result.
        """
        # Direct URL support — skip search entirely
        if group_name.startswith("http"):
            url = group_name.split("?")[0].rstrip("/")
            return url

        # Check cache first
        if group_name in self.group_urls:
            return self.group_urls[group_name]

        # Use FB's search
        from urllib.parse import quote
        search_url = f"https://www.facebook.com/search/groups/?q={quote(group_name)}"
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            # Page crash recovery — open a new page
            print(f"    (page crash, recovering...)")
            try:
                page = page.context.new_page()
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                return ""
        time.sleep(5)

        # Scroll down to trigger lazy-loaded results
        try:
            for _ in range(3):
                page.mouse.wheel(0, 500)
                time.sleep(1.5)
        except Exception:
            pass

        # Approach 1: Find group links in search results
        try:
            links = page.locator('a[href*="/groups/"]')
            count = links.count()
            print(f"    (found {count} group links on search page)")
            for i in range(min(10, count)):
                href = links.nth(i).get_attribute("href") or ""
                if "/groups/" not in href:
                    continue
                # Skip generic/nav group links
                clean = href.split("?")[0].rstrip("/")
                if clean.endswith("/groups") or any(x in href.lower() for x in (
                    "/groups/feed", "/groups/discover", "/groups/create",
                    "/groups/joins", "/groups/search",
                )):
                    continue
                text = ""
                try:
                    text = links.nth(i).inner_text(timeout=2000) or ""
                except Exception:
                    pass
                first_word = group_name.split()[0].lower()
                if first_word in text.lower() or first_word in href.lower() or not text:
                    group_url = href if href.startswith("http") else f"https://www.facebook.com{href}"
                    group_url = group_url.split("?")[0].rstrip("/")
                    self.group_urls[group_name] = group_url
                    self._save_group_cache()
                    print(f"    → matched: {group_url}")
                    return group_url
        except Exception as e:
            print(f"    (search error: {str(e)[:60]})")

        # Approach 2: Extract group IDs from page HTML (JS-rendered data)
        try:
            html = page.content()
            # FB embeds group IDs in JSON data like "groupID":"123456"
            group_ids = re.findall(r'"groupID"\s*:\s*"(\d+)"', html)
            if not group_ids:
                # Also try /groups/NUMERIC patterns
                group_ids = re.findall(r'/groups/(\d{5,})', html)
            if group_ids:
                # Deduplicate, take the first
                seen = set()
                for gid in group_ids:
                    if gid not in seen:
                        seen.add(gid)
                        group_url = f"https://www.facebook.com/groups/{gid}"
                        self.group_urls[group_name] = group_url
                        self._save_group_cache()
                        print(f"    → from HTML data: {group_url}")
                        return group_url
        except Exception:
            pass

        # Approach 3: Use FB's top search bar instead
        try:
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
            search_input = page.locator('input[type="search"], input[aria-label="Search Facebook"]').first
            search_input.click(timeout=5000)
            time.sleep(1)
            search_input.fill(group_name)
            time.sleep(1)
            page.keyboard.press("Enter")
            time.sleep(4)

            # Click "Groups" filter tab
            groups_tab = page.locator('a:has-text("Groups")').first
            if groups_tab.is_visible(timeout=3000):
                groups_tab.click()
                time.sleep(3)

            links = page.locator('a[href*="/groups/"]')
            count = links.count()
            print(f"    (top-bar search: found {count} group links)")
            for i in range(min(10, count)):
                href = links.nth(i).get_attribute("href") or ""
                if any(x in href.lower() for x in (
                    "/groups/feed", "/groups/discover", "/groups/create",
                    "/groups/joins", "/groups/search",
                )):
                    continue
                if "/groups/" in href:
                    group_url = href if href.startswith("http") else f"https://www.facebook.com{href}"
                    group_url = group_url.split("?")[0].rstrip("/")
                    self.group_urls[group_name] = group_url
                    self._save_group_cache()
                    print(f"    → from top-bar: {group_url}")
                    return group_url
        except Exception as e:
            print(f"    (top-bar search error: {str(e)[:60]})")

        return ""

    def post_to_groups(self, group_names: list[str], post_text: str,
                       image_path: str = "",
                       delay_between: tuple = (45, 90)) -> list[dict]:
        """Post to Facebook groups. Returns list of results per group.

        Flow per group:
        1. Find group URL (cached after first search)
        2. Navigate to group page
        3. Click "Write something" / post composer
        4. Type post with human-like delays
        5. Attach image if provided
        6. Click Post
        7. Wait 45-90s before next group (anti-spam)
        """
        results = []

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                FB_PROFILE_DIR,
                headless=self.headless,
                viewport={"width": 1400, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = context.new_page()

            if not self._ensure_logged_in(page):
                context.close()
                return results

            for group_name in group_names:
                result = {"group": group_name, "status": "pending", "url": ""}
                try:
                    result = self._post_to_single_group(page, group_name, post_text, image_path)
                except Exception as e:
                    err = str(e)[:100]
                    result["status"] = f"error: {err}"
                    # Recover from page crashes
                    if "crash" in err.lower() or "closed" in err.lower():
                        try:
                            page = context.new_page()
                            print(f"    (recovered with new page)")
                        except Exception:
                            pass

                results.append(result)
                status_icon = "✓" if result["status"] == "posted" else "→" if "join" in result["status"] else "✗"
                display_name = group_name.split("/groups/")[-1].rstrip("/") if "facebook.com" in group_name else group_name
                print(f"  {status_icon} [{result['status']}] {display_name}")

                # Human-like delay between posts (longer = safer)
                if group_name != group_names[-1]:
                    delay = random.uniform(*delay_between)
                    print(f"    Waiting {delay:.0f}s before next group...")
                    time.sleep(delay)

            context.close()

        return results

    def _post_to_single_group(self, page: Page, group_name: str, post_text: str, image_path: str = "") -> dict:
        """Navigate to a group and create a post."""
        result = {"group": group_name, "status": "pending", "url": ""}

        # Find group URL
        group_url = self._find_group_url(page, group_name)
        if not group_url:
            result["status"] = "group_not_found"
            return result

        result["url"] = group_url

        # Navigate to the group (with crash recovery)
        try:
            page.goto(group_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            if "crash" in str(e).lower():
                # Try with a fresh page
                page = page.context.new_page()
                page.goto(group_url, wait_until="domcontentloaded", timeout=30000)
            else:
                raise
        time.sleep(5)

        # Check if we need to join the group
        try:
            join_btn = page.locator('div[aria-label="Join group"], [aria-label="Join Group"]')
            if join_btn.count() > 0 and join_btn.first.is_visible(timeout=2000):
                join_btn.first.click()
                time.sleep(2)
                # Some groups require answering questions
                result["status"] = "join_requested"
                result["notes"] = "Sent join request — may need approval before posting"
                return result
        except Exception:
            pass

        # Find and click the post composer
        try:
            # Try multiple selectors for the "Write something" area
            composer_selectors = [
                '[role="button"]:has-text("Write something")',
                '[role="button"]:has-text("What\'s on your mind")',
                'div[data-pagelet="GroupInlineComposer"] [role="button"]',
                'div[aria-label="Write something..."]',
                'div[aria-label="Create a public post…"]',
                'div[aria-label="Write something"]',
                'span:has-text("Write something...")',
            ]
            clicked = False
            for sel in composer_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        print(f"    → Composer found via: {sel[:50]}")
                        btn.click()
                        time.sleep(3)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                print(f"    → Could not find composer. Page URL: {page.url[:80]}")
                result["status"] = "composer_not_found"
                return result

            # Wait for the editor to appear
            editor = page.locator('[role="textbox"][contenteditable="true"]').first
            editor.click(timeout=5000)
            time.sleep(0.5)

            # Type the post with human-like speed
            lines = post_text.split("\n")
            for i, line in enumerate(lines):
                if line.strip():
                    editor.type(line, delay=random.randint(15, 40))
                if i < len(lines) - 1:
                    page.keyboard.press("Shift+Enter")
                    time.sleep(random.uniform(0.1, 0.3))

            time.sleep(1.5)

            # Attach image if provided — use file chooser interception
            if image_path and os.path.exists(image_path):
                try:
                    photo_selectors = [
                        '[aria-label="Photo/video"]',
                        '[aria-label="Photo/Video"]',
                        '[aria-label*="Photo"]',
                    ]
                    with page.expect_file_chooser(timeout=5000) as fc_info:
                        for sel in photo_selectors:
                            btn = page.query_selector(sel)
                            if btn and btn.is_visible():
                                btn.click(force=True)
                                break
                    file_chooser = fc_info.value
                    file_chooser.set_files(image_path)
                    time.sleep(4)
                    print(f"    → Image attached: {os.path.basename(image_path)}")
                except Exception as e:
                    print(f"    → Image attach failed: {str(e)[:60]}")

            time.sleep(1.5)

            # Click Post button
            post_selectors = [
                'div[aria-label="Post"]',
                '[aria-label="Post"]',
                'div[role="button"]:has-text("Post")',
            ]
            for sel in post_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        time.sleep(4)
                        result["status"] = "posted"
                        return result
                except Exception:
                    continue

            result["status"] = "post_button_not_found"

        except Exception as e:
            result["status"] = f"post_failed: {str(e)[:80]}"

        return result

    def send_dm(self, page: Page, profile_url: str, message: str) -> dict:
        """Send a DM to a Facebook user via Messenger."""
        result = {"profile": profile_url, "status": "pending"}

        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            # Click Message button on profile
            msg_btn = page.locator('a:has-text("Message"), [aria-label="Message"]').first
            msg_btn.click(timeout=5000)
            time.sleep(3)

            # Type in the Messenger chat
            chat_input = page.locator('[role="textbox"][contenteditable="true"]').last
            chat_input.click(timeout=5000)
            time.sleep(0.5)

            for line in message.split("\n"):
                chat_input.type(line, delay=random.randint(30, 60))
                page.keyboard.press("Shift+Enter")
                time.sleep(random.uniform(0.2, 0.5))

            # Send
            page.keyboard.press("Enter")
            time.sleep(2)

            result["status"] = "sent"
        except Exception as e:
            result["status"] = f"error: {str(e)[:80]}"

        return result


# ═══════════════════════════════════════════════════════════════
# EMAIL — Discovery + sending via Gmail
# ═══════════════════════════════════════════════════════════════

class EmailOutreach:
    """Find host emails and send personalized outreach via Gmail SMTP."""

    def __init__(self):
        self.gmail_user = GMAIL_USER
        self.gmail_pass = GMAIL_APP_PASSWORD

    def discover_emails_from_listings(self, listings_path: str) -> list[dict]:
        """Try to discover host emails from listing data or external sources.

        Methods:
        1. Host website link on Airbnb profile (sometimes has email)
        2. Google search: "host_name airbnb host city email"
        3. Hunter.io API (if key available)
        """
        with open(listings_path) as f:
            listings = json.load(f)

        discovered = []
        for l in listings:
            host = l.get("host_name", "")
            if not host or host in ("", "Your Host"):
                continue
            discovered.append({
                "listing_id": l["listing_id"],
                "host_name": host,
                "city": l["city"],
                "email": "",  # To be filled by discovery methods
                "source": "",
            })

        return discovered

    def discover_email_from_profile(self, page: Page, listing_url: str) -> str:
        """Visit an Airbnb listing and try to find host's external links."""
        try:
            page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            # Look for host profile link
            html = page.content()

            # Some hosts link their website or social media
            website_match = re.search(r'"website"\s*:\s*"(https?://[^"]+)"', html)
            if website_match:
                return website_match.group(1)

            # Look for email patterns in the page
            email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', html)
            if email_match:
                email = email_match.group(0)
                # Filter out airbnb internal emails
                if "airbnb" not in email.lower():
                    return email

        except Exception:
            pass

        return ""

    def send_email(self, to_email: str, subject: str, body_html: str) -> bool:
        """Send a single email via Gmail SMTP."""
        if not self.gmail_user or not self.gmail_pass:
            print("  Gmail credentials not configured. Set GMAIL_USER and GMAIL_APP_PASSWORD.")
            return False

        msg = MIMEMultipart("alternative")
        msg["From"] = f"Umur from HostGuide <{self.gmail_user}>"
        msg["To"] = to_email
        msg["Subject"] = subject

        # Plain text version
        plain = re.sub(r'<[^>]+>', '', body_html)
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.gmail_user, self.gmail_pass)
                server.send_message(msg)
            return True
        except Exception as e:
            print(f"  Email failed: {e}")
            return False

    def send_batch(self, contacts: list[dict], template_fn, crm: OutreachCRM,
                   delay_between: tuple = (30, 90)) -> int:
        """Send emails to a batch of contacts with human-like delays.

        template_fn: function(contact) -> (subject, body_html)
        """
        sent = 0
        for i, contact in enumerate(contacts):
            if crm.was_contacted(contact["listing_id"], "email"):
                print(f"  [{i+1}] {contact['host_name']} — already contacted, skipping")
                continue

            if not contact.get("email"):
                continue

            subject, body = template_fn(contact)
            print(f"  [{i+1}] Sending to {contact['email']}...", end=" ")

            if self.send_email(contact["email"], subject, body):
                crm.mark_sent(contact["listing_id"], "email")
                sent += 1
                print("OK")
            else:
                print("FAILED")

            # Human-like delay
            if i < len(contacts) - 1:
                delay = random.uniform(*delay_between)
                time.sleep(delay)

        crm.save()
        return sent


# ═══════════════════════════════════════════════════════════════
# OUTREACH MANAGER — orchestrates all channels
# ═══════════════════════════════════════════════════════════════

class OutreachManager:
    """Central orchestrator for multi-channel outreach."""

    def __init__(self, city: str, city_config: dict):
        self.city = city
        self.city_config = city_config
        self.city_name = city_config["name"]
        self.crm = OutreachCRM(city)
        self.output_dir = Path(__file__).parent.parent / "output" / city

    def post_to_fb_groups(self, dry_run: bool = True):
        """Post the FB group post to all configured groups."""
        fb_post_path = self.output_dir / "fb_post.txt"
        if not fb_post_path.exists():
            print(f"No FB post found at {fb_post_path}. Run the pipeline first.")
            return

        post_text = fb_post_path.read_text()
        groups = self.city_config.get("fb_groups", [])

        # Look for guide screenshot to attach
        image_path = str(self.output_dir / "guide_preview.png")
        if not os.path.exists(image_path):
            image_path = ""

        if dry_run:
            print(f"\n{'='*60}")
            print(f"FB GROUP POST — {self.city_name} (DRY RUN)")
            print(f"{'='*60}")
            print(f"\nWould post to {len(groups)} groups:")
            for g in groups:
                display = g.split("/groups/")[-1].rstrip("/") if "facebook.com" in g else g
                print(f"  - {display}")
            print(f"\nPost text:\n{post_text[:300]}...")
            if image_path:
                print(f"\nImage: {image_path}")
            print(f"\nTo actually post, run with dry_run=False")
            return

        print(f"\n{'='*60}")
        print(f"FB GROUP POSTING — {self.city_name}")
        print(f"{'='*60}")
        print(f"Posting to {len(groups)} groups...")
        if image_path:
            print(f"With image: {os.path.basename(image_path)}")

        fb = FacebookOutreach(headless=False)
        results = fb.post_to_groups(groups, post_text, image_path=image_path)

        # Track in CRM
        for r in results:
            self.crm.add(Contact(
                listing_id="group_post",
                host_name="",
                city=self.city,
                channel="fb_group",
                status=r["status"],
                contacted_at=datetime.now().isoformat(),
                notes=f"Group: {r['group']}",
            ))
        self.crm.save()

        posted = sum(1 for r in results if r["status"] == "posted")
        print(f"\nPosted to {posted}/{len(groups)} groups")

    def send_dms(self, listings_path: str, dm_dir: str, dry_run: bool = True):
        """Send personalized DMs to hosts."""
        dm_path = Path(dm_dir)
        if not dm_path.exists():
            print(f"No DM templates found at {dm_path}")
            return

        dm_files = sorted(dm_path.glob("dm_*.txt"))
        if dry_run:
            print(f"\n{'='*60}")
            print(f"DM OUTREACH — {self.city_name} (DRY RUN)")
            print(f"{'='*60}")
            print(f"\n{len(dm_files)} DM templates ready")
            for f in dm_files[:3]:
                print(f"  - {f.name}")
                print(f"    Preview: {f.read_text()[:100]}...")
            print(f"\nTo send, run with dry_run=False (requires FB login)")
            return

    def send_emails(self, contacts: list[dict], guide_base_url: str = "",
                    dry_run: bool = True):
        """Send personalized emails to hosts with discovered emails."""
        if dry_run:
            print(f"\n{'='*60}")
            print(f"EMAIL OUTREACH — {self.city_name} (DRY RUN)")
            print(f"{'='*60}")
            with_email = [c for c in contacts if c.get("email")]
            print(f"\n{len(contacts)} contacts, {len(with_email)} with emails")
            for c in with_email[:5]:
                print(f"  - {c['host_name']} <{c['email']}>")
            if not GMAIL_USER:
                print("\n  Set GMAIL_USER and GMAIL_APP_PASSWORD to enable sending")
            print(f"\nTo send, run with dry_run=False")
            return

        emailer = EmailOutreach()

        def _template(contact):
            host = contact["host_name"]
            city = contact["city"]
            guide_url = f"{guide_base_url}/{contact['listing_id']}" if guide_base_url else "[guide link]"

            subject = f"Free guest guide for your {city} listing"
            body = f"""<div style="font-family: 'Segoe UI', sans-serif; max-width: 600px; color: #333;">
                <p>Hi {host},</p>
                <p>I noticed your Airbnb listing in {city} — great place!</p>
                <p>I've built a service that creates <strong>personalized neighborhood guides</strong> for Airbnb hosts.
                Instead of answering "where's the nearest grocery store?" every time, your guests get a beautiful guide
                specific to your apartment's exact location.</p>
                <p>I made a free sample for your listing: <a href="{guide_url}" style="color: #FF5A5F;">{guide_url}</a></p>
                <p>It includes walking directions to nearby restaurants, groceries, transit, landmarks, and local tips —
                everything a guest needs on Day 1.</p>
                <p>Your guests get a better experience, you get fewer repetitive messages, and your reviews improve.</p>
                <p>Want me to send you the full version? It's already done.</p>
                <p>Best,<br>Umur<br>
                <span style="color: #999; font-size: 13px;">HostGuide · hostguide.co</span></p>
            </div>"""
            return subject, body

        sent = emailer.send_batch(contacts, _template, self.crm)
        print(f"\nSent {sent} emails")

    def show_stats(self):
        """Show outreach statistics."""
        stats = self.crm.stats()
        print(f"\n{'='*60}")
        print(f"OUTREACH STATS — {self.city_name}")
        print(f"{'='*60}")
        print(f"Total contacts: {stats['total']}")
        print(f"\nBy status:")
        for s, count in stats["by_status"].items():
            print(f"  {s}: {count}")
        print(f"\nBy channel:")
        for ch, count in stats["by_channel"].items():
            print(f"  {ch}: {count}")


# ═══════════════════════════════════════════════════════════════
# CLI INTEGRATION
# ═══════════════════════════════════════════════════════════════

def run_outreach(city_key: str, city_config: dict, channels: list[str] = None,
                 dry_run: bool = True):
    """Run outreach for a city across specified channels.

    channels: list of "fb", "email", "dm" (default: all)
    """
    channels = channels or ["fb", "email", "dm"]
    mgr = OutreachManager(city_key, city_config)

    if "fb" in channels:
        mgr.post_to_fb_groups(dry_run=dry_run)

    if "dm" in channels:
        dm_dir = str(mgr.output_dir / "dms")
        listings_path = str(mgr.output_dir / "listings.json")
        mgr.send_dms(listings_path, dm_dir, dry_run=dry_run)

    if "email" in channels:
        emailer = EmailOutreach()
        contacts = emailer.discover_emails_from_listings(str(mgr.output_dir / "listings.json"))
        mgr.send_emails(contacts, dry_run=dry_run)

    mgr.show_stats()
