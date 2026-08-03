"""Profile-winner medals (🥇🥈🥉) in every profile dropdown.

Operator request 2026-07-26: the dashboard's medals should also appear
next to the items in the profile dropdowns so it's easy to see who is
winning and jump to their metrics. One ranking source — the SAME cached
/api/dashboard-totals payload the dashboard's medal column uses — so
the dropdowns can never disagree with the dashboard. The context
processor is CACHE-ONLY on the request path (never adds broker latency
to a page render); a cold cache returns no medals and warms in the
background.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from views import _medals_from_payload  # noqa: E402


class TestRanking:
    def test_top_three_by_pnl_pct(self):
        payload = {"profiles": [
            {"id": 1, "pnl_pct": -2.0},
            {"id": 2, "pnl_pct": 9.5},
            {"id": 3, "pnl_pct": 4.1},
            {"id": 4, "pnl_pct": 7.0},
        ]}
        m = _medals_from_payload(payload)
        assert m == {2: "🥇", 4: "🥈", 3: "🥉"}

    def test_fewer_than_three(self):
        m = _medals_from_payload({"profiles": [{"id": 7, "pnl_pct": 1}]})
        assert m == {7: "🥇"}

    def test_empty_and_malformed_safe(self):
        assert _medals_from_payload({}) == {}
        assert _medals_from_payload(None) == {}
        # missing pnl_pct treated as 0 — no crash
        assert _medals_from_payload({"profiles": [{"id": 1}]}) == {1: "🥇"}


class TestCrossProcessFallback:
    """gunicorn runs multiple workers with independent in-process
    caches — cache-only medals rendered flakily (operator: 'i do not
    see the medals'). Any process that computes the payload persists
    the ranking to a tiny file; every worker serves it instantly."""

    def test_write_then_read_restores_int_keys(self, monkeypatch,
                                               tmp_path):
        import views
        monkeypatch.setattr(views, "_MEDALS_FILE",
                            str(tmp_path / "medals.json"))
        payload = {"profiles": [
            {"id": 218, "pnl_pct": 9.0}, {"id": 217, "pnl_pct": 5.0},
            {"id": 211, "pnl_pct": 2.0}, {"id": 210, "pnl_pct": 1.0},
        ]}
        views._write_medals_file(1, payload)
        m = views._read_medals_file(1)
        assert m == {218: "🥇", 217: "🥈", 211: "🥉"}
        assert all(isinstance(k, int) for k in m), (
            "JSON stringifies keys; the reader must restore ints — "
            "templates look up profile_medals by integer p.id"
        )

    def test_stale_file_ignored(self, monkeypatch, tmp_path):
        import json
        import views
        f = tmp_path / "medals.json"
        monkeypatch.setattr(views, "_MEDALS_FILE", str(f))
        f.write_text(json.dumps(
            {"1": {"ts": 1000.0, "medals": {"218": "🥇"}}}))
        assert views._read_medals_file(1) == {}

    def test_missing_file_safe(self, monkeypatch, tmp_path):
        import views
        monkeypatch.setattr(views, "_MEDALS_FILE",
                            str(tmp_path / "nope.json"))
        assert views._read_medals_file(1) == {}


class TestStructural:
    def test_context_processor_registered_cache_only(self):
        src = open(os.path.join(
            os.path.dirname(__file__), os.pardir, "views.py")).read()
        assert "def inject_profile_medals" in src
        idx = src.index("def inject_profile_medals")
        body = src[idx:idx + 2200]
        assert "_ttl_cache_get" in body, (
            "the medal processor must be CACHE-ONLY on the request "
            "path — computing the payload inline adds broker latency "
            "to every page render"
        )
        assert "app_context_processor" in src[idx - 200:idx]

    def test_all_profile_dropdowns_carry_medals(self):
        base = os.path.join(os.path.dirname(__file__), os.pardir,
                            "templates")
        expected = {
            "ai.html": 3,            # main selector + timeline + resolver
            "ai_performance.html": 1,
            "trades.html": 1,
            "performance.html": 1,
        }
        for name, count in expected.items():
            tpl = open(os.path.join(base, name)).read()
            n = tpl.count("profile_medals")
            assert n >= count, (
                f"{name}: expected ≥{count} medal-annotated dropdown "
                f"option(s), found {n} — a profile dropdown lost its "
                f"winner medals"
            )
