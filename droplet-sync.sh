#!/bin/bash
# droplet-sync.sh — sync.sh's deploy discipline, executed ON the droplet.
#
# Operator directive 2026-07-24: web sessions kept re-deriving sync.sh's
# steps by hand. This is the same script, stage for stage, with exactly
# ONE structural difference: there is no Mac→droplet rsync stage — the
# code is committed and pushed to origin/main FROM the droplet, so the
# "transfer" is `git reset --hard origin/main` in place. The laptop
# catches up the next time the operator runs `git pull` before ./sync.sh.
#
# Every other stage is a faithful replica of sync.sh:
#   - pre-flight: clean tree, HEAD == origin/main (push first or abort)
#   - changed-set detection (prev deployed sha → HEAD, analog of the
#     rsync dry-run diff)
#   - git alignment + HEAD verify + tracked-drift check + CONTENT
#     verification (sha256 of trade_pipeline.py, HEAD blob vs disk)
#   - deploy markers (.deploy_sha / .deploy_timestamp)
#   - restart decision from the changed set (same WEB/SCHED patterns,
#     same --web/--scheduler/--all overrides)
#   - scheduler restart waits for the idle window
#   - final verification: services active + marker + post-restart rehash
#
# SELF-DETACHING BY DEFAULT (operator directive): the run survives the
# terminal/session closing. Output goes to deploy_logs/droplet-sync-
# <UTC>.log; the launching shell prints the log path and returns
# immediately. Follow with: tail -f <log>.

set -e

REMOTE_DIR="/opt/quantopsai"
cd "$REMOTE_DIR"

# ---------------------------------------------------------------------------
# Detach guard — everything below runs immune to hangups.
# ---------------------------------------------------------------------------
if [ -z "$DSYNC_DETACHED" ]; then
    mkdir -p deploy_logs
    LOG="deploy_logs/droplet-sync-$(date -u +%Y%m%dT%H%M%SZ).log"
    DSYNC_DETACHED=1 setsid nohup "$0" "$@" > "$LOG" 2>&1 < /dev/null &
    echo "droplet-sync: detached (pid $!) — survives terminal close."
    echo "log: ${REMOTE_DIR}/${LOG}"
    exit 0
fi

echo "droplet-sync started $(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# Handle flags (identical to sync.sh)
FORCE_MODE=""
if [[ "$1" == "--web" ]]; then FORCE_MODE="web"; fi
if [[ "$1" == "--scheduler" ]]; then FORCE_MODE="scheduler"; fi
if [[ "$1" == "--all" ]]; then FORCE_MODE="all"; fi

# ---------------------------------------------------------------------------
# Pre-flight gate (mirror of sync.sh): tree must be clean and HEAD must
# equal origin/main — otherwise the reset below would revert work, the
# exact silent-revert class sync.sh's gate exists to prevent.
# ---------------------------------------------------------------------------
if ! git diff-index --quiet HEAD -- 2>/dev/null; then
    echo "ERROR: Uncommitted changes in working tree."
    echo "Commit before running droplet-sync.sh — the deploy is a"
    echo "'git reset --hard origin/main' and would discard them."
    git status --short
    exit 1
fi
git fetch origin
LOCAL_HEAD=$(git rev-parse HEAD)
ORIGIN_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "UNKNOWN")
if [ "$LOCAL_HEAD" != "$ORIGIN_HEAD" ]; then
    echo "ERROR: Local HEAD ($LOCAL_HEAD) does not match origin/main ($ORIGIN_HEAD)."
    echo "Run: git push origin main"
    echo "(or reset to origin/main, if the remote is ahead) before deploying."
    exit 1
fi
echo "deploying $LOCAL_HEAD (matches origin/main)"

# ---------------------------------------------------------------------------
# Changed-set detection — the analog of sync.sh's rsync dry-run diff:
# what changed between the PREVIOUSLY DEPLOYED sha and this HEAD decides
# which services restart. Falls back to HEAD~1 when no valid marker.
# ---------------------------------------------------------------------------
PREV_SHA=$(cat .deploy_sha 2>/dev/null || echo "")
if [ -n "$PREV_SHA" ] && git cat-file -e "$PREV_SHA" 2>/dev/null; then
    CHANGED=$(git diff --name-only "$PREV_SHA" "$LOCAL_HEAD")
else
    echo "WARNING: no valid .deploy_sha — diffing HEAD~1..HEAD"
    CHANGED=$(git diff --name-only HEAD~1 "$LOCAL_HEAD" 2>/dev/null || true)
fi
if [ -z "$CHANGED" ]; then
    echo "No file changes since last deploy; running git-state verification anyway."
else
    echo "Changed files:"
    echo "$CHANGED" | sed 's/^/  /'
fi

# ---------------------------------------------------------------------------
# Git alignment + verification (mirror of sync.sh's post-deploy block).
# ---------------------------------------------------------------------------
git reset --hard origin/main
PROD_HEAD=$(git rev-parse HEAD)
if [ "$PROD_HEAD" != "$LOCAL_HEAD" ]; then
    echo "ERROR: HEAD ($PROD_HEAD) != expected ($LOCAL_HEAD) after reset."
    exit 1
fi
DRIFT=$(git status --porcelain | grep -v '^??' || true)
if [ -n "$DRIFT" ]; then
    echo "ERROR: Tracked files diverge from origin/main after reset:"
    echo "$DRIFT"
    exit 1
