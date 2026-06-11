"""Tests for create_experiment_profiles.py (#172, 2026-05-17).

Pins the 13-profile manifest matches docs/15 v2:
  - exactly $3M total capital
  - exactly 4 / 5 / 4 profiles per account
  - every ablation arm matches the Anchor's capital + flags except
    the one named flag (so ablation deltas are clean)
  - $25K Candidate + Replica have identical config (replica must
    be a true replica, not a typo'd near-replica)
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def manifest():
    from create_experiment_profiles import PROFILES
    return PROFILES


class TestManifestStructure:
    def test_total_capital_is_exactly_3M(self, manifest):
        total = sum(p["initial_capital"] for p in manifest)
        assert total == 3_000_000.0

    def test_thirteen_profiles_total(self, manifest):
        assert len(manifest) == 13

    def test_account_split_is_4_5_4(self, manifest):
        a1 = [p for p in manifest if p["name"].startswith("EXP-A1")]
        a2 = [p for p in manifest if p["name"].startswith("EXP-A2")]
        a3 = [p for p in manifest if p["name"].startswith("EXP-A3")]
        assert len(a1) == 4
        assert len(a2) == 5
        assert len(a3) == 4

    def test_account_1_capital_is_1M(self, manifest):
        a1_total = sum(p["initial_capital"] for p in manifest
                       if p["name"].startswith("EXP-A1"))
        assert a1_total == 1_000_000.0

    def test_account_2_capital_is_1M(self, manifest):
        """v2.1: ablations sized to fit Alpaca's $1M paper-account
        funding cap. 5 × $200K = $1M."""
        a2_total = sum(p["initial_capital"] for p in manifest
                       if p["name"].startswith("EXP-A2"))
        assert a2_total == 1_000_000.0

    def test_account_3_capital_is_1M(self, manifest):
        """v2.1: Aggressive Free profile grew from $450K → $700K to
        fill Account 3 to the $1M Alpaca cap.
        $25K + $25K + $250K + $700K = $1M."""
        a3_total = sum(p["initial_capital"] for p in manifest
                       if p["name"].startswith("EXP-A3"))
        assert a3_total == 1_000_000.0

    def test_no_account_exceeds_alpaca_funding_cap(self, manifest):
        """Hard invariant: no account total may exceed Alpaca's
        $1M per-paper-account cap (the constraint that drove v2.1)."""
        ALPACA_PAPER_CAP = 1_000_000.0
        for prefix in ("EXP-A1", "EXP-A2", "EXP-A3"):
            total = sum(p["initial_capital"] for p in manifest
                        if p["name"].startswith(prefix))
            assert total <= ALPACA_PAPER_CAP, (
                f"{prefix} totals ${total:,.0f} which exceeds "
                f"Alpaca's ${ALPACA_PAPER_CAP:,.0f} paper-account cap"
            )


class TestStrategyTypeAssignments:
    def test_one_buy_hold_one_anchor_two_random(self, manifest):
        a1 = [p for p in manifest if p["name"].startswith("EXP-A1")]
        types = sorted(p["strategy_type"] for p in a1)
        assert types == ["ai", "buy_hold", "random", "random"]

    def test_all_ablations_are_ai_type(self, manifest):
        a2 = [p for p in manifest if p["name"].startswith("EXP-A2")]
        assert all(p["strategy_type"] == "ai" for p in a2)

    def test_all_product_profiles_are_ai_type(self, manifest):
        a3 = [p for p in manifest if p["name"].startswith("EXP-A3")]
        assert all(p["strategy_type"] == "ai" for p in a3)


class TestAblationCleanDelta:
    """Each Account 2 ablation must match the Account 1 Anchor on every
    risk knob (position sizing, confidence threshold, the other enable
    flags), differing ONLY in the named flag plus the intentional
    capital reduction documented in docs/15 v2.1.

    Capital intentionally differs (Anchor $250K vs Ablation $200K) to
    fit Alpaca's $1M paper-account cap. Comparison metrics — % return
    and Sharpe — are capital-invariant for large-caps with
    percentage-based position sizing, so this is interpretively safe.
    The hard rule is: every PERCENTAGE-based knob matches Anchor, so
    the strategy behavior is identical, only the dollar magnitudes
    differ.
    """

    EXPECTED_ABLATION_CAPITAL = 200_000.0
    EXPECTED_ANCHOR_CAPITAL = 250_000.0

    def _anchor(self, manifest):
        return next(p for p in manifest
                    if p["name"] == "EXP-A1-FullSystemStandard")

    def _ablation_by_name(self, manifest, name):
        return next(p for p in manifest if p["name"] == name)

    def _assert_matches_anchor_except_named_flags(
        self, manifest, ablation_name, *off_flags,
    ):
        """Every ablation: capital is the intentional $200K, every
        percentage-based knob matches Anchor, only the named flags
        differ from Anchor."""
        anchor = self._anchor(manifest)
        ab = self._ablation_by_name(manifest, ablation_name)
        # Capital: intentional v2.1 difference (Alpaca cap)
        assert ab["initial_capital"] == self.EXPECTED_ABLATION_CAPITAL
        assert anchor["initial_capital"] == self.EXPECTED_ANCHOR_CAPITAL
        # Behavior knobs (percentage-based, capital-invariant) MUST match
        assert ab["max_position_pct"] == anchor["max_position_pct"]
        assert ab["max_total_positions"] == anchor["max_total_positions"]
        assert ab["ai_confidence_threshold"] == anchor["ai_confidence_threshold"]
        assert ab["enable_short_selling"] == anchor["enable_short_selling"]
        # Every enable_* flag matches Anchor EXCEPT the named off_flags
        for flag in ("enable_alt_data", "enable_meta_model",
                     "enable_self_tuning", "enable_options"):
            if flag in off_flags:
                assert ab[flag] == 0, (
                    f"{ablation_name}: {flag} should be 0 (off)")
                assert anchor[flag] == 1, (
                    f"Anchor must have {flag}=1 for the ablation "
                    "to mean anything"
                )
            else:
                assert ab[flag] == anchor[flag], (
                    f"{ablation_name}: {flag} must match Anchor "
                    "({anchor[flag]}), got {ab[flag]}"
                )

    def test_no_alt_data_only_differs_in_alt_data_flag(self, manifest):
        self._assert_matches_anchor_except_named_flags(
            manifest, "EXP-A2-NoAltData", "enable_alt_data",
        )

    def test_no_meta_model_only_differs_in_meta_flag(self, manifest):
        self._assert_matches_anchor_except_named_flags(
            manifest, "EXP-A2-NoMetaModel", "enable_meta_model",
        )

    def test_no_self_tuning_only_differs_in_self_tuning_flag(self, manifest):
        self._assert_matches_anchor_except_named_flags(
            manifest, "EXP-A2-NoSelfTuning", "enable_self_tuning",
        )

    def test_no_options_only_differs_in_options_flag(self, manifest):
        self._assert_matches_anchor_except_named_flags(
            manifest, "EXP-A2-NoOptions", "enable_options",
        )

    def test_combined_ablation_disables_both_named_flags(self, manifest):
        self._assert_matches_anchor_except_named_flags(
            manifest, "EXP-A2-NoAltData-NoMetaModel",
            "enable_alt_data", "enable_meta_model",
        )


class TestReplicaIsTrueReplica:
    """$25K Candidate and Replica must have IDENTICAL config —
    every per-profile knob equal. RNG divergence comes from the
    different profile_id assigned at create time."""

    def test_candidate_and_replica_have_identical_config(self, manifest):
        cand = next(p for p in manifest
                    if p["name"] == "EXP-A3-25K-Candidate")
        rep = next(p for p in manifest
                   if p["name"] == "EXP-A3-25K-Replica")
        # Every key except name must match
        cand_no_name = {k: v for k, v in cand.items() if k != "name"}
        rep_no_name = {k: v for k, v in rep.items() if k != "name"}
        assert cand_no_name == rep_no_name


class TestAggressiveProfileLiftsConstraints:
    def test_aggressive_free_drops_small_account_constraints(self, manifest):
        cand = next(p for p in manifest
                    if p["name"] == "EXP-A3-25K-Candidate")
        agg = next(p for p in manifest
                   if p["name"] == "EXP-A3-700K-AggressiveFree")
        # Aggressive lifts: at-least-as-many positions, shorts on,
        # smaller per-position. 2026-06-11 — strict > relaxed to >=:
        # ALL AI-driven profiles now carry max_total_positions=999
        # (operator contract: the AI decides position count; see
        # test_manifest_position_caps_2026_06_11), so position count
        # no longer differentiates Aggressive from Candidate. The
        # remaining differentiators (shorts, per-position size) are
        # still pinned strictly below.
        assert agg["max_total_positions"] >= cand["max_total_positions"]
        assert agg["enable_short_selling"] == 1
        assert cand["enable_short_selling"] == 0
        # Same AI signal stack (all flags ON on both)
        assert agg["enable_alt_data"] == cand["enable_alt_data"]
        assert agg["enable_meta_model"] == cand["enable_meta_model"]
        assert agg["enable_options"] == cand["enable_options"]


class TestApplyFlow:
    def test_dry_run_creates_nothing(self, monkeypatch):
        """Dry-run must not call create_trading_profile or
        update_trading_profile at all when no profiles exist yet."""
        import create_experiment_profiles
        with patch(
            "create_experiment_profiles._existing_profile_by_name",
            return_value=None,
        ), patch(
            "models.create_trading_profile",
        ) as fake_create, patch(
            "models.update_trading_profile",
        ) as fake_update, patch.object(
            sys, "argv", ["create_experiment_profiles.py"],
        ):  # no --apply
            rc = create_experiment_profiles.main()
        assert rc == 0
        fake_create.assert_not_called()
        fake_update.assert_not_called()

    def test_apply_creates_thirteen_profiles_when_none_exist(self, monkeypatch):
        import create_experiment_profiles
        with patch(
            "create_experiment_profiles._existing_profile_by_name",
            return_value=None,
        ), patch(
            "models.create_trading_profile", return_value=42,
        ) as fake_create, patch(
            "models.update_trading_profile",
        ) as fake_update, patch.object(
            sys, "argv",
            ["create_experiment_profiles.py", "--apply"],
        ):
            rc = create_experiment_profiles.main()
        assert rc == 0
        # 13 profiles created → 13 create calls + 13 update calls
        # (create makes a row with defaults, update fills it in)
        assert fake_create.call_count == 13
        assert fake_update.call_count == 13

    def test_apply_updates_existing_profiles_in_place(self, monkeypatch):
        """Idempotency: if profiles already exist by name, the
        script UPDATES them rather than creating duplicates."""
        import create_experiment_profiles
        with patch(
            "create_experiment_profiles._existing_profile_by_name",
            return_value={"id": 99, "name": "EXP-A1-FullSystemStandard"},
        ), patch(
            "models.create_trading_profile",
        ) as fake_create, patch(
            "models.update_trading_profile",
        ) as fake_update, patch.object(
            sys, "argv",
            ["create_experiment_profiles.py", "--apply"],
        ):
            rc = create_experiment_profiles.main()
        assert rc == 0
        # All 13 "exist" via the patch → zero creates, 13 updates
        fake_create.assert_not_called()
        assert fake_update.call_count == 13
