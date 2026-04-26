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

echo "Syncing code to ${DROPLET_IP}:${REMOTE_DIR}..."

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
    /Users/mackr0/Quantops/ \
    root@${DROPLET_IP}:${REMOTE_DIR}/ 2>/dev/null | grep '^<f' | awk '{print $2}' || true)

if [ -z "$CHANGED" ]; then
    echo "No files changed. Nothing to sync."
    exit 0
fi

echo "Changed files:"
echo "$CHANGED" | sed 's/^/  /'

# Actually sync
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
    /Users/mackr0/Quantops/ \
    root@${DROPLET_IP}:${REMOTE_DIR}/

echo "Sync complete."

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

# Verify
sleep 2
ssh root@${DROPLET_IP} "
    if systemctl is-active --quiet quantopsai && systemctl is-active --quiet quantopsai-web; then
        echo 'Both services running.'
    else
        echo 'WARNING: Service check failed!'
        systemctl is-active quantopsai || echo '  quantopsai: not running'
        systemctl is-active quantopsai-web || echo '  quantopsai-web: not running'
    fi
"
