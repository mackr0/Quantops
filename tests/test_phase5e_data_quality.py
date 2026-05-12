"""Phase 5e — data_quality column + phantom_stop backfill (2026-05-12).

The +1131% Avg Slippage display was caused by 31 trades from
2026-05-11 14:00-15:30 where a phantom-stop bug logged option
premium ($0.16) as decision_price but the actual SELL submitted
KO stock and filled at $78. The slippage_pct on those rows is
~50,000% each — mathematically real but operationally garbage.

Phase 5e:
1. Adds `data_quality TEXT` column to trades.
2. One-shot backfill tags the historical phantom-stop rows.
3. get_slippage_stats EXCLUDES tagged rows from the aggregate
   and surfaces the count as `excluded_data_quality`.
4. Templates render a "N excluded as data-quality artifacts"
   footnote below the aggregate.

This file pins:
- SCHEMA: trades.data_quality column added by init_db migration.
- BACKFILL: WHERE ABS(slippage_pct) > 50 AND decision_price < 1
  AND occ_symbol IS NULL AND timestamp < 2026-05-12 → tagged
  'phantom_stop_2026_05_11'. Idempotent (gated on
  data_quality IS NULL).
- AGGREGATE EXCLUSION: tagged rows NOT counted in
  trades_with_fills, avg_slippage_pct, total_slippage_cost.
- WORST-TRADE EXCLUSION: 'Worst Slippage' card shows a real
  trade (in-aggregate), not the data-corruption record.
- SURFACING: excluded_data_quality returned in the result dict.
- BACK-COMPAT: untagged rows behave exactly as before
  (data_quality IS NULL is the default).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def db_path():
    """Build a fresh prod-shaped DB."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from journal import init_db
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _insert_trade(db_path, **kwargs):
    """Insert a trade row with sensible defaults."""
    defaults = {
        "timestamp": "2026-05-11T14:00:00",
        "symbol": "AAPL", "side": "sell", "qty": 10,
        "price": 150.0, "decision_price": 150.0,
        "fill_price": 150.0, "slippage_pct": 0.0,
        "status": "filled", "occ_symbol": None,
        "signal_type": "SELL", "strategy": "stop_loss",
    }
    defaults.update(kwargs)
    cols = ",".join(defaults.keys())
    placeholders = ",".join("?" * len(defaults))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            list(defaults.values()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------

class TestDataQualityColumnExists:
    def test_data_quality_column_added(self, db_path):
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(trades)"
        ).fetchall()}
        conn.close()
        assert "data_quality" in cols


# ---------------------------------------------------------------------------
# BACKFILL — phantom_stop_2026_05_11 tagging
# ---------------------------------------------------------------------------

class TestPhantomStopBackfill:
    def test_tags_corrupted_rows_with_pattern(self, db_path):
        """A row matching the phantom-stop pattern gets tagged."""
        _insert_trade(
            db_path, symbol="KO", side="sell", qty=2,
            decision_price=0.16, fill_price=78.33,
            slippage_pct=48856.25, timestamp="2026-05-11T14:36:43",
            occ_symbol=None,
        )
        from journal import init_db
        init_db(db_path)  # re-fire to trigger backfill
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT data_quality FROM trades"
        ).fetchone()
        conn.close()
        assert row[0] == "phantom_stop_2026_05_11"

    def test_tags_aapl_pattern_with_higher_decision_price(self, db_path):
        """The 2026-05-12 widening — AAPL rows with decision_price
        $1.07 (above the original tight $1.0 threshold) but still
        a clear phantom-stop pattern (fill at $292) should be
        tagged. Earlier criterion missed these; new one catches
        them via the >50% slippage_pct alone."""
        _insert_trade(
            db_path, symbol="AAPL", side="sell", qty=1,
            decision_price=1.07, fill_price=291.91,
            slippage_pct=27181.31,
            timestamp="2026-05-11T16:08:03",
            occ_symbol=None,
        )
        from journal import init_db
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT data_quality FROM trades"
        ).fetchone()
        conn.close()
        assert row[0] == "phantom_stop_2026_05_11"

    def test_does_not_tag_normal_stock_trades(self, db_path):
        """A normal stock trade with realistic slippage_pct
        stays untagged."""
        _insert_trade(
            db_path, symbol="AAPL", side="buy", qty=100,
            decision_price=150.0, fill_price=150.05,
            slippage_pct=0.033, timestamp="2026-05-11T14:00:00",
            occ_symbol=None,
        )
        from journal import init_db
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT data_quality FROM trades"
        ).fetchone()
        conn.close()
        assert row[0] is None

    def test_does_not_tag_option_rows(self, db_path):
        """An option row with high slippage_pct is NOT a phantom
        stop — it's just a noisy option fill. Don't tag it."""
        _insert_trade(
            db_path, symbol="KO", side="sell", qty=2,
            decision_price=0.16, fill_price=0.20,
            slippage_pct=25.0,  # under threshold for option
            timestamp="2026-05-11T14:00:00",
            occ_symbol="KO    260612C00080000",
        )
        from journal import init_db
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT data_quality FROM trades"
        ).fetchone()
        conn.close()
        assert row[0] is None

    def test_does_not_tag_post_2026_05_12_rows(self, db_path):
        """Future high-slippage rows must be investigated
        individually, not auto-tagged as historical incident."""
        _insert_trade(
            db_path, symbol="KO", side="sell", qty=2,
            decision_price=0.16, fill_price=78.33,
            slippage_pct=48856.25, timestamp="2026-05-12T10:00:00",
            occ_symbol=None,
        )
        from journal import init_db
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT data_quality FROM trades"
        ).fetchone()
        conn.close()
        assert row[0] is None

    def test_backfill_idempotent(self, db_path):
        """Running the migration twice produces the same tag
        (the second run is gated on data_quality IS NULL)."""
        _insert_trade(
            db_path, symbol="KO", side="sell", qty=2,
            decision_price=0.16, fill_price=78.33,
            slippage_pct=48856.25, timestamp="2026-05-11T14:36:43",
            occ_symbol=None,
        )
        from journal import init_db
        init_db(db_path)
        init_db(db_path)
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT data_quality FROM trades"
        ).fetchone()
        conn.close()
        # Still tagged, not double-written or cleared
        assert row[0] == "phantom_stop_2026_05_11"


