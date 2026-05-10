"""Snake_case guardrail for sector / factor / scenario identifiers
that surface to users.

The existing `test_no_snake_case_in_api_responses.py` only flags
PARAM_BOUNDS keys. That left three other identifier families unguarded:

  - Sector codes      ('tech', 'comm_services', 'consumer_disc', ...)
  - Risk factor codes ('sector_tech', 'style_smallcap', 'Mkt-RF', ...)
  - Stress scenario IDs ('2008_lehman', '2020_covid', ...)

Each is a snake_case (or kebab-case) internal key that MUST be routed
through `display_name` before rendering. This test enforces:

  1. Every identifier in those three families has an explicit
     `display_name` mapping (no fallback for these load-bearing IDs —
     fallbacks are easy to drift away from).

  2. The rendered HTML of the AI / Performance / Dashboard pages
     does NOT contain any of those raw IDs in visible text positions
     (visible text = HTML with scripts, styles, attributes, and
     `<option value="">` stripped, matching the existing test's
     stripping rules).

This test would have caught the leaks introduced when the Item 2a
portfolio risk UI shipped without `| display_name` filters on factor
names and scenario IDs.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from typing import List, Set
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Canonical identifier sets (sourced from the modules that own them so
# adding a new sector / factor / scenario auto-extends coverage)
# ---------------------------------------------------------------------------

def _sector_codes() -> Set[str]:
    """Sector codes that appear in by_sector tables, sector rotation,
    and concentration warnings. Hard-coded list because the canonical
    source (sector_classifier) returns them but doesn't expose a list."""
    return {
        "tech", "finance", "energy", "healthcare", "industrial",
        "industrials", "consumer_disc", "consumer_staples",
        "utilities", "materials", "real_estate", "comm_services",
        "communication",
    }


def _factor_ids() -> Set[str]:
    """Risk model factor IDs (sector_*, style_*, French academic codes)."""
    from portfolio_risk_model import (
        SECTOR_ETFS, STYLE_ETFS, FRENCH_FACTORS,
    )
    return set(SECTOR_ETFS.keys()) | set(STYLE_ETFS.keys()) | set(FRENCH_FACTORS)


def _scenario_ids() -> Set[str]:
    """Historical stress scenario IDs."""
    from risk_stress_scenarios import SCENARIOS
    return {s.name for s in SCENARIOS}


# ---------------------------------------------------------------------------
# Test 1: every identifier has an explicit display_name entry
# ---------------------------------------------------------------------------

class TestEveryIdentifierHasDisplayName:
    """The display_name fallback (`tech` → 'Tech') is acceptable for
    lots of IDs but not for these three families: they show up in
    table headers and chart labels and the fallback drift is a leak
    waiting to happen. Force explicit entries."""

    def test_every_sector_code_has_display_name(self):
        from display_names import _DISPLAY_NAMES
        missing = [s for s in _sector_codes() if s not in _DISPLAY_NAMES]
        assert not missing, (
            f"Sector codes missing explicit display_name entries: "
            f"{sorted(missing)}. Add them to _DISPLAY_NAMES in "
            f"display_names.py."
        )

    def test_every_factor_id_has_display_name(self):
        from display_names import _DISPLAY_NAMES
        missing = [f for f in _factor_ids() if f not in _DISPLAY_NAMES]
        assert not missing, (
            f"Factor IDs missing explicit display_name entries: "
            f"{sorted(missing)}. Add them to _DISPLAY_NAMES."
        )

    def test_every_scenario_id_has_display_name(self):
        from display_names import _DISPLAY_NAMES
        missing = [s for s in _scenario_ids() if s not in _DISPLAY_NAMES]
        assert not missing, (
            f"Stress scenario IDs missing explicit display_name "
            f"entries: {sorted(missing)}. Add them to _DISPLAY_NAMES."
        )


# ---------------------------------------------------------------------------
# Test 2: rendered pages don't contain raw IDs in visible text
# ---------------------------------------------------------------------------

def _strip_html_to_visible_text(html: str) -> str:
    """Same stripping rules as test_no_snake_case_in_api_responses."""
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html,
                   flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html,
                   flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'(<option\b[^>]*\s)value="[^"]*"',
                   r"\1value=\"\"", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">"))
    html = re.sub(r"\s+", " ", html)
    return html


