"""Learning Scoreboard — is the system learning, arm by arm, week by week.

docs/25 step 2 (2026-08-23). Definitions live in
calculation_verification/learning.md and were written before this
module; every number here maps to an entry there. The page issues no
verdict — the pre-registered decision rule does that at the horizon.
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import statistics
from collections import defaultdict
from contextlib import closing
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BULL = ("BUY", "STRONG_BUY")
BEAR = ("SELL", "STRONG_SELL", "SHORT")
HOLD_BAND_PCT = 1.5
HIGH_CONF = 70
THIN_WEEK_N = 10

_RESOLVED_WHERE = (
    "status='resolved' AND actual_return_pct IS NOT NULL "
    "AND (data_quality IS NULL OR data_quality='')"
)


def iso_week(date_str: str) -> str:
    d = _dt.date.fromisoformat(str(date_str)[:10])
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _new_week() -> Dict[str, Any]:
    return {"n": 0, "hits": 0, "hi_n": 0, "hi_hits": 0, "lo_n": 0,
            "lo_hits": 0, "brier_sum": 0.0, "hold_n": 0, "hold_ok": 0,
            "ret_sum": 0.0}


def profile_weekly_predictions(db_path: str) -> Dict[str, Dict[str, Any]]:
    """{iso_week: raw counters} from one profile's resolved predictions."""
    weeks: Dict[str, Dict[str, Any]] = defaultdict(_new_week)
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            f"SELECT timestamp, UPPER(predicted_signal), confidence, "
            f"actual_return_pct FROM ai_predictions WHERE {_RESOLVED_WHERE}"
        ).fetchall()
    for ts, sig, conf, ret in rows:
        try:
            wk = iso_week(ts)
        except (TypeError, ValueError):
            continue
        w = weeks[wk]
        ret = float(ret)
        if sig in BULL or sig in BEAR:
            hit = (ret > 0) if sig in BULL else (ret < 0)
            w["n"] += 1
            w["hits"] += int(hit)
            w["ret_sum"] += ret
            try:
                c = float(conf or 0.0)
            except (TypeError, ValueError):
                c = 0.0
            c = max(0.0, min(100.0, c))
            w["brier_sum"] += (c / 100.0 - (1.0 if hit else 0.0)) ** 2
            if c >= HIGH_CONF:
                w["hi_n"] += 1
                w["hi_hits"] += int(hit)
            else:
                w["lo_n"] += 1
                w["lo_hits"] += int(hit)
        elif sig == "HOLD":
            w["hold_n"] += 1
            w["hold_ok"] += int(abs(ret) < HOLD_BAND_PCT)
    return dict(weeks)


def profile_weekly_equity_returns(db_path: str) -> Dict[str, float]:
    """{iso_week: weekly return fraction} from daily_snapshots — last
    equity of the week over last equity of the prior week; the first
    week (no prior) is absent."""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT date, equity FROM daily_snapshots WHERE equity IS NOT NULL "
            "AND equity > 0 ORDER BY date").fetchall()
    last_by_week: Dict[str, float] = {}
    for d, e in rows:
        try:
            last_by_week[iso_week(d)] = float(e)
        except (TypeError, ValueError):
            continue
    out: Dict[str, float] = {}
    prev: Optional[float] = None
    for wk in sorted(last_by_week):
        cur = last_by_week[wk]
        if prev:
            out[wk] = cur / prev - 1.0
        prev = cur
    return out


def weekly_returns_from_series(series: List[Tuple[str, float]]) -> Dict[str, float]:
    """Same rule as profile_weekly_equity_returns for an in-memory
    (date, equity) series (virtual benchmarks)."""
    last_by_week: Dict[str, float] = {}
    for d, e in series:
        if e and e > 0:
            last_by_week[iso_week(d)] = float(e)
    out: Dict[str, float] = {}
    prev: Optional[float] = None
    for wk in sorted(last_by_week):
        if prev:
            out[wk] = last_by_week[wk] / prev - 1.0
        prev = last_by_week[wk]
    return out


