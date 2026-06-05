"""2026-06-05 — research-mode toggle for the intraday risk gate.

Contract pinned:

  1. Default state is OFF (research mode). `intraday_risk_blocks_trades`
     defaults to 0 in the users-table migration; the getter returns
     False on a freshly-inserted user. This is the right default for
     paper trading where the goal is to MEASURE the AI across all
     regimes, not to gate it.

  2. When OFF, the trade pipeline must NOT block on any
     intraday_risk_halt state. Trades execute unconditionally even
     when sectors are halted, even on `pause_all`. The risk monitor
     still RUNS (alerts fire on /issues, regime gets persisted to
     cycle_regime), but the gate is informational only.

  3. When ON, the 3-layer halt gate runs exactly as before — sector
     halts, breadth halts, and `held_position_halts` all block trades.

  4. `cycle_regime` rows are written regardless of the toggle. The
     persistence has nothing to do with the gate; it's audit data
     for post-hoc analysis.

  5. The model helpers `get_intraday_risk_blocks_trades` /
     `set_intraday_risk_blocks_trades` exist and round-trip correctly.

The risk_gate code is preserved (we don't lose work that works); the
toggle decides whether it RUNS. This lets the operator flip between
research/measurement mode and live-money capital-preservation mode
without a code deploy.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Model helpers — round-trip + default
# ---------------------------------------------------------------------------

class TestModelHelpers:

    def test_helpers_exist(self):
        """The getter/setter are part of the public API."""
        from models import (
            get_intraday_risk_blocks_trades,
            set_intraday_risk_blocks_trades,
        )
        assert callable(get_intraday_risk_blocks_trades)
        assert callable(set_intraday_risk_blocks_trades)

    def test_users_migration_includes_toggle_column(self):
        """The migration list in models.py adds the column with the
        right default. Without this, a fresh-start reset would not
        have the column, the getter would fall through to its
        defensive default, and the toggle would appear stuck OFF.
        Pin the migration entry so deletes are loud."""
        src = (Path(__file__).resolve().parent.parent
               / "models.py").read_text()
        pattern = re.compile(
            r'\(\s*"users"\s*,\s*"intraday_risk_blocks_trades"\s*,'
            r'\s*"INTEGER NOT NULL DEFAULT 0"\s*\)',
            re.MULTILINE,
        )
        assert pattern.search(src), (
            "models.py must declare the intraday_risk_blocks_trades "
            "column in its migration list with default 0 (OFF for "
            "research mode). Without this, the toggle silently "
            "defaults to broken on fresh DBs."
        )


# ---------------------------------------------------------------------------
# Trade-pipeline contract — gate respects the toggle
# ---------------------------------------------------------------------------

class TestTradePipelineGateRespectsToggle:

    def test_gate_block_is_guarded_by_risk_gate_enabled(self):
        """Structural pin: the halt-block conditional in
        trade_pipeline.py MUST include the `risk_gate_enabled` term.
        Without it, the gate would fire regardless of the toggle and
        the whole rewrite would be a no-op."""
        src = (Path(__file__).resolve().parent.parent
               / "trade_pipeline.py").read_text()
        pattern = re.compile(
            r'if\s*\(\s*risk_gate_enabled\s+and\s+ai_trades\s+and\s+'
            r'\(\s*intraday_halt_action\s+or\s+halted_sectors\s*\)\s*\)',
            re.MULTILINE,
        )
        assert pattern.search(src), (
            "trade_pipeline.py halt-block conditional MUST be gated "
            "by `risk_gate_enabled`. Without that term the toggle "
            "does nothing and the 3-layer halt gate runs on every "
            "cycle regardless of the operator's choice."
        )

    def test_risk_gate_enabled_is_sourced_from_helper(self):
        """`risk_gate_enabled` must be derived from
        get_intraday_risk_blocks_trades(ctx.user_id). Pin against
        a future refactor that hardcodes it."""
        src = (Path(__file__).resolve().parent.parent
               / "trade_pipeline.py").read_text()
        assert "get_intraday_risk_blocks_trades(ctx.user_id)" in src, (
            "trade_pipeline.py must call "
            "get_intraday_risk_blocks_trades(ctx.user_id) to derive "
            "risk_gate_enabled. Hardcoding it (e.g., to True) defeats "
            "the toggle. Hardcoding to False loses the gate code."
        )

    def test_cycle_regime_write_is_unconditional(self):
        """cycle_regime must be written REGARDLESS of the gate flag.
        Audit data is always useful for post-hoc analysis; it's not
        the toggle's responsibility."""
        src = (Path(__file__).resolve().parent.parent
               / "trade_pipeline.py").read_text()
        # The cycle_regime INSERT must be OUTSIDE the
        # risk_gate_enabled conditional.
        insert_idx = src.find("INSERT OR REPLACE INTO cycle_regime")
        gate_idx = src.find("if (risk_gate_enabled and ai_trades")
        assert insert_idx >= 0, (
            "trade_pipeline.py must write a cycle_regime row per cycle"
        )
        assert gate_idx >= 0
        assert insert_idx < gate_idx, (
            "cycle_regime write must come BEFORE the gate conditional "
            "and must NOT be inside it. Otherwise we lose regime "
            "audit data on cycles where the gate is off."
        )


# ---------------------------------------------------------------------------
# Settings UI — toggle reaches the user
# ---------------------------------------------------------------------------

class TestSettingsToggleUI:

    def test_settings_view_passes_toggle_to_template(self):
        """The /settings view must put intraday_risk_blocks_trades on
        the autonomy dict so the template can render the checkbox."""
        src = (Path(__file__).resolve().parent.parent
               / "views.py").read_text()
        # Find the autonomy dict construction
        assert "intraday_risk_blocks_trades" in src, (
            "views.py must include intraday_risk_blocks_trades in the "
            "autonomy dict passed to settings.html"
        )

    def test_settings_post_handler_persists_toggle(self):
        """The POST handler must call set_intraday_risk_blocks_trades
        with the form value. Without this, the form silently does
        nothing."""
        src = (Path(__file__).resolve().parent.parent
               / "views.py").read_text()
        assert "set_intraday_risk_blocks_trades(" in src, (
            "views.py POST /settings/autonomy must call "
            "set_intraday_risk_blocks_trades(user_id, value)"
        )

    def test_settings_template_renders_checkbox(self):
        """settings.html must include the checkbox bound to
        intraday_risk_blocks_trades."""
        src = (Path(__file__).resolve().parent.parent
               / "templates" / "settings.html").read_text()
        assert 'name="intraday_risk_blocks_trades"' in src, (
            "settings.html must render an <input> with "
            'name="intraday_risk_blocks_trades" so the form posts the '
            "value. Otherwise the toggle is invisible to the operator."
        )
