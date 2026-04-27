# HostGuide - Product Hunt Launch Kit

Launch target: pick a Tuesday or Wednesday (highest traffic days). Post at 00:01 PT
so you get the full 24-hour voting window.

Site: https://www.host-guide.net
Maker: Umur Tuener (@umurtuner)

---

## Status snapshot (Apr 27, 2026)

- Site UP (200 OK, 262ms response). Static asset 404 fix deployed.
- **PH coming-soon page LIVE: https://www.producthunt.com/products/hostguide-2** (forum: `p/hostguide-2`)
- Launch scheduled: **May 12, 2026 00:01 PDT (07:01 UTC, 09:01 Geneva)**.
- 5 required PH assets uploaded; 4 gallery images visible on PH page. Missing optional: gallery_5 dashboard screenshot + 20s GIF demo.
- 9 cities scraped, 452 hosts queued, **0 sent**. 4 cities (austin, nashville, savannah, scottsdale) need first scrape.
- Distribution plan + daily cadence: see `tasks/distribution_plan.md`.
- 5 launch-week preflight routines scheduled May 8-12 06:00 UTC.
- New tooling:
  - `scripts/preflight_ph.py` - run T-48, T-24, morning-of (PASS/FAIL gates)
  - `scripts/crm_status.py` - one-shot dashboard across all city queues
  - `scripts/daily_outreach.py` - picks 30 messages/day across tiers
  - `scripts/enrich_linkedin.py` - Apollo-backed LinkedIn + email enrichment

**Action items still on user:**
1. Get followers from 1 -> 50 by May 11 (LinkedIn announcement, hunter DMs, IH soft-launch Apr 29).
2. Re-run Miami scrape (bounds widened) + scrape 4 empty cities.
3. Record 20s screen demo GIF.
4. Capture dashboard screenshot for gallery_5.
5. (Optional) Get APOLLO_API_KEY for email enrichment.

---

## Tagline (60 char max)

Primary:
> Personalized neighborhood guides for Airbnb hosts, in 60 seconds

Alternates (in order of preference):
- The welcome book your Airbnb guests will actually read
- Turn any Airbnb listing into a printable guest guide in 60s
- Stop answering "where's the grocery store?" - send this instead
- Auto-generated neighborhood guides for Airbnb hosts

---

## Description (260 char max - PH cuts hard at 260)

> HostGuide turns any Airbnb listing URL into a branded, printable neighborhood
> guide in 60 seconds. Walking directions, top cafes, transit, groceries, local
> tips - all tailored to the exact lat/lng of your place. Drop the PDF in your
> welcome book, send guests the link, get better reviews.

Character count: 258.

---

## First comment from maker (posts with launch - this is the one that sells it)

> Hey Hunters, Umur here.
>
> I started hosting on Airbnb a few years ago and quickly noticed the same
> pattern every week: "where's the nearest grocery store?", "how do I get to
> the beach?", "best coffee around here?". The welcome book I spent hours
> putting together in Canva was either too generic or already outdated.
>
> So I built HostGuide. You paste your Airbnb URL, it pulls your exact location,
> runs it through Google Places and our ranking model, and produces a printable
> PDF in 60 seconds. Walking times to transit and groceries, top-rated
> restaurants within 10 minutes, local tipping and taxi norms, emergency
> numbers, and a QR code guests can scan to pull the digital version on their
> phone.
>
> It works in every country I've tested so far (30+ and counting) and covers
> city-specific stuff like ride apps (Bolt in Portugal, Grab in SEA, Careem in
> Dubai, etc.). One-time $4.99 per guide, or $14.99 for a 5-pack. No
> subscription. No AI-slop filler paragraphs. Just a useful PDF.
>
> I'd love feedback from hosts and travelers here, especially on:
> 1. What's missing from your own welcome book today?
> 2. If you're a frequent Airbnb guest, what do you wish hosts actually included?
> 3. Cities you'd like me to test next - drop a listing URL and I'll make one
>    live in this thread.
>
> Thanks for checking it out!

