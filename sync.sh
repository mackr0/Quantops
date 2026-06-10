#!/bin/bash
# Smart sync to droplet — only restarts what actually changed.
#
# Usage:
#   ./sync.sh              # auto-detect what changed, restart minimally
#   ./sync.sh --web        # force web-only restart (templates, CSS, views)
#   ./sync.sh --scheduler  # force scheduler restart (pipeline, strategies)
#   ./sync.sh --all        # force restart everything (old behavior)

set -e

DROPLET_IP=${1:-"67.205.155.63"}
REMOTE_DIR="/opt/quantopsai"

# Handle flags
FORCE_MODE=""
if [[ "$1" == "--web" || "$2" == "--web" ]]; then FORCE_MODE="web"; fi
if [[ "$1" == "--scheduler" || "$2" == "--scheduler" ]]; then FORCE_MODE="scheduler"; fi
if [[ "$1" == "--all" || "$2" == "--all" ]]; then FORCE_MODE="all"; fi
# Strip flags from DROPLET_IP if a flag was passed as $1
if [[ "$DROPLET_IP" == --* ]]; then DROPLET_IP="67.205.155.63"; fi

LOCAL_REPO="/Users/mackr0/Quantops"

# ---------------------------------------------------------------------------
# Pre-flight gate: prod's .git/ is updated to origin/main as part of every
# deploy (see post-deploy block below). For that to be safe, local must be
# clean and origin/main must equal local HEAD. Otherwise rsync would ship
# code to prod that origin/main doesn't have, so the post-deploy reset
# would silently revert the just-deployed files.
# ---------------------------------------------------------------------------
if ! git -C "$LOCAL_REPO" diff-index --quiet HEAD -- 2>/dev/null; then
    echo "ERROR: Uncommitted changes in working tree."
    echo "Commit (or stash) before running sync.sh. Otherwise rsync would"
    echo "ship code to prod that origin/main doesn't reflect, and the"
    echo "post-deploy 'git reset --hard origin/main' would revert it."
    git -C "$LOCAL_REPO" status --short
    exit 1
fi
git -C "$LOCAL_REPO" fetch origin --quiet 2>/dev/null || true
LOCAL_HEAD=$(git -C "$LOCAL_REPO" rev-parse HEAD)
ORIGIN_HEAD=$(git -C "$LOCAL_REPO" rev-parse origin/main 2>/dev/null || echo "UNKNOWN")
if [ "$LOCAL_HEAD" != "$ORIGIN_HEAD" ]; then
    echo "ERROR: Local HEAD ($LOCAL_HEAD) does not match origin/main ($ORIGIN_HEAD)."
    echo "Run: git push origin main"
    echo "(or pull, if you're behind) before deploying."
    exit 1
fi

echo "Syncing code to ${DROPLET_IP}:${REMOTE_DIR}..."
echo "  local HEAD = $LOCAL_HEAD (matches origin/main)"

# Capture which files changed via rsync dry-run
CHANGED=$(rsync -az --delete --dry-run --itemize-changes \
    --exclude 'venv/' \
    --exclude '__pycache__/' \
    --exclude '.git/' \
    --exclude '.claude/' \
    --exclude '*.db' \
    --exclude '*.db-shm' \
    --exclude '*.db-wal' \
    --exclude '*.pyc' \
    --exclude '.env' \
    --exclude 'node_modules/' \
    --exclude '.DS_Store' \
    --exclude 'logs/' \
    --exclude 'backups/' \
    --exclude 'exports/' \
    --exclude '*.pkl' \
    --exclude 'cycle_data_*.json' \
    --exclude 'scheduler_status.json' \
    --exclude 'dynamic_screener_cache.json' \
    --exclude '.sync_test_marker' \
    --exclude '.daily_snapshot_done.marker' \
    --exclude '.daily_summary_sent_p*.marker' \
    --exclude '.weekly_digest_sent.marker' \
    --exclude '.capital_rebalance_done.marker' \
    --exclude '.post_mortem_done_p*.marker' \
    /Users/mackr0/Quantops/ \
    root@${DROPLET_IP}:${REMOTE_DIR}/ 2>/dev/null | grep '^<f' | awk '{print $2}' || true)

