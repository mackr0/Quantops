"""Tests for Phase 6 — multi-strategy registry, aggregation, and capital allocation.

Covers:
  - Registry discovery filters by market type and deprecation status.
  - aggregate_candidates() merges signals from multiple strategies with
    correct vote/score/signal semantics.
  - compute_capital_allocations() implements the equal-weight default,
    inverse-variance (Sharpe) weighting, and the 40% per-strategy cap.
"""

from __future__ import annotations

import sqlite3
import pytest


# ---------------------------------------------------------------------------
# Registry discovery
# ---------------------------------------------------------------------------

class TestRegistryDiscovery:
    def test_market_engine_applies_to_every_market(self):
        from strategies import discover_strategies
        for market in ("micro", "small", "midcap", "largecap", "crypto"):
            mods = discover_strategies(market)
            names = [m.NAME for m in mods]
            assert "market_engine" in names, (
                f"market_engine missing from {market}: {names}"
            )

    def test_insider_cluster_excluded_from_crypto(self):
        from strategies import discover_strategies
        crypto_names = [m.NAME for m in discover_strategies("crypto")]
        assert "insider_cluster" not in crypto_names

    def test_insider_cluster_included_in_equity_markets(self):
        from strategies import discover_strategies
        for market in ("micro", "small", "midcap", "largecap"):
            names = [m.NAME for m in discover_strategies(market)]
            assert "insider_cluster" in names, f"missing in {market}"

    def test_every_strategy_exposes_required_interface(self):
        from strategies import discover_strategies
        for mod in discover_strategies("small"):
            assert hasattr(mod, "NAME") and isinstance(mod.NAME, str)
            assert hasattr(mod, "APPLICABLE_MARKETS") and isinstance(
                mod.APPLICABLE_MARKETS, list
            )
            assert callable(getattr(mod, "find_candidates", None))

    def test_get_active_strategies_no_db_returns_all(self):
        from strategies import get_active_strategies, discover_strategies
        all_mods = discover_strategies("small")
        active = get_active_strategies("small", db_path=None)
        assert len(active) == len(all_mods)

    def test_get_active_strategies_filters_deprecated(self, tmp_profile_db):
        from strategies import get_active_strategies
        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO deprecated_strategies "
            "(strategy_type, deprecated_at, reason) "
            "VALUES ('insider_cluster', datetime('now'), 'test decay')"
        )
        conn.commit()
        conn.close()

        active_names = [m.NAME for m in get_active_strategies("small", db_path=tmp_profile_db)]
        assert "insider_cluster" not in active_names
        assert "market_engine" in active_names

    def test_restored_strategy_is_active_again(self, tmp_profile_db):
        from strategies import get_active_strategies
        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO deprecated_strategies "
            "(strategy_type, deprecated_at, reason, restored_at) "
            "VALUES ('insider_cluster', datetime('now', '-30 days'), 'test', datetime('now'))"
        )
        conn.commit()
        conn.close()

        active_names = [m.NAME for m in get_active_strategies("small", db_path=tmp_profile_db)]
        assert "insider_cluster" in active_names


# ---------------------------------------------------------------------------
# Candidate aggregation
# ---------------------------------------------------------------------------

