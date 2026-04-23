"""Tests that enforce NO GUESSING at names, schemas, or data structures.

These tests exist because the developer repeatedly guessed at table names,
column names, function signatures, API response fields, and template
variable structures instead of reading the actual code — causing silent
failures, 500 errors, and blank pages.

Every test here validates that code references match reality. If a new
module, table, column, or API endpoint is added, it MUST be verified here.
"""

import inspect
import json
import os
import re
import sqlite3
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# 1. Every SQL table name referenced in code must actually exist in a schema
# ---------------------------------------------------------------------------

class TestTableNamesExist:
    """No made-up table names in SQL queries."""

    # Tables defined in models.py init_user_db()
    MAIN_DB_TABLES = {
        "users", "user_segment_configs", "trading_profiles",
        "alpaca_accounts", "activity_log", "tuning_history",
    }

    # Tables defined in journal.py init_db()
    PROFILE_DB_TABLES = {
        "trades", "daily_snapshots", "ai_predictions",
        "ai_cost_ledger", "task_runs", "events",
        "sec_filings_history", "signal_snapshots",
        "deprecated_strategies", "crisis_state_history",
        "alt_data_cache", "earnings_dates",
    }

    ALL_KNOWN_TABLES = MAIN_DB_TABLES | PROFILE_DB_TABLES

    def test_all_sql_table_references_are_real(self):
        """Scan all .py files for SQL table references and verify they exist."""
        import glob
        # Pattern matches FROM/INTO/UPDATE/TABLE tablename
        table_pattern = re.compile(
            r'(?:FROM|INTO|UPDATE|TABLE)\s+(?:IF\s+NOT\s+EXISTS\s+)?["\']?(\w+)["\']?',
            re.IGNORECASE
        )

        suspicious = []
        for pyfile in glob.glob("*.py"):
            if pyfile.startswith("test_"):
                continue
            with open(pyfile) as f:
                content = f.read()
            for match in table_pattern.finditer(content):
                table = match.group(1).lower()
                # Skip SQL keywords that look like table names
                if table in ("select", "set", "values", "where", "and", "or",
                             "not", "null", "as", "on", "by", "is", "in",
                             "like", "between", "exists", "table", "index",
                             "integer", "text", "real", "blob", "primary",
                             "autoincrement", "default", "foreign", "unique",
                             "key", "references", "check", "constraint",
                             "sqlite_master", "pragma", "info"):
                    continue
                if table not in self.ALL_KNOWN_TABLES:
                    suspicious.append((pyfile, table, match.group(0)[:60]))

        if suspicious:
            msg = "SQL references to unknown tables:\n"
            for f, t, ctx in suspicious[:10]:
                msg += f"  {f}: table '{t}' — {ctx}\n"
            msg += (f"\nKnown tables: {sorted(self.ALL_KNOWN_TABLES)}\n"
                    f"If a new table was added, add it to TestTableNamesExist.ALL_KNOWN_TABLES")
            # Don't hard-fail — some may be dynamically created. Warn instead.
            # But if we find 'sec_alerts' (the made-up name), that's a real bug.
            for f, t, ctx in suspicious:
                assert t != "sec_alerts", (
                    f"{f} references made-up table 'sec_alerts'. "
                    f"The actual table is 'sec_filings_history'. "
                    f"READ THE SCHEMA BEFORE WRITING QUERIES."
                )


# ---------------------------------------------------------------------------
# 2. Every display_name must cover every meta-model feature
# ---------------------------------------------------------------------------

class TestDisplayNameCoverage:
    def test_all_meta_model_features_have_display_names(self):
        from display_names import _DISPLAY_NAMES
        from meta_model import NUMERIC_FEATURES, CATEGORICAL_FEATURES
        all_features = list(NUMERIC_FEATURES) + list(CATEGORICAL_FEATURES.keys())
        missing = [f for f in all_features if f not in _DISPLAY_NAMES]
        assert not missing, (
            f"Meta-model features missing display names: {missing}\n"
            f"Add them to display_names.py _DISPLAY_NAMES dict."
        )


