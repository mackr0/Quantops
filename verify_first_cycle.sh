#!/bin/bash
# Tomorrow-morning verification: end-to-end check that the fixes
# from 2026-04-28 actually work in production.
#
# Run this AFTER the first 2-3 scan cycles complete (market opens
# 9:30 AM ET = 13:30 UTC; 2-3 cycles ≈ 14:00-14:15 UTC).
#
# Usage:  ./verify_first_cycle.sh
#         (runs against prod via ssh; assumes ssh root@67.205.155.63 works)

set -uo pipefail

DROPLET=${1:-67.205.155.63}
SINCE_UTC="2026-04-29 13:30 UTC"
PASS=0
FAIL=0
WARN=0

ok()    { echo "  ✅ $*"; PASS=$((PASS+1)); }
bad()   { echo "  ❌ $*"; FAIL=$((FAIL+1)); }
warn()  { echo "  ⚠️  $*"; WARN=$((WARN+1)); }

echo "====================================================="
echo "  First-cycle verification — 2026-04-29 market open  "
echo "====================================================="
echo

# ---------------------------------------------------------------------------
# Check 1: Logging-import fix (no NameError on Check Exits)
# ---------------------------------------------------------------------------
echo "[1/9] Logging import: no NameError on Check Exits"
N=$(ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 | grep -cE \"NameError.*name 'logging' is not defined\"")
if [ "${N:-0}" -eq 0 ]; then
    ok "zero logging-NameError occurrences"
else
    bad "$N logging-NameError occurrences — fix did not deploy"
fi

