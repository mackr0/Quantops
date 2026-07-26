"""Shadow-eval decision-quality metrics — the /shadow page's engine.

Answers the operator's question (2026-07-25): "which models are making
the right decisions more often, and are there categories of decisions
one model is better at?" Everything derives from two ledgers the fleet
already writes:

  ai_shadow_calls   every shadowed primary call: the primary's parsed
                    decision, each shadow model's parsed decision, and
                    agreement (schema-aware, 2026-07-24).
  ai_predictions    the primary's per-symbol predictions with RESOLVED
                    outcomes. `actual_return_pct` is the RAW PRICE
                    return (verified: SHORT rows show positive pct when
                    the price rose), so grading is symmetric:
                    a BULLISH stance was right iff return > 0, a
                    BEARISH stance right iff return < 0.

The money metric is DISAGREEMENT OUTCOMES: on calls where a shadow
model disagreed with the primary and the underlying prediction has
since resolved, which side's stance matched the realized price move.
Agreement-only stats can't separate models; disagreements + outcomes
can. Category cuts (per purpose, per primary-action) expose where a
model has an edge even when its overall numbers look similar.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MATCH_WINDOW_SEC = 300.0   # shadow call ↔ prediction timestamp window

_BULLISH = {"BUY", "STRONG_BUY", "CONFIRM", "LONG", "BULLISH",
            "POSITIVE", "COVER"}
_BEARISH = {"SELL", "STRONG_SELL", "VETO", "SHORT", "BEARISH",
            "NEGATIVE"}
_NEUTRAL = {"HOLD", "NEUTRAL", "PASS", "CAUTION", "MIXED", "NONE"}


def stance(signal: Optional[str]) -> Optional[str]:
    """Map a parsed decision string to 'bullish' / 'bearish' /
    'neutral', or None when unmappable (e.g. the apex trade-set
    strings — graded at set level via `agreement`, not stance)."""
    if not signal or not isinstance(signal, str):
        return None
    s = signal.strip().upper()
    if s in _BULLISH:
        return "bullish"
    if s in _BEARISH:
        return "bearish"
    if s in _NEUTRAL:
        return "neutral"
    return None


def grade(stance_val: Optional[str],
          return_pct: Optional[float]) -> Optional[bool]:
    """True when the stance matched the realized RAW price move,
    False when it opposed it, None when ungradable (neutral stance,
    unmappable signal, or a ~flat outcome)."""
    if stance_val in (None, "neutral") or return_pct is None:
        return None
    if abs(return_pct) < 0.05:   # flat — neither side earns credit
        return None
    if stance_val == "bullish":
        return return_pct > 0
    return return_pct < 0


def _parse_ts(ts: Any) -> Optional[float]:
    try:
        s = str(ts)[:19].replace("T", " ")
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


class _ResolvedIndex:
    """symbol → sorted [(epoch, return_pct)] of resolved predictions
    for one profile DB; nearest-within-window lookup."""

    def __init__(self, conn: sqlite3.Connection):
        self._by_symbol: Dict[str, List[tuple]] = defaultdict(list)
        try:
            rows = conn.execute(
                "SELECT symbol, timestamp, actual_return_pct "
                "FROM ai_predictions WHERE status = 'resolved' "
                "AND actual_return_pct IS NOT NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for sym, ts, ret in rows:
            e = _parse_ts(ts)
            if e is not None and sym:
                self._by_symbol[str(sym).upper()].append(
                    (e, float(ret)))
        for lst in self._by_symbol.values():
            lst.sort()

    def outcome_for(self, symbol: str,
                    epoch: Optional[float]) -> Optional[float]:
        if not symbol or epoch is None:
            return None
        lst = self._by_symbol.get(symbol.upper())
        if not lst:
            return None
        i = bisect_left(lst, (epoch,))
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(lst):
                d = abs(lst[j][0] - epoch)
                if d <= _MATCH_WINDOW_SEC and (
                        best is None or d < best[0]):
                    best = (d, lst[j][1])
        return best[1] if best else None


def _classify_error(err: Optional[str]) -> Optional[str]:
    if not err:
        return None
    if "cost cap" in err:
        return "throttled"
    if "exceeded your current quota" in err or "insufficient_quota" in err:
        return "quota"
    return "error"


def _model_bucket() -> Dict[str, Any]:
    return {
        "calls": 0, "graded": 0, "agree": 0, "disagree": 0,
        "errors": 0, "quota": 0, "throttled": 0,
        "cost": 0.0, "latency_ms": 0, "latency_n": 0,
        # disagreement outcomes
        "dis_resolved": 0, "shadow_right": 0, "primary_right": 0,
        "both_ungraded": 0, "dis_pending": 0,
    }


def collect_fleet_metrics(profile_dbs: List[str],
                          days: int = 30) -> Dict[str, Any]:
    """Aggregate the full metric set across profile DBs.

    Returns a dict the /shadow template renders directly:
      overview, per_model, by_purpose, by_primary_action,
      recent_disagreements, daily.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)
              ).strftime("%Y-%m-%d %H:%M:%S")
    per_model: Dict[str, Dict] = defaultdict(_model_bucket)
    by_purpose: Dict[tuple, Dict] = defaultdict(_model_bucket)
    by_action: Dict[tuple, Dict] = defaultdict(_model_bucket)
    daily: Dict[tuple, Dict] = defaultdict(
        lambda: {"graded": 0, "agree": 0})
    recent_dis: List[Dict] = []
    overview = {"calls": 0, "graded": 0, "cost": 0.0,
                "profiles": 0, "since_days": days}

    for db in profile_dbs:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            try:
                rows = conn.execute(
                    "SELECT timestamp, purpose, provider, model, "
                    " parsed_signal, agreement, error, cost_usd, "
                    " latency_ms, primary_parsed "
                    "FROM ai_shadow_calls WHERE timestamp >= ? "
                    "ORDER BY id",
                    (cutoff,),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            if not rows:
                continue
            overview["profiles"] += 1
            resolved = _ResolvedIndex(conn)
        finally:
            conn.close()

        prof_label = db.replace("quantopsai_profile_", "p").replace(
            ".db", "")
        for (ts, purpose, provider, model, parsed_signal, agreement,
             err, cost, latency, primary_parsed) in rows:
            mkey = f"{provider}:{model}"
            m = per_model[mkey]
            m["calls"] += 1
            overview["calls"] += 1
            m["cost"] += float(cost or 0)
            overview["cost"] += float(cost or 0)
            if latency:
                m["latency_ms"] += int(latency)
                m["latency_n"] += 1
            ekind = _classify_error(err)
            if ekind:
                m[ekind + ("s" if ekind == "error" else "")] += 1
                continue
            if agreement not in (0, 1):
                continue
            # graded call
            m["graded"] += 1
            overview["graded"] += 1
            day = str(ts)[:10]
            dk = daily[(day, mkey)]
            dk["graded"] += 1
            try:
                pp = (json.loads(primary_parsed)
                      if primary_parsed else None)
            except (json.JSONDecodeError, TypeError, ValueError):
                pp = None
            from shadow_eval import _extract_signal
            primary_signal = _extract_signal(pp)
            symbol = (pp.get("symbol") if isinstance(pp, dict)
                      else None)
            pk = by_purpose[(purpose or "?", mkey)]
            pk["calls"] += 1
            pk["graded"] += 1
            p_st = stance(primary_signal)
            ak = by_action[(p_st or "set-level", mkey)]
            ak["calls"] += 1
            ak["graded"] += 1
            if agreement == 1:
                m["agree"] += 1
                dk["agree"] += 1
                pk["agree"] += 1
                ak["agree"] += 1
                continue
            # disagreement — the interesting rows
            m["disagree"] += 1
            pk["disagree"] += 1
            ak["disagree"] += 1
            outcome = resolved.outcome_for(
                symbol, _parse_ts(ts)) if symbol else None
            s_st = stance(parsed_signal)
            dis_row = {
                "ts": str(ts)[:16], "profile": prof_label,
                "symbol": symbol or "—", "purpose": purpose,
                "model": mkey,
                "primary": primary_signal or "?",
                "shadow": parsed_signal or "?",
                "outcome_pct": (round(outcome, 2)
                                if outcome is not None else None),
                "who_right": None,
            }
            if outcome is None:
                m["dis_pending"] += 1
                pk["dis_pending"] += 1
                ak["dis_pending"] += 1
            else:
                m["dis_resolved"] += 1
                pk["dis_resolved"] += 1
                ak["dis_resolved"] += 1
                p_right = grade(p_st, outcome)
                s_right = grade(s_st, outcome)
                if s_right:
                    m["shadow_right"] += 1
                    pk["shadow_right"] += 1
                    ak["shadow_right"] += 1
                    dis_row["who_right"] = "shadow"
                if p_right:
                    m["primary_right"] += 1
                    pk["primary_right"] += 1
                    ak["primary_right"] += 1
                    dis_row["who_right"] = (
                        "both" if s_right else "primary")
                if not p_right and not s_right:
                    m["both_ungraded"] += 1
                    pk["both_ungraded"] += 1
                    ak["both_ungraded"] += 1
                    dis_row["who_right"] = "neither/ungraded"
            recent_dis.append(dis_row)

    def _finalize(b: Dict) -> Dict:
        g = b["agree"] + b["disagree"]
        b["agreement_pct"] = round(b["agree"] / g * 100, 1) if g else None
        b["avg_latency_ms"] = (b["latency_ms"] // b["latency_n"]
                               if b["latency_n"] else None)
        b["cost"] = round(b["cost"], 4)
        return b

    per_model_out = {k: _finalize(dict(v))
                     for k, v in sorted(per_model.items())}
    by_purpose_out: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for (purpose, mkey), v in sorted(by_purpose.items()):
        by_purpose_out[purpose][mkey] = _finalize(dict(v))
    by_action_out: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for (act, mkey), v in sorted(by_action.items()):
        by_action_out[act][mkey] = _finalize(dict(v))
    daily_out: Dict[str, Dict[str, Any]] = defaultdict(dict)
    for (day, mkey), v in sorted(daily.items()):
        pct = (round(v["agree"] / v["graded"] * 100, 1)
               if v["graded"] else None)
        daily_out[day][mkey] = {"graded": v["graded"],
                                "agreement_pct": pct}
    recent_dis.sort(key=lambda r: r["ts"], reverse=True)
    overview["cost"] = round(overview["cost"], 4)
    return {
        "overview": overview,
        "per_model": per_model_out,
        "by_purpose": dict(by_purpose_out),
        "by_primary_action": dict(by_action_out),
        "recent_disagreements": recent_dis[:60],
        "daily": dict(daily_out),
    }
