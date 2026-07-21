"""2026-07-21 — universe price-band pins (max_price 20 → 1000).

The $20 ceiling made Energy/Staples/Real-Estate/mega-caps structurally
unsourceable while the AI's beta guidance urged exactly those sectors:
narrated rotations could never execute and cycles starved. The band
now admits the liquid market ($10–$1,000); the sector stratifier's
5-slots-per-sector guarantee becomes deliverable.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_config_ceiling_is_1000():
    import config
    assert config.SCREEN_MAX_PRICE == 1000.0
    assert config.SCREEN_MIN_PRICE == 10.0  # institutional floor stays


def test_user_context_default_ceiling_is_1000():
    from user_context import UserContext
    ctx = UserContext(user_id=1, segment="stocks")
    assert ctx.max_price == 1000.0
    assert ctx.min_price == 10.0


def test_schema_default_ceiling_is_1000():
    src = open(os.path.join(REPO, "models.py")).read()
    assert "max_price REAL NOT NULL DEFAULT 20.0" not in src, (
        "a schema default of 20 would re-strangle any newly created "
        "profile's universe")
    assert "max_price REAL NOT NULL DEFAULT 1000.0" in src


def test_migration_script_exists_and_targets_stock_profiles():
    path = os.path.join(
        REPO, "scripts", "migrate_universe_band_2026_07_21.py")
    src = open(path).read()
    assert "NEW_MAX = 1000.0" in src
    assert "crypto" in src  # crypto profiles excluded
    assert "custom" in src  # operator-customized bands untouched
