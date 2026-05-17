#!/bin/bash
# Daily morning health check (#172, 2026-05-17).
#
# Distinct from verify_first_cycle.sh:
#   verify_first_cycle.sh   — post-deploy regression check (pin every
#                              shipped fix to its deploy cutoff)
#   morning_health_check.sh — daily operational check (drift counts,
#                              audit freshness, profile snapshots)
#
# Built around the seven-tier integrity contract + audit_runner shipped
# 2026-05-17. Discovers active profiles dynamically from quantopsai.db
# (no hardcoded ID list) so it survives the fresh-experiment profile
# rotation.
#
# Usage:  ./morning_health_check.sh
#         ./morning_health_check.sh <droplet-ip>
#         ./morning_health_check.sh <ip> <since-utc>
#
# Returns 0 on clean (only warnings OK), 1 if any real failure.

set -uo pipefail

DROPLET=${1:-67.205.155.63}
TODAY_UTC=$(date -u +%Y-%m-%d)
SINCE_UTC=${2:-"$TODAY_UTC 00:00 UTC"}

PASS=0
FAIL=0
WARN=0

ok()    { echo "  ✅ $*"; PASS=$((PASS+1)); }
bad()   { echo "  ❌ $*"; FAIL=$((FAIL+1)); }
warn()  { echo "  ⚠️  $*"; WARN=$((WARN+1)); }
hdr()   { echo; echo "── $* ──"; }

# journalctl against prod scheduler
J() {
    ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 $1"
}

# sqlite3 one-shot against a DB on prod
SQL() {
    local db=$1 query=$2
    ssh root@$DROPLET "sqlite3 /opt/quantopsai/$db \"$query\"" 2>/dev/null
}

# Dynamic profile discovery — replaces the hardcoded list that
# verify_first_cycle.sh used. Re-run any time profiles change.
discover_profiles() {
    ssh root@$DROPLET "sqlite3 /opt/quantopsai/quantopsai.db \
        'SELECT id FROM trading_profiles WHERE enabled = 1 ORDER BY id'" 2>/dev/null
}

ACTIVE_PROFILE_IDS=$(discover_profiles)
ACTIVE_COUNT=$(echo "$ACTIVE_PROFILE_IDS" | grep -c .)

echo "====================================================="
echo "  Morning health check — $TODAY_UTC"
echo "  Window: $SINCE_UTC → now"
echo "  Active profiles: $ACTIVE_COUNT ($(echo $ACTIVE_PROFILE_IDS | tr '\n' ',' | sed 's/,$//'))"
echo "====================================================="

# =============================================================================
# Section 0 — Service health
# =============================================================================
hdr "0. Services + deploy hygiene"

echo "[0.1] systemd: quantopsai (scheduler) + quantopsai-web (gunicorn)"
SCHED_STATE=$(ssh root@$DROPLET "systemctl is-active quantopsai" 2>&1)
WEB_STATE=$(ssh root@$DROPLET "systemctl is-active quantopsai-web" 2>&1)
if [ "$SCHED_STATE" = "active" ] && [ "$WEB_STATE" = "active" ]; then
    ok "scheduler=$SCHED_STATE  web=$WEB_STATE"
else
    bad "scheduler=$SCHED_STATE  web=$WEB_STATE — at least one service is not running"
fi

echo
echo "[0.2] git: prod HEAD = origin/main"
PROD_HEAD=$(ssh root@$DROPLET "cd /opt/quantopsai && git rev-parse HEAD")
ORIGIN_HEAD=$(git -C /Users/mackr0/Quantops rev-parse origin/main 2>/dev/null \
    || git rev-parse origin/main 2>/dev/null \
    || echo "?")
if [ "$PROD_HEAD" = "$ORIGIN_HEAD" ] && [ "$PROD_HEAD" != "?" ]; then
    ok "prod HEAD = $PROD_HEAD (matches origin/main)"
else
    warn "prod HEAD = $PROD_HEAD, origin/main = $ORIGIN_HEAD (push pending?)"