if [ -z "$CHANGED" ]; then
    # 2026-06-10 — DO NOT EARLY-RETURN HERE. The pre-fix script
    # returned from the no-changes branch BEFORE running the .git/
    # alignment. Problem: rsync only detects file CONTENT diffs;
    # prod's .git/ tracking state can still be stale (e.g.,
    # previous deploy crashed post-rsync / pre-git-reset, leaving
    # prod files current but git HEAD pointing at the older
    # commit). The operator hit this multiple times today: sync.sh
    # reported success but prod.git was 4 commits behind
    # origin/main, and running the reset script picked up the
    # stale code paths.
    #
    # Instead: SKIP rsync (nothing to send) but still run the
    # post-deploy git-reset + content verification to align prod's
    # .git/ to origin/main. Costs ~2 seconds; catches the silent
    # drift class.
    echo "No file content changes via rsync; running git-state verification anyway."
    SKIP_RSYNC=true
else
    SKIP_RSYNC=false
    echo "Changed files:"
    echo "$CHANGED" | sed 's/^/  /'
fi

# Actually sync (skipped when rsync dry-run found nothing to transfer)
if ! $SKIP_RSYNC; then
    rsync -az --delete \
        --exclude 'venv/' \
        --exclude '__pycache__/' \
        --exclude '.git/' \
        --exclude '.claude/' \
        --exclude '*.db' \
        --exclude '*.db-shm' \
        --exclude '*.db-wal' \
        --exclude '*.pyc' \
        --exclude '.env' \
        --exclude 'node_modules/' \
        --exclude '.DS_Store' \
        --exclude 'logs/' \
        --exclude 'backups/' \
        --exclude 'exports/' \
        --exclude '*.pkl' \
        --exclude 'cycle_data_*.json' \
        --exclude 'scheduler_status.json' \
        --exclude 'dynamic_screener_cache.json' \
        --exclude '.sync_test_marker' \
        --exclude '.daily_snapshot_done.marker' \
        --exclude '.daily_summary_sent_p*.marker' \
        --exclude '.weekly_digest_sent.marker' \
        --exclude '.capital_rebalance_done.marker' \
        --exclude '.post_mortem_done_p*.marker' \
        /Users/mackr0/Quantops/ \
        root@${DROPLET_IP}:${REMOTE_DIR}/

    echo "Sync complete."
else
    echo "Sync skipped (no file delta)."
fi

# ---------------------------------------------------------------------------
# Post-deploy: sync prod's .git/ to origin/main so prod's git HEAD always
# tracks the deployed code. Without this step, rsync moves files but never
# updates .git/, and prod's git history silently drifts behind the deployed
# code — turning any future `git reset/checkout/pull` on prod into a
# catastrophic revert. Pre-flight gate above guarantees origin/main equals
# what we just rsync'd, so this reset is a no-op for tracked files.
#
# 2026-06-10 — HARDENED. The prior version had silent-failure modes the
# operator hit multiple times today:
#   - `--quiet` and `2>/dev/null` redirects suppressed real git errors
#     (e.g., a corrupted refspec or a fetch that couldn't reach origin).
#   - `set -e` inside SSH heredoc may not always abort the outer script
#     if the heredoc itself completes "successfully" (returns 0) after
#     printing the error to stderr.
#   - HEAD verification alone is fooled by clock-skewed git operations
#     where the SHA is correct but the file content somehow doesn't match.
#
# Defenses added:
#   1. No --quiet / 2>/dev/null — git errors print loud.
#   2. CONTENT verification: pick a file from the local working tree,
#      compute its SHA-256, then compute the same on prod after the
#      git reset. If they don't match, the deploy is BAD — abort.
#   3. Explicit check of ssh's exit code via `|| { ...; exit 1; }`.
#   4. The shipped tag — write the deploy SHA to /opt/quantopsai/.deploy_sha
#      so other tooling can verify deploy state without depending on
#      .git/ state being correct.
# ---------------------------------------------------------------------------
echo ""
echo "Aligning prod .git/ to origin/main..."

