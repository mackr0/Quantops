# Alt-Data Integration Plan — Wire 4 Standalone Projects into the AI

**Status:** Plan draft, ready for execution.

**Goal:** Move the four standalone alt-data projects from "built but
not yet wired in" to fully integrated AI-feeding signals, deployed
autonomously on the production droplet, with Layer 2 weight tuning so
each new signal is on the same self-correcting feedback loop as
everything else.

**The four projects:**

| Project | Source | Per-symbol Signal |
|---|---|---|
| `congresstrades` | Senate eFD + House Clerk | Recent congressional buys/sells (last 60 days) — count, dollar volume, party split |
| `edgar13f` | SEC EDGAR 13F-HR XML | Latest-quarter institutional ownership — total holders, top holder names, QoQ delta in shares held |
| `biotechevents` | ClinicalTrials.gov v2 + PDUFA tracker | Upcoming trial milestones for the sponsor — phase, days-until-milestone, recent phase changes |
| `stocktwits` | StockTwits REST API | Recent sentiment — 7-day bullish/bearish ratio, message volume, currently trending flag |

---

## Why now (the data-normalization argument)

The system has only ~1 week of resolved predictions. Adding 4 new
signals will shift the AI's behavior and a small pre-vs-post
comparison would be noisy. But:

1. The tuner's Layer 2 (signal weights) handles signal-level
   deprecation autonomously — if any new signal hurts WR, it gets
   nudged to 0.4 then 0.0 within ~9 days.
2. Layer 5 (cross-profile insight propagation) means a single
   profile discovering "stocktwits sentiment doesn't help me" can
   propagate that finding to peers automatically.
3. Doing it now means the long normalization period (~1 year of
   real-data accumulation) starts now. Delaying just delays the
   normalization clock.

So the cost of a 1-2 week behavior blip from new signals is small
compared to the lost runway if we wait.

---

## Architecture

### Read layer (in QuantOpsAI, plain SQLite, no project deps)

Each project gets one helper in `alternative_data.py`. The helper:

- Takes a `symbol` (and optionally a configurable DB path).
- Opens the project's SQLite DB read-only.
- Runs aggregation queries to produce a small dict of per-symbol stats.
- Caches results for 6 hours (alt-data refreshes once daily; no need
  to re-query within a cycle).
- Returns `{}` (graceful no-op) if the DB is missing / empty / errors.

DB paths default to `/opt/quantopsai-altdata/{project}/data/{db}.db`
on prod, `~/{project}/data/{db}.db` on local dev. Both via env vars
(`ALTDATA_BASE_PATH`) so devs and prod are configurable.

```python
# Approximate signatures
def get_congressional_recent(symbol: str) -> Dict[str, Any]:
    """{trades_60d, dollar_volume_60d, net_direction, last_filing_date,
        party_breakdown}"""

def get_13f_institutional(symbol: str) -> Dict[str, Any]:
    """{quarter, total_holders, total_shares, top_holder_name,
        top_holder_shares, qoq_share_change_pct}"""

def get_biotech_milestones(symbol: str) -> Dict[str, Any]:
    """{upcoming_pdufa_date, days_to_pdufa, drug_name, recent_phase_change,
        active_phase3_count}"""

def get_stocktwits_sentiment(symbol: str) -> Dict[str, Any]:
    """{net_sentiment_7d, message_count_7d, vs_avg_message_count,
        is_trending, last_updated}"""
```

### AI integration

Wire each helper into `alternative_data.get_all_alternative_data(symbol)`
under new keys: `congressional_recent`, `institutional_13f`,
`biotech_milestones`, `stocktwits_sentiment`.

Add to `ai_analyst._build_batch_prompt` four new `_weighted_signal_text`
blocks under the existing alt-data section. Each block:
- Renders a one-line summary if the data is meaningful (e.g.,
  "Congress: 3 buys, $250k, last filed 5d ago" or skips entirely
  if the project has no data for that symbol).
- Goes through `_weighted_signal_text(name, text)` so Layer 2 weights
  apply (omitted at weight 0.0, hint-decorated at 0.4/0.7).

### Layer 2 weight integration

Add 4 entries to `signal_weights.WEIGHTABLE_SIGNALS`:

```python
("congressional_recent",  "Congressional Trading (Recent)",
    lambda f: f.get("congressional_recent", {}).get("trades_60d", 0) > 0),
("institutional_13f",     "Institutional 13F Holdings",
    lambda f: f.get("institutional_13f", {}).get("total_holders", 0) > 0),
("biotech_milestones",    "Biotech Trial Milestones",
    lambda f: f.get("biotech_milestones", {}).get("days_to_pdufa") is not None),
("stocktwits_sentiment",  "StockTwits Sentiment",
    lambda f: f.get("stocktwits_sentiment", {}).get("message_count_7d", 0) > 0),
```

This puts them on the same 4-step ladder (1.0/0.7/0.4/0.0) as every
other signal. The tuner can autonomously discount or omit any of
them per-profile based on observed WR contribution.

### Meta-model feature integration

Add the same 4 keys to `features_payload` in `trade_pipeline.py`'s
prediction-recording path. Bool/numeric versions only (not raw nested
dicts) so the gradient-boosted classifier can use them.

---

## Production Deployment (Droplet Resources Verified)

Droplet has **52GB free disk**, **1.3GB available RAM**, **2 CPUs at
0.29 load**. The 4 projects total ~700MB on disk (fresh venvs +
DBs); each daily-refresh peak is well under 200MB; sequential
execution means no compounding pressure.