---

## Asset checklist (required for launch)

- [x] **Logo** (240x240 PNG) → `static/ph/logo_240.png`
- [x] **Gallery image 1** (1270x760 PNG) hero → `static/ph/gallery_1_hero.png`
- [x] **Gallery image 2** (1270x760 PNG) branded title card → `static/ph/gallery_2_og.png`
- [x] **Gallery image 3** (1270x760 PNG) sample guide preview → `static/ph/gallery_3_guide.png`
- [x] **Gallery image 4** (1270x760 PNG) guide bottom / QR section → `static/ph/gallery_4_qr.png`
- [ ] **Gallery image 5** (optional) - dashboard screenshot showing 2-3 generated guides (requires login; capture manually)
- [ ] **GIF or video** (optional but 2x conversion) - 20-second screen recording: paste URL, click generate, see PDF appear
- [ ] **Topics**: Travel, Productivity, Marketing, No-Code
- [ ] **Makers**: add @umurtuner as maker

Regenerate all assets at once: `python scripts/generate_ph_assets.py`

---

## Launch day playbook (minute-by-minute)

### T-48 hours
- Submit the product as a draft in Product Hunt (Ship), pick the launch date
- Tell 10-15 people directly (DM, not mass blast) about the launch time
- Pre-write the X/Twitter thread, LinkedIn post, and Airbnb Community post
- Verify the site is UP (run `curl -I https://www.host-guide.net`)

### T-24 hours
- Bump Render to Starter plan for launch day (remove cold starts)
- Pre-commit the Product Hunt banner on the landing page ("We're launching on PH!")

### 00:01 PT launch day
- Hit publish on PH
- Drop the maker comment immediately (first comment gets the most upvotes)
- Post the X thread: 5 tweets, show the PDF as an image attachment
- Post to LinkedIn: personal story angle, not product pitch
- Post to r/AirBnBHosts and r/Airbnb (if allowed - check subreddit rules)
- Post to the two Airbnb Community threads already running
- Send DMs to the 15 people you pre-warned

### Hours 1-6
- Reply to EVERY PH comment within 10 minutes
- For any host who drops their listing URL, generate a real guide and reply with the link (proof beats promises)
- Watch the rank - if we drop below #5, push another wave of DMs

### Hours 6-12
- Email the free-plan HostGuide users: "we're #X on Product Hunt today, would love a vote"
- Post update on X showing current rank
- Drop a second maker comment with a fresh angle ("top 3 most-requested cities so far")

### Hours 12-24
- Respond to any late comments
- Screenshot final rank for the site (proof/badge)
- Thank-you tweet tagging the top 10 upvoters

### T+24
- Add "As seen on Product Hunt" badge to the site
- Write a short post-mortem: what worked, what didn't, conversion rate from PH traffic

---

## X/Twitter launch thread (5 tweets, ready to copy)

**Tweet 1**
> I just launched HostGuide on @ProductHunt 🚀
>
> Paste any Airbnb URL, get a printable neighborhood guide for your guests in 60 seconds. No more "where's the grocery store?" messages.
>
> Upvote if you've ever hosted (link below)
> [gif of the flow]

**Tweet 2**
> Why I built it:
>
> I host in Geneva and got tired of answering the same guest questions every week. My Canva welcome book was outdated within a month. Every "city guide" on Google is SEO spam.
>
> So I made one that's personalized to each listing's exact lat/lng.

**Tweet 3**
> What's inside:
> - Walking times to metro and groceries
> - Top-rated cafes and restaurants within 10min
> - Local ride apps (Bolt, Grab, Careem)
> - Tipping and emergency numbers
> - QR code guests scan to get the digital version
>
> All in a branded PDF you drop in your welcome book.

**Tweet 4**
> Pricing: $4.99 one-time, or $14.99 for a 5-pack. No subscription.
>
> Works in 30+ countries so far. If you host anywhere in the world, I'd love to test yours live - drop your listing URL below.

