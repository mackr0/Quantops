#!/bin/bash
# Safe sync to droplet — preserves directory structure. NEVER copies venv,
# __pycache__, .git, database files, or .env.
# Usage: ./sync.sh [droplet-ip]

set -e

DROPLET_IP=${1:-"67.205.155.63"}
REMOTE_DIR="/opt/quantopsai"

echo "Syncing code to ${DROPLET_IP}:${REMOTE_DIR}..."

# Sync from the project root so the directory structure (templates/,
# strategies/, tests/, static/) is preserved on the server. --delete
# cleans up files removed locally, but excluded items (venv, .env,
# *.db, logs/, exports/) are protected on both sides.
rsync -az --delete \
    --exclude 'venv/' \
    --exclude '__pycache__/' \
    --exclude '.git/' \
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
    /Users/mackr0/Quantops/ \
    root@${DROPLET_IP}:${REMOTE_DIR}/

echo "Sync complete. Restarting services..."
ssh root@${DROPLET_IP} "systemctl restart quantopsai quantopsai-web"
sleep 3
ssh root@${DROPLET_IP} "systemctl is-active quantopsai && systemctl is-active quantopsai-web && echo 'Both services running'"
