"""Experiment 2 manifest (docs/25): nine arm-profiles, three models ×
three replicates, identical except the model — and the reset tooling
that builds them.

Replaces test_create_experiment_profiles_2026_05_17.py (Experiment 1's
13-profile ablation design, retired at tag exp1-system-stability-final).
"""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def manifest():
    from create_experiment_profiles import PROFILES
    return PROFILES


class TestManifestStructure:
    def test_twelve_profiles_three_per_arm(self, manifest):
        from create_experiment_profiles import ARMS, REPLICATES_PER_ARM
        assert len(manifest) == len(ARMS) * REPLICATES_PER_ARM == 12

    def test_equal_capital_and_full_account_allocation(self, manifest):
        caps = {p["initial_capital"] for p in manifest}
        assert caps == {250_000.0}
        # Four per $1M paper account → the account is FULLY allocated:
        # Σ profile cash == broker cash, or the cash-parity audit flags
        # the remainder as orphan cash every cycle.
        by_group = {}
        for p in manifest:
            g = p["name"].split("-")[1]
            by_group[g] = by_group.get(g, 0) + p["initial_capital"]
        assert by_group == {"A1": 1_000_000.0, "A2": 1_000_000.0,
                            "A3": 1_000_000.0}

    def test_each_account_hosts_one_replicate_of_every_arm(self, manifest):
        """A broker-account event must hit every arm equally."""
        per_group = {}
        for p in manifest:
            g = p["name"].split("-")[1]
            per_group.setdefault(g, set()).add((p["ai_provider"], p["ai_model"]))
        arms = {(p["ai_provider"], p["ai_model"]) for p in manifest}
        assert all(v == arms for v in per_group.values()), per_group

    def test_arms_differ_only_in_model_fields(self, manifest):
        varying = {"name", "ai_provider", "ai_model", "shadow_models"}
        base = {k: v for k, v in manifest[0].items() if k not in varying}
        for p in manifest[1:]:
            assert {k: v for k, v in p.items() if k not in varying} == base, \
                p["name"]

    def test_replicates_within_an_arm_are_identical_except_name(self, manifest):
        by_arm = {}
        for p in manifest:
            by_arm.setdefault((p["ai_provider"], p["ai_model"]), []).append(p)
        assert len(by_arm) == 4
        for stem, reps in by_arm.items():
            assert len(reps) == 3, stem
            ref = {k: v for k, v in reps[0].items() if k != "name"}
            for r in reps[1:]:
                assert {k: v for k, v in r.items() if k != "name"} == ref

    def test_arm_models_are_the_d9_cost_ladder(self, manifest):
        """Experiment 3 (docs/25 D9, 2026-11-02): four price points across
        three vendors, < $100/month fleet-wide (D10)."""
        arms = sorted({(p["ai_provider"], p["ai_model"]) for p in manifest})
        assert arms == [("anthropic", "claude-haiku-5-5"),
                        ("deepseek", "deepseek-flash"),
                        ("openai", "gpt-5.6-luna"),
                        ("openai", "gpt-6.1-terra")]

    def test_the_incumbent_is_an_arm(self, manifest):
        """gpt-5.6-luna is the only model with a continuous record since
        2026-08-24 (Experiment 2's other three arms died to a retirement
        and a credit outage); it stays a full arm so Experiment 3's
        before/after is readable on the same model."""
        luna = [p for p in manifest if p["ai_model"] == "gpt-5.6-luna"]
        assert len(luna) == 3 and all(p["strategy_type"] == "ai" for p in luna)

    def test_no_arm_runs_a_retired_or_deprecated_model(self, manifest):
        from ai_providers import model_status
        for p in manifest:
            assert model_status(p["ai_model"])["status"] == "active", p["ai_model"]

    def test_ladder_fits_the_budget(self, manifest):
        """Projected spend at September's measured volume (7.5M in /
        0.7M out per book-month; 1.0M out for reasoning models) must sit
        under config.FLEET_MONTHLY_AI_BUDGET_USD with headroom."""
        import config
        from ai_pricing import PRICING
        total = 0.0
        for p in manifest:
            pr = PRICING[p["ai_model"]]
            out_m = 1.0 if p["ai_model"].startswith("gpt-6") else 0.7
            total += 7.5 * pr["input"] + out_m * pr["output"]
        assert total < config.FLEET_MONTHLY_AI_BUDGET_USD * 0.6, total

    def test_every_model_and_shadow_is_registered(self):
        from create_experiment_profiles import _verify_manifest_totals
        _verify_manifest_totals()   # raises on an unregistered model

    def test_cross_shadowing_covers_every_other_arm(self, manifest):
        import json
        arms = {(p["ai_provider"], p["ai_model"]) for p in manifest}
        for p in manifest:
            own = f"{p['ai_provider']}:{p['ai_model']}"
            shadows = json.loads(p["shadow_models"])
            assert own not in shadows, "an arm must not shadow itself"
            others = {f"{a}:{m}" for a, m in arms} - {own}
            assert set(shadows) == others, p["name"]
            assert p["enable_shadow_eval"] == 1

    def test_no_baseline_profiles_in_manifest(self, manifest):
        """Baselines are virtual benchmarks now (decision D6)."""
        assert all(p["strategy_type"] == "ai" for p in manifest)

    def test_gate_seeds_are_integers_on_the_0_100_scale(self, manifest):
        for p in manifest:
            v = p["ai_confidence_threshold"]
            assert isinstance(v, int) and 0 < v <= 100