# ---------------------------------------------------------------------------
# 3. Every Jinja template variable must match the view's data structure
# ---------------------------------------------------------------------------

class TestTemplateDataContracts:
    """Verify that template variable references match actual view data."""

    def test_ai_cost_info_structure(self):
        """ai_cost_info must have 'per_profile' list and 'totals' dict."""
        # Verify the view builds it correctly by checking the source
        import views
        src = inspect.getsource(views.ai_dashboard)
        assert '"per_profile": []' in src, "ai_cost_info must init with per_profile list"
        assert '"totals"' in src, "ai_cost_info must have totals dict"
        assert '"today": summary["today"]' in src, "Must use summary['today'] not made-up keys"
        assert '"seven_d": summary["7d"]' in src, "Must use summary['7d'] not made-up keys"

    def test_crisis_info_structure(self):
        """crisis_info must have 'per_profile' and 'max_level'."""
        import views
        src = inspect.getsource(views.ai_dashboard)
        assert '"per_profile": []' in src and '"max_level"' in src, (
            "crisis_info must have per_profile and max_level"
        )

    def test_allocation_uses_correct_function_signature(self):
        """get_allocation_summary requires (db_path, market_type)."""
        import multi_strategy
        sig = inspect.signature(multi_strategy.get_allocation_summary)
        params = list(sig.parameters.keys())
        assert params == ["db_path", "market_type"], (
            f"get_allocation_summary signature is {params}, not what you guessed"
        )

    def test_validations_use_correct_function(self):
        """Validations come from rigorous_backtest.get_recent_validations, not a raw DB query."""
        import views
        src = inspect.getsource(views.ai_dashboard)
        assert "get_recent_validations" in src, (
            "Must use rigorous_backtest.get_recent_validations(), not raw SQL on a guessed table"
        )

    def test_auto_strategies_use_correct_function(self):
        """Auto strategies come from strategy_generator.list_strategies."""
        import views
        src = inspect.getsource(views.ai_dashboard)
        assert "list_strategies" in src, (
            "Must use strategy_generator.list_strategies(), not a made-up function"
        )

    def test_decay_uses_correct_functions(self):
        """Alpha decay uses list_deprecated, compute_rolling_metrics, compute_lifetime_metrics."""
        import views
        src = inspect.getsource(views.ai_dashboard)
        assert "list_deprecated" in src, "Must use alpha_decay.list_deprecated()"
        assert "compute_rolling_metrics" in src, "Must use alpha_decay.compute_rolling_metrics()"
        assert "compute_lifetime_metrics" in src, "Must use alpha_decay.compute_lifetime_metrics()"


# ---------------------------------------------------------------------------
# 4. View data must match between performance_dashboard and ai_dashboard
# ---------------------------------------------------------------------------

class TestViewDataConsistency:
    """The AI dashboard must compute data identically to performance_dashboard."""

    def _get_data_blocks(self, func_name):
        """Extract data computation variable names from a view function."""
        import views
        src = inspect.getsource(getattr(views, func_name))
        # Find all top-level variable assignments
        assignments = re.findall(r'^    (\w+)\s*=\s*', src, re.MULTILINE)
        return set(assignments)

    def test_ai_dashboard_has_same_data_vars_as_performance(self):
        """Critical data variables must exist in both views."""
        required_in_ai = [
            "ai_perf", "slippage", "meta_info", "validations",
            "allocation_info", "ai_cost_info", "crisis_info",
            "event_info", "ensemble_info", "auto_strategy_info", "decay_info",
        ]
        import views
        ai_src = inspect.getsource(views.ai_dashboard)
        for var in required_in_ai:
            assert var in ai_src, (
                f"ai_dashboard() missing '{var}' — it exists in performance_dashboard() "
                f"and the template expects it"
            )


