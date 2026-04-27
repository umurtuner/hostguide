"""Copy a canned launch post to the clipboard for manual paste anywhere.

For Reddit, Indie Hackers, Airbnb Community, PH forum - any web composer
where Playwright automation isn't worth the lift. One command, paste in
browser.

Run:
    python scripts/copy_post.py reddit
    python scripts/copy_post.py ih
    python scripts/copy_post.py airbnb_community
    python scripts/copy_post.py ph_forum
    python scripts/copy_post.py hn
    python scripts/copy_post.py --list
"""
from __future__ import annotations

import argparse
import subprocess
import sys

PH_URL = "https://www.producthunt.com/products/hostguide-2"
SITE = "https://www.host-guide.net"

POSTS = {
    "ph_forum": {
        "title": "Hosts: what's the most-asked guest question that ruins your week?",
        "where": f"PH forum: https://www.producthunt.com/p/hostguide-2 (post BEFORE launch, ~Apr 28-29)",
        "body": f"""I'm launching HostGuide on PH next Tuesday (Apr 28) - it generates printable welcome books for Airbnb guests. Before we go live, I'd love to know: what's the one guest question you wish your welcome book actually answered?

For me it was "where's the nearest grocery store?" - I got it 47 times in one summer despite a 12-page Canva guide that literally had a map.

Drop yours below. If you share your listing URL I'll generate a free guide for it and reply with the PDF - genuine usability research, no upvote ask.""",
    },

    "ih": {
        "title": "Going live on PH Apr 28 - built a niche SaaS for Airbnb hosts on the side",
        "where": "Indie Hackers Tasks/Milestones/Show: https://www.indiehackers.com/post (post Apr 29)",
        "body": f"""6 months ago I started building HostGuide as a weekend project. Live at host-guide.net.

The premise: every Airbnb host writes a welcome book in Canva. It's outdated in 6 weeks and nobody reads it. So I built a generator that pulls real nearby places from each listing's exact lat/lng and writes a printable PDF in 60 seconds.

Stack: Flask + Stripe + WeasyPrint + OpenStreetMap + Google Places + Claude. Hosted on Render.

Tech learnings I'd write a longer post about:
- The Airbnb scraper is HTTP-only (no Playwright in the hot path) - 800ms per listing
- Generated PDFs are templated HTML rendered by WeasyPrint, not headless Chrome - 2x faster, 10x cheaper
- Claude writes the narrative; structured place data is the spine
- 80% of the work was edge cases: junk city names from OG tags, OSM transit tagging quirks, US suburban density

Launching on PH Apr 28 - coming-soon: {PH_URL}. Would love feedback from anyone who's shipped a vertical SaaS in a niche I'm not in.""",
    },

    "reddit": {
        "title": "I built a free tool that generates printable neighborhood welcome books for Airbnb hosts (launched today on PH)",
        "where": "r/airbnb_hosts: https://www.reddit.com/r/airbnb_hosts/submit (post Apr 28 ~13:00 Geneva)",
        "body": f"""Long-time host here (Geneva). I got tired of answering "where's the grocery store?" 5x/week and rebuilt my welcome book as a tool you can use too: paste your Airbnb URL -> get a printable PDF with walking times to transit, top cafes, local ride apps, tipping norms.

Live at {SITE.replace('https://', '').replace('www.', '')}. First guide is on the house if you've never tried it.

Mod-friendly note: I'm not selling here - happy to give /r/airbnb_hosts a free guide for any listing in the comments. Drop your URL.

Also on Product Hunt today if anyone's there: {PH_URL}""",
    },

    "airbnb_community": {
        "title": "(reply to existing threads, no new title)",
        "where": "Existing 2 Airbnb Community threads (post Apr 27 evening or Apr 28 morning)",
        "body": f"""Update for anyone following this thread: we're launching on Product Hunt tomorrow (Tuesday). If you want to test a guide for your listing before the launch and give feedback, drop your Airbnb URL in a reply and I'll generate one and send it back in this thread. Totally free, no strings.

PH page: {PH_URL}""",
    },

    "hn": {
        "title": "Show HN: HostGuide - Generate printable Airbnb welcome books from a listing URL",
        "where": "https://news.ycombinator.com/submit (backup if PH gets snowed under)",
        "body": f"""Hi HN. I built HostGuide because my Canva welcome book stopped scaling and every alternative was either generic AI slop or a $99/mo subscription tool.

Paste an Airbnb URL -> site scrapes the listing's lat/lng/host/title via HTTP-only meta tags (no Playwright on the hot path), enriches with OSM Overpass + Google Places (just the rating field for cost), feeds it to Claude with strict no-cliche rules, renders as HTML -> PDF via WeasyPrint. Total time per guide: ~60 seconds.

Live: {SITE}
First guide on the house.

The interesting engineering problem turned out to be data quality across 200 countries: OSM tags transit differently in every city (Lisbon Metro is railway=subway_entrance, NYC subway is railway=station, London Underground is station=subway), and US suburbs need 5km radii while EU walkable cities need 1.5km. The whole pipeline has a quality gate that flags guides with <8 POIs.

Happy to answer questions about the stack, the quality gate, or why I picked Render over Fly.io.""",
    },

    "ph_first_comment": {
        "title": "(no title - this is the maker comment that auto-posts on launch)",
        "where": "Already wired into PH submission. Backup paste if needed.",
        "body": """Hey Hunters, Umur here.

I started hosting on Airbnb a few years ago and quickly noticed the same pattern every week: "where's the nearest grocery store?", "how do I get to the beach?", "best coffee around here?". The welcome book I spent hours putting together in Canva was either too generic or already outdated.

So I built HostGuide. You paste your Airbnb URL, it pulls your exact location, runs it through Google Places and our ranking model, and produces a printable PDF in 60 seconds. Walking times to transit and groceries, top-rated restaurants within 10 minutes, local tipping and taxi norms, emergency numbers, and a QR code guests can scan to pull the digital version on their phone.

It works in every country I've tested so far (30+ and counting) and covers city-specific stuff like ride apps (Bolt in Portugal, Grab in SEA, Careem in Dubai, etc.). One-time $4.99 per guide, or $14.99 for a 5-pack. No subscription. No AI-slop filler paragraphs. Just a useful PDF.

I'd love feedback from hosts and travelers here, especially on:
1. What's missing from your own welcome book today?
2. If you're a frequent Airbnb guest, what do you wish hosts actually included?
3. Cities you'd like me to test next - drop a listing URL and I'll make one live in this thread.

Thanks for checking it out!""",
    },

    "ph_second_comment": {
        "title": "(maker comment to drop ~hour 6 of launch day)",
        "where": "PH product page comment box, Apr 28 mid-morning Geneva",
        "body": f"""Quick mid-day update. Most-requested cities so far: [fill in top 3 from comments above].

Anyone whose city isn't covered yet - drop your Airbnb URL in this thread and I'll generate one live.

Also realised I never said thanks for the 100+ upvotes - appreciate every one. Back to answering questions.""",
    },
}