fi

echo
echo "[0.3] gunicorn workers fresh (< 7 days)"
WORKER_DAYS=$(ssh root@$DROPLET \
    "ps -eo etime,cmd | grep gunicorn | grep -v grep | head -1 | awk '{print \$1}'")
case "$WORKER_DAYS" in
    *-*)  bad "gunicorn worker uptime $WORKER_DAYS — sync.sh may have skipped restart" ;;
    *)    ok "gunicorn workers respawned within last 24h ($WORKER_DAYS)" ;;
esac

# =============================================================================
# Section A — Scheduler liveness
# =============================================================================
hdr "A. Scheduler liveness"

echo "[A1] Last scheduler cycle completed within 20 minutes"
# main_loop writes to scheduler_status.json on every cycle completion;
# its updated_at is the source of truth.
LAST_CYCLE_AGE_SEC=$(ssh root@$DROPLET \
    "cd /opt/quantopsai && /opt/quantopsai/venv/bin/python3 -c \"
import json, time
try:
    with open('scheduler_status.json') as f:
        s = json.load(f)
    print(int(time.time() - float(s.get('updated_at', 0))))
except Exception as e:
    print(99999)
\"" 2>/dev/null)
if [ "${LAST_CYCLE_AGE_SEC:-99999}" -lt 1200 ]; then
    ok "last cycle completed ${LAST_CYCLE_AGE_SEC}s ago"
elif [ "${LAST_CYCLE_AGE_SEC}" -lt 3600 ]; then
    warn "last cycle completed ${LAST_CYCLE_AGE_SEC}s ago — over 20 min, under 1 hour"
else
    bad "last cycle completed ${LAST_CYCLE_AGE_SEC}s ago — scheduler may be stalled"
fi

echo
echo "[A2] No TASK FAIL events since session start"
TASK_FAILS=$(J "| grep -c 'TASK FAIL'")
if [ "${TASK_FAILS:-0}" -eq 0 ]; then
    ok "zero TASK FAIL events"
else
    bad "$TASK_FAILS TASK FAIL events — investigate"
    J "| grep -B1 -A2 'TASK FAIL' | head -20"
fi

# =============================================================================
# Section B — Seven-tier integrity contract (audit_runner)
# =============================================================================
hdr "B. Integrity audits (#157,qty,#165,#167a,#167b,#166,#170)"

echo "[B1] audit_alerts table exists + audit_runner has run"
AUDIT_TABLE_ROWS=$(SQL "quantopsai.db" \
    "SELECT COUNT(*) FROM audit_alerts;")
if [ -z "$AUDIT_TABLE_ROWS" ]; then
    bad "audit_alerts table missing — audit_runner not yet deployed or DB never opened"
else
    ok "audit_alerts table exists ($AUDIT_TABLE_ROWS total rows ever recorded)"
fi

echo
echo "[B2] Unresolved drift by audit type"
# Show count of currently-active drift signatures, grouped by audit
DRIFT_BY_TYPE=$(SQL "quantopsai.db" \
    "SELECT audit_type, COUNT(*) FROM audit_alerts \
     WHERE resolved_at IS NULL GROUP BY audit_type;")
if [ -z "$DRIFT_BY_TYPE" ]; then
    ok "zero unresolved drift items across all 7 audit types"
else
    TOTAL_UNRESOLVED=$(SQL "quantopsai.db" \
        "SELECT COUNT(*) FROM audit_alerts WHERE resolved_at IS NULL;")
    bad "$TOTAL_UNRESOLVED unresolved drift item(s):"
    echo "$DRIFT_BY_TYPE" | while IFS='|' read -r atype count; do
        echo "      - $atype: $count"
    done
fi

echo
echo "[B3] First-detection alerts delivered (alert_sent flag)"
UNSENT=$(SQL "quantopsai.db" \
    "SELECT COUNT(*) FROM audit_alerts \
     WHERE resolved_at IS NULL AND alert_sent = 0;")
