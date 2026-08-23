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
    def test_nine_profiles_three_per_arm(self, manifest):
        from create_experiment_profiles import ARMS, REPLICATES_PER_ARM
        assert len(manifest) == len(ARMS) * REPLICATES_PER_ARM == 9

    def test_equal_capital_and_account_fit(self, manifest):
        caps = {p["initial_capital"] for p in manifest}
        assert caps == {250_000.0}
        # Three per $1M paper account → $750K, under Alpaca's cap.
        by_group = {}
        for p in manifest:
            g = p["name"].split("-")[1]
            by_group[g] = by_group.get(g, 0) + p["initial_capital"]
        assert by_group == {"A1": 750_000.0, "A2": 750_000.0,
                            "A3": 750_000.0}

    def test_arms_differ_only_in_model_fields(self, manifest):
        varying = {"name", "ai_provider", "ai_model", "shadow_models"}
        base = {k: v for k, v in manifest[0].items() if k not in varying}
        for p in manifest[1:]:
            assert {k: v for k, v in p.items() if k not in varying} == base, \
                p["name"]

    def test_replicates_within_an_arm_are_identical_except_name(self, manifest):
        by_arm = {}
        for p in manifest:
            by_arm.setdefault(p["name"].rsplit("-", 1)[0], []).append(p)
        assert len(by_arm) == 3
        for stem, reps in by_arm.items():
            assert len(reps) == 3, stem
            ref = {k: v for k, v in reps[0].items() if k != "name"}
            for r in reps[1:]:
                assert {k: v for k, v in r.items() if k != "name"} == ref

    def test_arm_models_are_the_d1_decision(self, manifest):
        arms = sorted({(p["ai_provider"], p["ai_model"]) for p in manifest})
        assert arms == [("google", "gemini-3.5-flash-lite"),
                        ("google", "gemini-3.7-flash"),
                        ("openai", "gpt-5.6-luna")]

    def test_every_model_and_shadow_is_registered(self):
        from create_experiment_profiles import _verify_manifest_totals
        _verify_manifest_totals()   # raises on an unregistered model

    def test_cross_shadowing_covers_the_other_two_arms(self, manifest):
        import json
        arms = {(p["ai_provider"], p["ai_model"]) for p in manifest}
        for p in manifest:
            own = f"{p['ai_provider']}:{p['ai_model']}"
            shadows = json.loads(p["shadow_models"])
            assert own not in shadows, "an arm must not shadow itself"
            others = {f"{a}:{m}" for a, m in arms} - {own}
            assert others <= set(shadows), p["name"]
            assert p["enable_shadow_eval"] == 1

    def test_luna_arm_bridges_experiment_one_evidence(self, manifest):
        import json
        luna = [p for p in manifest if p["ai_model"] == "gpt-5.6-luna"]
        for p in luna:
            assert "openai:gpt-4.1-nano" in json.loads(p["shadow_models"])

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

    def test_apply_creates_nine_profiles_when_none_exist(self):
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
        assert fake_create.call_count == 9
        assert fake_update.call_count == 9
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
        assert fake_update.call_count == 9


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
