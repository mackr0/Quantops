#!/bin/bash
ssh root@${1:-"67.205.155.63"} "systemctl status quantops && echo '---' && tail -20 /opt/quantops/logs/*.log"