class TestApplyFlow:
    def test_dry_run_creates_nothing(self):
        import create_experiment_profiles
        with patch("create_experiment_profiles._existing_profile_by_name",
                   return_value=None), \
             patch("models.create_trading_profile") as fake_create, \
             patch("models.update_trading_profile") as fake_update, \
             patch.object(sys, "argv", ["create_experiment_profiles.py"]):
            rc = create_experiment_profiles.main()
        assert rc == 0
        fake_create.assert_not_called()
        fake_update.assert_not_called()

    def test_apply_creates_twelve_profiles_when_none_exist(self):
        import create_experiment_profiles
        with patch("create_experiment_profiles._existing_profile_by_name",
                   return_value=None), \
             patch("models.create_trading_profile",
                   return_value=42) as fake_create, \
             patch("models.update_trading_profile") as fake_update, \
             patch.object(sys, "argv",
                          ["create_experiment_profiles.py", "--apply"]):
            rc = create_experiment_profiles.main()
        assert rc == 0
        assert fake_create.call_count == 12
        assert fake_update.call_count == 12
        # The model fields and shadow list reach the writer.
        kw = fake_update.call_args_list[0].kwargs
        assert {"ai_provider", "ai_model", "shadow_models",
                "enable_shadow_eval"} <= set(kw)

    def test_apply_updates_existing_profiles_in_place(self):
        import create_experiment_profiles
        with patch("create_experiment_profiles._existing_profile_by_name",
                   return_value={"id": 7}), \
             patch("models.create_trading_profile") as fake_create, \
             patch("models.update_trading_profile") as fake_update, \
             patch.object(sys, "argv",
                          ["create_experiment_profiles.py", "--apply"]):
            rc = create_experiment_profiles.main()
        assert rc == 0
        fake_create.assert_not_called()
        assert fake_update.call_count == 12