def copy(text: str) -> bool:
    if sys.platform == "darwin":
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(text.encode("utf-8"))
        return True
    if sys.platform.startswith("linux"):
        try:
            p = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"))
            return True
        except FileNotFoundError:
            return False
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("post", nargs="?", help="post name")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--with-title", action="store_true",
                        help="prepend the title above the body")
    args = parser.parse_args()

    if args.list or not args.post:
        print("\nAvailable posts:\n")
        for k, v in POSTS.items():
            print(f"  {k:<22} -> {v['where']}")
        print("\nUsage: python scripts/copy_post.py <post-name>\n")
        return

    if args.post not in POSTS:
        print(f"[err] unknown post '{args.post}'. Use --list to see options.", file=sys.stderr)
        sys.exit(1)

    post = POSTS[args.post]
    text = post["body"]
    if args.with_title and post["title"] and not post["title"].startswith("("):
        text = post["title"] + "\n\n" + text

    if copy(text):
        print(f"\n[ok] copied to clipboard ({len(text)} chars)")
    else:
        print(f"\n[err] no clipboard tool found - here's the text to paste:\n")

    print(f"Title: {post['title']}")
    print(f"Where: {post['where']}")
    print(f"\n--- BODY ({len(post['body'])} chars) ---\n")
    print(post["body"])
    print()


if __name__ == "__main__":
    main()
