#!/bin/bash
# Production verification — runs after the first 2-3 scan cycles
# of a market session. Confirms every shipped fix is actually
# behaving as designed in prod.
#
# Usage:  ./verify_first_cycle.sh
#         ./verify_first_cycle.sh <droplet-ip>      # alt host
#         ./verify_first_cycle.sh <ip> <since-utc>  # alt window
#
# Date is computed dynamically — runs today's checks against today's
# session. Pass an explicit "$2" like "2026-04-30 13:30 UTC" to
# verify a past session.

set -uo pipefail

DROPLET=${1:-67.205.155.63}
TODAY_UTC=$(date -u +%Y-%m-%d)
SINCE_UTC=${2:-"$TODAY_UTC 13:30 UTC"}    # 9:30 AM ET = market open

# Per-fix deploy cutoffs — prevents historic pre-deploy failures from
# being reported as current bugs. Update these timestamps when shipping
# a relevant fix. Anything BEFORE the cutoff is a known historic failure
# and shouldn't count against the fix.
RESILIENCE_DEPLOY_UTC="2026-04-30 17:09 UTC"      # check_exits per-position try/except
WASH_CLASSIFY_DEPLOY_UTC="2026-04-30 17:11 UTC"   # wash/insufficient-qty as SKIP
DEFER_TO_BROKER_DEPLOY_UTC="2026-04-30 21:19 UTC" # polling defers to broker trailing

PASS=0
FAIL=0
WARN=0

ok()    { echo "  ✅ $*"; PASS=$((PASS+1)); }
bad()   { echo "  ❌ $*"; FAIL=$((FAIL+1)); }
warn()  { echo "  ⚠️  $*"; WARN=$((WARN+1)); }
hdr()   { echo; echo "── $* ──"; }

# Helper: run a journalctl query against the prod scheduler
J() {
    ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 $1"
}

# Helper: journalctl query starting at a specific deploy cutoff
J_SINCE() {
    local since=$1; shift
    ssh root@$DROPLET "journalctl -u quantopsai --since '$since' --no-pager 2>&1 $1"
}

# Helper: SQL one-shot against a profile DB
SQL() {
    local db=$1 query=$2
    ssh root@$DROPLET "sqlite3 /opt/quantopsai/$db \"$query\"" 2>/dev/null
}

ALL_DBS="quantopsai_profile_1.db quantopsai_profile_3.db quantopsai_profile_4.db quantopsai_profile_5.db quantopsai_profile_6.db quantopsai_profile_7.db quantopsai_profile_8.db quantopsai_profile_9.db quantopsai_profile_10.db quantopsai_profile_11.db"
SHORTS_DBS="quantopsai_profile_1.db quantopsai_profile_3.db quantopsai_profile_4.db quantopsai_profile_10.db"

echo "====================================================="
echo "  Production verification — $TODAY_UTC"
echo "  Window: $SINCE_UTC → now"
echo "====================================================="

# =============================================================================
# Section 0 — Service health (both services, not just scheduler)
# =============================================================================
hdr "0. Both prod services active"

echo "[0.1] systemd: quantopsai (scheduler) + quantopsai-web (gunicorn)"
SCHED_STATE=$(ssh root@$DROPLET "systemctl is-active quantopsai" 2>&1)
WEB_STATE=$(ssh root@$DROPLET "systemctl is-active quantopsai-web" 2>&1)
if [ "$SCHED_STATE" = "active" ] && [ "$WEB_STATE" = "active" ]; then
    ok "scheduler=$SCHED_STATE  web=$WEB_STATE"
else
    bad "scheduler=$SCHED_STATE  web=$WEB_STATE — at least one service is not running"
fi

echo
echo "[0.2] gunicorn workers: respawned recently (deploy actually took)"
# After a sync.sh deploy, web workers should be hours old (or fresher),
# not days. A worker uptime > 7 days strongly suggests sync.sh's
# quantopsai-web restart was skipped — every UI fix in that window
# was invisible to users.
WORKER_DAYS=$(ssh root@$DROPLET \
    "ps -eo etime,cmd | grep gunicorn | grep -v grep | head -1 | awk '{print \$1}'")
echo "  oldest worker uptime: $WORKER_DAYS"
case "$WORKER_DAYS" in
    *-*)  bad "gunicorn worker has been running for days — sync.sh may have skipped quantopsai-web restart" ;;
    *)    ok "gunicorn workers respawned within the last 24h" ;;
esac

echo
echo "[0.3] git: prod HEAD == origin/main"
PROD_HEAD=$(ssh root@$DROPLET "cd /opt/quantopsai && git rev-parse HEAD")
ORIGIN_HEAD=$(git -C /Users/mackr0/Quantops rev-parse origin/main 2>/dev/null \
    || git rev-parse origin/main 2>/dev/null \
    || echo "?")