def spy_weekly_returns(start: str, end: str,
                       fetch=None) -> Dict[str, float]:
    """{iso_week: compounded weekly return fraction} from daily SPY
    returns. Empty (never zeros) when bars are unavailable."""
    if fetch is None:
        from metrics.legacy import _fetch_benchmark_returns as fetch
    try:
        daily = fetch("SPY", start, end) or {}
    except Exception as exc:
        logger.warning("learning scoreboard: SPY returns unavailable: %s: %s",
                       type(exc).__name__, exc)
        return {}
    acc: Dict[str, float] = {}
    for d in sorted(daily):
        try:
            wk = iso_week(d)
        except (TypeError, ValueError):
            continue
        acc[wk] = acc.get(wk, 1.0) * (1.0 + float(daily[d]))
    return {wk: v - 1.0 for wk, v in acc.items()}


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den * 100.0, 1) if den else None


def _finalize_week(w: Dict[str, Any]) -> Dict[str, Any]:
    n = w["n"]
    return {
        "n": n,
        "hit_rate": _rate(w["hits"], n),
        "hi_n": w["hi_n"], "hi_hit_rate": _rate(w["hi_hits"], w["hi_n"]),
        "lo_n": w["lo_n"], "lo_hit_rate": _rate(w["lo_hits"], w["lo_n"]),
        "brier": round(w["brier_sum"] / n, 3) if n else None,
        "hold_n": w["hold_n"], "hold_quality": _rate(w["hold_ok"], w["hold_n"]),
        "mean_move": round(w["ret_sum"] / n, 2) if n else None,
        "thin": n < THIN_WEEK_N,
    }


def tuner_scorecard(master_conn: sqlite3.Connection,
                    profile_ids: List[int]) -> Dict[str, Any]:
    counts: Dict[str, int] = defaultdict(int)
    if profile_ids:
        q = ("SELECT COALESCE(outcome_after, 'pending'), COUNT(*) FROM "
             "tuning_history WHERE profile_id IN (%s) GROUP BY 1"
             % ",".join("?" * len(profile_ids)))
        try:
            for o, c in master_conn.execute(q, profile_ids):
                counts[str(o)] += int(c)
        except sqlite3.OperationalError as exc:
            logger.warning("learning scoreboard: tuning_history unreadable: %s", exc)
    improved = counts.get("improved", 0)
    worsened = counts.get("worsened", 0)
    return {
        "total": sum(counts.values()),
        "improved": improved, "worsened": worsened,
        "unchanged": counts.get("unchanged", 0),
        "pending": counts.get("pending", 0),
        "other": sum(v for k, v in counts.items()
                     if k not in ("improved", "worsened", "unchanged", "pending")),
        "improved_share": _rate(improved, improved + worsened),
        "judged": improved + worsened,
    }