### Layout

```
/opt/quantopsai-altdata/
  congresstrades/
    {project source}
    venv/
    data/congress.db
  edgar13f/
    {project source}
    venv/
    data/edgar13f.db
  biotechevents/
    {project source}
    venv/
    data/biotechevents.db
  stocktwits/
    {project source}
    venv/
    data/stocktwits.db
  run-altdata-daily.sh   (copied from local)
  logs/
```

### Install procedure

1. `rsync` each project's source to the droplet (excluding existing
   venv/ and data/ — fresh on prod).
2. `python3 -m venv venv && pip install -r requirements.txt` per
   project.
3. `chmod +x run-altdata-daily.sh`.
4. Add to root crontab:
   `0 2 * * * cd /opt/quantopsai-altdata && ./run-altdata-daily.sh >> logs/altdata-$(date +\%Y\%m\%d).log 2>&1`
   (02:00 ET = 06:00 UTC, off hours.)
5. **First-run seeding** — manually invoke `./run-altdata-daily.sh`
   right after install so the DBs are populated before the next AI
   scan cycle. Without this, the read layer would no-op for the
   first 24 hours.

### Sync.sh exclusions

Add to the rsync `--exclude` list so deploys don't clobber the
alt-data project DBs:
```
--exclude '/opt/quantopsai-altdata/'
```

(Actually moot — sync.sh syncs to `/opt/quantopsai/`, not
`/opt/quantopsai-altdata/`. They're sibling paths and naturally
isolated. But documenting so future refactors don't cross the
streams.)

### API key handling

`congresstrades` and `edgar13f` use no API keys (public scraping).
`biotechevents` uses ClinicalTrials.gov v2 (no key required).
`stocktwits` uses the StockTwits REST API which is rate-limited but
no key required.

So nothing to provision — all four work out of the box on the
droplet's public-network IP.

---

## UI / Documentation Updates

1. **`templates/ai.html` — "What the AI Sees"** reference card:
   - Move 4 cards from "Built Locally — Not Yet Wired In" to "Per-Candidate Alternative Data"
   - Update the source-count heading from 15 → 19
   - Drop the "Built Locally — Not Yet Wired In" section if all four
     are now active (or update its wording if Patent Filing Velocity
     etc. remain placeholder).
2. **`AI_ARCHITECTURE.md`** — update §1c "What the AI Sees per
   Candidate" + the cost-per-cycle summary if call counts changed
   (they don't — these signals are local SQLite reads, no AI calls).
3. **`SELF_TUNING.md`** — add the 4 new weightable signals to the
   Layer 2 inventory.
4. **`CHANGELOG.md`** — single entry covering the integration.

---

## Anti-Regression Tests

1. **Read-layer unit tests** (`test_altdata_readers.py`) — for each
   helper: returns `{}` on missing DB, returns expected shape on
   seeded DB, handles SQL errors gracefully.
2. **Signal-weight registry coverage** — extend the existing
   `test_signal_weights_lifecycle` to verify the 4 new signals are
   in `WEIGHTABLE_SIGNALS` and their `is_active` predicates work.
3. **Prompt-builder integration** — verify each new signal block
   renders correctly + responds to weight 0.0/0.4/0.7/1.0 (extends
   `test_signal_weights.py`).
4. **No-snake-case guardrail** — already covers the 4 new signals
   automatically because they go through `display_label`.
5. **Schema-snapshot tests** — assert the read helpers don't
   regress when project schemas evolve. Use a seeded fixture per
   project that mirrors the production schema. If a project adds a
   column, the helper still works on the old schema (forward-compat).

---

## Implementation Waves

| Wave | Scope |
|---|---|
| **W1** | Read layer: 4 helpers in `alternative_data.py` + caching + tests + display_names entries |
| **W2** | AI integration: `get_all_alternative_data` wiring + 4 prompt blocks + Layer 2 weights + features_payload + tests |
| **W3** | Production deployment: rsync projects to `/opt/quantopsai-altdata/`, create venvs, seed DBs, install cron, verify first cron run |
| **W4** | UI + docs: "What the AI Sees" rewrite, AI_ARCHITECTURE.md update, SELF_TUNING.md update, CHANGELOG entry |

W1 + W2 can ship dormant (no harm if prod DBs aren't there yet —
helpers no-op gracefully). W3 makes them active. W4 closes the
documentation loop.

Total estimated scope: ~600 lines of new Python + ~300 lines of test
+ ~60 minutes of remote provisioning. Same shape and rigor as the
12-wave autonomous tuning rollout.

---

## Acceptance Criteria

- ✅ All 4 helpers return real data on prod (verified by hitting the
  AI page Operations → Active Lessons / Active Autonomy and seeing
  the 4 new signals show up in profiles' contexts).
- ✅ Cron runs nightly at 02:00 ET; logs visible in
  `/opt/quantopsai-altdata/logs/`.
- ✅ Layer 2 weights for the 4 new signals appear in
  `signal_weights.WEIGHTABLE_SIGNALS` and are tunable.
- ✅ "What the AI Sees" reference shows the signals as active.
- ✅ AI_ARCHITECTURE.md + SELF_TUNING.md + CHANGELOG.md updated.
- ✅ Full test suite green; new schema-snapshot tests cover the 4
  read helpers.
- ✅ Production deployed; first scan cycle after deploy includes
  the new signals in the AI prompt.
