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
echo "[1/7] Installing system dependencies..."
ssh ${REMOTE_USER}@${DROPLET_IP} "apt update -qq && apt install -y -qq python3-pip python3-venv git nginx > /dev/null 2>&1 && echo 'Done'"

# ── Step 2: Create remote directory ──────────────────────────────────
echo ""
echo "[2/7] Creating remote directory ${REMOTE_DIR}..."
ssh ${REMOTE_USER}@${DROPLET_IP} "mkdir -p ${REMOTE_DIR}/{logs,static,templates}"

# ── Step 3: Sync files ───────────────────────────────────────────────
echo ""
echo "[3/7] Syncing files to droplet..."
# SAFETY: Use explicit file list — NEVER sync venv, __pycache__, .git, or databases
rsync -avz --progress \
    --filter '- venv/' \
    --filter '- venv/**' \
    --filter '- __pycache__/' \
    --filter '- __pycache__/**' \
    --filter '- .git/' \
    --filter '- .git/**' \
    --filter '- *.db' \
    --filter '- *.db-shm' \
    --filter '- *.db-wal' \
    --filter '- *.pyc' \
    --filter '- .DS_Store' \
    --include '*.py' \
    --include '*.html' \
    --include '*.css' \
    --include '*.js' \
    --include '*.md' \
    --include '*.txt' \
    --include '*.sh' \
    --include '*.png' \
    --include '.env' \
    --include 'templates/' \
    --include 'templates/**' \
    --include 'static/' \
    --include 'static/**' \
    --include 'strategies/' \
    --include 'strategies/**' \
    --include 'tests/' \
    --include 'tests/**' \
    --exclude '*' \
    ./ ${REMOTE_USER}@${DROPLET_IP}:${REMOTE_DIR}/

# SAFETY CHECK: verify venv wasn't corrupted
echo ""
echo "Verifying remote venv integrity..."
ssh ${REMOTE_USER}@${DROPLET_IP} "head -1 ${REMOTE_DIR}/venv/bin/gunicorn | grep -q '/opt/quantopsai/venv' && echo 'SAFE: venv shebangs correct' || echo 'DANGER: venv corrupted — run: ${REMOTE_DIR}/venv/bin/pip install -r ${REMOTE_DIR}/requirements.txt'"

# ── Step 4: Set up Python environment ────────────────────────────────
echo ""
echo "[4/7] Setting up Python environment..."
ssh ${REMOTE_USER}@${DROPLET_IP} << 'SETUP'
cd /opt/quantopsai
if [ ! -d venv ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q 2>&1 | tail -3
pip install yfinance tzdata -q 2>&1 | tail -1
mkdir -p logs
SETUP

# ── Step 4b: Align prod .git/ to origin/main ─────────────────────────
# rsync above ships files but never touches .git/. Without this step,
# prod's git history silently drifts behind the deployed code, turning
# any future `git reset/checkout/pull` on prod into a catastrophic revert.
echo ""
echo "[4b/7] Aligning prod .git/ to origin/main..."
ssh ${REMOTE_USER}@${DROPLET_IP} "
    set -e
    git config --global --add safe.directory ${REMOTE_DIR} >/dev/null 2>&1 || true
    cd ${REMOTE_DIR}
    if [ -d .git ]; then
        git fetch origin --quiet
        git reset --hard origin/main >/dev/null
        echo \"  prod HEAD = \$(git rev-parse --short HEAD)\"
        DRIFT=\$(git status --porcelain | grep -v '^??' || true)
        if [ -n \"\$DRIFT\" ]; then
            echo 'WARNING: tracked files diverge from origin/main on prod after reset:'
            echo \"\$DRIFT\"
        fi
    else
        echo \"  no .git/ on prod yet — first deploy. Cloning to align refs...\"
        git clone --quiet --bare https://github.com/mackr0/Quantops.git .git-tmp
        mv .git-tmp/* .git-tmp/.git* . 2>/dev/null || true
        rmdir .git-tmp 2>/dev/null || true
        # Convert from bare to normal
        git config --bool core.bare false
        git reset --hard origin/main >/dev/null 2>&1 || true
        echo \"  prod HEAD = \$(git rev-parse --short HEAD 2>/dev/null || echo 'unset')\"
    fi
"

# ── Step 5: Run migration (safe to re-run) ───────────────────────────
echo ""
echo "[5/7] Running database migration..."
ssh ${REMOTE_USER}@${DROPLET_IP} "cd /opt/quantopsai && source venv/bin/activate && python3 migrate.py"

# ── Step 6: Create systemd services ──────────────────────────────────
echo ""
echo "[6/7] Creating systemd services..."
ssh ${REMOTE_USER}@${DROPLET_IP} << 'SERVICE'

# Scheduler service
cat > /etc/systemd/system/quantopsai.service << 'EOF'
[Unit]
Description=QuantOpsAI Trading Scheduler
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

# Web app service
cat > /etc/systemd/system/quantopsai-web.service << 'EOF'
[Unit]
Description=QuantOpsAI Web UI
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/quantopsai
ExecStart=/opt/quantopsai/venv/bin/gunicorn --bind 127.0.0.1:8000 --workers 2 --timeout 120 "app:create_app()"
Restart=on-failure
RestartSec=10
EnvironmentFile=/opt/quantopsai/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable quantopsai quantopsai-web
systemctl restart quantopsai quantopsai-web
SERVICE

# ── Step 7: Configure nginx ──────────────────────────────────────────
echo ""
echo "[7/7] Configuring nginx..."
ssh ${REMOTE_USER}@${DROPLET_IP} << 'NGINX'
cat > /etc/nginx/sites-available/quantopsai << 'EOF'
server {
    listen 80;
    server_name _;

    location /static/ {
        alias /opt/quantopsai/static/;
        expires 1d;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
EOF

# Enable site, disable default
ln -sf /etc/nginx/sites-available/quantopsai /etc/nginx/sites-enabled/quantopsai
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
NGINX

echo ""
echo "============================================"
echo "  Deployment complete!"
echo "============================================"
echo ""

# Print service status
ssh ${REMOTE_USER}@${DROPLET_IP} "systemctl status quantopsai --no-pager -l | head -10"
echo "---"
ssh ${REMOTE_USER}@${DROPLET_IP} "systemctl status quantopsai-web --no-pager -l | head -10"

echo ""
echo "Web UI: http://${DROPLET_IP}"
echo ""
echo "Useful commands:"
echo "  ./status_remote.sh ${DROPLET_IP}   — Check status and logs"
echo "  ./stop_remote.sh ${DROPLET_IP}     — Stop the service"
echo "  ./deploy.sh ${DROPLET_IP}          — Redeploy"