def _logged_in_client():
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        sess["_fresh"] = True
    return client, app


class TestPageRendersTranslateRawIds:
    """Hit the rendered pages and assert no raw identifier from the
    three families appears in visible text. Skips IDs that are real
    English words ('energy', 'healthcare', 'utilities', 'materials')
    since those overlap with normal page copy."""

    # IDs that are also real English words — they appear in unrelated
    # copy ("energy sector", "healthcare stocks") and can't be flagged
    # mechanically. Their display_name entry is still required (test 1)
    # but they're skipped here.
    #
    # NOT exempt: snake_case codes like `comm_services` / `consumer_disc`
    # / `consumer_staples` / `real_estate` / `sector_*` / `style_*` /
    # `2008_lehman` etc. Those have no plain-English collisions so any
    # appearance in visible text is a real leak.
    AMBIGUOUS_IDS = {
        "tech", "finance", "energy", "healthcare", "utilities",
        "materials", "communication", "industrial", "industrials",
        "Mom",   # 3-letter, false positives in body text
    }

    @pytest.fixture(autouse=True)
    def _seeded_db_and_user(self, tmp_path, monkeypatch):
        """Real temp SQLite DB seeded with the schemas the routes
        touch (users, trading_profiles, ...). The
        previous approach patched per-function and turned into
        whack-a-mole — every new DB call required another patch.
        Seeding a real DB once means the routes execute their actual
        code path."""
        # Point config + per-module DB_PATH consts at a fresh file
        db_file = str(tmp_path / "test_main.db")
        monkeypatch.setenv("DB_PATH", db_file)
        # Several modules cache config.DB_PATH at import time. Patch
        # the module-level reference too so they see the temp path.
        import config
        monkeypatch.setattr(config, "DB_PATH", db_file)
        import models
        monkeypatch.setattr(models, "DB_PATH", db_file, raising=False)
        import journal
        # journal._get_conn falls through to config.DB_PATH at call
        # time so no patch needed there.
        # Initialize main + journal schemas
        models.init_user_db(db_file)
        journal.init_db(db_file)
        # Seed one user + one profile so the dashboard has data
        import sqlite3
        conn = sqlite3.connect(db_file)
        conn.execute(
            "INSERT INTO users (id, email, password_hash, role, created_at) "
            "VALUES (1, 'test@example.com', 'x', 'user', datetime('now'))"
        )
        # Lean insert — relies on column defaults for everything not
        # listed (most columns have NOT NULL DEFAULT in init_user_db).
        conn.execute(
            "INSERT INTO trading_profiles "
            "(id, user_id, name, market_type) "
            "VALUES (1, 1, 'Test', 'midcap')"
        )
        conn.commit()
        conn.close()

        user_obj = type("U", (), {})()
        user_obj.is_authenticated = True
        user_obj.is_active = True
        user_obj.is_anonymous = False
        user_obj.effective_user_id = 1
        user_obj.id = 1
        user_obj.email = "test@example.com"
        user_obj.get_id = lambda: "1"

        profile = {
            "id": 1, "user_id": 1, "name": "Test", "enabled": 1,
            "max_position_pct": 0.10, "stop_loss_pct": 0.03,
            "take_profit_pct": 0.10,
        }

        # Seed sample portfolio-risk + concentration data so the
        # template actually renders the panels under test. Without
        # this, the leak section gets skipped and the test fails open.
        portfolio_risk_sample = [{
            "profile_id": 1, "profile_name": "Test",
            "snapshot_at": "2026-05-01T12:00:00",
            "equity": 100000, "sigma_pct": 1.5,
            "var_95_dollars": 2500, "var_99_dollars": 3500,
            "es_95_dollars": 3100, "mc_var_95_dollars": 2600,
            "n_symbols": 5,
            "factor_exposures": [
                ("sector_tech", 0.8), ("style_smallcap", -0.4),
                ("Mkt-RF", 0.95), ("SMB", -0.2),
            ],
            "grouped_share": {
                "sectors": 60.0, "styles": 25.0,
                "french": 10.0, "idio": 5.0,
            },
            "scenarios": [{
                "scenario": "2008_lehman",
                "description": "Lehman / GFC peak",
                "severity": "catastrophic",
                "total_pnl_pct": -0.45, "total_pnl_dollars": -45000,
                "worst_day_pct": -0.18, "worst_day_date": "2008-09-29",
                "max_drawdown_pct": -0.48, "approximation_quality": "medium",
            }, {
                "scenario": "2020_covid",
                "description": "COVID crash", "severity": "severe",
                "total_pnl_pct": -0.40, "total_pnl_dollars": -40000,
                "worst_day_pct": -0.14, "worst_day_date": "2020-03-16",
                "max_drawdown_pct": -0.42, "approximation_quality": "high",
            }],
        }]
        long_short_sample = [{
            "profile_id": 1, "profile_name": "Test",
            "shorts_enabled": True, "target_short_pct": 0.5,
            "target_book_beta": 0.0,
            "current_short_share": 0.4, "balance_state": "pass",
            "current_book_beta": 0.1, "book_beta_delta": 0.1,
            "kelly_long": None, "kelly_short": None,
            "drawdown_pct": 5.0, "drawdown_scale": 0.85,
            "risk_budget": None, "exposure": {},
            "concentration_warnings": [
                {"sector": "comm_services", "gross_pct": 32.0},
                {"sector": "consumer_disc", "gross_pct": 31.0},
            ],
            "num_positions": 5,
        }]
        # Specialist veto_stats — seed-data must include the snake_case
        # names that get displayed in the Veto Activity table. If this
        # fixture didn't include them, the panel would never render in
        # the test env and the snake-case scanner would miss any leak
        # there. Discovered 2026-05-10 — `pattern_recognizer` etc. were
        # leaking into the AI Operations page because the test never
        # rendered the Veto Activity panel.
        veto_stats_sample = {
            "window_days": 7,
            "total_vetoes_effective": 0,
            "total_vetoes_claimed": 60,
            "by_specialist": [
                {"name": "pattern_recognizer", "total": 718, "vetoes": 54,
                 "veto_rate_pct": 7.5, "has_authority": False},
                {"name": "sentiment_narrative", "total": 772, "vetoes": 6,
                 "veto_rate_pct": 0.8, "has_authority": False},
                {"name": "adversarial_reviewer", "total": 1320, "vetoes": 0,
                 "veto_rate_pct": 0.0, "has_authority": True},
                {"name": "earnings_analyst", "total": 508, "vetoes": 0,
                 "veto_rate_pct": 0.0, "has_authority": False},
                {"name": "risk_assessor", "total": 1249, "vetoes": 0,
                 "veto_rate_pct": 0.0, "has_authority": True},
            ],
        }
        # Patch only the per-page awareness builders (so the panels
        # under test get the seed shapes the templates expect).
        # Everything else uses the real seeded DB. Also patch the
        # journal helper that ai_dashboard calls for veto_stats so the
        # Veto Activity panel renders.
        patches = [
            patch("flask_login.utils._get_user", return_value=user_obj),
            patch("views._build_portfolio_risk_awareness",
                   return_value=portfolio_risk_sample),
            patch("views._build_long_short_awareness",
                   return_value=long_short_sample),
            patch("journal.get_specialist_veto_stats",
                   return_value=veto_stats_sample),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    def _flagged_ids(self) -> Set[str]:
        return ((_sector_codes() | _factor_ids() | _scenario_ids())
                - self.AMBIGUOUS_IDS)

    def _scan_page(self, route: str):
        client, _ = _logged_in_client()
        resp = client.get(route)
        # Hard-fail on non-200 — this is a guardrail, not a smoke test.
        # If the route can't render in the test env, fix the seed data.
        assert resp.status_code == 200, (
            f"{route} returned {resp.status_code} in test env. "
            f"Body excerpt: "
            f"{resp.data.decode('utf-8', 'ignore')[:500]}"
        )
        return _strip_html_to_visible_text(
            resp.data.decode("utf-8", "ignore")
        )

    @pytest.mark.parametrize("route", ["/ai", "/performance", "/dashboard"])
    def test_page_visible_text_has_no_raw_ids(self, route):
        text = self._scan_page(route)

        # Word-boundary matches only — avoids false positives on
        # substrings like 'lowvol' inside 'low_vol_etf'.
        leaks = []
        for raw_id in self._flagged_ids():
            pattern = r"\b" + re.escape(raw_id) + r"\b"
            if re.search(pattern, text):
                leaks.append(raw_id)
        assert not leaks, (
            f"{route} rendered visible text contains raw identifiers "
            f"that should have been routed through `| display_name`: "
            f"{sorted(leaks)}. Either:\n"
            f"  - Add `| display_name` to the template, or\n"
            f"  - Resolve the label server-side before passing to the\n"
            f"    template, or\n"
            f"  - If the ID is a legitimate body-text word, add it to\n"
            f"    AMBIGUOUS_IDS."
        )


# ---------------------------------------------------------------------------
# WIDE-COVERAGE: any snake_case in rendered visible text
# ---------------------------------------------------------------------------

# Snake_case substrings legitimately allowed in visible text (technical
# terms, code references in tooltips, file paths in error contexts,
# brand/code names that happen to look like snake_case, etc.).
#
# This list MUST grow over time but every entry needs justification.
# Adding a snake_case identifier from a NEW data source SHOULD NOT be
# the way to fix a leak — fix the leak with `| humanize` first.
SNAKE_CASE_VISIBLE_ALLOWLIST = {
    # File paths shown in operator tooltips/explanations
    "ai_predictions",       # "the ai_predictions table" referenced in operator-facing copy
    "trading_profiles",     # ditto
    "task_runs",            # operator-facing reference to task table
    "decision_log",         # historical reference (table being deleted)
    "activity_log",         # operator-facing reference
    "daily_snapshots",      # ditto
    # Technical/financial terms with underscores in operator copy
    "max_pain",             # options term
    "open_interest",        # options term
    "iv_rank",              # vol metric
    "win_rate",             # tooltip language
    "stop_loss",            # parameter language
    "take_profit",          # parameter language
    "ai_confidence",        # column header explanation
    "long_only",            # state label rendered as "long-only"
    # Common humanized-snake outputs that still contain underscores
    # because humanize() preserves them inside the title-cased token
    # (e.g., "Active" / "Pending review" — these are post-humanize words
    # so they wouldn't match the regex anyway; not allowlisted unless seen)
    # Period/window labels
    "first_friday",
    "last_friday",
}

# Tokens that LOOK like snake_case in visible text but are real identifiers
# people refer to verbatim — symbol names, model IDs, etc.
SNAKE_CASE_PATTERN = re.compile(
    r"\b([a-z][a-z0-9]*(?:_[a-z][a-z0-9]+){1,5})\b"
)


@pytest.fixture
def seeded_app(_seeded_db_and_user):
    """Reuse the seeded DB + user fixture that already exists on
    TestPageRendersTranslateRawIds. The fixture above (autouse on the
    class) doesn't help out-of-class, so we declare a thin wrapper."""
    yield


class TestNoArbitrarySnakeCaseInVisibleText:
    """Stronger version of the per-id-family check — scans rendered
    HTML for ANY snake_case-looking token in visible text. New leaks
    from data sources we haven't enumerated yet (e.g., specialist
    names, future identifier families) fail this test by default."""

    # Inherit the seeded fixture that the class above sets up.
    _seeded_db_and_user = TestPageRendersTranslateRawIds._seeded_db_and_user

    @pytest.mark.parametrize("route", ["/ai", "/performance", "/dashboard"])
    def test_no_unexplained_snake_case_in_visible_text(self, route):
        client, _ = _logged_in_client()
        resp = client.get(route)
        assert resp.status_code == 200, resp.data[:300]

        text = _strip_html_to_visible_text(
            resp.data.decode("utf-8", "ignore")
        )

        # Find every snake_case-looking token in visible text
        matches = set(SNAKE_CASE_PATTERN.findall(text))
        # Drop allowlisted ones
        leaks = sorted(matches - SNAKE_CASE_VISIBLE_ALLOWLIST)

        assert not leaks, (
            f"{route} rendered VISIBLE text contains raw snake_case "
            f"tokens that should be humanized:\n"
            f"  {leaks}\n\n"
            f"Each of these is shown to the user as e.g. "
            f"'pattern_recognizer' instead of 'Pattern Recognizer'. "
            f"Pipe the value through `| humanize` in the template, "
            f"or resolve to a human label server-side. "
            f"If a token is intentionally shown verbatim (e.g., a "
            f"file path or technical term), add it to "
            f"SNAKE_CASE_VISIBLE_ALLOWLIST in this test with a "
            f"comment explaining why."
        )