# Pick a representative production source file for content verification.
# trade_pipeline.py touches almost every deploy; if it's wrong, the deploy
# is broken regardless of which other files changed.
VERIFY_FILE="trade_pipeline.py"
LOCAL_SHA=$(shasum -a 256 "$LOCAL_REPO/$VERIFY_FILE" | awk '{print $1}')
echo "  verification file: $VERIFY_FILE (sha256=${LOCAL_SHA:0:12}...)"

ssh root@${DROPLET_IP} bash -s <<SSHEOF || { echo "ERROR: sync.sh post-deploy SSH block failed. Prod state is uncertain. Investigate before assuming the deploy took."; exit 1; }
set -euo pipefail
git config --global --add safe.directory ${REMOTE_DIR} >/dev/null 2>&1 || true
cd ${REMOTE_DIR}

# Loud git operations — surface every error.
git fetch origin
git reset --hard origin/main

PROD_HEAD=\$(git rev-parse HEAD)
if [ "\$PROD_HEAD" != "$LOCAL_HEAD" ]; then
    echo "ERROR: prod HEAD (\$PROD_HEAD) != local HEAD ($LOCAL_HEAD) after reset."
    echo "       Origin may not have received our push, or fetch failed silently."
    exit 1
fi

# Sanity: only untracked runtime artifacts should remain.
DRIFT=\$(git status --porcelain | grep -v '^??' || true)
if [ -n "\$DRIFT" ]; then
    echo "ERROR: Tracked files on prod diverge from origin/main:"
    echo "\$DRIFT"
    exit 1
fi

# CONTENT verification (defense beyond git HEAD).
PROD_SHA=\$(sha256sum "$VERIFY_FILE" 2>/dev/null | awk '{print \$1}')
if [ "\$PROD_SHA" != "$LOCAL_SHA" ]; then
    echo "ERROR: $VERIFY_FILE content mismatch."
    echo "       local sha256:  $LOCAL_SHA"
    echo "       prod sha256:   \$PROD_SHA"
    echo "       The git HEAD matches but the file content does NOT — something"
    echo "       between rsync and git reset corrupted state. Manual fix:"
    echo "       ssh root@$DROPLET_IP 'cd ${REMOTE_DIR} && git fetch && git reset --hard origin/main'"
    exit 1
fi

# Stamp deploy marker so downstream tooling can verify state without git.
echo "$LOCAL_HEAD" > ${REMOTE_DIR}/.deploy_sha
date -u +"%Y-%m-%dT%H:%M:%SZ" > ${REMOTE_DIR}/.deploy_timestamp

echo "  prod HEAD = \$(git rev-parse --short HEAD) (HEAD + content verified)"
echo "  .deploy_sha written: $LOCAL_HEAD"
SSHEOF

# Determine what needs restarting
NEED_WEB=false
NEED_SCHEDULER=false

if [ "$FORCE_MODE" = "all" ]; then
    NEED_WEB=true
    NEED_SCHEDULER=true
elif [ "$FORCE_MODE" = "web" ]; then
    NEED_WEB=true
elif [ "$FORCE_MODE" = "scheduler" ]; then
    NEED_SCHEDULER=true
else
    # Auto-detect from changed files
    # Web-needed files: anything the gunicorn process loads on startup,
    # including the schema migration in models.py (init_user_db runs in
    # create_app — without a web restart, ALTER TABLE migrations never
    # apply and per-DB writes that need new columns fail).
    WEB_PATTERNS="templates/|static/|views\.py|display_names\.py|app\.py|auth\.py|models\.py"
    # Scheduler files: everything else that's Python
    SCHED_PATTERNS="\.py$"

    while IFS= read -r file; do
        if echo "$file" | grep -qE "$WEB_PATTERNS"; then
            NEED_WEB=true
        fi
        # Any .py file that isn't web-only requires scheduler restart
        if echo "$file" | grep -qE "$SCHED_PATTERNS" && ! echo "$file" | grep -qE "^(templates/|static/|tests/)"; then
            # Check if it's ONLY a web file
            if ! echo "$file" | grep -qE "^(views\.py|display_names\.py|app\.py|auth\.py)$"; then
                NEED_SCHEDULER=true
            fi
        fi
        # Markdown/docs don't need any restart
    done <<< "$CHANGED"