class TestAggregateCandidates:
    def test_returns_expected_shape(self, sample_ctx, monkeypatch):
        # Force every strategy to return nothing — we only care about
        # shape of the empty-result dict here.
        from strategies import discover_strategies
        for mod in discover_strategies("small"):
            monkeypatch.setattr(mod, "find_candidates", lambda ctx, uni: [])

        from multi_strategy import aggregate_candidates
        out = aggregate_candidates(sample_ctx, ["AAPL", "MSFT"])
        assert set(out.keys()) == {"candidates", "per_strategy_counts", "active_strategies"}
        assert out["candidates"] == []
        assert isinstance(out["active_strategies"], list)
        assert "market_engine" in out["active_strategies"]

    def test_single_strategy_vote(self, sample_ctx, monkeypatch):
        from strategies import discover_strategies
        for mod in discover_strategies("small"):
            if mod.NAME == "market_engine":
                monkeypatch.setattr(mod, "find_candidates", lambda ctx, uni: [
                    {"symbol": "AAPL", "signal": "BUY", "score": 1,
                     "votes": {"market_engine": "BUY"}, "reason": "r"}
                ])
            else:
                monkeypatch.setattr(mod, "find_candidates", lambda ctx, uni: [])

        from multi_strategy import aggregate_candidates
        out = aggregate_candidates(sample_ctx, ["AAPL"])
        assert len(out["candidates"]) == 1
        entry = out["candidates"][0]
        assert entry["symbol"] == "AAPL"
        assert entry["signal"] == "BUY"
        assert entry["source_strategies"] == ["market_engine"]
        assert out["per_strategy_counts"]["market_engine"] == 1

    def test_two_strategies_agree_upgrades_to_strong_buy(self, sample_ctx, monkeypatch):
        from strategies import discover_strategies
        mods = discover_strategies("small")
        calls = {
            "market_engine": [{"symbol": "AAPL", "signal": "BUY", "score": 1,
                               "votes": {"market_engine": "BUY"}, "reason": "r1"}],
            "insider_cluster": [{"symbol": "AAPL", "signal": "BUY", "score": 2,
                                 "votes": {"insider_cluster": "BUY"}, "reason": "r2"}],
        }
        for mod in mods:
            results = calls.get(mod.NAME, [])
            monkeypatch.setattr(mod, "find_candidates", lambda ctx, uni, r=results: r)

        from multi_strategy import aggregate_candidates
        out = aggregate_candidates(sample_ctx, ["AAPL"])
        entry = [c for c in out["candidates"] if c["symbol"] == "AAPL"][0]
        assert entry["signal"] == "STRONG_BUY"
        assert set(entry["source_strategies"]) == {"market_engine", "insider_cluster"}
        assert entry["votes"]["market_engine"] == "BUY"
        assert entry["votes"]["insider_cluster"] == "BUY"

    def test_conflicting_signals_do_not_flip_dominant(self, sample_ctx, monkeypatch):
        from strategies import discover_strategies
        # SELL votes only survive aggregation when shorting is enabled
        sample_ctx.enable_short_selling = True
        mods = discover_strategies("small")
        calls = {
            "market_engine": [{"symbol": "AAPL", "signal": "BUY", "score": 1,
                               "votes": {"market_engine": "BUY"}, "reason": "r1"}],
            "gap_reversal": [{"symbol": "AAPL", "signal": "SELL", "score": 1,
                              "votes": {"gap_reversal": "SELL"}, "reason": "r2"}],
        }
        for mod in mods:
            results = calls.get(mod.NAME, [])
            monkeypatch.setattr(mod, "find_candidates", lambda ctx, uni, r=results: r)

        from multi_strategy import aggregate_candidates
        out = aggregate_candidates(sample_ctx, ["AAPL"])
        entry = [c for c in out["candidates"] if c["symbol"] == "AAPL"][0]
        # First strategy wins the dominant signal; conflict recorded in votes
        assert entry["votes"]["gap_reversal"] == "SELL"
        assert entry["votes"]["market_engine"] == "BUY"
        assert entry["signal"] in {"BUY", "HOLD"}  # score stays at 1 → BUY

    def test_failing_strategy_does_not_abort_pipeline(self, sample_ctx, monkeypatch):
        from strategies import discover_strategies
        def boom(ctx, uni):
            raise RuntimeError("strategy exploded")

        for mod in discover_strategies("small"):
            if mod.NAME == "insider_cluster":
                monkeypatch.setattr(mod, "find_candidates", boom)
            elif mod.NAME == "market_engine":
                monkeypatch.setattr(mod, "find_candidates", lambda ctx, uni: [
                    {"symbol": "AAPL", "signal": "BUY", "score": 1,
                     "votes": {"market_engine": "BUY"}, "reason": "r"}
                ])
            else:
                monkeypatch.setattr(mod, "find_candidates", lambda ctx, uni: [])

        from multi_strategy import aggregate_candidates
        out = aggregate_candidates(sample_ctx, ["AAPL"])
        assert len(out["candidates"]) == 1
        # The failing strategy is not recorded in per_strategy_counts
        assert "insider_cluster" not in out["per_strategy_counts"]
        assert out["per_strategy_counts"]["market_engine"] == 1

    def test_skips_candidate_without_symbol(self, sample_ctx, monkeypatch):
        from strategies import discover_strategies
        for mod in discover_strategies("small"):
            if mod.NAME == "market_engine":
                monkeypatch.setattr(mod, "find_candidates", lambda ctx, uni: [
                    {"symbol": "", "signal": "BUY", "score": 1, "votes": {}, "reason": "r"},
                    {"symbol": "AAPL", "signal": "BUY", "score": 1,
                     "votes": {"market_engine": "BUY"}, "reason": "r"},
                ])
            else:
                monkeypatch.setattr(mod, "find_candidates", lambda ctx, uni: [])

        from multi_strategy import aggregate_candidates
        out = aggregate_candidates(sample_ctx, ["AAPL"])
        symbols = [c["symbol"] for c in out["candidates"]]
        assert symbols == ["AAPL"]


# ---------------------------------------------------------------------------
# Capital allocation math
# ---------------------------------------------------------------------------