if [ "$PROD_HEAD" = "$ORIGIN_HEAD" ] && [ "$PROD_HEAD" != "?" ]; then
    ok "prod HEAD = $PROD_HEAD (matches origin/main)"
else
    warn "prod HEAD = $PROD_HEAD, origin/main = $ORIGIN_HEAD (mismatch — was sync.sh run?)"
fi

# =============================================================================
# Section A — Core scheduler health
# =============================================================================
hdr "A. Core scheduler health"

echo "[A1] Logging-import: no NameError on Check Exits"
N=$(J "| grep -cE \"NameError.*name 'logging' is not defined\"")
if [ "${N:-0}" -eq 0 ]; then
    ok "zero logging-NameError occurrences"
else
    bad "$N logging-NameError occurrences — fix did not deploy"
fi

echo
echo "[A2] check_exits resilience — per-position failures stay LOCAL"
# Window starts at the resilience-deploy cutoff so pre-deploy historic
# failures don't count. We expect: many "Exit submission failed"
# WARNINGs, ZERO TASK FAILs after the fix.
EXIT_FAILS=$(J_SINCE "$RESILIENCE_DEPLOY_UTC" "| grep -c 'Exit submission failed'")
TASK_FAILS=$(J_SINCE "$RESILIENCE_DEPLOY_UTC" "| grep -cE 'TASK FAIL.*Check Exits'")
HISTORIC_TASK_FAILS=$(J "| grep -cE 'TASK FAIL.*Check Exits'")
HISTORIC_TASK_FAILS=$((HISTORIC_TASK_FAILS - TASK_FAILS))
if [ "${TASK_FAILS:-0}" -eq 0 ]; then
    if [ "$HISTORIC_TASK_FAILS" -gt 0 ]; then
        ok "zero Check Exits TASK FAILs since resilience deploy ($HISTORIC_TASK_FAILS pre-deploy historic, $EXIT_FAILS contained WARNINGs since)"
    else
        ok "zero Check Exits TASK FAILs ($EXIT_FAILS contained WARNINGs)"
    fi
else
    bad "$TASK_FAILS Check Exits TASK FAILs after $RESILIENCE_DEPLOY_UTC — resilience patch regressed"
    J_SINCE "$RESILIENCE_DEPLOY_UTC" "| grep -B1 -A3 'TASK FAIL.*Check Exits' | head -30"
fi

echo
echo "[A3] Scan failures (any TASK FAIL since resilience deploy)"
FAILS=$(J_SINCE "$RESILIENCE_DEPLOY_UTC" "| grep -c 'TASK FAIL'")
if [ "${FAILS:-0}" -eq 0 ]; then
    ok "zero TASK FAIL events since resilience deploy"
else
    bad "$FAILS TASK FAIL events since $RESILIENCE_DEPLOY_UTC — investigate"
    J_SINCE "$RESILIENCE_DEPLOY_UTC" "| grep -B1 -A2 'TASK FAIL' | head -20"
fi

# =============================================================================
# Section B — Cost & Quality Levers (1, 2, 3)
# =============================================================================
hdr "B. Cost & Quality Levers"

echo "[B1] Lever 1 — persistent cache (L2 disk hits)"
N=$(J "| grep -cE 'Using persisted (ensemble|political)'")
if [ "${N:-0}" -gt 0 ]; then
    ok "$N L2 cache hits — persistent cache survived restart"
else
    warn "no persisted-cache hits — may be normal if no restart in window"
fi

echo
echo "[B2] Lever 2 — meta-pregate firing"
N=$(J "| grep -c 'Meta-pregate: dropped'")
SHORT_BYPASS=$(J "| grep -c 'Meta-pregate: bypassed.*short candidates'")
if [ "${N:-0}" -gt 0 ]; then
    SAVED=$(J "| grep 'Meta-pregate: dropped' | grep -oE 'saves [0-9]+' | awk '{s+=\$2} END {print s+0}'")
    ok "$N pregate fires; ${SAVED:-0} specialist calls saved"
    if [ "${SHORT_BYPASS:-0}" -gt 0 ]; then
        ok "  └─ $SHORT_BYPASS short-bypass events (insufficient short training data → fall open)"
    fi
else
    warn "no pregate fires — could be that all candidates passed"
fi

echo
echo "[B3] Lever 3 — disable list reaching ensemble"
SKIP_N=$(J "| grep -c 'ensemble: skipping'")
LOW_CALL_N=$(J "| grep -E 'Specialist ensemble: [123] calls' | wc -l")
TOTAL_CALLS=$(J "| grep -cE 'Specialist ensemble: [0-9]+ calls'")