if [ "${UNSENT:-0}" -eq 0 ]; then
    ok "every active drift item has been emailed (or none exist)"
else
    bad "$UNSENT active drift item(s) have NEVER been emailed — notify path broken?"
fi

# =============================================================================
# Section C — Reconciler heartbeat (dynamic per profile)
# =============================================================================
hdr "C. Reconciler heartbeat (per active profile)"

if [ "$ACTIVE_COUNT" -eq 0 ]; then
    warn "no active profiles — skipping heartbeat check"
else
    STALE_PROFILES=""
    for pid in $ACTIVE_PROFILE_IDS; do
        # Latest "Reconcile Trade Statuses" row in task_runs.
        LATEST=$(SQL "quantopsai_profile_${pid}.db" \
            "SELECT MAX(started_at) FROM task_runs \
             WHERE task_name LIKE '%Reconcile Trade Statuses%';")
        if [ -z "$LATEST" ] || [ "$LATEST" = "" ]; then
            STALE_PROFILES="$STALE_PROFILES pid${pid}(never-ran)"
            continue
        fi
        # Age in minutes
        AGE_MIN=$(ssh root@$DROPLET \
            "/opt/quantopsai/venv/bin/python3 -c \"
from datetime import datetime, timezone
ts = '$LATEST'.replace('Z','+00:00')
try:
    t = datetime.fromisoformat(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    age = (datetime.now(tz=timezone.utc) - t).total_seconds() / 60
    print(int(age))
except Exception:
    print(99999)
\"")
        if [ "${AGE_MIN:-99999}" -gt 60 ]; then
            STALE_PROFILES="$STALE_PROFILES pid${pid}(${AGE_MIN}m)"
        fi
    done
    if [ -z "$STALE_PROFILES" ]; then
        ok "all $ACTIVE_COUNT active profiles ran reconciler within last 60 min"
    else
        bad "stale reconciler:$STALE_PROFILES — silent cron failure or scheduler stall"
    fi
fi

# =============================================================================
# Section D — Activity capture (#168)
# =============================================================================
hdr "D. Broker activity capture (DIV/OPEXP/OPASN/OPXRC)"

echo "[D1] Capture task ran today on every active profile"
if [ "$ACTIVE_COUNT" -eq 0 ]; then
    warn "no active profiles"
else
    NEVER_CAPTURED=""
    for pid in $ACTIVE_PROFILE_IDS; do
        N=$(SQL "quantopsai_profile_${pid}.db" \
            "SELECT COUNT(*) FROM task_runs \
             WHERE task_name LIKE '%Capture Broker Activities%' \
               AND date(started_at) = '$TODAY_UTC';")
        if [ "${N:-0}" -eq 0 ]; then
            NEVER_CAPTURED="$NEVER_CAPTURED pid${pid}"
        fi
    done
    if [ -z "$NEVER_CAPTURED" ]; then
        ok "capture task ran today on all $ACTIVE_COUNT active profiles"
    else
        bad "no capture today:$NEVER_CAPTURED"
    fi
fi

echo
echo "[D2] Activities captured in last 7 days (DIV / OPEXP / OPASN / OPXRC)"
TOTAL_DIV=0
TOTAL_OPEXP=0
TOTAL_OPASN=0
for pid in $ACTIVE_PROFILE_IDS; do
    D=$(SQL "quantopsai_profile_${pid}.db" \
        "SELECT COUNT(*) FROM trades WHERE signal_type='DIVIDEND' \
         AND date(timestamp) >= date('now','-7 days');")
    E=$(SQL "quantopsai_profile_${pid}.db" \
        "SELECT COUNT(*) FROM trades WHERE signal_type='OPEXP' \
         AND date(timestamp) >= date('now','-7 days');")
    A=$(SQL "quantopsai_profile_${pid}.db" \
        "SELECT COUNT(*) FROM trades WHERE signal_type='OPASN' \
         AND date(timestamp) >= date('now','-7 days');")
    TOTAL_DIV=$((TOTAL_DIV + ${D:-0}))
    TOTAL_OPEXP=$((TOTAL_OPEXP + ${E:-0}))
    TOTAL_OPASN=$((TOTAL_OPASN + ${A:-0}))
done
echo "      DIV=$TOTAL_DIV  OPEXP=$TOTAL_OPEXP  OPASN=$TOTAL_OPASN (across all profiles, 7-day window)"
# Zero is fine for a brand-new account — just informational
ok "activity counts captured"

# =============================================================================
# Section E — Daily equity snapshot
# =============================================================================
hdr "E. Daily equity snapshot (#164)"

echo "[E1] Today's daily_snapshots row exists on every active profile"
MISSING_SNAPSHOT=""
for pid in $ACTIVE_PROFILE_IDS; do
    N=$(SQL "quantopsai_profile_${pid}.db" \
        "SELECT COUNT(*) FROM daily_snapshots \
         WHERE date = '$TODAY_UTC';")
    if [ "${N:-0}" -eq 0 ]; then
        MISSING_SNAPSHOT="$MISSING_SNAPSHOT pid${pid}"
    fi
done
if [ -z "$MISSING_SNAPSHOT" ]; then
    ok "every active profile has a daily_snapshots row for $TODAY_UTC"
elif [ "$(date -u +%H)" -lt 16 ]; then
    # Daily snapshot fires at end of trading day (~16:30 UTC). Before
    # that, missing today's row is normal.
    warn "snapshot missing for$MISSING_SNAPSHOT — normal pre-market-close"
else
    bad "snapshot missing for$MISSING_SNAPSHOT — _task_daily_snapshot may have failed"
fi

# =============================================================================
# Section F — Comparative-returns API responds
# =============================================================================
hdr "F. Comparative-returns API"

echo "[F1] /api/comparative-returns returns valid JSON"
API_RESPONSE=$(ssh root@$DROPLET \
    "curl -s -b /tmp/.qa_cookie http://localhost:8000/api/comparative-returns 2>&1 || echo FAILED")
if echo "$API_RESPONSE" | grep -qE '("series"|"empty_state")'; then
    # Count returned series (jq fallback)
    SERIES_COUNT=$(ssh root@$DROPLET \
        "/opt/quantopsai/venv/bin/python3 -c \"
import json, sys
try:
    d = json.loads('''$API_RESPONSE''')
    print(len(d.get('series', [])))
except Exception:
    print(-1)
\"" 2>/dev/null)
    if [ "${SERIES_COUNT:-0}" -ge 0 ]; then
        ok "API responded — $SERIES_COUNT series returned"
    else
        warn "API responded but couldn't count series (auth required for non-empty data)"
    fi
elif echo "$API_RESPONSE" | grep -qE '(login|sign.?in)'; then
    warn "API needs login cookie — endpoint reachable but not authenticated in this script"
else
    bad "API failed: $(echo $API_RESPONSE | head -c 200)"
fi

# =============================================================================
# Section G — AI cost so far today
# =============================================================================
hdr "G. AI cost today"

TOTAL=0
for pid in $ACTIVE_PROFILE_IDS; do
    C=$(SQL "quantopsai_profile_${pid}.db" \
        "SELECT printf('%.4f',COALESCE(SUM(estimated_cost_usd),0)) \
         FROM ai_cost_ledger WHERE date(timestamp)='$TODAY_UTC';")
    TOTAL=$(echo "$TOTAL + ${C:-0}" | bc -l 2>/dev/null || echo "$TOTAL")
done
echo "  Cumulative across $ACTIVE_COUNT profiles: \$$(printf '%.2f' $TOTAL)"

# =============================================================================
# Section H — Alt-data freshness
# =============================================================================
hdr "H. Alt-data DBs refreshed within 30h"

# Auto-discover every altdata DB by glob rather than hardcoding the
# (project, filename) pairs. Hardcoding broke this section once when
# edgar_form4's filename was guessed as 'form4.db' (the actual file
# is 'edgar_form4.db'); discovery eliminates the guessing surface
# entirely and survives any future altdata module being added.
STALE=""
NOW_EPOCH=$(date -u +%s)
DBS=$(ssh root@$DROPLET "ls /opt/quantopsai/altdata/*/data/*.db 2>/dev/null")
for db_path in $DBS; do
    proj=$(basename "$(dirname "$(dirname "$db_path")")")
    db_name=$(basename "$db_path")
    MTIME=$(ssh root@$DROPLET "stat -c %Y $db_path 2>/dev/null" \
        || echo 0)
    AGE_HOURS=$(( (NOW_EPOCH - MTIME) / 3600 ))
    if [ "$AGE_HOURS" -gt 30 ]; then
        STALE="$STALE $proj/$db_name(${AGE_HOURS}h)"
    fi
done
if [ -z "$STALE" ]; then
    ok "all alt-data DBs refreshed in last 30h"
else
    bad "stale alt-data DBs:$STALE — daily cron may have failed"
fi

# =============================================================================
# Section I — Options bucket P&L (action signal, #171)
# =============================================================================
hdr "I. Options bucket — 30-day P&L per profile"

if [ "$ACTIVE_COUNT" -eq 0 ]; then
    warn "no active profiles"
else
    BLEEDING=""
    for pid in $ACTIVE_PROFILE_IDS; do
        # Get profile's options 30-day P&L
        PNL=$(SQL "quantopsai_profile_${pid}.db" \
            "SELECT printf('%.2f', COALESCE(SUM(pnl), 0)) FROM trades \
             WHERE status='closed' AND occ_symbol IS NOT NULL \
               AND pnl IS NOT NULL \
               AND timestamp >= datetime('now','-30 days');")
        # Get initial capital + enable_options flag
        META=$(SQL "quantopsai.db" \
            "SELECT initial_capital, enable_options FROM trading_profiles \
             WHERE id=$pid;")
        INITIAL=$(echo "$META" | cut -d'|' -f1)
        ENABLED=$(echo "$META" | cut -d'|' -f2)
        if [ -n "${PNL:-}" ] && [ -n "${INITIAL:-}" ]; then
            # bc for floating-point compare
            PCT=$(echo "scale=2; ($PNL / $INITIAL) * 100" | bc -l 2>/dev/null)
            STATUS="enabled"
            [ "${ENABLED:-1}" = "0" ] && STATUS="DISABLED-by-cutoff"
            echo "      pid${pid}: \$$(printf '%8.2f' $PNL) (${PCT}% of capital, options=${STATUS})"
            # Below -3% with enable still on means the cutoff hasn't
            # fired yet but we're in the danger zone.
            BLEED_CHECK=$(echo "$PCT < -3.0" | bc -l 2>/dev/null)
            if [ "${BLEED_CHECK:-0}" = "1" ] && [ "${ENABLED:-1}" = "1" ]; then
                BLEEDING="$BLEEDING pid${pid}(${PCT}%)"
            fi
        fi
    done
    if [ -z "$BLEEDING" ]; then
        ok "no profile bleeding options P&L without cutoff already firing"
    else
        warn "options P&L < -3% but cutoff hasn't fired yet:$BLEEDING (next self-tune cycle should disable)"
    fi
fi

# =============================================================================
# Summary
# =============================================================================
echo
echo "====================================================="
echo "  Morning summary: $PASS pass / $WARN warn / $FAIL fail"
echo "====================================================="
if [ "$FAIL" -eq 0 ]; then
    if [ "$WARN" -eq 0 ]; then
        echo "  Clean — system healthy, all seven integrity audits green."
    else
        echo "  Mostly clean — warnings worth a glance, no blocking issues."
    fi
    exit 0
else
    echo "  $FAIL real failure(s) — investigate before trading the rest of the day."
    exit 1
fi
