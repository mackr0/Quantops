# 07 — Operations

**Audience:** SRE, ops engineers, anyone responsible for keeping the platform up.
**Purpose:** deploy, monitor, recover, audit. After reading this, an ops engineer can ship a change, diagnose a failure, and restore service.
**Last updated:** 2026-05-03.

## 1. Production layout

Single droplet at `67.205.155.63`. Resources: ~$6-12/month standard tier (CPU + 1-2 GB RAM is sufficient for the current workload).

```
/opt/quantopsai/
├── *.py                            # source (rsynced from local via sync.sh)
├── templates/                      # Jinja templates
├── tests/                          # test suite (NOT run on prod)
├── strategies/                     # strategy plugins
├── docs/                           # documentation
├── venv/                           # Python 3.9 venv
├── quantopsai.db                   # master DB (users, profiles, audit logs)
├── quantopsai_profile_<id>.db      # per-profile DB (1 per profile, 10+ total)
├── .cache/                         # disk caches (slippage K, Ken French CSVs)
├── .env                            # env vars (DB_PATH, ALTDATA_BASE_PATH, etc.)
├── online_meta_model_p<id>.pkl     # SGD freshness layer (per profile)
├── meta_model_<id>.pkl             # GBM batch model (per profile)
└── logs/                           # systemd journals (via journalctl)

/opt/quantopsai/altdata/             # merged into the Quantops repo 2026-05-04
├── biotechevents/                  # PDUFA + clinical trial scraper
├── congresstrades/                 # eFD + House Clerk scraper
├── edgar13f/                       # 13F-HR scraper
├── stocktwits/                     # StockTwits API ingest
├── run-altdata-daily.sh            # nightly orchestrator (uses Quantops venv)
└── logs/

/etc/systemd/system/
├── quantopsai-web.service
└── quantopsai-scheduler.service

/etc/nginx/sites-enabled/
└── quantopsai                      # TLS termination + reverse proxy → :8000
```

## 2. Process model

Three running processes:

| Service | Port | Purpose |
|---|---|---|
| `nginx` | 80, 443 | TLS termination + reverse proxy. |
| `quantopsai-web` | localhost:8000 | Gunicorn + Flask app. 4 workers default. |
| `quantopsai-scheduler` | (no port) | The trading loop. 24/7 process. |

`quantopsai-web` and `quantopsai-scheduler` both run from `/opt/quantopsai/venv/bin/python`. Both unit files use `Restart=always` with `RestartSec=5` so a crash auto-recovers.

## 3. Deploy

### sync.sh

Local: `sync.sh` is the single deploy command. Steps:

1. `rsync -avz --delete --exclude=...` from local to `/opt/quantopsai/`. Excludes `__pycache__`, `.cache/`, `*.db`, `tests/__pycache__`, etc.
2. `ssh root@67.205.155.63 'cd /opt/quantopsai && git fetch && git reset --hard origin/main'` — sync prod's `.git/` to GitHub. Without this step prod git would drift since rsync skips `.git/`.
3. Wait for the scheduler to be idle (no active task), then `systemctl restart quantopsai-scheduler quantopsai-web`.
4. Verify both services running.

Failure modes:

- **rsync fails:** check SSH connectivity. `ssh root@67.205.155.63` from the local machine.
- **Git reset fails:** check the prod git remote is still pointing at GitHub. `cd /opt/quantopsai && git remote -v`.
- **Scheduler doesn't return to idle:** `journalctl -u quantopsai-scheduler --since '5 min ago'` to find the stuck task. May need `systemctl restart quantopsai-scheduler` with `--force` (will kill in-flight tasks).

### Schema migrations

Migrations are baked into `models.init_user_db` and `journal.init_db`. They run on every process start; idempotent.

After a deploy that adds a schema column, the new column is available the next time a service starts. Existing rows have NULL for the new column (or the default if specified in the migration).

To force an immediate migration without restarting the scheduler:

```bash
ssh root@67.205.155.63 'cd /opt/quantopsai && /opt/quantopsai/venv/bin/python -c "
from journal import init_db
import os
for db in [f for f in os.listdir(\".\") if f.startswith(\"quantopsai_profile_\") and f.endswith(\".db\")]:
    init_db(db)
print(\"profile DBs migrated\")
from models import init_user_db
init_user_db()
print(\"master DB migrated\")
"'
```

