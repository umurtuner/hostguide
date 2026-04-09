# HostGuide — Feature Backlog

## Queued

### Landing Page with Paywall
- Simple landing page where users input their Airbnb listing link
- System generates the guide automatically
- Guide preview shown (blurred or partial)
- Paywall to download full guide:
  - $5 one-time per guide
  - $19/month unlimited listings
  - First guide free (lead gen)
- Stack: static HTML or simple Flask page, Stripe for payments
- Priority: HIGH — this is the monetization path

### Google Places API Integration
- Replace Overpass/OSM with Google Places API for reliable POI data
- Especially critical for US suburban listings where OSM is sparse
- Need: GOOGLE_MAPS_API_KEY
- Priority: HIGH — current OSM approach is unreliable (rate limiting, 504s)

### Public Deployment
- Deploy static site to public URL (GitHub Pages or Vercel)
- Needs: `gh auth login` or `npx vercel login` (manual browser auth)
- Priority: MEDIUM

### FB Group Monitoring
- Monitor 40 pending FB group approvals
- Post freemium copy + guide screenshots when approved
- Priority: MEDIUM
