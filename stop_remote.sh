#!/bin/bash
ssh root@${1:-"67.205.155.63"} "systemctl stop quantops && echo 'Quantops stopped'"