**Tweet 5**
> PH launch: https://www.producthunt.com/products/hostguide-2
> Site: https://www.host-guide.net
>
> RT appreciated. Back to answering PH comments.

---

## LinkedIn post (personal story, 1300 chars)

> Quick story.
>
> Last summer I got my 47th "where's the nearest grocery store?" message from an Airbnb guest and realized my welcome book wasn't working. I had spent hours making it in Canva. It looked nice. Nobody read it.
>
> So I built a tool for myself that generates a printable neighborhood guide from any Airbnb listing URL. Walking directions to transit and groceries, top-rated cafes within 10 minutes, local taxi apps, tipping norms, all tailored to the exact lat/lng of the place.
>
> My guest messages dropped by 70%. Reviews started mentioning "the guide was so helpful."
>
> Today I'm launching it on Product Hunt as HostGuide. It's a side project - I still run MarTech for Pampers by day - but it solves a real problem I had.
>
> If you host on Airbnb, or you know someone who does, I'd love your feedback. First 100 launch-day users get a guide on the house.
>
> Link in the comments.

---

## Airbnb Community forum post (for the 2 existing threads - one update each)

> Update for anyone following this thread: we're launching on Product Hunt
> tomorrow (Tuesday). If you want to test a guide for your listing before the
> launch and give feedback, drop your Airbnb URL in a reply and I'll generate
> one and send it back in this thread. Totally free, no strings.

---

## KPIs to track

- PH final rank (target: top 5)
- PH upvotes (target: 300+)
- Site visits from PH referrer (target: 2000+)
- Signups (target: 150+)
- Paid conversions in first 48h (target: 20+)
- Cost to acquire: ($X ad spend + Render upgrade) / paid signups

---

## Pre-launch sequence (Apr 27 - May 11, 2026)