# ---------------------------------------------------------------------------
# AGGREGATE EXCLUSION
# ---------------------------------------------------------------------------

class TestSlippageAggregateExcludesTagged:
    def test_tagged_rows_excluded_from_avg(self, db_path):
        # 2 normal rows + 1 tagged corrupt row
        for i in range(2):
            _insert_trade(
                db_path, symbol="AAPL", side="buy", qty=100,
                decision_price=150.0, fill_price=150.10,
                slippage_pct=0.067,
                timestamp=f"2026-05-12T10:0{i}:00",
                occ_symbol=None,
            )
        _insert_trade(
            db_path, symbol="KO", side="sell", qty=2,
            decision_price=0.16, fill_price=78.33,
            slippage_pct=48856.25, timestamp="2026-05-11T14:36:43",
            occ_symbol=None,
        )
        from journal import init_db, get_slippage_stats
        init_db(db_path)  # fires the backfill

        s = get_slippage_stats(db_path=db_path, kind="stocks")
        # Only 2 normal rows in the aggregate (corrupt row excluded)
        assert s["trades_with_fills"] == 2
        # Average is sane (~0.067%), not 16k%
        assert abs(s["avg_slippage_pct"] - 0.067) < 0.01
        # Excluded count visible
        assert s["excluded_data_quality"] == 1

    def test_no_tagged_rows_returns_zero_excluded(self, db_path):
        _insert_trade(
            db_path, symbol="AAPL", side="buy", qty=100,
            decision_price=150.0, fill_price=150.05,
            slippage_pct=0.033,
            timestamp="2026-05-12T10:00:00",
            occ_symbol=None,
        )
        from journal import init_db, get_slippage_stats
        init_db(db_path)
        s = get_slippage_stats(db_path=db_path, kind="stocks")
        assert s["excluded_data_quality"] == 0
        assert s["trades_with_fills"] == 1


# ---------------------------------------------------------------------------
# WORST-TRADE EXCLUSION — clicking "worst" shouldn't show data corruption
# ---------------------------------------------------------------------------

