# HostGuide Lead Report — Apr 28, 2026 (since Apr 21)

Generated from Gmail scan: `from:airbnb.com after:2026/04/21`.

**Top insight:** active Airbnb conversations are NOT rate-limited the same way as new contact-host messages. The 7 hosts below replied to your today's outreach — you can follow up in those existing threads right now without hitting the daily cap.

---

## TIER 1 — HOT: Hosts who replied to your Apr 28 outreach

These are sitting in your Airbnb inbox waiting. Each one had an inbound message in the last few hours. Reply in the existing thread with a soft HostGuide pitch.

| # | Host | Listing | City | Status | Thread / signal |
|---|------|---------|------|--------|-----------------|
| 1 | **Vibra Bonita CO** | Polados hidden 2BR gem | Medellín | **PRE-APPROVED your trip** (most engaged) | Reply window closes 1:01 PM Colombia time Apr 29 |
| 2 | **Stanton** (host + co-host) | Small Serenity 4 - Total Remodel - 4 Min to Beach | Destin, FL | Replied 2x (host + co-host both) | Strongest engagement signal |
| 3 | **Mitch & Lee** | Beachside Ground-Floor Getaway with Pool | Destin, FL | Replied "Hello Umar, thank you for reaching out. No..." | Warm |
| 4 | **Minty** (co-host) | Luxury and Stylish - 4BD 4BTH - Las Letras | Madrid | Replied "Hello Umur, thanks for reaching out and..." | Warm |
| 5 | **Equipa Alexandra** (co-host) | Parque Nações Junto ao Rio | Lisbon | Replied "Thanks for the..." | Warm |
| 6 | **Tagus** | Tagus's place | Lisboa | Sent invitation to book | Engaged |
| 7 | **The Homeboat Company** | Homeboat in Lisbon | Lisbon | Sent invitation to book | Unique angle (boat) |

### Suggested follow-up message (paste in same Airbnb thread)

> Hey [name], thanks for getting back. Quick thing while we're chatting — I host in Geneva and built a small tool that auto-generates a printable neighborhood guide from any Airbnb URL in 60s (host-guide.net). Walking times, top cafes, transit, ride apps. $1.99 to test on your [neighborhood] listing if you want to see how it does for [city].

---

## TIER 2 — Inbound guest activity (Apr 21-28)

Guests who booked or inquired at your Modern Stylish Apartment (now unlisted Apr 22 — these conversations are still in your inbox).

| Guest | Stay dates | From | Status |
|-------|------------|------|--------|
| Adriano Mendes | Apr 24-27 (PAST) | Lisbon, Portugal | Stayed, no review yet |
| Spandana | May 21-23 (FUTURE) | (booker) | Booking modified Apr 23 |
| Olivia | May 8-11 (FUTURE) | (booker) | Confirmed Apr 23 |
| Stéphanie | Jul 11-13 (REQUEST EXPIRED Apr 15) | Geneva, Switzerland | Request let expire |
| Joshua | Jun 17-20 (REQUEST EXPIRED Apr 9) | California, US | Has 18 reviews — likely a host himself |

**Note:** Your listing was unlisted Apr 22. Future bookings (Olivia May 8-11, Spandana May 21-23) likely got cancelled — check your Airbnb support thread (multiple "New message from Airbnb Support" emails on Apr 22).

---

## TIER 3 — Past hosts you stayed with (warm relationships, still relevant)

You've already hit Joao (the 1 you sent today). Remaining 6 in `outreach_crm/queue_past_hosts.md` are still there. Plus newer ones since Apr 21:

- **Joao** (Lisbon, Albuquerque Suite 2) — sent today ✓
- **Filipa** (Lisbon, Charming Apartment Historical Center) — recent stay Apr 5-10
- **Zurin Charm Hotel** (Lisbon, standard double) — recent stay Mar 29-Apr 2
- **Miguel** (Costa da Caparica, "Appartamento accogliente con piante e colori") — stayed Mar 24-27, NEW since past_hosts queue was built
- **Sara** (San Pietro, Italy, Camera Doppia Silice with jacuzzi) — stayed Mar 25-29, NEW since past_hosts queue was built
- **Camilla** (San Pietro?, "Appartamento accogliente con piante e colori") — host name from another stay

Action: re-run `python scripts/build_past_hosts_queue.py` (or manually add Miguel + Sara + Camilla to `queue_past_hosts.md`) before sending the next batch.

---

## What to do now (in priority order)

1. **Open Airbnb inbox**, find the 7 Tier 1 threads (sorted by today's date).
2. Reply with the soft pitch above, customizing the [name] / [city] tokens.
3. These 7 sends do NOT count against the daily cap (existing threads).
4. Mark each as "sent" in the CRM via:
   ```bash
   python scripts/build_outreach_queue.py <city> --mark-sent <listing_id>
   ```

Tier 1 should be done in ~20 min. Tier 3 follow-ups can wait until tomorrow when the rate-limit window resets.