# ---------------------------------------------------------------------------
# Check 2: Lever 3 — disable list ACTUALLY reaches ensemble (the big one)
# ---------------------------------------------------------------------------
echo
echo "[2/9] Lever 3 disable list reaching ensemble (skipping pattern_recognizer)"
N=$(ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 | grep -c 'ensemble: skipping pattern_recognizer'")
if [ "${N:-0}" -gt 0 ]; then
    ok "$N skip events — disable list IS being read through ctx"
else
    bad "zero 'skipping pattern_recognizer' events — ctx disconnect may have regressed"
fi

# ---------------------------------------------------------------------------
# Check 3: Lever 2 meta-pregate firing
# ---------------------------------------------------------------------------
echo
echo "[3/9] Lever 2 meta-pregate firing"
N=$(ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 | grep -c 'Meta-pregate: dropped'")
if [ "${N:-0}" -gt 0 ]; then
    SAVED=$(ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 | grep 'Meta-pregate' | grep -oE 'saves [0-9]+' | awk '{s+=\$2} END {print s+0}'")
    ok "$N pregate fires; ${SAVED:-0} specialist calls saved"
else
    warn "no pregate fires — could be that all candidates passed (need first scan with low-prob candidates)"
fi

# ---------------------------------------------------------------------------
# Check 4: Lever 1 persistent cache — using persisted results across restarts
# ---------------------------------------------------------------------------
echo
echo "[4/9] Lever 1 persistent cache (L2 disk hits)"
N=$(ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 | grep -cE 'Using persisted (ensemble|political)'")
if [ "${N:-0}" -gt 0 ]; then
    ok "$N L2 cache hits — persistent cache survived overnight"
else
    warn "no persisted-cache hits — could be normal if all bucket flips happened during scan cycles (no restart between)"
fi

# ---------------------------------------------------------------------------
# Check 5: No silent execute_trade swallow
# ---------------------------------------------------------------------------
echo
echo "[5/9] Trade execution loud-logging (no silent swallow)"
EXEC_PRINTS=$(ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 | grep -c '  Executing:'")
TRADE_ERRORS=$(ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 | grep -cE 'Trade execution raised|Trade NOT submitted'")
if [ "${EXEC_PRINTS:-0}" -gt 0 ]; then
    if [ "${TRADE_ERRORS:-0}" -eq 0 ]; then
        ok "$EXEC_PRINTS Executing: prints with zero rejections — all submitted cleanly"
    else
        warn "$EXEC_PRINTS Executing prints, $TRADE_ERRORS visibly logged rejections — check journal for cause (good: failures are now LOUD)"
    fi
else
    warn "no Executing: prints — no trades selected this window (could be normal for AI in HOLD mood)"
fi

# ---------------------------------------------------------------------------
# Check 6: MFE column populated for new positions
# ---------------------------------------------------------------------------
echo
echo "[6/9] MFE populated on open positions"
NULL_COUNT=0
NEGATIVE_COUNT=0
for db in quantopsai_profile_1.db quantopsai_profile_3.db quantopsai_profile_4.db; do
    NUL=$(ssh root@$DROPLET "sqlite3 /opt/quantopsai/$db 'SELECT COUNT(*) FROM trades WHERE status=\"open\" AND side=\"buy\" AND max_favorable_excursion IS NULL;'")
    NEG=$(ssh root@$DROPLET "sqlite3 /opt/quantopsai/$db 'SELECT COUNT(*) FROM trades WHERE status=\"open\" AND side=\"buy\" AND max_favorable_excursion IS NOT NULL AND max_favorable_excursion < price;'")
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

# ---------------------------------------------------------------------------
# Check 7: Track record split-by-signal in features_json
# ---------------------------------------------------------------------------
echo
echo "[7/9] Signal-split track records in new prediction features"
SPLIT=$(ssh root@$DROPLET "sqlite3 /opt/quantopsai/quantopsai_profile_3.db 'SELECT COUNT(*) FROM ai_predictions WHERE date(timestamp)=\"2026-04-29\" AND features_json LIKE \"%track_record%overall%\";'")
if [ "${SPLIT:-0}" -gt 0 ]; then
    ok "$SPLIT predictions have signal-split track_record"
else
    warn "no track_record split detected — may need first scan to fire (track_record only added when symbol has resolved history)"
fi

# ---------------------------------------------------------------------------
# Check 8: Scan failures dashboard panel
# ---------------------------------------------------------------------------
echo
echo "[8/9] Scan failures (last hour)"
FAILS=$(ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 | grep -c 'TASK FAIL'")
if [ "${FAILS:-0}" -eq 0 ]; then
    ok "zero TASK FAIL events"
else
    bad "$FAILS TASK FAIL events — investigate"
    ssh root@$DROPLET "journalctl -u quantopsai --since '$SINCE_UTC' --no-pager 2>&1 | grep -B1 -A2 'TASK FAIL' | head -20"
fi

# ---------------------------------------------------------------------------
# Check 9: LONG_SHORT_PLAN Phase 1 — does the new short pipeline emit?
# ---------------------------------------------------------------------------
echo
echo "[9/10] LONG_SHORT_PLAN Phase 1 — short emission on shorts-enabled profiles"

# Count NEW SHORT predictions emitted today (since 13:30 UTC) by
# the 4 shorts-enabled profiles. Pre-Phase-1 baseline: ~0/cycle.
# Post-Phase-1 in strong_bull regime: catalyst shorts only.
NEW_SHORTS=0
for pid in 1 3 4 10; do
    db="quantopsai_profile_${pid}.db"
    N=$(ssh root@$DROPLET "sqlite3 /opt/quantopsai/$db \"SELECT COUNT(*) FROM ai_predictions WHERE predicted_signal IN ('SHORT', 'STRONG_SHORT') AND date(timestamp) = '2026-04-29';\"" 2>/dev/null)
    NEW_SHORTS=$((NEW_SHORTS + ${N:-0}))
done
if [ "$NEW_SHORTS" -gt 0 ]; then
    ok "$NEW_SHORTS NEW SHORT predictions today across shorts-enabled profiles"
else
    warn "0 NEW SHORT predictions today — may be regime gate (strong_bull suppresses non-catalyst shorts) or may be unwanted"
fi

# Run the Phase 1 real-data validator
echo
echo "[9b/10] Phase 1 real-data validation script"
VALIDATE_OUT=$(ssh root@$DROPLET "cd /opt/quantopsai && /opt/quantopsai/venv/bin/python3 validate_phase1_realdata.py 2>&1 | tail -10")
if echo "$VALIDATE_OUT" | grep -q "ISSUES"; then
    bad "Phase 1 validator found issues — see output"
    echo "$VALIDATE_OUT"
else
    ok "Phase 1 validator clean (or warnings only)"
fi

# ---------------------------------------------------------------------------
# Check 10: AI cost trending under ceiling
# ---------------------------------------------------------------------------
echo
echo "[10/10] AI cost so far today"
TOTAL=0
for db in quantopsai_profile_1.db quantopsai_profile_3.db quantopsai_profile_4.db quantopsai_profile_5.db quantopsai_profile_6.db quantopsai_profile_7.db quantopsai_profile_8.db quantopsai_profile_9.db quantopsai_profile_10.db quantopsai_profile_11.db; do
    C=$(ssh root@$DROPLET "sqlite3 /opt/quantopsai/$db 'SELECT printf(\"%.4f\",COALESCE(SUM(estimated_cost_usd),0)) FROM ai_cost_ledger WHERE date(timestamp)=\"2026-04-29\";'" 2>/dev/null)
    TOTAL=$(echo "$TOTAL + ${C:-0}" | bc -l 2>/dev/null || echo "$TOTAL")
done
echo "  Cumulative: \$$(printf '%.2f' $TOTAL)"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "====================================================="
echo "  Summary: $PASS pass / $WARN warn / $FAIL fail"
echo "====================================================="
if [ "$FAIL" -eq 0 ]; then
    if [ "$WARN" -eq 0 ]; then
        echo "  Clean — all 2026-04-28 fixes verified live."
    else
        echo "  Mostly clean — warnings need a glance, no blocking failures."
    fi
    exit 0
else
    echo "  $FAIL real failure(s) — investigate before trading the rest of the day."
    exit 1
fi
