# Universe popup — Calculation Register

Sources: `views.py universe_popup` (~5921) + `/api/universe/<id>`
(~5965), `segments.py`, `models.get_cached_names`.
Verification pass: 2026-08-13 at `70ffbe2`.

**"N symbols"** — length of the segment's static in-code universe
list. **VERIFIED**

**"+ N custom"** — watchlist symbols not already in the base set
(profile `custom_watchlist` JSON; parse failure → empty, never an
error). **VERIFIED (code)**

**Name column** — cached `symbol_names` lookup; popup renders blank
for unknown names while the API variant falls back to the ticker —
a cosmetic inconsistency, noted, below suspect threshold.
**VERIFIED (code)**

**Header label** — popup uses the profile's asset-class label; the
API uses the segment's name — same nuance as above, noted.

## Open items — none registered (two cosmetic popup-vs-API
inconsistencies noted inline).
