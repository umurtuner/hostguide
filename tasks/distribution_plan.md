# HostGuide Distribution Plan

**Window:** Apr 27 - Apr 28, 2026 (PH launch day) and 2 weeks of post-launch ramp.
**Goal:** 450+ host conversations, 50 PH coming-soon followers, 20 paid signups in launch week.

---

## Current state (Apr 27, 2026)

| Cohort | Cities | Listings scraped | Hosts queued | Sent so far |
|--------|--------|------------------|--------------|-------------|
| Active | 9      | 459              | 452          | 0           |
| Empty  | 4      | 0                | 0            | 0           |
| **Total** | **13** | **459**       | **452**      | **0**       |

**Active cities:** miami (18), madrid (64), lisbon (52), medellin (54), bogota (50), dublin (54), tampa (53), orlando (53), destin (54).

**Empty cities (need first scrape):** austin, nashville, savannah, scottsdale.

**Miami underrepresented:** 18 vs ~54 elsewhere. Bounds widened in `config/cities.yaml` (lat 25.65-25.95, lon -80.40 to -80.10) and 7 new neighborhoods added. Re-run:
```bash
HEADLESS=false python -m hostguide.run miami
python scripts/build_outreach_queue.py miami
```

**Empty city scrape (one shot):**
```bash
for c in austin nashville savannah scottsdale; do HEADLESS=false python -m hostguide.run $c; done
python scripts/discover_hosts_all.py
for c in austin nashville savannah scottsdale; do python scripts/build_outreach_queue.py $c; done
```

---

## Tiering

Outreach is rate-limited by humans (Airbnb caps Contact Host at ~20/day per account, FB groups need throttled posts). Tier cities so the most leverage gets the most attention.

### Tier A - PH-launch fuel (50% of daily outreach)
**miami, lisbon, madrid, austin**

Why: Three are anchor markets in our PH copy + topic tags (Travel, Marketing). Austin is added because it's a high-density STR market and the pre-launch IH/HN audience skews to US tech hubs. These cities feed PH "drop your URL and I'll generate a guide live" demonstrations.

Channels:
- Airbnb Contact Host DM (warm, personalized per listing)
- 4 FB groups per city (already in `cities.yaml`)
- LinkedIn 1st-degree DMs (template in `producthunt_launch.md`)
- Direct ping to PH coming-soon page

### Tier B - Volume plays (33% of daily)
**medellin, bogota, tampa, orlando**

Why: Largest existing queues (211 hosts), lowest acquisition cost. Already validated POI quality in output samples. These are the cities where we expect the bulk of paid conversions because hosts in these markets have less polished welcome books to begin with.

Channels: Airbnb Contact Host DM + FB groups (3-4 per city). Skip LinkedIn (lower ROI in LATAM/secondary US).

### Tier C - Long tail (17% of daily)
**dublin, nashville, savannah, scottsdale, destin**

Why: Smaller markets, ride PH afterglow. Don't burn pre-launch capacity here. Post-PH, Reddit r/airbnb_hosts city threads + niche FB groups will pick these up organically.

Channels: Reddit + FB groups + queued Contact Host messages.