# Pre-check: any profile actually have specialists disabled? If every
# profile has disabled_specialists='[]' there's nothing to verify.
DISABLED_PROFILES=$(SQL quantopsai.db \
    "SELECT COUNT(*) FROM trading_profiles WHERE enabled=1 AND disabled_specialists != '[]' AND disabled_specialists IS NOT NULL")
DISABLED_PROFILES=${DISABLED_PROFILES:-0}

if [ "$DISABLED_PROFILES" -eq 0 ]; then
    ok "no profile has any specialist disabled — nothing to verify (lever inactive by config)"
elif [ "${SKIP_N:-0}" -gt 0 ]; then
    ok "$SKIP_N skip-log events + $LOW_CALL_N reduced-call cycles ($DISABLED_PROFILES profiles with disabled specialists)"
elif [ "${TOTAL_CALLS:-0}" -eq 0 ]; then
    # No ensemble activity in window — could be pre-market, market closed,
    # or no candidates passed the funnel. Cross-check a wider window so
    # we can tell the difference between "lever broken" and "no activity."
    WIDER_SKIP=$(ssh root@$DROPLET "journalctl -u quantopsai \
        --since '5 days ago' --no-pager 2>&1 \
        | grep -c 'ensemble: skipping'")
    WIDER_TOTAL=$(ssh root@$DROPLET "journalctl -u quantopsai \
        --since '5 days ago' --no-pager 2>&1 \
        | grep -cE 'Specialist ensemble: [0-9]+ calls'")
    if [ "${WIDER_SKIP:-0}" -gt 0 ]; then
        ok "no ensemble activity in window (market closed?) but disable list IS firing in 5-day backstop ($WIDER_SKIP skips / $WIDER_TOTAL calls)"
    elif [ "${WIDER_TOTAL:-0}" -gt 0 ]; then
        bad "5-day backstop: $WIDER_TOTAL ensemble calls but ZERO skip-logs — disable list ignored"
    else
        warn "no ensemble activity in 5-day window — can't verify (try after the first scan cycle)"
    fi
elif [ "${LOW_CALL_N:-0}" -gt 0 ]; then
    warn "no INFO skip-logs but $LOW_CALL_N reduced-call cycles — disable IS firing but log level may have regressed"
else
    bad "$TOTAL_CALLS ensemble calls in window, zero skip-logs, every cycle shows 4 calls — disable list ignored"
fi

# =============================================================================
# Section C — INTRADAY_STOPS_PLAN (broker stops, TPs, trailing)
# =============================================================================
hdr "C. INTRADAY_STOPS_PLAN broker orders"

echo "[C1] Stage 1+3 — protective orders being placed"
TRAIL_PLACED=$(J "| grep -c 'Protective trailing stop placed'")
STOP_PLACED=$(J "| grep -c 'Protective stop placed'")
TOTAL_PLACED=$((TRAIL_PLACED + STOP_PLACED))
if [ "$TOTAL_PLACED" -gt 0 ]; then
    ok "$TRAIL_PLACED trailing + $STOP_PLACED static stops placed at broker today"
else
    warn "no protective orders placed — may indicate sweep not running"
fi

echo
echo "[C2] Stage 3 — polling defers to broker trailing (the real test)"
DEFERS=$(J "| grep -c 'deferred to broker'")
POLLING_TRAILS=$(J "| grep -cE 'Trailing stop triggered:.*price.*<.*trailing stop'")
if [ "$DEFERS" -gt 0 ]; then
    ok "$DEFERS defer events — polling letting broker fire on tick data"
    if [ "$POLLING_TRAILS" -gt 0 ]; then
        warn "  └─ $POLLING_TRAILS polling-fired trails too — fallback active for symbols without broker order"
    fi
elif [ "$POLLING_TRAILS" -gt 0 ]; then
    warn "$POLLING_TRAILS polling trails but ZERO defers — broker not getting a chance to fire (qty conflicts? sweep not running?)"
else
    warn "no trailing-stop triggers in window — no positions reversed enough to test"
fi

echo
echo "[C3] Broker stops actively placed (snapshot now)"
TRAIL_IDS=0
STOP_IDS=0
OPEN_POS=0
for db in $ALL_DBS; do
    O=$(SQL "$db" "SELECT COUNT(*) FROM trades WHERE side='buy' AND status='open';" 2>/dev/null)
    T=$(SQL "$db" "SELECT COUNT(*) FROM trades WHERE side='buy' AND status='open' AND protective_trailing_order_id IS NOT NULL;" 2>/dev/null)
    S=$(SQL "$db" "SELECT COUNT(*) FROM trades WHERE side='buy' AND status='open' AND protective_stop_order_id IS NOT NULL;" 2>/dev/null)
    OPEN_POS=$((OPEN_POS + ${O:-0}))
    TRAIL_IDS=$((TRAIL_IDS + ${T:-0}))
    STOP_IDS=$((STOP_IDS + ${S:-0}))
