#!/bin/bash
# Safe sync to droplet — NEVER copies venv, __pycache__, .git, or database files
# Usage: ./sync.sh [droplet-ip]

set -e

DROPLET_IP=${1:-"67.205.155.63"}
REMOTE_DIR="/opt/quantopsai"

echo "Syncing code to ${DROPLET_IP}:${REMOTE_DIR}..."

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
    --filter '- venv/**' \
    --filter '- __pycache__/**' \
    --filter '- .git/**' \
    /Users/mackr0/Quantops/*.py \
    /Users/mackr0/Quantops/*.sh \
    /Users/mackr0/Quantops/*.txt \
    /Users/mackr0/Quantops/*.md \
    /Users/mackr0/Quantops/templates/ \
    /Users/mackr0/Quantops/static/ \
    ${REMOTE_DIR}/

echo "Sync complete. Restarting services..."
ssh root@${DROPLET_IP} "systemctl restart quantopsai quantopsai-web"
sleep 3
ssh root@${DROPLET_IP} "systemctl is-active quantopsai && systemctl is-active quantopsai-web && echo 'Both services running'"
