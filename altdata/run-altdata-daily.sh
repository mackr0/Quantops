#!/usr/bin/env bash
# Daily refresh for the four bundled alt-data scrapers.
#
# Sequential (not parallel) so we don't compound API pressure on any
# single upstream. Total runtime ~30-50 min depending on disclosure
# volume since last run. Idempotent — safe to interrupt and re-run.
#
# Usage (from any cwd):
#   bash altdata/run-altdata-daily.sh                       # all four
#   bash altdata/run-altdata-daily.sh --skip stocktwits     # skip one
#   PROJECTS="congresstrades edgar13f" bash altdata/run-altdata-daily.sh

set -e

# Locate the script + Quantops repo root regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Use the Quantops venv (single shared environment after the
# 2026-05-03 merge — no per-project venvs).
VENV_PYTHON="$REPO_ROOT/venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "✗ Quantops venv not found at $VENV_PYTHON"
    echo "  Run: cd $REPO_ROOT && python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    exit 1
fi

DEFAULT_PROJECTS=(
    "congresstrades"
    "edgar13f"
    "biotechevents"
    "stocktwits"
)

SKIPS=()
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --skip)
            SKIPS+=("$2")
            shift 2
            ;;
        *)
            echo "Unknown arg: $1"
            echo "Usage: $0 [--skip PROJECT_NAME ...]"
            exit 1
            ;;
    esac
done

if [ -n "$PROJECTS" ]; then
    read -ra PROJECT_ARRAY <<< "$PROJECTS"
else
    PROJECT_ARRAY=("${DEFAULT_PROJECTS[@]}")
fi

PROJECTS_TO_RUN=()
for p in "${PROJECT_ARRAY[@]}"; do
    skip_this=false
    for s in "${SKIPS[@]}"; do
        if [ "$p" = "$s" ]; then
            skip_this=true
            break
        fi
    done
    [ "$skip_this" = false ] && PROJECTS_TO_RUN+=("$p")
done

START_TIME=$(date -u +%s)
TOTAL=${#PROJECTS_TO_RUN[@]}

echo ""
echo "======================================================================"
echo "  ALT-DATA DAILY REFRESH"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "  Running: ${PROJECTS_TO_RUN[*]}"
echo "======================================================================"

FAILED=()
SUCCEEDED=()

i=0
for proj in "${PROJECTS_TO_RUN[@]}"; do
    i=$((i + 1))
    proj_dir="$SCRIPT_DIR/$proj"

    if [ ! -d "$proj_dir" ]; then
        echo ""
        echo "[$i/$TOTAL] $proj — SKIPPED (directory not found at $proj_dir)"
        FAILED+=("$proj (missing dir)")
        continue
    fi

    echo ""
    echo "----------------------------------------------------------------------"
    echo "[$i/$TOTAL] $proj"
    echo "----------------------------------------------------------------------"

    proj_start=$(date -u +%s)
    if (cd "$proj_dir" && "$VENV_PYTHON" -m "$proj".cli daily); then
        proj_end=$(date -u +%s)
        echo "  ✓ done in $((proj_end - proj_start))s"
        SUCCEEDED+=("$proj")
    else
        echo "  ✗ failed (continuing to next project)"
        FAILED+=("$proj (run error)")
    fi
done

END_TIME=$(date -u +%s)
ELAPSED=$((END_TIME - START_TIME))

echo ""
echo "======================================================================"
echo "  SUMMARY"
echo "======================================================================"
echo "  Elapsed: ${ELAPSED}s ($(( ELAPSED / 60 ))m $(( ELAPSED % 60 ))s)"
echo "  Succeeded (${#SUCCEEDED[@]}): ${SUCCEEDED[*]}"
if [ "${#FAILED[@]}" -gt 0 ]; then
    echo "  Failed (${#FAILED[@]}): ${FAILED[*]}"
    echo ""
    exit 1
fi
echo ""
exit 0