fi

# Execute restarts
if $NEED_SCHEDULER; then
    echo ""
    echo "Scheduler code changed — waiting for safe restart window..."
    # Check if market is closed or if we're between cycles
    ssh root@${DROPLET_IP} "
        # Check if the scheduler is sleeping (between cycles)
        LAST_LINE=\$(journalctl -u quantopsai --no-pager -n 1 2>/dev/null | tail -1)
        if echo \"\$LAST_LINE\" | grep -q 'sleeping until\|Market closed\|Sleeping'; then
            echo 'Scheduler is idle — safe to restart.'
        else
            echo 'WARNING: Scheduler may be mid-cycle. Waiting up to 60s for cycle end...'
            for i in \$(seq 1 12); do
                sleep 5
                LAST=\$(journalctl -u quantopsai --no-pager -n 1 2>/dev/null | tail -1)
                if echo \"\$LAST\" | grep -q 'sleeping until\|Market closed\|Sleeping\|completed'; then
                    echo 'Cycle ended — safe to restart.'
                    break
                fi
                if [ \$i -eq 12 ]; then
                    echo 'Timeout waiting for cycle — restarting anyway.'
                fi
            done
        fi
        systemctl restart quantopsai
    "
    echo "Scheduler restarted."
fi

if $NEED_WEB; then
    ssh root@${DROPLET_IP} "systemctl restart quantopsai-web"
    echo "Web server restarted."
fi

if ! $NEED_WEB && ! $NEED_SCHEDULER; then
    echo "Only docs/tests changed — no restart needed."
fi

# Final verification: services up AND deploy marker matches what we sent.
# 2026-06-10 — added the deploy-marker check + post-restart re-verify of
# trade_pipeline.py content. The previous "Both services running" was the
# only success signal and the operator (rightly) trusted it; with today's
# silent .git-drift class, "services running" was true while prod code
# was 4 commits stale. The marker + re-hash makes that mismatch impossible
# to claim as success.
sleep 2
ssh root@${DROPLET_IP} bash -s <<SSHEOF || { echo "ERROR: Final verification failed. Investigate before trusting the deploy."; exit 1; }
set -euo pipefail
QUANTOPS_OK=false
WEB_OK=false
if systemctl is-active --quiet quantopsai; then QUANTOPS_OK=true; fi
if systemctl is-active --quiet quantopsai-web; then WEB_OK=true; fi

if ! \$QUANTOPS_OK; then
    echo "WARNING: quantopsai (scheduler) is not active."
    systemctl is-active quantopsai || true
fi
if ! \$WEB_OK; then
    echo "WARNING: quantopsai-web is not active."
    systemctl is-active quantopsai-web || true
fi

if [ ! -f ${REMOTE_DIR}/.deploy_sha ]; then
    echo "ERROR: ${REMOTE_DIR}/.deploy_sha missing — post-deploy block did not run."
    exit 1
fi
DEPLOYED_SHA=\$(cat ${REMOTE_DIR}/.deploy_sha)
if [ "\$DEPLOYED_SHA" != "$LOCAL_HEAD" ]; then
    echo "ERROR: .deploy_sha (\$DEPLOYED_SHA) != local HEAD ($LOCAL_HEAD)."
    exit 1
fi
# Re-verify content AFTER restart — guards against the restart somehow
# loading from a cached / stale path (unlikely but cheap to check).
PROD_SHA=\$(sha256sum ${REMOTE_DIR}/$VERIFY_FILE | awk '{print \$1}')
if [ "\$PROD_SHA" != "$LOCAL_SHA" ]; then
    echo "ERROR: $VERIFY_FILE drifted between deploy + restart."
    echo "       expected: $LOCAL_SHA"
    echo "       actual:   \$PROD_SHA"
    exit 1
fi

if \$QUANTOPS_OK && \$WEB_OK; then
    echo "Both services running."
    echo "  prod @ \$(cat ${REMOTE_DIR}/.deploy_sha | cut -c1-12) (HEAD + content verified)"
    echo "  deployed: \$(cat ${REMOTE_DIR}/.deploy_timestamp)"
else
    exit 1
fi
SSHEOF
