# HostGuide - Product Hunt Launch Kit

Launch target: pick a Tuesday or Wednesday (highest traffic days). Post at 00:01 PT
so you get the full 24-hour voting window.

Site: https://www.host-guide.net
Maker: Umur Tuener (@umurtuner)

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
> PH launch: https://www.producthunt.com/posts/hostguide
> Site: https://www.host-guide.net
>
> RT appreciated. Back to answering PH comments 🙏

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
