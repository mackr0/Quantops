#!/bin/bash
# Daily SQLite backup for QuantOpsAI — the one and only backup system.
#
# Cron: 0 5 * * * /opt/quantopsai/backup_daily.sh
#
# Produces files matching the pattern db_integrity.find_latest_backup
# expects:
#   /opt/quantopsai/backups/<db_filename>.<YYYYMMDD-HHMM>
#
# Uses sqlite3 .backup (online backup — safe while the DB is being
# written by the scheduler). Logs to syslog under tag "quantopsai-backup"
# and writes a machine-readable summary to ${BACKUP_DIR}/last_run.status,
# which premarket_smoke_test.py reads every weekday morning.
#
# Order of operations — housekeeping FIRST, then new snapshots:
#   1. remove orphaned -wal/-shm sidecars
#   2. gzip snapshots older than ~2 days, smallest first
#   3. prune snapshots older than RETAIN_DAYS
#   4. snapshot the irreplaceable DBs (master, per-profile, validations)
#   5. snapshot the alt-data caches — on one day a week only
#
# Why that order and those rules (2026-10-27 incident): the disk filled
# to 100% with two backup systems each copying every DB daily, including
# ~8GB of alt-data caches. gzip needs free space to write its output, so
# once the disk was full compression stopped too and nothing freed space
# again; the scheduler's logging, its backups and the DigitalOcean web
# console all broke. Now every snapshot is refused unless the disk keeps
# at least BACKUP_MIN_FREE_GB free AFTER it is written, housekeeping runs
# before anything new is written, and the alt-data DBs (the bulk of the
# bytes; rebuilt by the daily refresher) are snapshotted weekly.
#
# The newest snapshots stay uncompressed because
# db_integrity.find_latest_backup / restore_from_backup do not read
# .gz — to restore from an older backup, gunzip it first.
#
# Environment overrides (used by tests; defaults are production):
#   BACKUP_REPO_ROOT    live DB location           (/opt/quantopsai)
#   BACKUP_DIR          where snapshots go         (<repo>/backups)
#   BACKUP_RETAIN_DAYS  prune older than this      (14)
#   BACKUP_MIN_FREE_GB  headroom kept after write  (5)
#   BACKUP_ALTDATA_DOW  1=Mon .. 7=Sun, or "always" (7)
#   BACKUP_TODAY_DOW    override today's weekday   (date -u +%u)
#   BACKUP_STATUS_FILE  summary file               (<backup dir>/last_run.status)
set -u

LOG_TAG="${BACKUP_LOG_TAG:-quantopsai-backup}"
REPO_ROOT="${BACKUP_REPO_ROOT:-/opt/quantopsai}"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups}"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-14}"
MIN_FREE_GB="${BACKUP_MIN_FREE_GB:-5}"
MIN_FREE_BYTES=$(( MIN_FREE_GB * 1024 * 1024 * 1024 ))
ALTDATA_DOW="${BACKUP_ALTDATA_DOW:-7}"
TODAY_DOW="${BACKUP_TODAY_DOW:-$(date -u +%u)}"
STATUS_FILE="${BACKUP_STATUS_FILE:-${BACKUP_DIR}/last_run.status}"
TS="$(date -u +%Y%m%d-%H%M)"

mkdir -p "$BACKUP_DIR"

BACKED_UP=0
FAILED=0
SKIPPED_DISK=0
COMPRESSED=0
PRUNED=0
ORPHANS=0

log() {
    logger -t "$LOG_TAG" "$1" 2>/dev/null || true
    echo "[$(date -u +%FT%TZ)] $1"
}

# Portable (GNU + BSD) helpers.
file_size() { wc -c < "$1" | tr -d ' '; }
free_bytes() { echo $(( $(df -Pk "$BACKUP_DIR" | awk 'NR==2{print $4}') * 1024 )); }
human() {
    local b="$1"
    if (( b >= 1073741824 )); then printf "%.1fGB" "$(echo "$b / 1073741824" | bc -l)"
    elif (( b >= 1048576 )); then printf "%.0fMB" "$(echo "$b / 1048576" | bc -l)"
    else printf "%dB" "$b"; fi
}

# Glob-style pattern for the TS suffix we write: YYYYMMDD-HHMM
TS_GLOB='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]'

# ── 1. Orphaned sidecars ─────────────────────────────────────────────
# -shm/-wal files whose parent backup is gone (created when a post-run
# integrity check opens a backup; never restorable on their own, and
# the TS prune pattern below does not match them).
while IFS= read -r sc; do
    parent="${sc%-shm}"
    parent="${parent%-wal}"
    [[ -f "$parent" ]] || { rm -f "$sc" && ORPHANS=$((ORPHANS+1)); }
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name "*.${TS_GLOB}-shm" -o -name "*.${TS_GLOB}-wal" \))
log "removed ${ORPHANS} orphaned sidecar files"

