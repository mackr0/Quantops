#!/bin/bash
IP=${1:-"67.205.155.63"}
echo "=== Scheduler ==="
ssh root@$IP "systemctl status quantopsai --no-pager -l | head -10"
echo ""
echo "=== Web UI ==="
ssh root@$IP "systemctl status quantopsai-web --no-pager -l | head -10"
echo ""
echo "=== Recent Logs ==="
ssh root@$IP "tail -20 /opt/quantopsai/logs/*.log 2>/dev/null || echo 'No logs yet'"
