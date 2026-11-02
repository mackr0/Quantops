#!/bin/bash
# Native-memory attribution sampler for the scheduler (2026-11-01).
#
# tracemalloc (the .memdiag_trace flag) sees Python and numpy allocations
# only. The ~670MB that malloc_trim cannot return is held below that —
# glibc heap used by C extensions, large anonymous mmaps, or library
# pages. This sampler reads the kernel's view of the LIVE scheduler every
# INTERVAL seconds until END (UTC HH:MM) and appends one block per sample:
#   smaps_rollup (Rss/Anonymous/Swap), the [heap] size, the ten largest
#   anonymous mappings, RSS summed per mapped file (.so / .db), thread
#   count, and open fds by type (sockets, sqlite files, pipes).
# Growth that lands in [heap] is C malloc; in large anonymous mappings is
# a big-buffer holder (numpy/arrow/ssl); in a .so's pages is library
# text/data. Re-resolves the service PID each sample so a restart does
# not break the series.
#
#   setsid nohup scripts/memdiag_native_sampler.sh 13:00 21:30 900 \
#       > deploy_logs/memdiag-native-$(date -u +%F).log 2>&1 < /dev/null &
START="${1:-13:00}"; END="${2:-21:30}"; INTERVAL="${3:-900}"
now_s() { date -u +%s; }
to_s() { date -u -d "$(date -u +%F) $1" +%s; }
start_s=$(to_s "$START"); end_s=$(to_s "$END")
[ "$(now_s)" -lt "$start_s" ] && sleep $(( start_s - $(now_s) ))
while [ "$(now_s)" -lt "$end_s" ]; do
  PID=$(systemctl show quantopsai -p MainPID --value)
  echo "===== $(date -u +%FT%TZ) pid=$PID ====="
  if [ -n "$PID" ] && [ "$PID" != "0" ] && [ -r /proc/$PID/smaps_rollup ]; then
    grep -E "^(Rss|Anonymous|Swap|Private_Dirty):" /proc/$PID/smaps_rollup | tr '\n' ' '; echo
    echo "threads=$(grep Threads /proc/$PID/status | awk '{print $2}') fds=$(ls /proc/$PID/fd 2>/dev/null | wc -l)"
    echo "-- fds by type:"; ls -l /proc/$PID/fd 2>/dev/null | awk '{print $NF}' | sed -E 's/:\[[0-9]+\]//; s/\/opt\/quantopsai\/quantopsai_profile_[0-9]+\.db.*/profile_db/; s/\/opt\/quantopsai\/quantopsai\.db.*/master_db/; s/^[0-9]+$//' | sort | uniq -c | sort -rn | head -8
    echo "-- largest anonymous mappings (kB):"
    awk '/^[0-9a-f]+-[0-9a-f]+ /{name=(NF>=6)?$6:""} /^Rss:/{if(name=="" || name=="[heap]") print $2, (name==""?"[anon]":name)}' /proc/$PID/smaps | sort -rn | head -10
    echo "-- rss per mapped file (kB, top 12):"
    awk '/^[0-9a-f]+-[0-9a-f]+ /{name=(NF>=6)?$6:"[anon]"} /^Rss:/{rss[name]+=$2} END{for(n in rss) print rss[n], n}' /proc/$PID/smaps | sort -rn | head -12
  else
    echo "scheduler pid not readable"
  fi
  sleep "$INTERVAL"
done
echo "===== sampler done $(date -u +%FT%TZ) ====="
