"""2026-09-20 — the Learning page states where the OWNED model stands.

The page scored only the rented-model arms. The owned (fine-tuned)
model had been trained three times, never beaten its untrained base,
and — found 2026-09-20 — had never been trained mainly on its answers;
none of that was visible in the app. The panel renders the one status
record that every batch verdict updates (`finetune/status.json`), and
it fails VISIBLY when that record is missing or incomplete.
"""
from __future__ import annotations

import json
import re

import pytest

from finetune.status import STATUS_PATH, load_status


class TestStatusRecord:
    def test_shipped_record_is_complete_and_says_not_in_use(self):
        s = load_status()
        assert "error" not in s
        assert s["in_use"] is False
        assert "never" in s["headline"].lower()
        assert [r["round"] for r in s["rounds"]][:3] == [1, 2, 3]

    def test_shipped_record_matches_the_training_log(self):
        """The numbers the app shows are the ones docs/27 records."""
        rounds = {r["round"]: r for r in load_status()["rounds"]}
        assert (rounds[2]["ours_pct"], rounds[2]["untrained_pct"],
                rounds[2]["exam"]) == (38.6, 37.3, 158)
        assert (rounds[3]["ours_pct"], rounds[3]["untrained_pct"],
                rounds[3]["exam"], rounds[3]["studied"]) == (
                    27.6, 31.3, 134, 34157)
        import os
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "docs",
                               "27_FINETUNE_TRAINING_LOG.md")) as fh:
            log = fh.read()
        for needle in ("38.6%", "37.3%", "27.6%", "31.3%", "34,157"):
            assert needle in log

    def test_copy_is_plain_english(self):
        """User-facing copy never leaks code identifiers or paths."""
        def _strings(node):
            if isinstance(node, str):
                yield node
            elif isinstance(node, dict):
                for v in node.values():
                    yield from _strings(v)
            elif isinstance(node, list):
                for v in node:
                    yield from _strings(v)
        for text in _strings(json.load(open(STATUS_PATH))):
            assert not re.search(r"[a-z]_[a-z]", text), text
            assert not re.search(r"\.(py|json|jsonl|md)\b", text), text
            assert "/" not in text.replace("2026-", ""), text

    def test_missing_record_is_an_error_not_an_empty_panel(self, tmp_path):
        assert "error" in load_status(str(tmp_path / "nope.json"))

    @pytest.mark.parametrize("body", [
        "not json",
        json.dumps({"updated": "2026-09-20"}),
        json.dumps({"updated": "x", "in_use": False, "headline": "h",
                    "bar": "b", "rounds": [{"round": 1}], "next": "n"}),
        json.dumps({"updated": "x", "in_use": False, "headline": "h",
                    "bar": "b", "rounds": "three", "next": "n"}),
    ])
    def test_malformed_record_is_an_error(self, tmp_path, body):
        p = tmp_path / "status.json"
        p.write_text(body)
        assert "error" in load_status(str(p))


def _client(tmp_main_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # `tmp_main_db` already points config.DB_PATH at the temp database
    # and restores it. Do NOT also monkeypatch it here: the two undo in
    # the wrong order at teardown (the fixture restores ":memory:", then
    # monkeypatch "restores" the temp path it saw), leaving
    # config.DB_PATH aimed at a dead temp database for every later
    # test. This file did exactly that for a few hours on 2026-09-20 —
    # it was the polluter behind ten order-dependent failures.
    from models import create_user
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    create_user("t@t.com", "password123", "T", is_admin=True)
    client = app.test_client()
    client.post("/login", data={"email": "t@t.com",
                                "password": "password123"},
                follow_redirects=True)
    return client


class TestPanelRenders:
    def test_learning_page_shows_the_real_status(self, tmp_main_db,
                                                 tmp_path, monkeypatch):
        status = load_status()
        r = _client(tmp_main_db, tmp_path, monkeypatch).get("/learning")
        assert r.status_code == 200
        html = r.data.decode()
        assert "Our own model" in html
        assert "has never made or influenced a trading decision" in html
        assert "Same model, untrained" in html
        assert "38.6%" in html and "37.3%" in html
        assert "34,157" in html
        assert "Why rounds 1 to 3 were not a fair test" in html
        assert status["updated"] in html
        # the rented-arm scoreboard is still there
        assert "Learning Scoreboard" in html

    def test_unreadable_status_is_shown_as_unavailable(
            self, tmp_main_db, tmp_path, monkeypatch):
        import finetune.status as fs
        monkeypatch.setattr(fs, "STATUS_PATH", str(tmp_path / "gone.json"))
        r = _client(tmp_main_db, tmp_path, monkeypatch).get("/learning")
        assert r.status_code == 200
        html = r.data.decode()
        assert "Status unavailable" in html
        assert "has never made or influenced" not in html