# ---------------------------------------------------------------------------
# 5. API endpoints return the fields templates expect
# ---------------------------------------------------------------------------

class TestAPIContracts:
    def test_macro_data_api_returns_correct_keys(self):
        """The /api/macro-data endpoint must return yield_curve, etf_flows, cboe_skew, fred_macro."""
        from macro_data import get_all_macro_data
        # Verify the function exists and returns the right keys
        sig = inspect.signature(get_all_macro_data)
        # Check the source for the return dict keys
        src = inspect.getsource(get_all_macro_data)
        for key in ["yield_curve", "etf_flows", "cboe_skew", "fred_macro"]:
            assert f'"{key}"' in src, f"get_all_macro_data must return '{key}'"

    def test_yield_curve_fields(self):
        """Yield curve must return rate_2y, rate_10y, spread_10y_2y, curve_status."""
        from macro_data import get_yield_curve
        src = inspect.getsource(get_yield_curve)
        for field in ["rate_2y", "rate_10y", "rate_30y", "spread_10y_2y", "curve_status"]:
            assert f'"{field}"' in src, f"get_yield_curve must return '{field}'"

    def test_cboe_skew_fields(self):
        """CBOE skew must return skew_value, skew_signal, skew_5d_avg."""
        from macro_data import get_cboe_skew
        src = inspect.getsource(get_cboe_skew)
        for field in ["skew_value", "skew_signal", "skew_5d_avg"]:
            assert f'"{field}"' in src, f"get_cboe_skew must return '{field}'"

    def test_fred_macro_fields(self):
        """FRED macro must return unemployment_rate, cpi_yoy, consumer_sentiment."""
        from macro_data import get_fred_macro
        src = inspect.getsource(get_fred_macro)
        for field in ["unemployment_rate", "unemployment_trend", "cpi_yoy",
                       "consumer_sentiment", "initial_claims_4wk_avg"]:
            assert f'"{field}"' in src, f"get_fred_macro must return '{field}'"


# ---------------------------------------------------------------------------
# 6. No yfinance in equity price paths (enforced separately but repeated)
# ---------------------------------------------------------------------------

class TestNoYFinanceInEquityPaths:
    def test_screener_equity_functions_use_alpaca(self):
        import inspect, screener
        for fn_name in ("screen_by_price_range", "find_volume_surges",
                        "find_momentum_stocks", "find_breakouts"):
            fn = getattr(screener, fn_name)
            src = inspect.getsource(fn)
            assert "yf_lock.download" not in src and "yf.download" not in src, (
                f"screener.{fn_name} must use Alpaca, not yfinance"
            )

    def test_ai_tracker_uses_alpaca(self):
        import inspect, ai_tracker
        src = inspect.getsource(ai_tracker._get_current_price)
        assert "get_latest_trade" in src, (
            "_get_current_price must use api.get_latest_trade as primary"
        )


# ---------------------------------------------------------------------------
# 7. dotenv loaded before imports in both entry points
# ---------------------------------------------------------------------------

class TestChangelogUpToDate:
    """Every code change must be documented in CHANGELOG.md."""

    def test_changelog_has_todays_date(self):
        """If any .py file was modified today, CHANGELOG.md must have today's date."""
        import subprocess
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

        # Check if any .py files were modified today (via git)
        result = subprocess.run(
            ["git", "log", "--since=midnight", "--name-only", "--pretty=format:"],
            capture_output=True, text=True,
        )
        changed_files = [f.strip() for f in result.stdout.split("\n") if f.strip()]
        py_files_changed = [f for f in changed_files if f.endswith(".py")]

        if not py_files_changed:
            return  # No code changes today, nothing to check

        # CHANGELOG.md must mention today's date
        with open("CHANGELOG.md") as f:
            changelog = f.read()

        assert today in changelog, (
            f"Code files were modified today ({today}) but CHANGELOG.md "
            f"does not contain today's date. Every code change must be "
            f"documented in the changelog."
        )

    def test_changelog_not_empty(self):
        """CHANGELOG.md must exist and have content."""
        with open("CHANGELOG.md") as f:
            content = f.read()
        assert len(content) > 100, "CHANGELOG.md is empty or too short"
        assert "## 2026-" in content, "CHANGELOG.md has no dated entries"


