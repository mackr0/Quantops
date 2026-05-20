"""Standalone cron entry point for pre-market alt-data warmup.

Runs at 04:00 ET (08:00 UTC during EDT) via cron. See
docs/21_ALTDATA_PREMARKET_WARMUP.md for full design.

Cron entry to install on prod:
    0 8 * * 1-5 cd /opt/quantopsai && /opt/quantopsai/venv/bin/python3 \\
        premarket_warmup.py >> logs/warmup.log 2>&1
"""
import sys

if __name__ == "__main__":
    from altdata_warmup import main
    sys.exit(main())
