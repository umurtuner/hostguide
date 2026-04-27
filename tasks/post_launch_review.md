# HostGuide PH Launch Post-Mortem - Apr 29, 2026

Fill in within 24h of launch close. The remote agent scheduled for Apr 29 06:00 UTC will pre-fill the metric rows from the live PH page.

---

## Headline result

- **PH final rank:** _____ (target: top 5)
- **PH upvotes:** _____ (target: 300+)
- **Site visits from PH:** _____ (target: 2000+)
- **New signups:** _____ (target: 150+)
- **Paid conversions in 48h:** _____ (target: 20+)
- **Cost to acquire (CAC):** $_____ (Render upgrade + ads / paid signups)

---

## What worked

- [ ] Pre-launch coming-soon followers reached: _____ / 50 target
- [ ] Best-performing channel by visits: _____
- [ ] Best-performing channel by signups: _____
- [ ] Best-performing channel by paid conv: _____
- [ ] Maker comment engagement (replies received): _____
- [ ] Live demo guides generated for PH commenters: _____
- [ ] Notable upvoter / share moment:

## What didn't work

- [ ] Channels that brought zero conversion:
- [ ] PH comment categories that weren't pre-templated:
- [ ] Site / payment / generation issues during the spike:
- [ ] Render cold starts (any 503s? Was the Starter upgrade enough?):

## Surprises

- [ ] Geography of upvoters (vs target US / EU host markets):
- [ ] Languages requested that we don't support yet:
- [ ] Listing types that broke the scraper or POI gate:
- [ ] Press / inbound DMs from launch:

---

## Decisions to log in `decisions.csv`

For each decision, add a row with date, decision, reasoning, expected_outcome, review_date (30d out), status, actual_outcome.

- [ ] Keep / kill the LinkedIn skip rule (set Apr 27 due to P&G; was the alternative-channel volume enough?)
- [ ] Apollo enrichment: did the LinkedIn URLs convert? Continue subscription or kill?
- [ ] Tier-A vs Tier-B / Tier-C city ROI: shift the daily picker weights for next campaign?
- [ ] PH vs HN vs IH: which audience generated the highest-LTV customers?
- [ ] Render plan: stay on Starter or upgrade further / downgrade?
- [ ] Pricing: did Starter ($4.99/mo) outsell Single ($1.99) and Pro ($14.99)? Reprice?

---

## Follow-up actions (load into tasks.json with priorities)

- P0: Reply to any unfulfilled "drop your URL and I'll generate one" promises in PH comments
- P0: Email all paid customers a thank-you + ask for review on the dashboard
- P1: Add "As seen on Product Hunt" badge to the site (already env-toggled in `src/app.py`)
- P1: Capture testimonials from the highest-engagement upvoters for the landing page
- P2: Update `tasks/distribution_plan.md` with revised tier weights for week 3-4 ramp
- P2: Schedule 30-day retention check on first-week customers
- P3: Write the IH long-form follow-up post ("how my PH launch went")

---

## Deliverables to produce

- [ ] Screenshot of final PH rank for site badge / social proof
- [ ] Tweet: thank-you to top 10 upvoters (use scripts/post_x_compose.py with custom text)
- [ ] LinkedIn post: SKIP per P&G rule
- [ ] Update `memory/project_hostguide.md` with launch outcome
- [ ] Update `memory/decisions.csv` with all decisions above
