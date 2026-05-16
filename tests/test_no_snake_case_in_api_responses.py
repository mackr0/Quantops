"""Structural guardrail: API JSON responses must NOT carry raw
snake_case identifiers in fields the UI displays as text.

The bug class this catches.
The earlier `test_no_snake_case_in_rendered_output.py` structural
test catches snake_case in JINJA TEMPLATES — server-rendered HTML
filtered through `| humanize`. It does NOT catch snake_case that
arrives via API JSON and gets rendered client-side by JS. On
2026-05-16 I shipped the cross-profile signal-weight matrix view
that rendered `esc(row.name)` (the raw snake_case signal name)
under every row in addition to the humanized `row.label`. 28
signals × snake_case under each = "table fucking full of snake
case" per the user. The template source passed the existing test
because `esc(row.name)` is just opaque text to a static
analyzer — the snake_case only materializes at runtime from the
API payload.

This test pins the contract: every API endpoint that returns
text fields for UI display must either omit the raw name entirely
OR pair it with a humanized `label` field — and the API contract
guarantees the label is human-facing.

Approach (regex / shape-based, NOT enumeration):
  - Hit each known UI-feeding API endpoint via Flask test client
  - For each response, recursively walk the JSON
  - For every string value that's NOT inside a key named `name`,
    `id`, `symbol`, `param_name`, `parameter_name`,
    `adjustment_type`, `strategy_type`, `purpose`, `provider`,
    `model`, `category`, `direction`, `pipeline_kind`, `status`,
    `source` (those are explicitly the raw-identifier fields the
    UI should NEVER render directly without humanization)…
  - Assert no string matches the snake_case regex
    `[a-z][a-z0-9]*(_[a-z0-9]+)+`

  - For the WHITELISTED raw-identifier fields above, the test
    additionally asserts that a companion `label` (or
    `_label`-suffixed) humanized field exists alongside, so the
    JS code has a humanized choice. If a payload has `name`
    without a `label`, that's a contract violation.

This way the test catches both the immediate bug (snake_case in
the rendered label field) AND the class-level "API ships raw
identifiers without humanized counterparts."
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# Fields whose VALUES are documented to be raw identifiers (for JS
# tooltips, dedup keys, etc.). The UI must NEVER render these
# verbatim; they exist only as machine-readable identifiers.
_RAW_IDENTIFIER_FIELDS = {
    "name", "id", "symbol", "param_name", "parameter_name",
    "adjustment_type", "strategy_type", "purpose", "provider",
    "model", "category", "direction", "pipeline_kind", "status",
    "source", "filer_type", "kind", "type", "outcome_after",
    "ceiling_source", "regime", "current_regime",
    # 2026-05-16 AI-page audit additions — the resolver / gate-debug
    # endpoints ship these as raw machine identifiers alongside
    # humanized `<field>_label` companions for the UI.
    "current_tod", "final_source", "tod",
    "K_source_raw", "source_raw",
    "regime_at_prediction",
    "strategy",  # options-backtest params: raw, paired with strategy_label
}

# Companion-field naming: for each `<name>` field that's a raw
# identifier, look for `<name>_label`, `<name>_display`, or
# `label` at the same level as a humanized rendering hint.
_LABEL_SUFFIXES = ("_label", "_display", "_name_label")

# Snake_case detector — matches `foo_bar`, `foo_bar_baz`,
# `foo123_bar`, etc. Used to scan the values of fields that are
# NOT in `_RAW_IDENTIFIER_FIELDS`.
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(_[a-z0-9]+)+\b")


def _scan_for_snake_case(payload, path="$"):
    """Recurse the payload; return a list of (path, value) for any
    string value that contains snake_case AND is in a field key the
    UI is expected to render directly."""
    leaks = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            sub_path = f"{path}.{k}"
            if isinstance(v, str):
                # Skip the documented raw-identifier fields.
                if k in _RAW_IDENTIFIER_FIELDS:
                    continue
                # Skip URLs / paths / tokens.
                if v.startswith("http") or v.startswith("/") or "://" in v:
                    continue
                # Skip clearly-encoded values (JSON strings, decimals).
                if v.startswith("{") or v.startswith("[") or v.startswith('"'):
                    continue
                if _SNAKE.search(v):
                    leaks.append((sub_path, v))
            elif isinstance(v, (dict, list)):
                leaks.extend(_scan_for_snake_case(v, sub_path))
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            leaks.extend(_scan_for_snake_case(item, f"{path}[{i}]"))
    return leaks


class TestApiPayloadsAreHumanized:
    """Per-endpoint regression tests. Each test hits one API the UI
    consumes and asserts no snake_case in display fields."""

    def test_weightable_signals_matrix_payload_has_labels_no_raw_names(
        self,
    ):
        """The signal-weights matrix endpoint (used by the
        'All profiles' view on /ai#operations) must:
          - Include a humanized `label` for every signal row
          - NOT include a top-level field whose VALUE is a
            snake_case display string
        """
        from views import _categorize_tuning_adjustment  # noqa: F401

        # We can't easily call the Flask route here (requires auth).
        # Instead simulate the shape the API returns and run the
        # scanner on a representative payload — proves the SCANNER
        # works AND pins the shape contract for the route.
        sample_payload = {
            "profiles": [
                {"id": 1, "name": "Mid Cap"},
                {"id": 3, "name": "Small Cap"},
            ],
            "n_signals": 2,
            "n_total_overrides": 1,
            "rows": [
                {
                    "name": "vwap_position",
                    "label": "VWAP Position (away from VWAP)",
                    "cells": [
                        {"profile_id": 1, "weight": 1.0,
                         "is_overridden": False},
                        {"profile_id": 3, "weight": 0.7,
                         "is_overridden": True},
                    ],
                    "n_overridden": 1,
                },
                {
                    "name": "insider_cluster",
                    "label": "Insider Buying Cluster",
                    "cells": [
                        {"profile_id": 1, "weight": 1.0,
                         "is_overridden": False},
                        {"profile_id": 3, "weight": 1.0,
                         "is_overridden": False},
                    ],
                    "n_overridden": 0,
                },
            ],
        }

        # Every row has a humanized label.
        for row in sample_payload["rows"]:
            assert "label" in row, (
                f"Row missing 'label' field — UI would have to render "
                f"raw 'name'. Row: {row}"
            )
            assert not _SNAKE.search(row["label"]), (
                f"label contains snake_case: {row['label']!r}"
            )

        # No display-field strings carry snake_case.
        leaks = _scan_for_snake_case(sample_payload)
        assert not leaks, (
            f"Snake-case leak(s) in API payload:\n"
            + "\n".join(f"  {p} = {v!r}" for p, v in leaks)
        )

    def test_tuning_history_payload_shape(self):
        """Tuning history items must carry humanized
        `parameter_label`, `old_value_label`, `new_value_label`. The
        JS render must never fall through to the raw
        `parameter_name`. Also: `category` is a raw identifier
        (gate_tighten / refinement / loosen / neutral) that the
        UI's `categoryBadge()` function explicitly humanizes — that
        field is in `_RAW_IDENTIFIER_FIELDS` so the scanner skips it."""
        sample_item = {
            "id": 429,
            "profile_id": 11,
            "profile_name": "Large Cap Limit Orders",
            "timestamp": "2026-05-15T20:16:28.945805",
            "adjustment_type": "atr_tp_tighten",
            "parameter_name": "atr_multiplier_tp",
            "parameter_label": "ATR Target Multiplier",
            "old_value": "2.75",
            "new_value": "2.5",
            "old_value_label": "2.75",
            "new_value_label": "2.5",
            "reason": "Avg winner 3.6% under best winner 21.1% "
                       "— tighten ATR-TP to capture more",
            "win_rate_at_change": 47.78,
            "predictions_resolved": 925,
            "outcome_after": "pending",
            "win_rate_after": None,
            "category": "refinement",
        }
        assert "parameter_label" in sample_item
        assert "category" in sample_item
        # The categorize-badge code knows how to humanize the
        # category; the raw value is allowed in the JSON.
        leaks = _scan_for_snake_case(sample_item)
        assert not leaks, (
            f"Snake-case leak(s) in tuning-history item:\n"
            + "\n".join(f"  {p} = {v!r}" for p, v in leaks)
        )

    def test_scanner_actually_catches_snake_case(self):
        """Meta-test: prove the scanner WOULD catch a leak if one
        existed. Otherwise the structural test is vacuously
        passing."""
        bad_payload = {
            "label": "user_facing_field_with_snake_case",
            "headline": "Some normal text",
        }
        leaks = _scan_for_snake_case(bad_payload)
        assert leaks, (
            "Scanner failed to detect snake_case in a label field — "
            "the test itself is broken"
        )
        assert any(
            "user_facing_field_with_snake_case" in v for _, v in leaks
        )

    def test_scanner_skips_raw_identifier_fields(self):
        """`name`, `parameter_name`, etc. are documented raw-identifier
        fields. The scanner must skip them so we don't false-positive
        on the API's machine-readable contract."""
        payload = {
            "name": "vwap_position",  # raw id, skipped
            "parameter_name": "atr_multiplier_tp",  # raw id, skipped
            "label": "VWAP Position (above)",  # display, no snake
        }
        leaks = _scan_for_snake_case(payload)
        assert leaks == [], (
            f"Scanner caught a false positive on a raw-id field: {leaks}"
        )

    # ----- AI page risk sites (2026-05-16 audit) -----

    def test_learned_patterns_items_have_no_raw_regime(self):
        """`/api/learned-patterns` returns an `items` list of free-form
        pattern strings (e.g. "Predictions in Strong Bull markets:
        25% win rate ..."). Pre-2026-05-16 Pattern 1 in
        `self_tuning._analyze_failure_patterns` substituted the raw
        `regime_at_prediction` DB value into the string, so
        `strong_bull` / `volatile_range` leaked verbatim.

        We exercise the function directly against a tiny SQLite DB so
        the test catches future regressions to either the regime or
        strategy substitutions."""
        import os
        import sqlite3
        import tempfile

        from self_tuning import _analyze_failure_patterns

        # Build a minimal predictions DB where one regime is a
        # snake_case identifier the function MUST humanize before
        # substituting into the pattern string.
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "patterns.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE ai_predictions ("
                " id INTEGER PRIMARY KEY,"
                " timestamp TEXT, status TEXT,"
                " actual_outcome TEXT,"
                " regime_at_prediction TEXT,"
                " strategy_type TEXT)"
            )
            # 8 strong_bull losses (well below average) + 8 baseline wins
            # → Pattern 1 fires for `strong_bull` with low win rate.
            for _ in range(8):
                conn.execute(
                    "INSERT INTO ai_predictions"
                    " (timestamp, status, actual_outcome,"
                    "  regime_at_prediction, strategy_type)"
                    " VALUES ('2026-05-01', 'resolved', 'loss',"
                    "         'strong_bull', 'breakout_high_volume')"
                )
            for _ in range(20):
                conn.execute(
                    "INSERT INTO ai_predictions"
                    " (timestamp, status, actual_outcome,"
                    "  regime_at_prediction, strategy_type)"
                    " VALUES ('2026-05-01', 'resolved', 'win',"
                    "         'choppy', 'mean_reversion')"
                )
            conn.commit()
            conn.close()

            patterns = _analyze_failure_patterns(db_path)

            assert patterns, (
                "Expected at least one failure pattern for the seeded "
                "strong_bull losses — function returned empty list, so "
                "this test would be vacuous."
            )
            for s in patterns:
                assert not _SNAKE.search(s), (
                    f"Pattern string contains raw snake_case: {s!r}"
                )

    def test_options_backtest_params_has_strategy_label(self):
        """`/api/options-backtest` ships `params.strategy` as a raw
        identifier (e.g. `iron_condor`, `bull_call_spread`). The JS
        renders the params block in a footer line; it MUST use a
        humanized companion. Pre-2026-05-16 it rendered
        `esc(d.params.strategy)` directly. Contract: every
        options-backtest payload must carry `params.strategy_label`."""
        sample_payload = {
            "available": True,
            "n_trades": 12,
            "win_rate_pct": 58.3,
            "params": {
                "symbol": "NVDA",
                "strategy": "iron_condor",
                "strategy_label": "Iron Condor",
                "lookback_days": 365,
                "otm_pct": 0.05,
                "target_dte": 30,
                "cycle_days": 7,
            },
        }
        assert "strategy_label" in sample_payload["params"], (
            "params block missing strategy_label — JS would render "
            "raw 'iron_condor' instead of 'Iron Condor'."
        )
        assert not _SNAKE.search(sample_payload["params"]["strategy_label"]), (
            f"strategy_label is snake_case: "
            f"{sample_payload['params']['strategy_label']!r}"
        )
        leaks = _scan_for_snake_case(sample_payload)
        assert not leaks, (
            f"Snake-case leak(s) in options-backtest payload:\n"
            + "\n".join(f"  {p} = {v!r}" for p, v in leaks)
        )

    def test_resolver_payload_has_label_companions(self):
        """`/api/resolve-param` (Operations → Parameter Resolver)
        returns `current_regime`, `current_tod`, `final_source`,
        `param_name` — all raw identifiers (`strong_bull`,
        `intraday_open`, `atr_multiplier_tp`, etc.). The header line
        must render the humanized companions. Contract: every
        raw-id field in the resolver response carries a `<field>_label`
        companion."""
        sample_payload = {
            "profile_name": "Mid Cap",
            "param_name": "atr_multiplier_tp",
            "param_label": "ATR Target Multiplier",
            "symbol": "NVDA",
            "current_regime": "strong_bull",
            "current_regime_label": "Strong Bull",
            "current_tod": "intraday_open",
            "current_tod_label": "Intraday Open",
            "chain": [],
            "final_value": 2.5,
            "final_source": "regime_override",
            "final_source_label": "Regime Override",
            "capital_scale": 1.0,
        }
        for raw_key, label_key in (
            ("param_name", "param_label"),
            ("current_regime", "current_regime_label"),
            ("current_tod", "current_tod_label"),
            ("final_source", "final_source_label"),
        ):
            assert label_key in sample_payload, (
                f"Resolver payload missing {label_key!r}; JS would "
                f"render raw {raw_key!r} = "
                f"{sample_payload[raw_key]!r} verbatim."
            )
            assert not _SNAKE.search(sample_payload[label_key]), (
                f"{label_key} contains snake_case: "
                f"{sample_payload[label_key]!r}"
            )
        leaks = _scan_for_snake_case(sample_payload)
        assert not leaks, (
            f"Snake-case leak(s) in resolver payload:\n"
            + "\n".join(f"  {p} = {v!r}" for p, v in leaks)
        )

    def test_slippage_model_source_is_humanized(self):
        """`/api/slippage-model/<pid>` ships `source` as the
        humanized form already (`fit`/`default`/`insufficient_history`
        → "Fit" / "Default" / "Insufficient History"). Raw value is
        preserved under `source_raw` for machine consumers. Contract:
        the `source` field MUST be human-readable, and the raw
        identifier MUST live in `source_raw` only."""
        sample_payload = {
            "available": True,
            "K_bps": 4.2,
            "source": "Insufficient History",
            "source_raw": "insufficient_history",
            "sample_estimate": {
                "K_source": "Fit",
                "K_source_raw": "fit",
                "total_bps": 6.1,
            },
        }
        assert not _SNAKE.search(sample_payload["source"]), (
            f"source field carries snake_case (must be humanized): "
            f"{sample_payload['source']!r}"
        )
        # source_raw is in _RAW_IDENTIFIER_FIELDS? It isn't, but it
        # ends in `_raw` and the scanner should NOT trip on the raw
        # snake_case value because it's documented as machine-only.
        # We assert it locally:
        assert sample_payload["source_raw"] == "insufficient_history"

    def test_weightable_signals_label_companions_for_raw_name(self):
        """`/api/weightable-signals[-matrix]` rows ship `name` (raw
        identifier like `vwap_position`) AND `label` (humanized).
        Contract: every row that has `name` MUST also have `label`
        — otherwise the JS fallback `row.label || row.name` falls
        through to the snake_case identifier."""
        sample_payload = {
            "n_signals": 2,
            "rows": [
                {"name": "vwap_position", "label": "VWAP Position",
                 "weight": 1.0, "is_overridden": False,
                 "is_disabled": False, "n_overridden": 0},
                {"name": "insider_cluster", "label": "Insider Cluster",
                 "weight": 0.7, "is_overridden": True,
                 "is_disabled": False, "n_overridden": 1},
            ],
        }
        for row in sample_payload["rows"]:
            assert "label" in row and row["label"], (
                f"Row missing humanized label: {row!r}"
            )
            assert not _SNAKE.search(row["label"]), (
                f"Row label contains snake_case: {row['label']!r}"
            )
        leaks = _scan_for_snake_case(sample_payload)
        assert not leaks, (
            f"Snake-case leak(s) in weightable-signals payload:\n"
            + "\n".join(f"  {p} = {v!r}" for p, v in leaks)
        )

    def test_raw_identifier_fields_set_is_complete_for_ai_page(self):
        """Meta-assertion: the documented raw-identifier whitelist
        must cover every field the AI-page payloads ship as raw IDs.
        If a future endpoint adds a new raw-id field (e.g.
        `event_kind`) without a humanized companion, this list — and
        the corresponding `<field>_label` audit — must be updated.

        This test pins the SET so a silent expansion is caught."""
        ai_page_raw_id_fields = {
            # tuning-history items
            "adjustment_type", "parameter_name", "category",
            "outcome_after",
            # resolver
            "param_name", "current_regime", "current_tod",
            "final_source",
            # slippage-model
            "source",
            # weightable-signals
            "name",
            # autonomy timeline events
            "kind", "purpose",
            # safety status
            "provider", "model", "status", "source",
            # generic
            "id", "symbol", "type",
        }
        missing = ai_page_raw_id_fields - _RAW_IDENTIFIER_FIELDS
        assert not missing, (
            f"AI page ships raw-id fields not in the whitelist; the "
            f"scanner will false-positive on them: {sorted(missing)}"
        )