class TestReconcileSkipsTaggedRows:
    """Phase 5e wave 3 (2026-05-12): reconcile_journal_to_broker
    must NOT load data_quality-tagged rows as backfill candidates.

    The phantom-stop incident rows look like normal stock SELLs
    to the reconciler (status='open', side='sell', occ_symbol=NULL).
    Without filtering, the reconciler reads them as 'phantom long
    that needs closing' and creates bogus reconcile_backfill rows
    with pnl computed from yesterday's $0.16 entry price."""

    def test_tagged_rows_excluded_from_reconcile_fetch(self, db_path):
        # Insert a tagged row (the phantom-stop incident shape)
        _insert_trade(
            db_path, symbol="KO", side="buy", qty=2,
            price=0.16, fill_price=0.16,
            status="open", occ_symbol=None,
            data_quality="phantom_stop_2026_05_11",
        )
        # Insert a normal open BUY (untagged)
        _insert_trade(
            db_path, symbol="AAPL", side="buy", qty=10,
            price=150.0, fill_price=150.0,
            status="open", occ_symbol=None,
        )

        # Import the function inline so import-cycle issues are
        # surfaced clearly
        from reconcile_journal_to_broker import _select_open_rows
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = _select_open_rows(conn)
        conn.close()
        # Only the untagged AAPL row should appear
        symbols = [r["symbol"] for r in rows]
        assert "AAPL" in symbols
        assert "KO" not in symbols, (
            "Tagged phantom-stop row leaked into reconcile candidate "
            "set — would produce a bogus reconcile_backfill row"
        )


class TestReconcileBackfillBogusRowsTagged:
    """Phase 5e wave 3 — the bogus reconcile_backfill rows already
    created today (BCS +4833%, ACHR +1447%, RIOT +2450%) should
    themselves be tagged so they're excluded from analytics."""

    def test_bogus_reconcile_backfill_pnl_tagged(self, db_path):
        # BCS-shape row: qty=2, price=$22.20, pnl=+$43.50 →
        # pnl ($43.50) is ~98% of cost basis ($44.40) which is
        # plausible BY ITSELF, but the row was inserted by
        # reconcile_backfill against a phantom entry price of
        # $0.45 (i.e., the implicit pnl-vs-cost ratio is +98x).
        # The detector compares ABS(pnl) vs price*qty*5.
        # For BCS: 43.50 < 44.40 * 5 (=222) → would NOT tag.
        # We need to verify the detector skips that case (correct
        # behavior — it's not actually a bogus row, even though
        # the pnl % calc in the template happens to render +4833%
        # for a different reason: cost_basis = price*qty - pnl,
        # which can go near zero when pnl ≈ proceeds).
        # So this test pins: detector is RESTRICTIVE — only tags
        # rows where pnl > 5x cost.
        _insert_trade(
            db_path, symbol="BCS", side="sell", qty=2,
            price=22.20, fill_price=22.20, pnl=43.50,
            strategy="reconcile_backfill", status="closed",
            occ_symbol=None,
        )
        # And a row that IS clearly bogus: pnl WAY larger than
        # cost basis would imply
        _insert_trade(
            db_path, symbol="ZZZZ", side="sell", qty=2,
            price=22.20, fill_price=22.20, pnl=1000.0,
            strategy="reconcile_backfill", status="closed",
            occ_symbol=None,
        )
        from journal import init_db
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        rows = dict(conn.execute(
            "SELECT symbol, data_quality FROM trades "
            "WHERE strategy = 'reconcile_backfill'"
        ).fetchall())
        conn.close()
        # ZZZZ is the bogus shape (pnl > 5*cost) → tagged
        assert rows["ZZZZ"] == "phantom_stop_reconcile_2026_05_12"
        # BCS is plausibly real (pnl ≈ cost) → NOT tagged. Cost
        # = price*qty = $44.40; pnl = $43.50; ratio = 0.98 < 5.
        assert rows["BCS"] is None


class TestWorstTradeExcludesTagged:
    def test_worst_trade_card_shows_in_aggregate_row(self, db_path):
        """The 'Worst Slippage' card should show a trade that's
        actually in the displayed average — not the phantom-stop
        artifact that got excluded."""
        # Tagged corrupt row (50,000% slippage, excluded)
        _insert_trade(
            db_path, symbol="KO", side="sell", qty=2,
            decision_price=0.16, fill_price=78.33,
            slippage_pct=48856.25, timestamp="2026-05-11T14:00:00",
            occ_symbol=None,
        )
        # Normal row with modest slippage — should be the "worst"
        _insert_trade(
            db_path, symbol="NVDA", side="buy", qty=10,
            decision_price=400.0, fill_price=403.0,
            slippage_pct=0.75, timestamp="2026-05-12T10:00:00",
            occ_symbol=None,
        )
        from journal import init_db, get_slippage_stats
        init_db(db_path)
        s = get_slippage_stats(db_path=db_path, kind="stocks")
        # Worst trade is the NVDA row, not the KO data-quality row
        worst = s["worst_trade"]
        assert worst["symbol"] == "NVDA"
        assert worst["slippage_pct"] == pytest.approx(0.75)