done
COVERED=$((TRAIL_IDS + STOP_IDS))
if [ "$OPEN_POS" -gt 0 ]; then
    PCT=$((COVERED * 100 / OPEN_POS))
    if [ "$PCT" -ge 80 ]; then
        ok "$COVERED / $OPEN_POS open positions have a broker protective order ($PCT%)"
    elif [ "$PCT" -ge 40 ]; then
        warn "$COVERED / $OPEN_POS positions covered ($PCT%) — sweep may be lagging"
    else
        bad "$COVERED / $OPEN_POS positions covered ($PCT%) — broker protection NOT working"
    fi
else
    warn "no open positions"
fi

echo
echo "[C4] MFE populated on open positions"
NULL_COUNT=0
NEGATIVE_COUNT=0
for db in $ALL_DBS; do
    NUL=$(SQL "$db" "SELECT COUNT(*) FROM trades WHERE status='open' AND side='buy' AND max_favorable_excursion IS NULL;" 2>/dev/null)
    NEG=$(SQL "$db" "SELECT COUNT(*) FROM trades WHERE status='open' AND side='buy' AND max_favorable_excursion IS NOT NULL AND max_favorable_excursion < price;" 2>/dev/null)
    NULL_COUNT=$((NULL_COUNT + ${NUL:-0}))
    NEGATIVE_COUNT=$((NEGATIVE_COUNT + ${NEG:-0}))
done
if [ "$NULL_COUNT" -eq 0 ]; then
    ok "all open longs have MFE populated"
else
    warn "$NULL_COUNT open longs missing MFE — updater may not have run yet"
fi
if [ "$NEGATIVE_COUNT" -eq 0 ]; then
    ok "no MFE values below entry — floor fix working"
else
    bad "$NEGATIVE_COUNT longs have MFE below entry — floor regression"
fi

# =============================================================================
# Section D — Long/Short capability (LONG_SHORT_PLAN Phases 1-4)
# =============================================================================
hdr "D. Long/Short capability"

echo "[D1] Short emission on shorts-enabled profiles"
NEW_SHORTS=0
for db in $SHORTS_DBS; do
    N=$(SQL "$db" "SELECT COUNT(*) FROM ai_predictions WHERE prediction_type='directional_short' AND date(timestamp) = '$TODAY_UTC';" 2>/dev/null)
    NEW_SHORTS=$((NEW_SHORTS + ${N:-0}))
done
if [ "$NEW_SHORTS" -gt 0 ]; then
    ok "$NEW_SHORTS NEW SHORT predictions today across shorts-enabled profiles"
else
    warn "0 NEW SHORT predictions — strong-bull regime suppresses non-mandate profiles, profile_10 should still emit"
fi

echo
echo "[D2] Regime gate respects target_short_pct (P10 has 50% mandate)"
# When profile_10 (target_short_pct=0.5) sees the regime gate fire,
# it should bypass for shorts. Look for the gate-bypass pattern in
# logs OR confirm shorts are emitting.
P10_SHORTS=$(SQL "quantopsai_profile_10.db" "SELECT COUNT(*) FROM ai_predictions WHERE prediction_type='directional_short' AND date(timestamp) = '$TODAY_UTC';" 2>/dev/null)
REGIME_FILTERS=$(J "| grep -c 'filtered for regime gate'")
if [ "${P10_SHORTS:-0}" -gt 0 ]; then
    ok "profile_10 emitted $P10_SHORTS shorts despite regime gate ($REGIME_FILTERS regime-gate filters across all profiles)"
elif [ "$REGIME_FILTERS" -gt 0 ]; then
    warn "regime gate firing but profile_10 emitted 0 shorts — bypass may not be working"
else
    warn "no regime-gate activity (could be neutral regime)"
fi

echo
echo "[D3] relative_weakness_universe strategy emitting candidates"
RS_HITS=$(J "| grep -oE \"'relative_weakness_universe': [0-9]+\" | awk -F': ' '{s+=\$2} END {print s+0}'")
if [ "${RS_HITS:-0}" -gt 0 ]; then
    ok "RS universe strategy emitted $RS_HITS candidates today"
else
    warn "RS universe emitted 0 candidates — may indicate market is trending strongly with no laggards"
fi

echo
echo "[D4] LONG_SHORT_PLAN Phase 1 real-data validator"
VALIDATE_OUT=$(ssh root@$DROPLET "cd /opt/quantopsai && /opt/quantopsai/venv/bin/python3 validate_phase1_realdata.py 2>&1 | tail -10")
if echo "$VALIDATE_OUT" | grep -q "ISSUES"; then
    bad "Phase 1 validator found issues — see output"
    echo "$VALIDATE_OUT"