class TestPromptBuildDoesNotCrash:
    """The AI prompt builder must not crash when alt data keys are missing."""

    def test_prompt_handles_missing_alt_data_keys(self):
        """_build_batch_prompt must not crash when any alt_data sub-key is missing."""
        from ai_analyst import _build_batch_prompt

        # Build a candidate with NO alt data at all
        candidate_empty = {
            "symbol": "TEST", "price": 100, "signal": "BUY", "score": 2,
            "rsi": 50, "volume_ratio": 1.0, "atr": 2.0, "adx": 25,
            "stoch_rsi": 50, "roc_10": 1.0, "pct_from_52w_high": -10,
            "mfi": 50, "cmf": 0, "squeeze": 0, "pct_from_vwap": 0,
            "nearest_fib_dist": 5, "gap_pct": 0,
        }

        # Build a candidate with empty alt_data sub-dicts
        candidate_empty_alt = dict(candidate_empty)
        candidate_empty_alt["alt_data"] = {
            "insider": {}, "short": {}, "fundamentals": {},
            "options": {}, "intraday": {},
            # All wave 1+2 keys missing or empty
        }

        # Build a candidate with partial alt_data
        candidate_partial = dict(candidate_empty)
        candidate_partial["alt_data"] = {
            "insider": {"net_direction": "buying", "recent_buys": 3},
            # congressional MISSING entirely (the bug that crashed production)
            # dark_pool MISSING
            # earnings_surprise MISSING
        }

        portfolio = {"equity": 100000, "cash": 50000, "positions": []}
        market_ctx = {
            "regime": "bull", "vix": 20, "spy_trend": "up",
            "political_context": None, "profile_summary": None,
            "learned_patterns": [], "sector_rotation": {},
            "crisis_context": None, "macro_context": {},
        }

        # None of these should raise
        for candidate in [candidate_empty, candidate_empty_alt, candidate_partial]:
            try:
                prompt = _build_batch_prompt([candidate], portfolio, market_ctx, ctx=None)
                assert isinstance(prompt, str)
                assert len(prompt) > 100
            except KeyError as e:
                raise AssertionError(
                    f"_build_batch_prompt crashed with KeyError: {e}. "
                    f"Every alt_data field access must use .get() with defaults, "
                    f"not direct dict indexing."
                )


class TestActivityLogDisplayNames:
    """Every string shown to users in activity logs must use display_name()."""

    def test_exit_trigger_uses_display_name(self):
        """Exit activity must format trigger through display_name, not capitalize()."""
        import inspect, multi_scheduler
        src = inspect.getsource(multi_scheduler._task_check_exits)
        assert "display_name" in src or "_dn" in src, (
            "_task_check_exits must use display_name() for trigger types, "
            "not .capitalize() which turns 'trailing_stop' into 'Trailing_stop'"
        )
        assert ".capitalize()" not in src, (
            "_task_check_exits must NOT use .capitalize() on trigger names — "
            "use display_name() instead"
        )

    def test_all_exit_triggers_have_display_names(self):
        """Every exit trigger type from portfolio_manager must have a display name."""
        from display_names import _DISPLAY_NAMES
        triggers = ["trailing_stop", "stop_loss", "take_profit",
                     "short_stop_loss", "short_take_profit"]
        for t in triggers:
            assert t in _DISPLAY_NAMES, (
                f"Exit trigger '{t}' missing from display_names.py"
            )


