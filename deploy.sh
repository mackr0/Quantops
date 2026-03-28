#!/bin/bash
# Deploy QuantOpsAI to DigitalOcean droplet
# Usage: ./deploy.sh <droplet-ip>

set -e

DROPLET_IP=${1:-"67.205.155.63"}
REMOTE_USER="root"
REMOTE_DIR="/opt/quantopsai"

echo "============================================"
echo "  Deploying QuantOpsAI to ${REMOTE_USER}@${DROPLET_IP}"
echo "============================================"

# ── Step 1: Install system dependencies ──────────────────────────────
echo ""
echo "[1/5] Installing system dependencies..."
ssh ${REMOTE_USER}@${DROPLET_IP} "apt update && apt install -y python3-pip python3-venv git"

# ── Step 2: Create remote directory ──────────────────────────────────
echo ""
echo "[2/5] Creating remote directory ${REMOTE_DIR}..."
ssh ${REMOTE_USER}@${DROPLET_IP} "mkdir -p ${REMOTE_DIR}"

# ── Step 3: Sync files ───────────────────────────────────────────────
echo ""
echo "[3/5] Syncing files to droplet..."
rsync -avz --progress \
    --exclude 'venv/' \
    --exclude '__pycache__/' \
    --exclude '.git/' \
    --exclude '*.db' \
    --include '*.py' \
    --include 'requirements.txt' \
    --include '.env' \
    --include '*/' \
    --exclude '*' \
    ./ ${REMOTE_USER}@${DROPLET_IP}:${REMOTE_DIR}/

# ── Step 4: Set up Python environment ────────────────────────────────
echo ""
echo "[4/5] Setting up Python environment..."
ssh ${REMOTE_USER}@${DROPLET_IP} << 'SETUP'
cd /opt/quantopsai
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p logs
SETUP

# ── Step 5: Create and enable systemd service ────────────────────────
echo ""
echo "[5/5] Creating systemd service..."
ssh ${REMOTE_USER}@${DROPLET_IP} << 'SERVICE'
cat > /etc/systemd/system/quantopsai.service << 'EOF'
[Unit]
Description=QuantOpsAI Autonomous Trading Scheduler
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/quantopsai
ExecStart=/opt/quantopsai/venv/bin/python3 multi_scheduler.py
Restart=on-failure
RestartSec=30
EnvironmentFile=/opt/quantopsai/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable quantopsai
systemctl restart quantopsai
SERVICE

echo ""
echo "============================================"
echo "  Deployment complete!"
echo "============================================"
echo ""

# Print service status
ssh ${REMOTE_USER}@${DROPLET_IP} "systemctl status quantopsai --no-pager"

echo ""
echo "Useful commands:"
echo "  ./status_remote.sh ${DROPLET_IP}   — Check status and logs"
echo "  ./stop_remote.sh ${DROPLET_IP}     — Stop the service"
echo "  ./deploy.sh ${DROPLET_IP}          — Redeploy"