def collect_scoreboard(profiles: List[Dict[str, Any]],
                       db_path_for, *,
                       master_conn: Optional[sqlite3.Connection] = None,
                       benchmark_series: Optional[List[Dict[str, Any]]] = None,
                       spy_fetch=None) -> Dict[str, Any]:
    """Build the page payload.

    profiles: trading_profiles rows (dicts with id, name, strategy_type,
      ai_provider, ai_model, enabled). db_path_for(profile_id) -> path.
    benchmark_series: [{"profile_name", "strategy_type", "points":[...]}]
      in comparative_returns shape (virtual benchmarks); optional.
    """
    arms: Dict[str, Dict[str, Any]] = {}
    all_weeks: set = set()
    min_date: Optional[str] = None
    max_date: Optional[str] = None
    for p in profiles:
        if not p.get("enabled", 1):
            continue
        if (p.get("strategy_type") or "ai") != "ai":
            continue
        key = f"{p.get('ai_provider')}:{p.get('ai_model')}"
        arm = arms.setdefault(key, {"label": key, "replicates": [],
                                    "weeks": {}, "profile_ids": []})
        db = db_path_for(p["id"])
        rep = {"profile_id": p["id"], "name": p.get("name") or f"p{p['id']}",
               "weeks": {}, "equity_weeks": {}}
        try:
            raw = profile_weekly_predictions(db)
            rep["weeks"] = {wk: _finalize_week(w) for wk, w in raw.items()}
            rep["equity_weeks"] = profile_weekly_equity_returns(db)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("learning scoreboard: profile %s unreadable: %s: %s",
                           p["id"], type(exc).__name__, exc)
            rep["error"] = f"{type(exc).__name__}: {exc}"
        arm["replicates"].append(rep)
        arm["profile_ids"].append(p["id"])
        all_weeks.update(rep["weeks"]); all_weeks.update(rep["equity_weeks"])
        # Raw week aggregation across replicates (sum counters, not
        # averaging rates — a 2-sample replicate must not weigh like a
        # 200-sample one).
        for wk, w in raw.items() if "error" not in rep else []:
            agg = arm["weeks"].setdefault(wk, _new_week())
            for k in _new_week():
                agg[k] += w[k]
        try:
            with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as c:
                row = c.execute("SELECT MIN(date), MAX(date) FROM daily_snapshots").fetchone()
            if row and row[0]:
                min_date = min(min_date, row[0]) if min_date else row[0]
                max_date = max(max_date, row[1]) if max_date else row[1]
        except sqlite3.Error:
            pass

    # SPY + benchmarks over the observed span.
    spy: Dict[str, float] = {}
    if min_date and max_date:
        spy = spy_weekly_returns(min_date, max_date, fetch=spy_fetch)
    bench: Dict[str, Dict[str, Any]] = {}
    for b in benchmark_series or []:
        series = [(pt["date"], 1.0 + pt["return_pct"] / 100.0) for pt in b.get("points", [])]
        wr = weekly_returns_from_series(series)
        kind = b.get("strategy_type", "?")
        for wk, v in wr.items():
            slot = bench.setdefault(wk, {"buy_hold": None, "random": []})
            if kind == "buy_hold":
                slot["buy_hold"] = v
            elif kind == "random":
                slot["random"].append(v)
        all_weeks.update(wr)

    weeks_sorted = sorted(all_weeks)
    for arm in arms.values():
        arm["weeks"] = {wk: _finalize_week(w) for wk, w in arm["weeks"].items()}
        for wk in weeks_sorted:
            vals = [r["equity_weeks"][wk] for r in arm["replicates"]
                    if wk in r["equity_weeks"]]
            row = arm["weeks"].setdefault(wk, _finalize_week(_new_week()))
            if vals:
                mean = sum(vals) / len(vals)
                row["equity_ret"] = round(mean * 100, 2)
                row["equity_ret_min"] = round(min(vals) * 100, 2)
                row["equity_ret_max"] = round(max(vals) * 100, 2)
                row["equity_n"] = len(vals)
                row["excess"] = (round((mean - spy[wk]) * 100, 2)
                                 if wk in spy else None)
            else:
                row.update({"equity_ret": None, "equity_ret_min": None,
                            "equity_ret_max": None, "equity_n": 0,
                            "excess": None})
        if master_conn is not None:
            arm["tuner"] = tuner_scorecard(master_conn, arm["profile_ids"])
        else:
            arm["tuner"] = None

    bench_rows = {}
    for wk, slot in bench.items():
        rnd = sorted(slot["random"])
        bench_rows[wk] = {
            "buy_hold": round(slot["buy_hold"] * 100, 2) if slot["buy_hold"] is not None else None,
            "random_min": round(rnd[0] * 100, 2) if rnd else None,
            "random_median": round(statistics.median(rnd) * 100, 2) if rnd else None,
            "random_max": round(rnd[-1] * 100, 2) if rnd else None,
            "random_n": len(rnd),
        }
    return {
        "arms": dict(sorted(arms.items())),
        "weeks": weeks_sorted,
        "spy": {wk: round(v * 100, 2) for wk, v in spy.items()},
        "benchmarks": bench_rows,
        "thin_week_n": THIN_WEEK_N,
        "high_conf": HIGH_CONF,
        "hold_band": HOLD_BAND_PCT,
        "empty": not arms,
    }