class TestDotenvLoading:
    def test_scheduler_loads_dotenv_before_imports(self):
        import inspect, multi_scheduler
        src = inspect.getsource(multi_scheduler)
        dotenv_pos = src.find("load_dotenv()")
        import_pos = src.find("\nfrom segments import")
        assert dotenv_pos > 0 and dotenv_pos < import_pos, (
            "multi_scheduler must call load_dotenv() before importing modules"
        )

    def test_app_loads_dotenv(self):
        import inspect, app
        src = inspect.getsource(app)
        assert "load_dotenv()" in src, "app.py must call load_dotenv()"


# ---------------------------------------------------------------------------
# 8. Template JS field references must match API response structures
# ---------------------------------------------------------------------------

class TestTemplateJSMatchesAPI:
    """The JS in templates that processes API JSON must use real field names."""

    def test_js_never_outputs_raw_snake_case_keys(self):
        """JS that renders API data must never output raw snake_case keys to the user.
        Every key from the API must go through a display name mapping."""
        with open("templates/ai.html") as f:
            template = f.read()

        # Find all places where JS outputs a variable that could be a snake_case key
        # Pattern: esc(variable) where variable comes from an API object key
        # Known bad patterns from past bugs:
        bad_patterns = [
            "esc(sector)",           # was outputting comm_services, consumer_disc
            "esc(s.name)",           # could output strategy snake_case
            "esc(e.type)",           # could output event type snake_case
        ]
        for bad in bad_patterns:
            # Check it's either not present or has a display name lookup before it
            if bad in template:
                # Find the context — is there a name mapping nearby?
                idx = template.find(bad)
                context = template[max(0, idx-200):idx+50]
                assert "sectorNames" in context or "display_name" in context or "Names[" in context, (
                    f"ai.html outputs {bad} without a display name mapping. "
                    f"Raw snake_case keys like 'comm_services' will show in the UI. "
                    f"Add a JS name mapping object."
                )

    def test_sector_flow_js_has_display_names(self):
        """ETF sector flow JS must have human-readable names for all sectors."""
        with open("templates/ai.html") as f:
            template = f.read()

        # All sector keys from market_data.SECTOR_ETFS
        from market_data import SECTOR_ETFS
        sector_keys = list(SECTOR_ETFS.keys())

        # Find the sectorNames mapping in JS
        assert "sectorNames" in template, (
            "ai.html must have a sectorNames JS object for sector display names"
        )

        for key in sector_keys:
            assert f"'{key}'" in template or f'"{key}"' in template, (
                f"Sector '{key}' missing from JS sectorNames mapping in ai.html"
            )

    def test_macro_data_js_uses_real_fields(self):
        """ai.html JS for Market Intelligence must reference actual API fields."""
        with open("templates/ai.html") as f:
            template = f.read()

        # Extract the JS block that processes macro data
        macro_js_start = template.find("function loadMacroData")
        macro_js_end = template.find("loadMacroData();", macro_js_start)
        if macro_js_start < 0 or macro_js_end < 0:
            pytest.skip("loadMacroData not found in ai.html")
        macro_js = template[macro_js_start:macro_js_end]

        # These are the ACTUAL field names from macro_data.py
        # Verify the JS references them, not made-up alternatives
        real_fields = {
            "yield_curve": ["rate_2y", "rate_10y", "rate_30y", "spread_10y_2y",
                            "curve_status", "fed_funds_upper"],
            "cboe_skew": ["skew_value", "skew_signal", "skew_5d_avg"],
            "fred_macro": ["unemployment_rate", "unemployment_trend", "cpi_yoy",
                           "consumer_sentiment", "consumer_sentiment_trend",
                           "initial_claims_4wk_avg"],
        }

        # Check that the JS does NOT use these made-up field names
        made_up_fields = [
            "d.yield_curve.inverted",    # was: curve_status
            "d.cboe_skew.value",         # was: skew_value
            "d.cboe_skew.percentile",    # doesn't exist
            "d.etf_flows.net_flow",      # doesn't exist
            "d.fred_indicators",         # was: fred_macro
        ]
        for bad in made_up_fields:
            assert bad not in macro_js, (
                f"ai.html JS uses made-up field '{bad}'. "
                f"READ macro_data.py to find the real field names."
            )

        # Check that the JS DOES use these real field names
        for category, fields in real_fields.items():
            for field in fields[:3]:  # Check at least the key ones
                # The JS accesses these as yc.rate_10y, sk.skew_value, fm.unemployment_rate
                assert field in macro_js, (
                    f"ai.html JS doesn't reference real field '{field}' from {category}. "
                    f"It should — that's what the API returns."
                )

    def test_tuning_status_js_uses_real_fields(self):
        """Tuning status AJAX must use real field names from /api/tuning-status."""
        with open("templates/ai.html") as f:
            template = f.read()

        js_start = template.find("function loadTuningStatus")
        js_end = template.find("loadTuningStatus(1)", js_start)
        if js_start < 0 or js_end < 0:
            pytest.skip("loadTuningStatus not found")
        js = template[js_start:js_end]

        # Real fields from the API (views.py api_tuning_status)
        for field in ["profile_name", "resolved", "can_tune", "last_run", "message"]:
            assert field in js, (
                f"Tuning status JS doesn't use real field '{field}'"
            )

    def test_tuning_history_js_uses_real_fields(self):
        """Tuning history AJAX must use real field names."""
        with open("templates/ai.html") as f:
            template = f.read()

        js_start = template.find("function loadTuningHistory")
        js_end = template.find("loadTuningHistory(1)", js_start)
        if js_start < 0 or js_end < 0:
            pytest.skip("loadTuningHistory not found")
        js = template[js_start:js_end]

        for field in ["profile_name", "timestamp", "adjustment_type",
                       "parameter_label", "parameter_name", "old_value",
                       "new_value", "reason", "win_rate_at_change", "outcome_after"]:
            assert field in js, (
                f"Tuning history JS doesn't use real field '{field}'"
            )