fi
# CONTENT verification (defense beyond git HEAD): the HEAD blob of
# trade_pipeline.py must equal the file on disk.
VERIFY_FILE="trade_pipeline.py"
HEAD_SHA=$(git show "HEAD:${VERIFY_FILE}" | sha256sum | awk '{print $1}')
DISK_SHA=$(sha256sum "$VERIFY_FILE" | awk '{print $1}')
if [ "$HEAD_SHA" != "$DISK_SHA" ]; then
    echo "ERROR: ${VERIFY_FILE} on disk does not match HEAD blob."
    echo "       head: $HEAD_SHA"
    echo "       disk: $DISK_SHA"
    exit 1
fi
echo "$LOCAL_HEAD" > .deploy_sha
date -u +"%Y-%m-%dT%H:%M:%SZ" > .deploy_timestamp
echo "  HEAD + content verified; .deploy_sha written: $LOCAL_HEAD"

# ---------------------------------------------------------------------------
# Restart decision (identical patterns to sync.sh).
# ---------------------------------------------------------------------------
NEED_WEB=false
NEED_SCHEDULER=false
if [ "$FORCE_MODE" = "all" ]; then
    NEED_WEB=true; NEED_SCHEDULER=true
elif [ "$FORCE_MODE" = "web" ]; then
    NEED_WEB=true
elif [ "$FORCE_MODE" = "scheduler" ]; then
    NEED_SCHEDULER=true
else
    # 2026-07-25 — the web app imports the ENTIRE book-computation
    # stack (journal, aggregate_audit, integrity_audit, client, ...),
    # so ANY production .py change requires a web restart too. The old
    # narrow WEB_PATTERNS (inherited from sync.sh) left gunicorn
    # running STALE journal code after a journal-only deploy: the
    # /issues page then audited the books with old algebra and showed
    # a phantom $38,000 equity/cash error while the books and the
    # scheduler were correct. Web restarts are graceful and cheap —
    # restart both on any .py change; templates/static stay web-only;
    # docs/tests restart nothing.
    WEB_PATTERNS="templates/|static/|\.py$"
    SCHED_PATTERNS="\.py$"
    while IFS= read -r file; do
        [ -z "$file" ] && continue
        if echo "$file" | grep -qE "^(tests/|scripts/)"; then
            continue
        fi
        if echo "$file" | grep -qE "$WEB_PATTERNS"; then
            NEED_WEB=true
        fi
        if echo "$file" | grep -qE "$SCHED_PATTERNS" && ! echo "$file" | grep -qE "^(templates/|static/)"; then
            if ! echo "$file" | grep -qE "^(views\.py|display_names\.py|app\.py|auth\.py)$"; then
                NEED_SCHEDULER=true
            fi
        fi
    done <<< "$CHANGED"
fi

# ---------------------------------------------------------------------------
# Restarts (identical idle-window wait to sync.sh).
# ---------------------------------------------------------------------------
if $NEED_SCHEDULER; then
    echo "Scheduler code changed — waiting for safe restart window..."
    LAST_LINE=$(journalctl -u quantopsai --no-pager -n 1 2>/dev/null | tail -1)
    if echo "$LAST_LINE" | grep -q 'sleeping until\|Market closed\|Sleeping'; then
        echo 'Scheduler is idle — safe to restart.'
    else
        echo 'WARNING: Scheduler may be mid-cycle. Waiting up to 60s for cycle end...'
        for i in $(seq 1 12); do
            sleep 5
            LAST=$(journalctl -u quantopsai --no-pager -n 1 2>/dev/null | tail -1)
            if echo "$LAST" | grep -q 'sleeping until\|Market closed\|Sleeping\|completed'; then
                echo 'Cycle ended — safe to restart.'
                break
            fi
            if [ "$i" -eq 12 ]; then
                echo 'Timeout waiting for cycle — restarting anyway.'
            fi
        done
    fi
    systemctl restart quantopsai
    echo "Scheduler restarted."
fi
if $NEED_WEB; then
    systemctl restart quantopsai-web
    echo "Web server restarted."
fi
if ! $NEED_WEB && ! $NEED_SCHEDULER; then
    echo "Only docs/tests changed — no restart needed."
fi

# ---------------------------------------------------------------------------
# Final verification (mirror of sync.sh): services up AND marker matches
# AND content re-verified after restart.
# ---------------------------------------------------------------------------
sleep 2
QUANTOPS_OK=false
WEB_OK=false
if systemctl is-active --quiet quantopsai; then QUANTOPS_OK=true; fi
if systemctl is-active --quiet quantopsai-web; then WEB_OK=true; fi
if ! $QUANTOPS_OK; then
    echo "WARNING: quantopsai (scheduler) is not active."
    systemctl is-active quantopsai || true
fi
if ! $WEB_OK; then
    echo "WARNING: quantopsai-web is not active."
    systemctl is-active quantopsai-web || true
fi
DEPLOYED_SHA=$(cat .deploy_sha)
if [ "$DEPLOYED_SHA" != "$LOCAL_HEAD" ]; then
    echo "ERROR: .deploy_sha ($DEPLOYED_SHA) != deployed HEAD ($LOCAL_HEAD)."
    exit 1
fi
DISK_SHA2=$(sha256sum "$VERIFY_FILE" | awk '{print $1}')
if [ "$DISK_SHA2" != "$HEAD_SHA" ]; then
    echo "ERROR: ${VERIFY_FILE} drifted between deploy + restart."
    exit 1
fi
if $QUANTOPS_OK && $WEB_OK; then
    echo "Both services running."
    echo "  prod @ $(cut -c1-12 .deploy_sha) (HEAD + content verified)"
    echo "  deployed: $(cat .deploy_timestamp)"
    echo "droplet-sync finished OK $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
else
    exit 1
fi