else
    ok "Phase 1 validator clean (or warnings only)"
fi

echo
echo "[D5] Kelly recommendation populated for at least one profile"
KELLY_FOUND=0
for db in $ALL_DBS; do
    pid=$(echo $db | grep -oE '[0-9]+' | head -1)
    K=$(ssh root@$DROPLET "cd /opt/quantopsai && /opt/quantopsai/venv/bin/python3 -c \"
from kelly_sizing import compute_kelly_recommendation
r = compute_kelly_recommendation('$db', 'long')
print(1 if r else 0)
\" 2>/dev/null")
    if [ "${K:-0}" = "1" ]; then
        KELLY_FOUND=$((KELLY_FOUND + 1))
    fi
done
if [ "$KELLY_FOUND" -gt 0 ]; then
    ok "Kelly recommendations producing on $KELLY_FOUND profiles (need ≥30 resolved entries with positive edge)"
else
    warn "no profile has enough resolved entries for Kelly — early days"
fi

# =============================================================================
# Section E — Trade-quality metrics (Fix 1, Fix 3)
# =============================================================================
hdr "E. Trade-quality metrics"

echo "[E1] Fix 3 — scratch classification visible in metrics"
# Static check: m.scratch_trades is in the template; runtime check:
# at least one profile has trades closed today, and the scratch_rate
# field comes back. Without market activity this is hard to verify
# at runtime, so we just check the static contract.
SCRATCH_IN_METRICS=$(ssh root@$DROPLET "grep -c 'scratch_trades' /opt/quantopsai/metrics.py")
SCRATCH_IN_TPL=$(ssh root@$DROPLET "grep -c 'scratch_rate' /opt/quantopsai/templates/performance.html")
if [ "${SCRATCH_IN_METRICS:-0}" -gt 0 ] && [ "${SCRATCH_IN_TPL:-0}" -gt 0 ]; then
    ok "scratch_trades + scratch_rate present in metrics + dashboard"
else
    bad "scratch classification missing from metrics.py or template — Fix 3 regressed"
fi

echo
echo "[E2] Fix 1 — MFE capture computable"
CAPTURE_FOUND=0
for db in $ALL_DBS; do
    pid=$(echo $db | grep -oE '[0-9]+' | head -1)
    C=$(ssh root@$DROPLET "cd /opt/quantopsai && /opt/quantopsai/venv/bin/python3 -c \"
from mfe_capture import compute_capture_ratio
r = compute_capture_ratio('$db')
print(1 if r else 0)
\" 2>/dev/null")
    if [ "${C:-0}" = "1" ]; then
        CAPTURE_FOUND=$((CAPTURE_FOUND + 1))
    fi
done
if [ "$CAPTURE_FOUND" -gt 0 ]; then
    ok "MFE capture ratio computable on $CAPTURE_FOUND profiles"
else
    warn "no profile has enough closed trades with favorable MFE — needs more closed trades"
fi

echo
echo "[E3] Slippage tracking — cost is signed (not absolute)"
# Check that signed cost field exists in get_slippage_stats output
SIGNED_OK=$(ssh root@$DROPLET "grep -c 'total_slippage_magnitude' /opt/quantopsai/journal.py")
if [ "${SIGNED_OK:-0}" -gt 0 ]; then
    ok "signed slippage cost + execution-variance magnitude both available"
else
    bad "signed slippage cost missing — total_slippage_magnitude not in journal.get_slippage_stats"
fi

# =============================================================================
# Section F — Trade execution behavior
# =============================================================================
hdr "F. Trade execution behavior"

echo "[F1] Trade execution loud-logging (rejections classified, not silent)"
# Window starts at wash-classify deploy so pre-deploy unclassified
# noise doesn't count.
EXEC_PRINTS=$(J "| grep -c '  Executing:'")
WASH_SKIPS=$(J "| grep -c 'Wash-trade detected'")
TRADE_ERRORS=$(J_SINCE "$WASH_CLASSIFY_DEPLOY_UTC" "| grep -cE 'Trade execution raised'")
if [ "${EXEC_PRINTS:-0}" -gt 0 ]; then
    if [ "${TRADE_ERRORS:-0}" -eq 0 ]; then
        ok "$EXEC_PRINTS Executing prints; rejections classified ($WASH_SKIPS wash-trade SKIPs)"
    else
        warn "$EXEC_PRINTS Executing, $TRADE_ERRORS unclassified errors since $WASH_CLASSIFY_DEPLOY_UTC — investigate"
        J_SINCE "$WASH_CLASSIFY_DEPLOY_UTC" "| grep 'Trade execution raised' | head -3"
    fi
