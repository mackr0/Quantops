"""Wheel-symbols corruption class killed (P1.5, 2026-07-26).

10 of 13 profiles had `wheel_symbols = '["[]"]'` — the wheel iterated
a "ticker" named []. Mechanism: the DB stores JSON text ('[]'), the
settings template only joins LIST values (`is iterable and is not
string`), and — unlike custom_watchlist, which every profile-dict
builder parses to a list — wheel_symbols was never parsed. So the
textarea displayed the raw text `[]`, and the next settings save
split it on commas and stored `'["[]"]'`.

Pinned here: profile-dict builders return parsed lists; the writer
and the parser both refuse non-ticker tokens; the corrupt legacy
value parses to []; and the exact round-trip that created the
corruption now converges to a clean empty list.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models import _parse_wheel_symbols, _valid_wheel_tickers  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestParser:
    def test_legacy_corrupt_value_parses_to_empty(self):
        assert _parse_wheel_symbols('["[]"]') == []

    def test_clean_json_list(self):
        assert _parse_wheel_symbols('["MSFT", "aapl", "KO"]') == [
            "MSFT", "AAPL", "KO"]

    def test_real_ticker_shapes_survive(self):
        assert _parse_wheel_symbols('["BRK.B", "BF-B", "GOOGL"]') == [
            "BRK.B", "BF-B", "GOOGL"]

    def test_junk_tokens_dropped_not_traded(self):
        assert _parse_wheel_symbols('["[]", "{", "AAPL", "", "123"]') == [
            "AAPL"]

    def test_none_and_garbage_inputs(self):
        assert _parse_wheel_symbols(None) == []
        assert _parse_wheel_symbols("") == []
        assert _parse_wheel_symbols("not json") == []
        assert _parse_wheel_symbols('{"a": 1}') == []


class TestRoundTrip:
    def test_the_corruption_round_trip_now_converges(self):
        """The exact sequence that corrupted 10 profiles: raw JSON
        text rendered into the textarea, then saved. The writer-side
        filter must yield a clean empty list, not a [] 'ticker'."""
        rendered_textarea = "[]"  # what the old template displayed
        saved = _valid_wheel_tickers(rendered_textarea.split(","))
        assert saved == []
        assert _parse_wheel_symbols(json.dumps(saved)) == []

    def test_normal_user_input_round_trip(self):
        saved = _valid_wheel_tickers(" msft , AAPL, ko ".split(","))
        assert saved == ["MSFT", "AAPL", "KO"]


class TestProfileDictBuilders:
    def test_builders_return_parsed_lists(self):
        """Every profile-dict builder must hand templates a LIST (the
        custom_watchlist treatment) — a raw JSON string here is what
        the template renders verbatim into the save round-trip."""
        src = open(os.path.join(REPO, "models.py")).read()
        assert src.count(
            'd["wheel_symbols"] = _parse_wheel_symbols('
        ) >= 3, (
            "get_trading_profile / get_user_profiles / "
            "get_active_profiles must parse wheel_symbols to a list "
            "— the settings textarea renders this value."
        )

    def test_writer_validates_tokens(self):
        src = open(os.path.join(REPO, "views.py")).read()
        assert "_valid_wheel_tickers(wheel_raw.split(" in src, (
            "the settings writer must filter wheel tokens through "
            "the ticker validator — otherwise junk persists again."
        )

    def test_ctx_path_rejects_corrupt_value(self):
        """models.py:1900 builds ctx.wheel_symbols through the same
        parser — the wheel must never receive a [] 'ticker' even if
        a corrupt value is still stored somewhere."""
        assert _parse_wheel_symbols('["[]", "MSFT"]') == ["MSFT"]
