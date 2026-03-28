#!/bin/bash
ssh root@${1:-"67.205.155.63"} "systemctl status quantopsai && echo '---' && tail -20 /opt/quantopsai/logs/*.log"