else
    warn "no Executing prints — no trades selected (could be normal)"
fi

echo "[F2] Track record reputation system populated"
# track_record is intentionally NOT in features_json (it's a narrative
# string, not a numeric ML feature — see trade_pipeline:1408-1413
# where it's explicitly excluded). Instead verify the reputation
# system is producing data: get_symbol_reputation should return >0
# symbols with at least one profile that has resolved history.
REP_COUNT=$(ssh root@$DROPLET "cd /opt/quantopsai && /opt/quantopsai/venv/bin/python3 -c \"
from self_tuning import get_symbol_reputation
n = max((len(get_symbol_reputation(f'quantopsai_profile_{p}.db')) for p in (1,3,4,8,9,11)), default=0)
print(n)
\"" 2>/dev/null)
if [ "${REP_COUNT:-0}" -ge 10 ]; then
    ok "symbol reputation populated ($REP_COUNT symbols on best profile) — AI gets per-symbol track records"
elif [ "${REP_COUNT:-0}" -gt 0 ]; then
    warn "only $REP_COUNT symbols have resolved history — AI track-record context still building"
else
    bad "zero symbols in reputation — track_record system not producing"
fi

echo
echo "[F3] Pending orders panel — filtered to this profile only"
# Static check: views.py filters by owned order IDs. No runtime way to
# verify without authenticated dashboard render.
FILTER_OK=$(ssh root@$DROPLET "grep -c 'owned_ids' /opt/quantopsai/views.py")
if [ "${FILTER_OK:-0}" -gt 0 ]; then
    ok "owned_ids cross-reference present in pending-orders fetch"
else
    bad "pending-orders filter missing — sibling-profile orders will leak into dashboard"
fi

# =============================================================================
# Section G — Cost
# =============================================================================
hdr "G. Cost"

echo "[G1] AI cost so far today"
TOTAL=0
for db in $ALL_DBS; do
    C=$(SQL "$db" "SELECT printf('%.4f',COALESCE(SUM(estimated_cost_usd),0)) FROM ai_cost_ledger WHERE date(timestamp)='$TODAY_UTC';" 2>/dev/null)
    TOTAL=$(echo "$TOTAL + ${C:-0}" | bc -l 2>/dev/null || echo "$TOTAL")
done
echo "  Cumulative: \$$(printf '%.2f' $TOTAL)"

# =============================================================================
# Section H — Alt-data merge integrity (2026-05-04 merge)
# =============================================================================
hdr "H. Alt-data scrapers (merged into altdata/ subdirectory)"

echo "[H1] All 4 alt-data DBs at the new bundled path"
MISSING=""
for proj in congresstrades stocktwits biotechevents edgar13f; do
    case $proj in
        congresstrades) db="congress.db" ;;
        stocktwits)     db="stocktwits.db" ;;
        biotechevents)  db="biotechevents.db" ;;
        edgar13f)       db="edgar13f.db" ;;
    esac
    EXISTS=$(ssh root@$DROPLET \
        "test -f /opt/quantopsai/altdata/$proj/data/$db && echo Y || echo N")
    if [ "$EXISTS" = "N" ]; then
        MISSING="$MISSING $proj"
    fi
done
if [ -z "$MISSING" ]; then
    ok "all 4 DBs present at /opt/quantopsai/altdata/<project>/data/"
else
    bad "missing DBs at new path:$MISSING"
fi

echo
echo "[H2] Alt-data DBs refreshed within last 30h (cron 06:00 UTC daily)"
NOW_EPOCH=$(date -u +%s)
STALE=""
for proj in congresstrades stocktwits biotechevents edgar13f; do
    case $proj in
        congresstrades) db="congress.db" ;;
        stocktwits)     db="stocktwits.db" ;;
        biotechevents)  db="biotechevents.db" ;;
        edgar13f)       db="edgar13f.db" ;;
    esac
    MTIME=$(ssh root@$DROPLET \
        "stat -c %Y /opt/quantopsai/altdata/$proj/data/$db 2>/dev/null" \
        || echo 0)
    AGE_HOURS=$(( (NOW_EPOCH - MTIME) / 3600 ))
    if [ "$AGE_HOURS" -gt 30 ]; then
        STALE="$STALE $proj(${AGE_HOURS}h)"
    fi
done
if [ -z "$STALE" ]; then
    ok "all 4 DBs refreshed in last 30h"
else
    bad "stale DBs:$STALE — daily cron may have failed"
fi

echo
echo "[H3] Cron entry uses the new altdata/ path"
CRON_OK=$(ssh root@$DROPLET "crontab -l 2>/dev/null | \
    grep -c 'altdata/run-altdata-daily.sh'")