## 4. Logs

`journalctl` is the source of truth.

```bash
# Scheduler logs (last hour)
journalctl -u quantopsai-scheduler --since '1 hour ago' | less

# Web app logs (last 100 lines)
journalctl -u quantopsai-web -n 100

# Both, real-time follow
journalctl -u quantopsai-scheduler -u quantopsai-web -f

# Filter for errors
journalctl -u quantopsai-scheduler --since today | grep -i error

# Filter by profile
journalctl -u quantopsai-scheduler --since today | grep "Mid Cap"
```

Log retention is journald's default (rotated by size + age). For longer retention, the AI cost ledger and `task_runs` tables persist forever per profile.

## 5. Monitoring

### Health-check endpoints (HTTP)

| Endpoint | Returns |
|---|---|
| `/api/scheduler-status` | Last cycle time per profile + active task. |
| `/api/cost-guard-status` | Daily AI spend + headroom. |
| `/api/scan-status/<profile_id>` | Per-profile scan timeliness. |
| `/api/portfolio/<profile_id>` | Equity + positions. |
| `/api/cycle-data/<profile_id>` | Last cycle's candidate set + outcomes. |

These should be sampled every 5 minutes by an external uptime checker (UptimeRobot, Healthchecks.io, or similar). The platform does NOT have a built-in alerting stack; that's external.

### Database health checks

```bash
# Master DB integrity
sqlite3 /opt/quantopsai/quantopsai.db 'PRAGMA integrity_check;'

# Per-profile DB integrity (loop)
for db in /opt/quantopsai/quantopsai_profile_*.db; do
    echo "== $db =="
    sqlite3 "$db" 'PRAGMA integrity_check;'
done
```

### Free disk

The DBs grow ~10-50 MB per month per profile. Caches grow modestly. Plan for 1-2 GB free at minimum.

```bash
df -h /opt/quantopsai
du -sh /opt/quantopsai/*.db
```

### AI cost monitoring

`/api/cost-guard-status` returns today's spend + headroom + the daily ceiling. The cost guard auto-suppresses spend-affecting actions (model upgrades, ensemble re-runs) when over budget.

```bash
# Spot check: what did we spend in the last 24h?
sqlite3 /opt/quantopsai/quantopsai_profile_3.db \
  "SELECT SUM(usd_cost) FROM ai_cost_ledger WHERE timestamp >= datetime('now', '-1 day')"
```

## 6. Backups

`_task_db_backup` runs once daily per profile. Snapshots both the master DB and each per-profile DB to a `backups/` directory with date-stamped filenames. Retention: 7 daily + 4 weekly + 3 monthly (rolling).

```bash
# Manual backup (via Python)
ssh root@67.205.155.63 'cd /opt/quantopsai && /opt/quantopsai/venv/bin/python -c "
from backup_db import backup_all_dbs
backup_all_dbs()
"'

# View backup directory
ssh root@67.205.155.63 'ls -lt /opt/quantopsai/backups/ | head'
```

Restoring a backup is a manual operation: stop the scheduler, copy the desired backup file to the live DB path, restart.

## 7. Cron / scheduled tasks

The `quantopsai-scheduler` process IS the scheduler. There is no system cron. Per-cycle and once-per-day tasks are dispatched inside `multi_scheduler.run_scheduler()`.

The 4 alt-data scrapers (now bundled in `altdata/` after the 2026-05-04 merge) are orchestrated by a single system cron entry that calls `altdata/run-altdata-daily.sh` (refreshes all 4 sequentially using the Quantops venv).

```bash
# Cron entry on prod (06:00 UTC daily)
0 6 * * * cd /opt/quantopsai && ALTDATA_BASE_PATH=/opt/quantopsai/altdata bash altdata/run-altdata-daily.sh >> logs/altdata-$(date +%Y%m%d).log 2>&1

# View it
ssh root@67.205.155.63 'crontab -l'
```