class TestCapitalAllocations:
    def test_empty_names_returns_empty(self, tmp_profile_db):
        from multi_strategy import compute_capital_allocations
        assert compute_capital_allocations([], tmp_profile_db) == {}

    def test_no_track_record_gets_default_weight(self, tmp_profile_db):
        # Zero resolved predictions → every strategy falls back to DEFAULT_WEIGHT.
        # After normalization, weights should be equal and sum to 1.
        from multi_strategy import compute_capital_allocations
        names = ["market_engine", "insider_cluster", "earnings_drift"]
        weights = compute_capital_allocations(names, tmp_profile_db)
        assert set(weights.keys()) == set(names)
        assert pytest.approx(sum(weights.values()), abs=1e-6) == 1.0
        expected = 1.0 / len(names)
        for w in weights.values():
            assert pytest.approx(w, abs=1e-6) == expected

    def test_weights_always_sum_to_one(self, tmp_profile_db):
        from multi_strategy import compute_capital_allocations
        for names in (
            ["market_engine"],
            ["market_engine", "insider_cluster"],
            ["market_engine", "insider_cluster", "earnings_drift",
             "vol_regime", "max_pain_pinning", "gap_reversal"],
        ):
            w = compute_capital_allocations(names, tmp_profile_db)
            assert pytest.approx(sum(w.values()), abs=1e-6) == 1.0

    def test_no_strategy_exceeds_forty_percent_cap(self, tmp_profile_db, monkeypatch):
        # Simulate one strategy with a massive Sharpe (well above 4.0 cap)
        # and others with zero track record. Without a cap, the hot strategy
        # would get 100%. With the cap, it must be <= 40%.
        import multi_strategy

        def fake_rolling(db, name, window_days=30):
            if name == "market_engine":
                return {"sharpe_ratio": 10.0, "n_predictions": 50, "win_rate": 0.8}
            return {"sharpe_ratio": 0, "n_predictions": 0, "win_rate": 0}

        def fake_lifetime(db, name):
            if name == "market_engine":
                return {"sharpe_ratio": 8.0, "n_predictions": 200}
            return {"sharpe_ratio": 0, "n_predictions": 0}

        monkeypatch.setattr(multi_strategy, "compute_rolling_metrics",
                            fake_rolling, raising=False)
        monkeypatch.setattr(multi_strategy, "compute_lifetime_metrics",
                            fake_lifetime, raising=False)
        # Patch the lazy imports inside compute_capital_allocations
        import alpha_decay
        monkeypatch.setattr(alpha_decay, "compute_rolling_metrics", fake_rolling)
        monkeypatch.setattr(alpha_decay, "compute_lifetime_metrics", fake_lifetime)

        names = ["market_engine", "insider_cluster", "earnings_drift"]
        weights = multi_strategy.compute_capital_allocations(names, tmp_profile_db)
        assert weights["market_engine"] <= 0.40 + 1e-6
        assert pytest.approx(sum(weights.values()), abs=1e-6) == 1.0

    def test_negative_sharpe_gets_minimum_weight(self, tmp_profile_db, monkeypatch):
        import multi_strategy
        import alpha_decay

        def fake_rolling(db, name, window_days=30):
            # All strategies losing — but one is less losing
            return {"sharpe_ratio": -0.5, "n_predictions": 30, "win_rate": 0.3}

        def fake_lifetime(db, name):
            return {"sharpe_ratio": -0.2, "n_predictions": 100}

        monkeypatch.setattr(alpha_decay, "compute_rolling_metrics", fake_rolling)
        monkeypatch.setattr(alpha_decay, "compute_lifetime_metrics", fake_lifetime)

        names = ["market_engine", "insider_cluster"]
        weights = multi_strategy.compute_capital_allocations(names, tmp_profile_db)
        # All equal weight since all are equally bad
        assert pytest.approx(sum(weights.values()), abs=1e-6) == 1.0
        for w in weights.values():
            assert 0 < w <= 1.0


# ---------------------------------------------------------------------------
# Allocation summary (dashboard feeder)
# ---------------------------------------------------------------------------

class TestAllocationSummary:
    def test_returns_row_per_active_strategy(self, tmp_profile_db):
        from multi_strategy import get_allocation_summary
        from strategies import get_active_strategies

        summary = get_allocation_summary(tmp_profile_db, "small")
        active_names = {m.NAME for m in get_active_strategies("small", db_path=tmp_profile_db)}
        summary_names = {row["name"] for row in summary}
        assert summary_names == active_names

    def test_summary_rows_have_required_keys(self, tmp_profile_db):
        from multi_strategy import get_allocation_summary
        summary = get_allocation_summary(tmp_profile_db, "small")
        required = {"name", "weight", "rolling_sharpe", "lifetime_sharpe",
                    "rolling_n", "lifetime_n", "rolling_win_rate"}
        for row in summary:
            assert required.issubset(row.keys())

    def test_summary_weights_sum_to_one(self, tmp_profile_db):
        from multi_strategy import get_allocation_summary
        summary = get_allocation_summary(tmp_profile_db, "small")
        if summary:
            assert pytest.approx(sum(r["weight"] for r in summary), abs=1e-3) == 1.0