CRON_STALE=$(ssh root@$DROPLET "crontab -l 2>/dev/null | \
    grep -c 'quantopsai-altdata'")
if [ "${CRON_OK:-0}" -gt 0 ] && [ "${CRON_STALE:-0}" -eq 0 ]; then
    ok "cron points at altdata/run-altdata-daily.sh, no stale references"
else
    bad "cron has $CRON_OK new-path entries and $CRON_STALE old-path references"
fi

echo
echo "[H4] alternative_data._altdata_db resolves to new path"
RESOLVED=$(ssh root@$DROPLET "cd /opt/quantopsai && \
    ALTDATA_BASE_PATH=/opt/quantopsai/altdata \
    /opt/quantopsai/venv/bin/python -c \
    'from alternative_data import _altdata_db; \
print(_altdata_db(\"biotechevents\", \"biotechevents.db\"))' 2>&1")
if echo "$RESOLVED" | grep -q "/opt/quantopsai/altdata/biotechevents/data/biotechevents.db"; then
    ok "path resolution OK ($RESOLVED)"
else
    bad "path resolution wrong: $RESOLVED"
fi

# =============================================================================
# Section I — PDUFA + AdComm signal landing in AI prompts
# =============================================================================
hdr "I. PDUFA + AdComm catalysts"

echo "[I1] pdufa_events table populated"
PDUFA_N=$(ssh root@$DROPLET \
    "sqlite3 /opt/quantopsai/altdata/biotechevents/data/biotechevents.db \
    'SELECT COUNT(*) FROM pdufa_events' 2>/dev/null")
if [ "${PDUFA_N:-0}" -gt 0 ]; then
    ok "$PDUFA_N PDUFA events stored"
else
    bad "pdufa_events empty — EDGAR scrape produced 0 rows"
fi

echo
echo "[I2] Drug names extracted (not all '(see filing)')"
PLACEHOLDER_N=$(ssh root@$DROPLET \
    "sqlite3 /opt/quantopsai/altdata/biotechevents/data/biotechevents.db \
    \"SELECT COUNT(*) FROM pdufa_events WHERE drug_name LIKE '(see%' OR drug_name LIKE '%placeholder%'\" 2>/dev/null")
PLACEHOLDER_N=${PLACEHOLDER_N:-0}
if [ "${PDUFA_N:-0}" -gt 0 ]; then
    REAL_N=$((PDUFA_N - PLACEHOLDER_N))
    PCT=$((REAL_N * 100 / PDUFA_N))
    if [ "$PCT" -ge 60 ]; then
        ok "$REAL_N / $PDUFA_N rows have parsed drug names ($PCT%)"
    elif [ "$PCT" -ge 30 ]; then
        warn "only $REAL_N / $PDUFA_N rows have real drug names ($PCT%) — patterns may need extending"
    else
        bad "$REAL_N / $PDUFA_N have real drug names ($PCT%) — drug-name extraction regressed"
    fi
fi

echo
echo "[I3] _task_pdufa_scrape ran today (idempotency table)"
TODAY_RUN=$(ssh root@$DROPLET \
    "sqlite3 /opt/quantopsai/quantopsai.db \
    \"SELECT 1 FROM pdufa_scrape_runs WHERE run_date='$TODAY_UTC'\" 2>/dev/null")
if [ "${TODAY_RUN:-}" = "1" ]; then
    ok "pdufa_scrape_runs has entry for $TODAY_UTC"
else
    warn "no pdufa_scrape_runs row for today yet — may not have fired (runs once/day)"
fi

echo
echo "[I4] adcomm_events table exists (zero rows is fine — AdComms are rare)"
ADCOMM_TBL=$(ssh root@$DROPLET \
    "sqlite3 /opt/quantopsai/altdata/biotechevents/data/biotechevents.db \
    \"SELECT name FROM sqlite_master WHERE type='table' AND name='adcomm_events'\" 2>/dev/null")
if [ "$ADCOMM_TBL" = "adcomm_events" ]; then
    ADCOMM_N=$(ssh root@$DROPLET \
        "sqlite3 /opt/quantopsai/altdata/biotechevents/data/biotechevents.db \
        'SELECT COUNT(*) FROM adcomm_events' 2>/dev/null")
    ok "adcomm_events table exists ($ADCOMM_N rows)"
else
    bad "adcomm_events table missing — eager-create in run_full_sync regressed"
fi