The PDUFA event scraper (`pdufa_scraper.py`, OPEN_ITEMS #6) is scheduled separately by `multi_scheduler._task_pdufa_scrape` (once per UTC day, idempotent). It pulls "PDUFA date" mentions from SEC EDGAR 8-K full-text search and writes to `altdata/biotechevents/data/biotechevents.db.pdufa_events`.

## 8. Common operational scenarios

### 8a. Deploy went out, scheduler stuck

```bash
ssh root@67.205.155.63
journalctl -u quantopsai-scheduler -f         # see what it's doing
systemctl status quantopsai-scheduler         # is it actually running?
systemctl restart quantopsai-scheduler        # restart if needed
```

### 8b. Web app returns 500 on a specific page

Most likely cause: a stale dashboard data structure. Check:

```bash
journalctl -u quantopsai-web --since '10 min ago' | grep -i traceback
```

If the error references a per-profile DB column, run the schema migration as shown in §3.

### 8c. AI provider unavailable / rate-limited

The `ai_providers` layer has retry-with-backoff. If a provider is hard-down, the cycle fails for that profile but the scheduler continues with other profiles. The next cycle (5 min later) re-tries.

If a provider is rate-limited persistently, switch the profile to a different provider via the settings page (no code change required).

### 8d. Alpaca outage

Order submissions fail with `(503) Service Unavailable` or similar. The trade pipeline catches these as recoverable; logs a warning; skips submission for that cycle. Existing protective stops AT the broker still work — the broker's risk system isn't dependent on our connectivity.

### 8e. Schema drift (column added on prod doesn't exist locally, or vice versa)

`init_user_db` and `init_db` are idempotent, so prod auto-migrates on next start. If LOCAL is missing a column that prod has (e.g. a test DB), restart the local web app or just delete the local DB to force re-creation.

### 8f. A specific profile is stuck pending forever

Most likely a stuck pending order. Check:

```bash
sqlite3 /opt/quantopsai/quantopsai_profile_<id>.db \
  "SELECT id, symbol, status, timestamp FROM trades WHERE status='open' ORDER BY id DESC LIMIT 20"
```

Manual reconciliation: `_task_reconcile_trade_statuses` runs each cycle. If it's not reconciling, the order may be stuck on the broker. Check Alpaca dashboard.

### 8g. AI cost spike

Symptom: `/api/cost-guard-status` shows over-budget. Investigation:

```bash
sqlite3 /opt/quantopsai/quantopsai_profile_<id>.db \
  "SELECT purpose, COUNT(*), SUM(usd_cost) FROM ai_cost_ledger
   WHERE timestamp >= datetime('now', '-1 day')
   GROUP BY purpose ORDER BY SUM(usd_cost) DESC"
```

Most common causes:

- A specialist's calibration broke and it's re-running on every cycle. Check the AI Awareness ensemble panel for veto rate spikes.
- A new model is configured (`ai_model`) at higher cost than baseline. Verify settings.
- A specific cache is missing (e.g. a third-party API down causes repeated retries). Check log for `cache miss` patterns.

## 9. Incident response

### Severity classes

- **SEV-1 (immediate):** orders submitted incorrectly, P&L corruption, security breach. **Action:** stop the scheduler immediately (`systemctl stop quantopsai-scheduler`); investigate before resuming.
- **SEV-2 (within hours):** AI provider down, Alpaca outage, single profile stuck. **Action:** monitor; let the scheduler retry; investigate root cause if persistent.
- **SEV-3 (within days):** dashboard glitch, cost overrun, performance degradation, slippage K drift. **Action:** investigate during normal hours; fix in next deploy.

### Stopping the scheduler immediately

```bash
ssh root@67.205.155.63 'systemctl stop quantopsai-scheduler'
```

The web app stays up; users can still see dashboards but no new trades fire. Existing protective stops at the broker remain active.

### Restoring from backup

Worst case (data corruption):

```bash
ssh root@67.205.155.63
systemctl stop quantopsai-scheduler quantopsai-web
cp /opt/quantopsai/backups/quantopsai_profile_3_2026-05-02.db \
   /opt/quantopsai/quantopsai_profile_3.db
systemctl start quantopsai-web quantopsai-scheduler
```

The scheduler will pick up where the backup left off. Any trades that resolved between the backup and the restoration are lost from the journal but still reflected in the broker's actual book — `_task_reconcile_trade_statuses` will eventually surface the discrepancy.

### Killing a stuck task

```bash
# Find the process
ssh root@67.205.155.63 'ps aux | grep python'

# Kill the whole scheduler (recovers automatically)
ssh root@67.205.155.63 'systemctl restart quantopsai-scheduler'
```

The watchdog (`_task_run_watchdog`) tries to do this automatically for tasks running longer than the cycle window, but a hard SIGKILL is sometimes needed.

## 10. Adding a new profile

1. Settings page → "Create new profile" → fill in name, market_type, Alpaca account.
2. The schema migration auto-runs; new per-profile DB is created on first cycle.
3. The first cycle for the new profile fires within 5 minutes of settings save.

## 11. Adding a new Alpaca account

1. Generate paper API keys at Alpaca dashboard.
2. Add the account via the Settings → Alpaca Accounts page.
3. Map profiles to it via the per-profile settings.

The 3-account cap is enforced at the master DB layer (`alpaca_accounts`); attempting to add a 4th surfaces a UI error (Alpaca's per-user limit). Move to multiple users for additional accounts.

## 12. Cost ceiling

The cost guard daily ceiling is per-user, not per-profile. Configure via settings → Cost Guard. Default: trailing-7-day-avg × 1.5 (floor $5/day).

## 13. Quarterly housekeeping

Once a quarter:

1. **Run the grep sweep** documented in `OPEN_ITEMS.md` "How this list is maintained" — catches code-level deferrals.
2. **Re-validate Ken French data fetch** — Dartmouth occasionally changes URL formats.
3. **Re-validate scrape targets** — BiopharmCatalyst, App Store RSS, Wikipedia API, all have layout drift risk.
4. **Review specialist Platt-scaling slopes** — the AI Awareness ensemble panel surfaces this.
5. **Audit the `_DISPLAY_NAMES` registry** — make sure no recent identifiers are missing entries.
6. **Update macro event calendar** in `macro_event_tracker.py` with the next year of FOMC / CPI / NFP.
7. **Review the OPEN_ITEMS list** — close stale items, add new ones.

## 14. Failure modes (catalog)

Documented historical failure modes that informed the guardrails:

- **Disabled-specialists drift (2026-04-28):** `_task_specialist_health_check` wrote to DB but UserContext didn't carry the field, so the running scheduler ignored the disable list. Fixed via `test_ctx_field_round_trip` guardrail.
- **Snake-case in /settings 500 (multiple):** prof.get('field', default) returns None when DB has NULL, then None × 100 crashes. Fixed via `(prof.get('field') or default)` pattern + regression test using `PRAGMA table_info` to NULL all nullable numeric fields.
- **Yfinance overuse (multiple):** new code added yfinance calls instead of Alpaca. Fixed via auto-memory feedback rule and `feedback_alpaca_first_data.md`.
- **CHANGELOG / commit pairing miss (multiple):** production .py commits without CHANGELOG entries. Fixed via `test_recent_py_commits_paired_with_changelog`.
- **Hidden levers (multiple):** new feature ships without settings UI. Fixed via the four UI-coverage guardrails (see `docs/10_METHODOLOGY.md`).
- **Skipped tests (2026-05-02):** silent test skips hid real failures. Fixed by removing all skips; CI fails on any new skip.
- **Slippage saturation (2026-05-01):** SGD with raw mixed-scale features saturated to 0.0/1.0 on prod data. Fixed with StandardScaler + sklearn defaults for bootstrap fit.
- **Tz-naive vs tz-aware index mismatch (2026-05-01):** Alpaca bars (tz-aware) joined with Ken French (tz-naive). Fixed via explicit `tz_localize(None).normalize()` in stress scenarios.

The full incident catalog is in `CHANGELOG.md`.

## See also

- `docs/04_TECHNICAL_REFERENCE.md` — system architecture.
- `docs/08_RISK_CONTROLS.md` — risk gates.
- `OPEN_ITEMS.md` — what's pending.
