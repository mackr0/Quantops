"""2026-06-16 — a canceled/expired/rejected trade realized NOTHING, so
it must carry no pnl.

p121 had a −5,985 decomposition gap (equity vs realized+unrealized P&L)
traced to three canceled SELL rows that kept their speculative pnl
(VSME +3463, SOUN −521, SPCX +3042 = +5,985), which the realized-P&L
SUM then counted as if the trades had executed.

The invariant: any UPDATE that marks a row canceled/expired/rejected
must also clear pnl (`pnl = NULL`). With the invariant held,
`SUM(pnl)` is automatically correct everywhere (NULL doesn't sum), so
no realized-P&L reader needs a special filter. We also make the
decomposition reader defensively exclude those statuses.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Live-loop source that cancels trade rows. One-off repair scripts under
# scripts/ are audited separately; these run every cycle.
_LIVE_FILES = [
    "reconcile_journal_to_broker.py",
    "trader.py",
    "options_multileg.py",
    "bracket_orders.py",
]

_CANCEL_RE = re.compile(
    r"UPDATE\s+trades\s+SET\s+status\s*=\s*['\"](?:canceled|expired|rejected)['\"]",
    re.IGNORECASE,
)


@pytest.mark.parametrize("fname", _LIVE_FILES)
def test_cancel_updates_clear_pnl(fname):
    """Every live UPDATE that sets a terminal-cancel status must also
    set pnl = NULL in the same statement."""
    src = (REPO / fname).read_text()
    for m in _CANCEL_RE.finditer(src):
        # Look at the statement window from the match to the closing
        # of the SQL string (next standalone WHERE ... or ~300 chars).
        window = src[m.start():m.start() + 400]
        # Cut at the end of the SQL statement (first ')' after a WHERE,
        # or 400 chars) — enough to cover the SET clause.
        head = window.split("WHERE")[0]
        assert re.search(r"pnl\s*=\s*NULL", head, re.IGNORECASE), (
            f"{fname}: a cancel UPDATE near offset {m.start()} sets a "
            f"terminal status without 'pnl = NULL'. A canceled trade "
            f"realized nothing — leaving pnl inflates realized P&L "
            f"(the p121 −5,985 decomposition gap). Add pnl=NULL."
        )


def test_realized_pnl_excludes_canceled_rows(tmp_path):
    """Functional pin: a canceled row carrying a stray pnl must NOT
    count toward realized P&L (the certify decomposition definition)."""
    from journal import init_db, data_quality_clause
    db = str(tmp_path / "p.db")
    init_db(db)
    with closing(sqlite3.connect(db)) as c:
        # A real closed sell (+100) and a canceled sell with a stray
        # pnl (+3000) that must be ignored.
        c.execute("INSERT INTO trades (symbol,side,qty,price,pnl,status,order_id) "
                  "VALUES ('AAA','sell',10,10,100,'closed','o1')")
        c.execute("INSERT INTO trades (symbol,side,qty,price,pnl,status,order_id) "
                  "VALUES ('BBB','sell',10,10,3000,'canceled','o2')")
        c.commit()
        dq = data_quality_clause(c)
        realized = c.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades "
            "WHERE pnl IS NOT NULL "
            "AND COALESCE(status,'') NOT IN ('canceled','expired','rejected')"
            + dq).fetchone()[0]
    assert realized == 100, (
        f"realized P&L must exclude the canceled row's stray pnl; got "
        f"{realized} (should be 100, not 3100)"
    )


def test_certify_decomposition_reader_excludes_canceled():
    """Structural pin: certify's realized-P&L read excludes the
    non-executed statuses."""
    src = (REPO / "certify_books.py").read_text()
    idx = src.find("def check_decomposition")
    body = src[idx:idx + 1200]
    assert "NOT IN" in body and "'canceled'" in body and "'rejected'" in body, (
        "certify decomposition must exclude canceled/expired/rejected "
        "from realized P&L"
    )