### Drop / rebuild
**Rochester:** Orphaned in `output/` but not in `cities.yaml`. Decision: leave the sample guide deployed (it's a real proof point in DMs) but don't generate outreach for it.

---

## Daily cadence

`scripts/daily_outreach.py --target 30` picks the daily batch:
- Tier A: 15 messages
- Tier B: 10 messages
- Tier C: 5 messages

That's ~450 messages over 15 days = entire backlog cleared by PH launch.

Workflow:
1. Morning: `python scripts/daily_outreach.py` writes `outreach_crm/daily/daily_YYYY-MM-DD.md`
2. Midday: send the messages from that file (Airbnb + FB)
3. Evening: `python scripts/build_outreach_queue.py <city> --mark-sent <id>` for each one sent
4. Status check: `python scripts/crm_status.py`

---

## Channel mix per week

| Channel              | Daily volume | Owner    | Notes                                          |
|----------------------|--------------|----------|-----------------------------------------------|
| Airbnb Contact Host  | 20           | Manual   | Hard cap from Airbnb anti-spam                |
| FB group post        | 1-2          | Semi-auto| Use `scripts/post_fb_groups.py` (manual click)|
| LinkedIn DM          | 5            | Manual   | PH hunter outreach template                    |
| X/Twitter DM         | 3            | Manual   | Indie hackers + hosts                          |
| Reddit comment       | 2            | Manual   | Helpful first, link second                     |
| Past-host warm leads | 1-2          | Manual   | `queue_past_hosts.md` - personal touch        |

Total: ~30/day. Sustainable for 2 weeks without burnout.

---

## LinkedIn enrichment layer (parallel work)

`scripts/enrich_linkedin.py --all` fills `outreach_crm/linkedin_<city>.csv` with verified email + LinkedIn URL via Apollo.io.

Cost: $49/mo for 1k credits ~= 700 hosts enriched (some won't match).

Once enriched, the 30/day cadence above gets a third channel for Tier A:
1. Send Contact Host DM
2. If no reply in 3 days, send LinkedIn connect
3. If they accept and don't reply, send email

Setup:
```bash
export APOLLO_API_KEY=...
python scripts/enrich_linkedin.py --all --limit 50  # smoke test first
python scripts/enrich_linkedin.py --all             # full run
```

---

## Cross-city orchestration

| Day                | Cities active           | Activity                                          |
|--------------------|-------------------------|---------------------------------------------------|
| Apr 27 (today)     | All                     | Submit PH coming-soon page (manual, on PH Ship)   |
| Apr 27-28          | Miami + 4 empty cities  | Re-scrape and seed listings.json + hosts.json     |
| Apr 28             | All 13                  | Run `enrich_linkedin.py --all` (background)       |
| Apr 29             | All                     | IH soft-launch post; daily_outreach starts        |
| Apr 29 - May 4     | All                     | 30/day Contact Host + 5/day LinkedIn DM           |
| May 5 - May 7      | Tier A                  | Push to 50 PH followers; second IH update         |
| May 8-10           | Tier A                  | Activation reminders to 50 followers              |
| Apr 27 evening     | All                     | X teaser + past-host warm DMs + PH forum thread   |
| **Apr 28 LAUNCH**  | All                     | Per `producthunt_launch.md` minute-by-minute      |
| Apr 29 - May 12    | Tier B + Tier C         | Ride PH afterglow; bulk Contact Host              |

---

## KPI dashboard (refresh weekly)

```bash
python scripts/crm_status.py
```

| Metric                    | Apr 27 | Target by Apr 28 |
|---------------------------|--------|------------------|
| Cities with full pipeline | 9      | 13               |
| Hosts queued              | 452    | 600+             |
| Hosts contacted           | 0      | 450              |
| Replies                   | 0      | 45 (10% reply)   |
| LinkedIn matches          | 0      | 250              |
| PH coming-soon followers  | -      | 50               |
| Paid signups              | 0      | 20               |

---

## Risks + mitigations

- **Airbnb account ban:** Cap Contact Host at 20/day, vary message phrasing per listing (already personalized via `generate_contact_host`).
- **FB group removal:** Lead with helpful content (free guide for one member), not link-first. Use `scripts/post_fb_groups.py` which throttles.
- **Apollo unmatched hosts:** Many Airbnb hosts use first-name-only profiles. Expect ~40% match rate; the unmatched rows still serve as a manual-lookup worklist.
- **PH momentum dies after hour 6:** The playbook has a "second wave of DMs" trigger. Daily_outreach.py keeps queue warm for that drop-in.
- **Render cold start during PH spike:** Bump to Starter plan T-24 hours per playbook.

---

See also: `tasks/producthunt_launch.md` for the launch-day minute-by-minute.