class TestResetScript:
    """The Experiment 2 reset script is excluded from ruff (generated
    lineage), so its contracts are pinned here instead."""

    def _load(self, monkeypatch):
        for var in ("RESET_ALPACA_A1_KEY", "RESET_ALPACA_A1_SECRET",
                    "RESET_ALPACA_A2_KEY", "RESET_ALPACA_A2_SECRET",
                    "RESET_ALPACA_A3_KEY", "RESET_ALPACA_A3_SECRET",
                    "RESET_NEW_GOOGLE_AI_KEY", "RESET_NEW_OPENAI_AI_KEY"):
            monkeypatch.delenv(var, raising=False)
        sys.modules.pop("full_fresh_start_2026_08_24", None)
        return importlib.import_module("full_fresh_start_2026_08_24")

    def test_no_keys_in_source(self, monkeypatch):
        mod = self._load(monkeypatch)
        for name, _label, k, s in mod.NEW_KEYS:
            assert k.startswith("TODO") and s.startswith("TODO"), name
        src = open(mod.__file__).read()
        # Alpaca paper key ids start with PK and are 20 chars; the
        # 07-08 lineage shipped three of them in git.
        import re
        assert not re.search(r'"PK[A-Z0-9]{18,}"', src)

    def test_steps_present_and_ordered(self, monkeypatch):
        mod = self._load(monkeypatch)
        for fn in ("step1_verify_keys", "step1c_archive_learning_data",
                   "step2_destroy_old_state", "step4_build_profiles",
                   "step4b_create_virtual_benchmarks",
                   "step5c_install_new_ai_key"):
            assert callable(getattr(mod, fn)), fn
        import inspect
        main_src = inspect.getsource(mod.main)
        order = [main_src.index(s) for s in (
            "step1_verify_keys(", "step1c_archive_learning_data(",
            "step2_destroy_old_state(", "step4_build_profiles(",
            "step5c_install_new_ai_key(", "step4b_create_virtual_benchmarks(")]
        assert order == sorted(order), "archive before wipe; keys before benchmarks"

    def test_verify_keys_refuses_without_ai_keys(self, monkeypatch, capsys):
        mod = self._load(monkeypatch)
        monkeypatch.setenv("ENCRYPTION_KEY", "x")
        assert mod.step1_verify_keys() is False
        assert "no AI key" in capsys.readouterr().out


class TestResetScript20261103:
    """The Experiment 3 reset script (clone of 08-24 with the D9 manifest
    and three-provider keys)."""

    def _load(self, monkeypatch):
        for v in ("RESET_NEW_GOOGLE_AI_KEY", "RESET_NEW_OPENAI_AI_KEY",
                  "RESET_NEW_ANTHROPIC_AI_KEY", "RESET_NEW_DEEPSEEK_AI_KEY"):
            monkeypatch.delenv(v, raising=False)
        import importlib
        sys.modules.pop("full_fresh_start_2026_11_03", None)
        return importlib.import_module("full_fresh_start_2026_11_03")

    def test_no_keys_in_source(self, monkeypatch):
        mod = self._load(monkeypatch)
        for name, _label, k, s in mod.NEW_KEYS:
            assert k.startswith("TODO") and s.startswith("TODO"), name
        import re
        src = open(mod.__file__).read()
        assert not re.search(r'"PK[A-Z0-9]{18,}"', src)
        assert not re.search(r"sk-ant-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{32,}", src)

    def test_three_provider_keys_and_order(self, monkeypatch):
        mod = self._load(monkeypatch)
        assert set(mod.NEW_AI_KEYS) >= {"anthropic", "deepseek", "openai"}
        import inspect
        main_src = inspect.getsource(mod.main)
        order = [main_src.index(s) for s in (
            "step1_verify_keys(", "step1c_archive_learning_data(",
            "step2_destroy_old_state(", "step4_build_profiles(",
            "step5c_install_new_ai_key(", "step4b_create_virtual_benchmarks(")]
        assert order == sorted(order)
        assert "EXPERIMENT 3" in main_src

    def test_verify_keys_refuses_without_every_arm_provider(self, monkeypatch, capsys):
        mod = self._load(monkeypatch)
        monkeypatch.setenv("ENCRYPTION_KEY", "x")
        assert mod.step1_verify_keys() is False
        out = capsys.readouterr().out
        assert "no AI key" in out and "anthropic" in out and "deepseek" in out