echo
echo "[I5] get_biotech_milestones returns PDUFA + AdComm fields end-to-end"
BIO_FIELDS=$(ssh root@$DROPLET "cd /opt/quantopsai && \
    ALTDATA_BASE_PATH=/opt/quantopsai/altdata \
    /opt/quantopsai/venv/bin/python -c \
    'from alternative_data import get_biotech_milestones; \
r = get_biotech_milestones(\"ARVN\"); \
fields = [k for k in (\"upcoming_pdufa_date\",\"days_to_pdufa\",\"drug_name\",\"upcoming_adcomm_date\",\"days_to_adcomm\",\"adcomm_committee\") if k in r]; \
print(len(fields))' 2>&1")
if [ "${BIO_FIELDS:-0}" = "6" ]; then
    ok "all 6 PDUFA+AdComm fields surfaced to AI"
else
    bad "biotech_milestones returns only $BIO_FIELDS / 6 expected fields"
fi

# =============================================================================
# Section J — UI / deploy hygiene
# =============================================================================
hdr "J. UI guardrails + deploy hygiene"

echo "[J1] No 'Item 5c' / 'Item Nx' refs in rendered /ai page"
ITEM_HITS=$(ssh root@$DROPLET \
    "curl -s http://localhost:8000/ai 2>/dev/null \
    | grep -cE '\(Item [0-9]+[a-z]?\)|\(OPEN_ITEMS'")
if [ "${ITEM_HITS:-0}" -eq 0 ]; then
    ok "zero internal-tracker refs in rendered /ai HTML"
else
    bad "$ITEM_HITS '(Item Nx)' refs in rendered /ai — template fix didn't reach gunicorn workers"
fi

echo
echo "[J2] No raw snake_case in dropdown <option> text on /ai"
SC_HITS=$(ssh root@$DROPLET \
    "curl -s http://localhost:8000/ai 2>/dev/null \
    | grep -cE '<option[^>]*>(long_put|long_call|bull_call_spread|bear_put_spread|iron_condor|covered_call|protective_put|cash_secured_put|iron_butterfly|long_straddle|short_straddle|long_strangle|calendar_spread|diagonal_spread)<'")
if [ "${SC_HITS:-0}" -eq 0 ]; then
    ok "zero snake_case option text in rendered /ai"
else
    bad "$SC_HITS dropdown options still rendering raw snake_case"
fi

echo
echo "[J3] Blanket template guardrail (test_no_internal_leakage) passes on prod"
LEAK_TEST=$(ssh root@$DROPLET "cd /opt/quantopsai && \
    /opt/quantopsai/venv/bin/python -m pytest \
    tests/test_no_internal_leakage_in_templates.py \
    -q --no-header --timeout=30 2>&1 | tail -1")
if echo "$LEAK_TEST" | grep -qE '[0-9]+ passed' \
        && ! echo "$LEAK_TEST" | grep -qE 'failed|error'; then
    ok "static template guardrail green on prod ($LEAK_TEST)"
else
    bad "template guardrail FAILED on prod: $LEAK_TEST"
fi

echo
echo "[J4] /api/options-backtest endpoint works for every dropdown option"
OPT_TEST=$(ssh root@$DROPLET "cd /opt/quantopsai && \
    /opt/quantopsai/venv/bin/python -m pytest \
    tests/test_options_backtest_api.py \
    -q --no-header --timeout=120 2>&1 | tail -1")
if echo "$OPT_TEST" | grep -qE '[0-9]+ passed' \
        && ! echo "$OPT_TEST" | grep -qE 'failed|error'; then
    ok "all 5 dropdown strategies execute end-to-end ($OPT_TEST)"
else
    bad "options-backtest endpoint smoke test FAILED on prod: $OPT_TEST"
fi

echo
echo "[J5] Display-name guardrails (existing tests) still passing on prod"
DN_TEST=$(ssh root@$DROPLET "cd /opt/quantopsai && \
    /opt/quantopsai/venv/bin/python -m pytest \
    tests/test_display_names.py \
    tests/test_no_snake_case_in_api_responses.py \
    tests/test_no_snake_case_in_user_facing_ids.py \
    tests/test_no_snake_case_in_optimizer_strings.py \
    -q --no-header --timeout=60 2>&1 | tail -1")
if echo "$DN_TEST" | grep -qE '[0-9]+ passed' \
        && ! echo "$DN_TEST" | grep -qE 'failed|error'; then
    ok "all snake_case + display-name guardrails green ($DN_TEST)"
else
    bad "snake_case guardrail regression: $DN_TEST"
fi

# =============================================================================
# Summary
# =============================================================================
echo
echo "====================================================="
echo "  Summary: $PASS pass / $WARN warn / $FAIL fail"
echo "====================================================="
if [ "$FAIL" -eq 0 ]; then
    if [ "$WARN" -eq 0 ]; then
        echo "  Clean — every shipped fix verified live."
    else
        echo "  Mostly clean — warnings need a glance, no blocking failures."
    fi
    exit 0
else
    echo "  $FAIL real failure(s) — investigate before trading the rest of the day."
    exit 1
fi