Goal: 50+ followers on the PH coming-soon page so the launch hits the front
page in the first 4 hours (PH's algorithm weights early velocity heavily).

**Apr 27-28 — Submit coming-soon page**
- Go to https://www.producthunt.com/ship → "New Product"
- Upload the same assets from /static/ph/ (logo, gallery)
- Use the tagline + description from the top of this file
- Set launch date: **Tuesday May 12, 2026**
- This gives a public follow URL of the form: producthunt.com/products/hostguide
- Save that URL somewhere obvious — every DM below uses it

**Apr 29 - May 7 — Hunter outreach (target 50 follows)**
- Use the LinkedIn DM template below for 30 1st-degree marketing/SaaS contacts
- Use the X/Twitter DM template for 15 indie hacker / host community follows
- Post the IH "I'm building" thread for organic follows

**May 8-10 — Activation reminders**
- Post a "launching Tuesday" update on LinkedIn (don't drop link, generate curiosity)
- DM the 50 followers with the May-11 reminder template below
- Test the dashboard flow end-to-end yourself with a fresh email

**May 11 (Mon)**
- Final pre-launch DM to all followers
- Post X teaser tweet
- Send to forum responders

---

## Coming-soon page submission text (PH Ship)

**Name:** HostGuide
**Tagline:** Personalized neighborhood guides for Airbnb hosts, in 60 seconds
**Description:** (use the 258-char description from top of this file)
**Topics:** Travel, Productivity, Marketing, No-Code
**URL:** https://www.host-guide.net
**Launch date:** May 12, 2026
**Hunter:** @umurtuner (self-hunting)
**Maker comment:** (use the maker comment from top of this file)

---

## Hunter outreach DMs

### LinkedIn (1st-degree marketers, SaaS PMs, founders) — send 5/day Apr 29 - May 4

> Hey [Name] — quick one.
>
> I'm launching a side project on Product Hunt next Tuesday (May 12). It's HostGuide — a tool that generates printable neighborhood welcome books for Airbnb hosts. Built it because my own Canva welcome book stopped scaling.
>
> If it sounds interesting, you can follow the coming-soon page so you get a ping when it ships:
>
> https://www.producthunt.com/products/hostguide-2
>
> Happy to return the favor when you launch something. Thanks!

### X/Twitter DMs (indie hackers, hosts you follow) — send 3/day Apr 29 - May 4

> Hey — launching HostGuide on PH Tuesday May 12. Auto-generates printable welcome books from any Airbnb URL. Coming-soon page if you want a ping at ship time: https://www.producthunt.com/products/hostguide-2. Cheers!

### Final reminder DM (send May 11 to everyone who followed)

> Hey [Name] — heads-up, HostGuide goes live on PH at 09:01 Geneva tomorrow (00:01 PT).
>
> If you want to check it out: https://www.producthunt.com/products/hostguide-2
>
> Thanks again for following!

---

## Day-of comment-reply templates (paste into PH within 10 min of comment)

### "Does this work for [city/country] X?"
> Yes — anywhere with OpenStreetMap coverage (so 200+ countries). The data is hand-tested in Lisbon, Madrid, Dublin, Miami, Tampa, Orlando, Medellin, Bogota. Drop your listing URL and I'll generate yours live in this thread!

### "How is this different from a Canva template?"
> Canva templates are static — every host fills them in by hand and they go stale fast. HostGuide pulls actual nearby places from each listing's exact lat/lng and regenerates the data per-listing. So you don't write "the cafe down the street" — it actually finds the cafe down the street with the walking time.

### "Why not just AI-generate the whole thing?"
> The "AI city guide" output is generic ("experience the vibrant culture"). HostGuide grounds Claude on real OSM/Maps data so every recommendation is a named place with walking time and address. The AI does the voice, the data does the truth.

### "What's the data source?"
> OpenStreetMap for coverage + Google Places for ratings (Places API New, just the cheapest fields). Anthropic Claude for the narrative.

### "Can I white-label it for my host management business?"
> Not yet, but DM me. If 3+ pros ask in the launch I'll prioritize a B2B tier this quarter.

### "Does this work for hotels?"
> Same workflow — paste the property URL, get a printable welcome book per room. One pro-host (Zurin Charm Hotel in Lisbon) is using it as a per-room branded PDF. Drop the URL and I'll show you.

### "Open source?"
> The frontend is closed; the data layer (enricher, scraper) might end up open. If you want to contribute or fork, reply and I'll prioritize.

---

## Reddit r/airbnb_hosts post (May 12, 1pm Geneva, after PH momentum)

**Title:** I built a free tool that generates printable neighborhood welcome books for Airbnb hosts (launched today on PH)

**Body:**
> Long-time host here (Geneva). I got tired of answering "where's the grocery store?" 5x/week and rebuilt my welcome book as a tool you can use too: paste your Airbnb URL → get a printable PDF with walking times to transit, top cafes, local ride apps, tipping norms.
>
> Live at host-guide.net. First guide is on the house if you've never tried it.
>
> Mod-friendly note: I'm not selling here — happy to give /r/airbnb_hosts a free guide for any listing in the comments. Drop your URL.
>
> Also on Product Hunt today if anyone's there: https://www.producthunt.com/products/hostguide-2

---

## Indie Hackers post (Apr 29 — soft pre-launch, ~2 weeks early)

**Title:** Going live on PH May 12 — built a niche SaaS for Airbnb hosts on the side

**Body:**
> 6 months ago I started building HostGuide as a weekend project. Live at host-guide.net.
>
> The premise: every Airbnb host writes a welcome book in Canva. It's outdated in 6 weeks and nobody reads it. So I built a generator that pulls real nearby places from each listing's exact lat/lng and writes a printable PDF in 60 seconds.
>
> Stack: Flask + Stripe + WeasyPrint + OpenStreetMap + Google Places + Claude. Hosted on Render.
>
> Tech learnings I'd write a longer post about:
> - The Airbnb scraper is HTTP-only (no Playwright in the hot path) — 800ms per listing
> - Generated PDFs are templated HTML rendered by WeasyPrint, not headless Chrome — 2x faster, 10x cheaper
> - Claude writes the narrative; structured place data is the spine
> - 80% of the work was edge cases: junk city names from OG tags, OSM transit tagging quirks, US suburban density
>
> Launching on PH May 12 — coming-soon: https://www.producthunt.com/products/hostguide-2. Would love feedback from anyone who's shipped a vertical SaaS in a niche I'm not in.

---

## Show HN post (alternative to PH if PH gets snowed under)

**Title:** Show HN: HostGuide – Generate printable Airbnb welcome books from a listing URL

**Body:**
> Hi HN. I built HostGuide because my Canva welcome book stopped scaling and every alternative was either generic AI slop or a $99/mo subscription tool.
>
> Paste an Airbnb URL → site scrapes the listing's lat/lng/host/title via HTTP-only meta tags (no Playwright on the hot path), enriches with OSM Overpass + Google Places (just the rating field for cost), feeds it to Claude with strict no-cliché rules, renders as HTML → PDF via WeasyPrint. Total time per guide: ~60 seconds.
>
> Live: https://www.host-guide.net
> First guide on the house.
>
> The interesting engineering problem turned out to be data quality across 200 countries: OSM tags transit differently in every city (Lisbon Metro is railway=subway_entrance, NYC subway is railway=station, London Underground is station=subway), and US suburbs need 5km radii while EU walkable cities need 1.5km. The whole pipeline has a quality gate that flags guides with <8 POIs.
>
> Happy to answer questions about the stack, the quality gate, or why I picked Render over Fly.io.

---

## PH forum thread (post on `p/hostguide-2` ~Apr 28-29, before launch)

PH suggests starting a forum thread to engage early users. Research-flavored beats promo-flavored — comments become social proof for launch day.

**Title:**
> Hosts: what's the most-asked guest question that ruins your week?

**Body:**
> I'm launching HostGuide on PH next Tuesday (May 12) - it generates printable welcome books for Airbnb guests. Before we go live, I'd love to know: what's the one guest question you wish your welcome book actually answered?
>
> For me it was "where's the nearest grocery store?" - I got it 47 times in one summer despite a 12-page Canva guide that literally had a map.
>
> Drop yours below. If you share your listing URL I'll generate a free guide for it and reply with the PDF - genuine usability research, no upvote ask.

---

## LinkedIn launch announcement (post Apr 28-29, ~1300 chars)

**Headline (first line - the scroll-stopper):**
> My 47th "where's the grocery store?" message broke me. So I built HostGuide.

**Full body:**
> My 47th "where's the grocery store?" message broke me. So I built HostGuide.
>
> I host on Airbnb in Geneva. Every week, the same questions: where's the metro, where's coffee, where's the beach. My welcome book had answers. Nobody read it - 12 pages of Canva, outdated within a month, generic by design.
>
> So I built a tool that generates a printable neighborhood guide from any Airbnb listing URL in 60 seconds. Walking times to transit and groceries, top-rated cafes within 10 minutes, local ride apps (Bolt, Grab, Careem), tipping norms, emergency numbers, and a QR code guests scan for the digital version. All tailored to the exact lat/lng of the place.
>
> My guest messages dropped by 70%. Reviews started mentioning "the guide was so helpful."
>
> It's launching on Product Hunt on Tuesday May 12 as HostGuide. Side project - I still run MarTech for Pampers by day - but it solves a real problem I had.
>
> If you host on Airbnb, or know someone who does, you can follow the coming-soon page so you get a ping when it ships:
>
> https://www.producthunt.com/products/hostguide-2
>
> First guide on the house for everyone who follows.

**Comment to drop on your own post (5 min after posting):**
> P.S. - if you want to test it before launch, drop your Airbnb URL in a reply and I'll generate a guide for it tonight.
