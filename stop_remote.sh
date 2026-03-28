#!/bin/bash
ssh root@${1:-"67.205.155.63"} "systemctl stop quantopsai quantopsai-web && echo 'QuantOpsAI stopped (scheduler + web)'"