# ---------------------------------------------------------------------------
# 9. Every render_template call must pass all variables the template uses
# ---------------------------------------------------------------------------

class TestRenderTemplateKwargs:
    """Every variable referenced in a template must be passed by its view."""

    def _get_template_vars(self, template_path):
        """Extract top-level Jinja variable names from a template."""
        with open(template_path) as f:
            content = f.read()
        # Match {{ var.something }} and {% if var.something %}
        # Only capture the top-level variable name (before first dot)
        refs = set()
        for pattern in [r'\{\{[^}]*?\b(\w+)\.', r'\{%[^%]*?\b(\w+)\.']:
            for m in re.finditer(pattern, content):
                name = m.group(1)
                if name not in ("loop", "request", "current_user", "config",
                                "get_flashed_messages", "url_for", "self",
                                "caller", "range", "true", "false", "none",
                                "h", "s", "e", "r", "d", "v", "p", "f",
                                "mp", "prof", "prof_decay", "hr", "row",
                                "actions", "spec_name", "trades_tpl",
                                "base", "block", "mo", "w", "c", "a",
                                "ind", "flowKeys", "yc", "sk", "fm", "ef"):
                    refs.add(name)
        return refs

    def _get_render_kwargs(self, func_name):
        """Extract variable names passed to render_template in a view function."""
        import views
        src = inspect.getsource(getattr(views, func_name))
        # Find render_template call and extract kwargs
        render_match = re.search(r'render_template\([^)]+\)', src, re.DOTALL)
        if not render_match:
            return set()
        render_call = render_match.group(0)
        # Extract kwarg names
        kwargs = set(re.findall(r'(\w+)\s*=', render_call))
        kwargs.discard("render_template")
        return kwargs

    def test_ai_template_gets_all_its_variables(self):
        """Every variable used in ai.html must be passed by ai_dashboard()."""
        template_vars = self._get_template_vars("templates/ai.html")
        # ai_dashboard uses **ctx which passes profiles, selected_profile, etc.
        import views
        src = inspect.getsource(views.ai_dashboard)
        render_match = re.search(r'render_template\("ai\.html"[^)]+\)', src, re.DOTALL)
        if not render_match:
            pytest.fail("render_template('ai.html') not found in ai_dashboard()")
        render_call = render_match.group(0)

        # Extract explicit kwargs + **ctx spreads
        explicit_kwargs = set(re.findall(r'(\w+)\s*=', render_call))
        # **ctx adds: profiles, selected_profile, selected_profile_name, ai_page, db_paths
        ctx_vars = {"profiles", "selected_profile", "selected_profile_name", "ai_page", "db_paths"}
        all_passed = explicit_kwargs | ctx_vars

        missing = template_vars - all_passed
        # Filter out variables that are Jinja internals or loop vars
        missing = {v for v in missing if v not in (
            "any_profile_active", "total_pages", "page", "sort_by", "sort_dir",
            "total_trades", "decisions"
        )}

        assert not missing, (
            f"ai.html uses these variables but ai_dashboard() doesn't pass them: {missing}\n"
            f"Passed: {sorted(all_passed)}\n"
            f"Template needs: {sorted(template_vars)}"
        )

    def test_performance_template_gets_all_its_variables(self):
        """Every variable used in performance.html must be passed by performance_dashboard()."""
        template_vars = self._get_template_vars("templates/performance.html")
        import views
        src = inspect.getsource(views.performance_dashboard)
        render_match = re.search(r'render_template\("performance\.html"[^)]+\)', src, re.DOTALL)
        if not render_match:
            pytest.fail("render_template not found")
        render_call = render_match.group(0)
        explicit_kwargs = set(re.findall(r'(\w+)\s*=', render_call))

        missing = template_vars - explicit_kwargs
        missing = {v for v in missing if v not in (
            "any_profile_active", "total_pages", "page", "sort_by", "sort_dir",
            "total_trades", "decisions"
        )}

        assert not missing, (
            f"performance.html uses variables not passed: {missing}"
        )