# ── 2. Compress snapshots older than ~2 days, smallest first ─────────
# Smallest first so that on a nearly-full disk each success frees room
# for the next one instead of the biggest file failing immediately.
while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    if nice -n 10 gzip -f "$f"; then
        COMPRESSED=$((COMPRESSED+1))
    else
        log "WARN: gzip failed for $f"
    fi
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "*.${TS_GLOB}" -mtime +1 \
    | while IFS= read -r p; do echo "$(file_size "$p") $p"; done \
    | sort -n | cut -d' ' -f2-)
log "compressed ${COMPRESSED} backups"

# ── 3. Prune snapshots older than RETAIN_DAYS ────────────────────────
# Only files we wrote (TS suffix format, plus their gzipped form), so we
# never delete legacy hand-named files.
while IFS= read -r victim; do
    rm -f "$victim" && PRUNED=$((PRUNED+1))
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name "*.${TS_GLOB}" -o -name "*.${TS_GLOB}.gz" \) \
    -mtime "+${RETAIN_DAYS}")
log "pruned ${PRUNED} backups older than ${RETAIN_DAYS} days"

# ── 4./5. Snapshots ──────────────────────────────────────────────────
backup_one() {
    local src="$1"
    [[ -f "$src" ]] || { log "skip: $src missing"; return 0; }
    local name dst size free need
    name="$(basename "$src")"
    dst="${BACKUP_DIR}/${name}.${TS}"

    # Headroom guard: refuse any snapshot that would leave less than
    # MIN_FREE_BYTES on the disk. A full disk is worse than a missed
    # backup — it stops the scheduler's logging and journal writes.
    size="$(file_size "$src")"
    free="$(free_bytes)"
    need=$(( size + MIN_FREE_BYTES ))
    if (( free < need )); then
        log "FAIL: ${name} skipped — disk headroom: $(human "$free") free, need $(human "$need") (snapshot $(human "$size") + ${MIN_FREE_GB}GB floor)"
        SKIPPED_DISK=$((SKIPPED_DISK+1))
        FAILED=$((FAILED+1))
        return 1
    fi

    if sqlite3 "$src" ".backup '$dst'" 2>&1 | grep -q .; then
        # sqlite3 .backup prints nothing on success, error text on failure
        log "FAIL: ${name} backup failed"
        rm -f "$dst"
        FAILED=$((FAILED+1))
        return 1
    fi
    if [[ ! -s "$dst" ]]; then
        log "FAIL: ${name} backup produced empty file"
        rm -f "$dst"
        FAILED=$((FAILED+1))
        return 1
    fi
    BACKED_UP=$((BACKED_UP+1))
    log "ok: ${name} -> ${name}.${TS} ($(file_size "$dst") bytes)"
}

# Irreplaceable first: master, per-profile (all proprietary training
# data), strategy validations.
backup_one "${REPO_ROOT}/quantopsai.db"
for f in "${REPO_ROOT}"/quantopsai_profile_*.db; do
    [[ -e "$f" ]] && backup_one "$f"
done
backup_one "${REPO_ROOT}/strategy_validations.db"

# Alt-data DBs (each has a unique basename: biotechevents.db, congress.db,
# edgar13f.db, stocktwits.db — no collisions in the flat backup dir).
# These are the bulk of the bytes and are refreshed daily by
# altdata/run-altdata-daily.sh, so one snapshot a week is enough.
ALTDATA_INCLUDED=no
if [[ "$ALTDATA_DOW" == "always" || "$ALTDATA_DOW" == "$TODAY_DOW" ]]; then
    ALTDATA_INCLUDED=yes
    for f in "${REPO_ROOT}"/altdata/*/data/*.db; do
        [[ -e "$f" ]] && backup_one "$f"
    done
else
    log "alt-data snapshots skipped today (weekday ${TODAY_DOW}; taken on weekday ${ALTDATA_DOW})"
fi

# ── Summary ──────────────────────────────────────────────────────────
RESULT=OK
(( FAILED > 0 )) && RESULT=FAIL
FREE_AFTER="$(free_bytes)"

# Atomic write so a reader never sees a half-written file. If even this
# tiny write fails the disk is in serious trouble — say so.
if printf 'result=%s\ntimestamp=%s\nbacked_up=%d\nfailed=%d\nskipped_disk=%d\ncompressed=%d\npruned=%d\norphans_removed=%d\naltdata_included=%s\nfree_after_bytes=%d\n' \
        "$RESULT" "$(date -u +%FT%TZ)" "$BACKED_UP" "$FAILED" "$SKIPPED_DISK" \
        "$COMPRESSED" "$PRUNED" "$ORPHANS" "$ALTDATA_INCLUDED" "$FREE_AFTER" \
        > "${STATUS_FILE}.tmp" && mv -f "${STATUS_FILE}.tmp" "$STATUS_FILE"; then
    :
else
    log "FAIL: could not write status file ${STATUS_FILE}"
    RESULT=FAIL
fi

log "backup run complete: result=${RESULT} backed_up=${BACKED_UP} failed=${FAILED} skipped_disk=${SKIPPED_DISK} compressed=${COMPRESSED} pruned=${PRUNED} free_after=$(human "$FREE_AFTER")"
[[ "$RESULT" == "OK" ]]
