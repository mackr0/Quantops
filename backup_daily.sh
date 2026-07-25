#!/bin/bash
# Daily SQLite backup for QuantOpsAI.
#
# Cron: 0 5 * * * /opt/quantopsai/backup_daily.sh
#
# Produces files matching the pattern db_integrity.find_latest_backup
# expects:
#   /opt/quantopsai/backups/<db_filename>.<YYYYMMDD-HHMM>
#
# Uses sqlite3 .backup (online backup — safe while the DB is being
# written by the scheduler). Prunes files older than 14 days. Logs to
# syslog under tag "quantopsai-backup".
#
# Backups older than ~2 days are gzipped to keep disk usage sane
# (biotechevents.db + edgar13f.db alone are ~2GB/day uncompressed).
# The newest backups stay uncompressed because
# db_integrity.find_latest_backup / restore_from_backup do not read
# .gz — to restore from an older backup, gunzip it first.
set -u

LOG_TAG="${BACKUP_LOG_TAG:-quantopsai-backup}"
REPO_ROOT="${BACKUP_REPO_ROOT:-/opt/quantopsai}"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups}"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-14}"
TS="$(date -u +%Y%m%d-%H%M)"

mkdir -p "$BACKUP_DIR"

log() {
    logger -t "$LOG_TAG" "$1" 2>/dev/null || true
    echo "[$(date -u +%FT%TZ)] $1"
}

backup_one() {
    local src="$1"
    [[ -f "$src" ]] || { log "skip: $src missing"; return 0; }
    local name dst
    name="$(basename "$src")"
    dst="${BACKUP_DIR}/${name}.${TS}"
    if sqlite3 "$src" ".backup '$dst'" 2>&1 | grep -q .; then
        # sqlite3 .backup prints nothing on success, error text on failure
        log "FAIL: ${name} backup failed"
        rm -f "$dst"
        return 1
    fi
    if [[ ! -s "$dst" ]]; then
        log "FAIL: ${name} backup produced empty file"
        rm -f "$dst"
        return 1
    fi
    log "ok: ${name} -> ${name}.${TS} ($(stat -c%s "$dst" 2>/dev/null || echo '?') bytes)"
}

# Master DB
backup_one "${REPO_ROOT}/quantopsai.db"

# Per-profile DBs
for f in "${REPO_ROOT}"/quantopsai_profile_*.db; do
    [[ -e "$f" ]] && backup_one "$f"
done

# Strategy validations (optional, may not exist)
backup_one "${REPO_ROOT}/strategy_validations.db"

# Alt-data DBs (each has a unique basename: biotechevents.db, congress.db,
# edgar13f.db, stocktwits.db — no collisions in the flat backup dir)
for f in "${REPO_ROOT}"/altdata/*/data/*.db; do
    [[ -e "$f" ]] && backup_one "$f"
done

# Glob-style pattern for the TS suffix we write: YYYYMMDD-HHMM
TS_GLOB='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]'

# Compress backups older than ~2 days. The newest ones are left
# uncompressed so db_integrity.find_latest_backup (which does not
# understand .gz) always has a directly restorable file, even if a
# day's backup run fails.
COMPRESSED=0
while IFS= read -r f; do
    if nice -n 10 gzip -f "$f"; then
        COMPRESSED=$((COMPRESSED+1))
    else
        log "WARN: gzip failed for $f"
    fi
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "*.${TS_GLOB}" -mtime +1)
log "compressed ${COMPRESSED} backups"

# Remove -shm/-wal sidecars whose parent backup is gone (created when a
# post-run integrity check opens a backup; never restorable on their
# own, and the TS prune pattern below does not match them).
ORPHANS=0
while IFS= read -r sc; do
    parent="${sc%-shm}"
    parent="${parent%-wal}"
    [[ -f "$parent" ]] || { rm -f "$sc" && ORPHANS=$((ORPHANS+1)); }
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name "*.${TS_GLOB}-shm" -o -name "*.${TS_GLOB}-wal" \))
log "removed ${ORPHANS} orphaned sidecar files"

# Prune backups older than RETAIN_DAYS. Only files we wrote (TS suffix
# format, plus their gzipped form), so we never delete legacy
# hand-named files.
PRUNED=0
while IFS= read -r victim; do
    rm -f "$victim" && PRUNED=$((PRUNED+1))
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name "*.${TS_GLOB}" -o -name "*.${TS_GLOB}.gz" \) \
    -mtime "+${RETAIN_DAYS}")
log "pruned ${PRUNED} backups older than ${RETAIN_DAYS} days"

log "backup run complete"