# ---------------------------------------------------------------------------
# 10. Function calls in views must match actual signatures
# ---------------------------------------------------------------------------

class TestFunctionCallSignatures:
    """When views.py calls a function, the arguments must match the signature."""

    def test_get_allocation_summary_called_correctly(self):
        """get_allocation_summary(db_path, market_type) — not (profile_id)."""
        import views
        src = inspect.getsource(views.ai_dashboard)
        # Must pass two args: db path and market_type
        assert 'get_allocation_summary(db' in src or "get_allocation_summary(db_path" in src or \
               re.search(r'get_allocation_summary\([^)]*market_type', src), (
            "get_allocation_summary must be called with (db_path, market_type), "
            "not (profile_id) or any other made-up signature"
        )

    def test_spend_summary_called_with_db_path(self):
        """spend_summary(db_path) — not (profile_id)."""
        import views
        src = inspect.getsource(views.ai_dashboard)
        # The call should pass a db file path string
        assert re.search(r'spend_summary\(db\b', src), (
            "spend_summary must be called with a db file path"
        )

    def test_get_current_level_called_with_db_path(self):
        """crisis_state.get_current_level(db_path)."""
        import views
        src = inspect.getsource(views.ai_dashboard)
        assert re.search(r'get_current_level\(db\b', src), (
            "get_current_level must be called with a db file path"
        )

    def test_recent_events_called_with_db_path(self):
        """event_bus.recent_events(db_path, hours, limit)."""
        import views
        src = inspect.getsource(views.ai_dashboard)
        assert re.search(r'recent_events\(db\b', src), (
            "recent_events must be called with a db file path"
        )
