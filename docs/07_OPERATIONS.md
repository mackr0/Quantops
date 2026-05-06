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
# All DBs in one shot — uses the same logic the scheduler runs at startup.
ssh root@67.205.155.63 'cd /opt/quantopsai && venv/bin/python -c "
from db_integrity import check_all_dbs, any_corrupt
results = check_all_dbs()
for path, info in results.items():
    print(f\"{path}: {info[\\\"status\\\"]} — {info[\\\"detail\\\"]}\")
print(\"corrupt:\", any_corrupt(results))
"'
```

The `db_integrity.check_db` function uses `PRAGMA quick_check` (storage-level only) and pre-screens for valid file size + SQLite magic-header. **Don't use `PRAGMA integrity_check` directly** — it also reports NOT NULL / UNIQUE / FK constraint violations on existing rows, which are NOT file corruption (and are a known false-positive after schema migrations that add NOT NULL columns).

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

`backup_daily.sh` runs from system cron at 05:00 UTC every day. Uses sqlite3's online `.backup` command (safe while the scheduler is writing). Snapshots the master DB, every per-profile DB, and the four alt-data DBs to `/opt/quantopsai/backups/` with filenames like `quantopsai.db.20260506-0500`. Prunes backups older than 14 days.

```bash
# Cron entry on prod
0 5 * * * /opt/quantopsai/backup_daily.sh

# Run a backup manually
ssh root@67.205.155.63 'bash /opt/quantopsai/backup_daily.sh'

# View backup directory
ssh root@67.205.155.63 'ls -lt /opt/quantopsai/backups/ | head'
```

Filename format produced: `<dbname>.db.<YYYYMMDD-HHMM>`. The `find_latest_backup` helper recognizes both this format and the legacy ad-hoc snapshot pattern `<basename>_<YYYY-MM-DD>_<HHMM>.db`. It explicitly rejects sidecar files (`-wal`, `-shm`) and corrupt-archive files (`<name>.corrupt-<TS>`) — both of which can appear in the directory and would corrupt a restore if matched.

Restoring a backup is a one-command operation — see §9 "Restoring from backup" for the verified runbook.

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

This runbook was rehearsed end-to-end on 2026-05-05 against a sandbox copy of `quantopsai_profile_11.db`. The rehearsal surfaced three latent bugs (sidecar matching, 0-byte file accepted as valid, `corrupt-*` archive matching) — all are fixed and covered by `tests/test_db_integrity.py`. Use this runbook with confidence.

Worst case: a DB has corrupted (orchestrator startup integrity check halted, `[scheduler] DB corrupt — refusing to start` in logs).

#### Step 1 — Identify the corrupt DB

```bash
ssh root@67.205.155.63 'cd /opt/quantopsai && venv/bin/python -c "
from db_integrity import check_all_dbs, any_corrupt
results = check_all_dbs()
for path, info in results.items():
    print(path, info)
print(\"corrupt:\", any_corrupt(results))
"'
```

#### Step 2 — Stop both prod services

```bash
ssh root@67.205.155.63 'systemctl stop quantopsai quantopsai-web'
```

This protects the live file from being written while you restore. The web app stays down for the duration; trading pauses.

#### Step 3 — Dry-run the restore (no files moved)

```bash
ssh root@67.205.155.63 'cd /opt/quantopsai && venv/bin/python -c "
from db_integrity import restore_from_backup
print(restore_from_backup(\"<corrupt_db_filename>\", dry_run=True))
"'
```

Replace `<corrupt_db_filename>` with the basename only, e.g. `quantopsai.db` or `quantopsai_profile_3.db`. Confirm the printed `from_backup` path is what you expect (most-recent backup, real file, NOT a `-wal` / `-shm` / `corrupt-*` file). If `status: error` and `detail: no backup found`, stop here and investigate — see "Recovery without a usable backup" below.

#### Step 4 — Real restore

```bash
ssh root@67.205.155.63 'cd /opt/quantopsai && venv/bin/python -c "
from db_integrity import restore_from_backup
print(restore_from_backup(\"<corrupt_db_filename>\"))
"'
```

The function:
1. Finds the latest backup (filtering out sidecars and corrupt-archive files).
2. Verifies the backup itself passes `quick_check` AND has a valid SQLite header (size + magic-bytes pre-check). Refuses to proceed if the backup is bad.
3. Moves the corrupt original aside as `<filename>.corrupt-<UTC-timestamp>`.
4. Copies the verified backup into place.
5. Re-runs `check_db` on the restored file. If verification fails, reports an error (the corrupt original is still archived alongside, so you can investigate manually).

Expected output on success:

```
{"status": "ok", "detail": "restored", "from_backup": "/opt/quantopsai/backups/<filename>.<TS>"}
DB restored: ... (corrupt original archived as .../<filename>.corrupt-<TS>)
```

#### Step 5 — Re-verify, then restart services

```bash
ssh root@67.205.155.63 'cd /opt/quantopsai && venv/bin/python -c "
from db_integrity import check_all_dbs, any_corrupt
print(\"corrupt after restore:\", any_corrupt(check_all_dbs()))
"'
ssh root@67.205.155.63 'systemctl start quantopsai-web quantopsai'
```

`any_corrupt` should print `[]`. The scheduler picks up where the backup left off. Any trades that resolved between the backup and the restoration are lost from the journal but still reflected in the broker's actual book — `_task_reconcile_trade_statuses` will surface the discrepancy on the next cycle.

#### Step 6 — Don't delete the corrupt archive yet

The function leaves the corrupt original at `<live_path>.corrupt-<TS>`. Keep it around for at least 7 days in case forensic analysis is needed (e.g. a malformed write surfaced from a code path that should be hardened). If disk space is tight, move it off-box rather than deleting it.

#### Recovery without a usable backup

If `restore_from_backup` reports no backup, your last resort is the `.dump` salvage path:

```bash
ssh root@67.205.155.63 'cd /opt/quantopsai && \
  sqlite3 quantopsai.db ".recover" | sqlite3 quantopsai.db.recovered'
```

This skips corrupt pages and produces a new DB file with whatever it could read. **Some rows will be lost.** Compare row counts before promoting `quantopsai.db.recovered` over `quantopsai.db`. Trading should be halted until you've manually verified critical tables (`trading_profiles`, `trades`, `ai_predictions`).

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
