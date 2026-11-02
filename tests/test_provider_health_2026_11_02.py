"""2026-11-02 — AI provider health: the warning system (docs/25 D8).

Nine of twelve Experiment 2 books made no AI decision for two to six
weeks (gpt-4.1-nano retired 10-23; Google prepaid credits depleted;
both Gemini arm models deprecated 10-08) and nothing said so — the
operator found it in a P&L review. These tests pin: failure
classification, the probe's fail-closed contract, alert edges (first
failure → alert, repeat only after 24h, recovery row), the decision-
flow watchdog's market-minute arithmetic, the deprecation lookahead
against the lifecycle table, the credit-runway projection against the
fleet budget, the picker badge honoring the lifecycle table, the cost
guard's budget-derived ceiling, and the scheduler/dashboard wiring.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

import provider_health as ph  # noqa: E402


class _Exc(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status


class TestClassification:
    def test_openai_retired_model_is_deprecated_model(self):
        e = _Exc("Error code: 404 - {'error': {'message': 'The model `gpt-4.1-nano` has been "
                 "deprecated', 'code': 'model_not_found'}}", 404)
        assert ph.classify_failure(e)[0] == ph.DEPRECATED_MODEL

    def test_google_prepayment_402_is_credits(self):
        e = _Exc("402 RESOURCE_EXHAUSTED. {'error': {'code': 402, 'message': 'Your prepayment "
                 "credits are depleted.'}}", 402)
        assert ph.classify_failure(e)[0] == ph.CREDITS

    def test_openai_insufficient_quota_429_is_credits_not_quota(self):
        e = _Exc("Error code: 429 - insufficient_quota: You exceeded your current quota, "
                 "please check your plan and billing details", 429)
        assert ph.classify_failure(e)[0] == ph.CREDITS

    def test_plain_rate_limit_is_quota(self):
        assert ph.classify_failure(_Exc("Rate limit reached for requests", 429))[0] == ph.QUOTA

    def test_auth_and_outage_and_unknown(self):
        assert ph.classify_failure(_Exc("Incorrect API key provided", 401))[0] == ph.AUTH
        assert ph.classify_failure(_Exc("overloaded_error", 529))[0] == ph.OUTAGE
        assert ph.classify_failure(_Exc("something odd"))[0] == ph.UNKNOWN

    def test_status_read_from_google_style_code_attribute(self):
        e = Exception("boom")
        e.code = 403
        assert ph.classify_failure(e)[0] == ph.AUTH


class TestProbe:
    def test_no_key_is_auth_failure_not_pass(self):
        ok, cls, detail, _ = ph.probe_model("openai", "gpt-5.6-luna", "")
        assert not ok and cls == ph.AUTH

    def test_no_model_is_failure(self):
        ok, cls, _, _ = ph.probe_model("openai", "", "k")
        assert not ok and cls == ph.UNKNOWN

    def test_probe_calls_provider_with_tiny_budget(self, monkeypatch):
        seen = {}
        def fake(provider, prompt, model, key, max_tokens, **kw):
            seen.update(provider=provider, model=model, key=key, max_tokens=max_tokens)
            return ("OK", 5, 1, 0)
        import ai_providers
        monkeypatch.setattr(ai_providers, "_call_provider", fake)
        ok, cls, _, _ = ph.probe_model("anthropic", "claude-haiku-5-5", "k")
        assert ok and cls is None
        assert seen["max_tokens"] == ph.PROBE_MAX_TOKENS and seen["model"] == "claude-haiku-5-5"

    def test_probe_exception_is_classified_never_raised(self, monkeypatch):
        import ai_providers
        def boom(*a, **k):
            raise _Exc("The model `x` has been deprecated", 404)
        monkeypatch.setattr(ai_providers, "_call_provider", boom)
        ok, cls, _, _ = ph.probe_model("openai", "x", "k")
        assert not ok and cls == ph.DEPRECATED_MODEL


class TestAlertEdges:
    def _ctx(self, pid=229):
        return SimpleNamespace(profile_id=pid, user_id=1, display_name="EXP-A1-TEST",
                               ai_provider="openai", ai_model="gpt-4.1-nano", ai_api_key="k",
                               db_path=None)

    def test_first_failure_alerts_repeat_waits_recovery_logs(self, tmp_main_db, monkeypatch):
        import ai_providers
        monkeypatch.setattr(ai_providers, "_call_provider",
                            lambda *a, **k: (_ for _ in ()).throw(_Exc("model_not_found", 404)))
        emails = []
        import notifications
        monkeypatch.setattr(notifications, "notify_error",
                            lambda msg, context="", ctx=None: emails.append(context))
        acts = []
        import models
        monkeypatch.setattr(models, "log_activity",
                            lambda pid, uid, kind, title, detail, symbol=None: acts.append((kind, title)) or 1)
        row = ph.probe_profile(self._ctx())
        assert row["ok"] == 0 and row["failure_class"] == ph.DEPRECATED_MODEL
        assert row["first_failed_at"] and row["last_alerted_at"]
        assert emails and "DEPRECATED_MODEL" in emails[0]
        assert acts[0][0] == "provider_health_failed"
        # same failure again within 24h: persisted, not re-alerted
        row2 = ph.probe_profile(self._ctx())
        assert row2["first_failed_at"] == row["first_failed_at"]
        assert len(emails) == 1
        # recovery
        monkeypatch.setattr(ai_providers, "_call_provider", lambda *a, **k: ("OK", 1, 1, 0))
        row3 = ph.probe_profile(self._ctx())
        assert row3["ok"] == 1 and row3["first_failed_at"] is None
        assert acts[-1][0] == "provider_health_recovered"
        assert [r for r in ph.current_status() if r["profile_id"] == 229 and r["check_kind"] == "probe"][0]["ok"] == 1

    def test_class_change_realerts(self, tmp_main_db, monkeypatch):
        import ai_providers
        import notifications
        import models
        emails = []
        monkeypatch.setattr(notifications, "notify_error",
                            lambda msg, context="", ctx=None: emails.append(context))
        monkeypatch.setattr(models, "log_activity", lambda *a, **k: 1)
        monkeypatch.setattr(ai_providers, "_call_provider",
                            lambda *a, **k: (_ for _ in ()).throw(_Exc("rate limit", 429)))
        ph.probe_profile(self._ctx(230))
        monkeypatch.setattr(ai_providers, "_call_provider",
                            lambda *a, **k: (_ for _ in ()).throw(_Exc("credits depleted", 402)))
        ph.probe_profile(self._ctx(230))
        assert len(emails) == 2 and "CREDITS" in emails[1]


class TestDecisionFlowWatchdog:
    def test_market_minutes_skip_nights_and_weekends(self):
        # Friday 15:00 ET → Monday 10:00 ET = 60 + 30 market minutes
        fri = datetime(2026, 10, 30, 19, 0, tzinfo=timezone.utc)   # 15:00 ET (EDT)
        mon = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)    # 10:00 ET (EST)
        assert ph._market_minutes_since(fri.isoformat(), mon) == pytest.approx(90, abs=1)

    def test_stall_fires_after_threshold_and_not_before(self, tmp_main_db, tmp_path, monkeypatch):
        import sqlite3
        import models
        monkeypatch.setattr(models, "log_activity", lambda *a, **k: 1)
        import notifications
        monkeypatch.setattr(notifications, "notify_error", lambda *a, **k: None)
        db = str(tmp_path / "p.db")
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE ai_cost_ledger (timestamp TEXT, provider TEXT, model TEXT, "
                      "input_tokens INT, output_tokens INT, purpose TEXT, estimated_cost_usd REAL, "
                      "call_id TEXT, cached_tokens INT)")
            c.execute("INSERT INTO ai_cost_ledger VALUES ('2026-10-23T17:00:00', 'openai', 'x', 1, 1, "
                      "'batch_select', 0.01, NULL, 0)")
        ctx = SimpleNamespace(profile_id=231, user_id=1, display_name="T", ai_provider="openai",
                              ai_model="x", db_path=db)
        soon = datetime(2026, 10, 23, 19, 0, tzinfo=timezone.utc)       # 2h later same day
        assert ph.decision_flow_check(ctx, soon)["ok"] == 1
        later = datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc)      # next Monday afternoon
        row = ph.decision_flow_check(ctx, later)
        assert row["ok"] == 0 and "no successful AI decision" in row["detail"]

    def test_empty_book_never_alerts(self, tmp_main_db, tmp_path, monkeypatch):
        import sqlite3
        import models
        monkeypatch.setattr(models, "log_activity", lambda *a, **k: 1)
        db = str(tmp_path / "p.db")
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE ai_cost_ledger (timestamp TEXT, purpose TEXT, provider TEXT, estimated_cost_usd REAL)")
        ctx = SimpleNamespace(profile_id=232, user_id=1, display_name="T", ai_provider="openai",
                              ai_model="x", db_path=db)
        assert ph.decision_flow_check(ctx)["ok"] == 1


class TestDeprecationLookahead:
    def _ctx(self, model, provider="openai"):
        return SimpleNamespace(profile_id=233, user_id=1, display_name="T", ai_provider=provider,
                               ai_model=model, ai_api_key="", db_path=None)

    def test_retired_and_near_retirement_fail_active_passes(self, tmp_main_db, monkeypatch):
        import models
        import notifications
        monkeypatch.setattr(models, "log_activity", lambda *a, **k: 1)
        monkeypatch.setattr(notifications, "notify_error", lambda *a, **k: None)
        assert ph.deprecation_lookahead(self._ctx("gpt-4.1-nano"))["ok"] == 0
        assert ph.deprecation_lookahead(self._ctx("gpt-5-nano"))["ok"] == 0      # retires 2026-12-11
        assert ph.deprecation_lookahead(self._ctx("gpt-5.6-luna"))["ok"] == 1

    def test_lifecycle_table_shape_and_catalog_consistency(self):
        from ai_providers import MODEL_STATUS, PROVIDERS, model_status
        offered = {m for p in PROVIDERS.values() for m in p["models"]}
        for mid, st in MODEL_STATUS.items():
            assert st["status"] in ("deprecated", "retired")
            assert st["retire_on"] and len(st["retire_on"]) == 10
            assert st["replacement"]
            if st["status"] == "retired":
                assert mid not in offered, f"retired {mid} is still offered"
        assert model_status("claude-haiku-5-5")["status"] == "active"

    def test_openai_shutdown_date_overrides_table(self, monkeypatch):
        monkeypatch.setattr(ph, "_openai_shutdown_date", lambda m, k: "2099-01-01")
        st = ph.lifecycle_for("openai", "gpt-5.6-luna", api_key="k")
        assert st["status"] == "deprecated" and st["retire_on"] == "2099-01-01"
        assert st["source"] == "openai:shutdown_date"


class TestCreditRunway:
    def test_projection_against_budget(self, tmp_main_db, tmp_path, monkeypatch):
        import sqlite3
        import models
        import notifications
        monkeypatch.setattr(models, "log_activity", lambda *a, **k: 1)
        monkeypatch.setattr(notifications, "notify_error", lambda *a, **k: None)
        db = str(tmp_path / "p.db")
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE ai_cost_ledger (timestamp TEXT, provider TEXT, estimated_cost_usd REAL)")
            c.executemany("INSERT INTO ai_cost_ledger VALUES (?, ?, ?)",
                          [(now, "openai", 7.0), (now, "anthropic", 14.0)])   # $21 in 7d → $90/mo
        row = ph.credit_runway([db], budget_usd=100.0)
        assert row["projected_monthly_usd"] == pytest.approx(90.0)
        assert row["ok"] == 0 and row["failure_class"] == ph.CREDITS        # 90% > 80%
        ok_row = ph.credit_runway([db], budget_usd=200.0)
        assert ok_row["ok"] == 1


class TestPickerAndBudget:
    def test_picker_badge_never_says_available_for_retired(self, monkeypatch):
        import ai_providers
        monkeypatch.setattr(ai_providers, "available_model_ids",
                            lambda provider, force=False: frozenset({"gpt-5.6-luna", "gpt-4.1-nano"}))
        cat = {m["id"]: m for m in ai_providers.get_model_catalog("openai")}
        assert cat["gpt-5.6-luna"]["available"] is True and cat["gpt-5.6-luna"]["status"] == "active"
        assert cat["gpt-5-nano"]["status"] == "deprecated" and cat["gpt-5-nano"]["retire_on"] == "2026-12-11"
        assert "gpt-4.1-nano" not in cat

    def test_every_offered_model_is_priced_and_arms_present(self):
        from ai_providers import PROVIDERS
        from ai_pricing import PRICING
        offered = {m for p in PROVIDERS.values() for m in p["models"]}
        assert not (offered - set(PRICING)), offered - set(PRICING)
        for arm in ("claude-haiku-5-5", "deepseek-flash", "gpt-5.6-luna", "gpt-6.1-terra"):
            assert arm in offered and arm in PRICING

    def test_cost_guard_ceiling_is_budget_bound(self, monkeypatch):
        import cost_guard
        import config
        monkeypatch.setattr(config, "FLEET_MONTHLY_AI_BUDGET_USD", 100.0)
        monkeypatch.setattr(config, "FLEET_TRADING_DAYS_PER_MONTH", 20)
        monkeypatch.setattr(cost_guard, "_user_or_auto_ceiling_usd", lambda uid: 14.0)
        assert cost_guard.daily_ceiling_usd(1) == pytest.approx(5.0)       # budget wins over $14 override
        monkeypatch.setattr(cost_guard, "_user_or_auto_ceiling_usd", lambda uid: 2.0)
        assert cost_guard.daily_ceiling_usd(1) == pytest.approx(2.0)       # override can only lower


class TestWiring:
    def test_scheduler_runs_the_task_and_dashboard_shows_failures(self):
        src = open(os.path.join(REPO, "multi_scheduler.py")).read()
        assert "_task_provider_health(ctx)" in src and "ph.run_daily_checks(" in src
        v = open(os.path.join(REPO, "views.py")).read()
        assert "provider_health_failures=_provider_health_failures" in v
        assert '"/api/provider-health"' in v
        html = open(os.path.join(REPO, "templates", "dashboard.html")).read()
        assert "provider-health-banner" in html and "no decisions" in html

    def test_health_tasks_run_every_cycle_not_only_at_snapshot(self):
        """2026-11-02: the first post-deploy cycle never reached the
        provider probe because it sat under `if run_snapshot:` (daily).
        Both health probes must be dispatched OUTSIDE that block."""
        src = open(os.path.join(REPO, "multi_scheduler.py")).read()
        i_snap = src.index("    if run_snapshot:\n        run_task(\n            f\"[{seg_label}] Daily Snapshot\"")
        i_prov = src.index('f"[{seg_label}] Provider Health"')
        i_dsh = src.index('f"[{seg_label}] Data Source Health"')
        assert i_prov < i_snap and i_dsh < i_snap
        # and at function-body indentation (4 spaces), not nested (8)
        line = src[src.rfind("\n", 0, i_prov) + 1:i_prov]
        assert line == "            " or line.strip() == ""      # inside run_task( call, 12 spaces
        call_start = src.rfind("    run_task(", 0, i_prov)
        assert src[call_start - 1] == "\n" and src[call_start:call_start + 13] == "    run_task("

    def test_master_schema_creates_the_table(self, tmp_main_db):
        import sqlite3
        with sqlite3.connect(tmp_main_db) as c:
            cols = [r[1] for r in c.execute("pragma table_info(provider_health)")]
        assert "failure_class" in cols and "first_failed_at" in cols
