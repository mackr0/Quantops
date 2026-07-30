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

# Shadow call ↔ prediction timestamp window. Widened 300s → 1800s on
# 2026-07-30: measured against the live ledgers, a 300s window matched
# only 22.6% of reviewer calls that HAD a resolved prediction for the
# symbol, discarding roughly half the available outcomes to ordinary
# jitter between when a specialist runs and when the prediction row is
# written. 1800s recovers them (fleet resolved disagreements 164 → 243)
# and still sits inside one trading cycle; past ~1h the nearest match
# starts landing in a DIFFERENT cycle, which would be a false join.
# shadow_eval.fetch_recently_resolved_disagreements imports THIS value
# so the daily email and the /shadow page can never drift apart.
MATCH_WINDOW_SEC = 1800.0
_MATCH_WINDOW_SEC = MATCH_WINDOW_SEC   # back-compat alias

# Band inside which a move is "no real move". Matches
# shadow_eval._VERDICT_NOISE_PCT so the daily email and this page agree
# on what counts as flat. The old 0.05% threshold was noise-level: it
# treated a 0.06% drift as a genuine directional outcome.
NOISE_PCT = 1.5

_BULLISH = {"BUY", "STRONG_BUY", "CONFIRM", "LONG", "BULLISH",
            "POSITIVE", "COVER"}
_BEARISH = {"SELL", "STRONG_SELL", "VETO", "SHORT", "BEARISH",
            "NEGATIVE"}
_NEUTRAL = {"HOLD", "NEUTRAL", "PASS", "CAUTION", "MIXED", "NONE"}


# Specialists holding VETO authority (mirrors ensemble.VETO_AUTHORIZED)
# decide whether a proposed trade HAPPENS. Their verdict is a gate, not
# a forecast: "VETO" does not claim the price will fall, it claims this
# entry has a failure mode. Grading a gate against raw price direction
# is a category error — it scores the reviewer as though it had made a
# directional call it never made.
_GATE_PURPOSES = {"ensemble:adversarial_reviewer", "ensemble:risk_assessor"}

# SELL / STRONG_SELL are deliberately absent from BOTH sets. In the
# reviewer's contract a SELL verdict supports closing an EXISTING
# position — it is not a ruling on the candidate being reviewed, so it
# cannot be scored against that candidate's entry either way.
_BLOCK_VERDICTS = {"VETO", "BLOCK"}
_ALLOW_VERDICTS = {"HOLD", "BUY", "STRONG_BUY", "CONFIRM", "PASS", "ALLOW"}


def gate_call(signal: Optional[str]) -> Optional[str]:
    """Map a VETO-authority specialist's verdict to 'block' / 'allow',
    or None when unmappable."""
    if not signal or not isinstance(signal, str):
        return None
    s = signal.strip().upper()
    if s in _BLOCK_VERDICTS:
        return "block"
    if s in _ALLOW_VERDICTS:
        return "allow"
    return None


def trade_pnl(predicted_signal: Optional[str],
              return_pct: Optional[float]) -> Optional[float]:
    """P&L of the trade the system actually took on this symbol, given
    the RAW price return. Returns None when NO trade was taken — the
    prediction row exists but records a pass (`predicted_signal`
    'HOLD'), so there is no position whose outcome a gate verdict could
    be right or wrong about.

    This distinction is the difference between a measurement and a
    number: on 2026-07-30 all 55 matched reviewer disagreements in the
    fleet resolved against non-trades. Scoring them as though a
    position existed produced an apparent 31-2 result for a shadow
    model on decisions that never moved a dollar.
    """
    if return_pct is None:
        return None
    s = (predicted_signal or "").strip().upper()
    if s in ("BUY", "STRONG_BUY", "LONG"):
        return float(return_pct)
    if s in ("SHORT", "SELL", "STRONG_SELL"):
        return -float(return_pct)
    return None


def grade_gate(gate_val: Optional[str],
               pnl_pct: Optional[float]) -> Optional[bool]:
    """Was this gate verdict the right call about a trade that was
    TAKEN? A block is right when the position lost money; an allow is
    right when it made money. None inside the noise band — a position
    that went nowhere vindicates neither the reviewer who blocked it
    nor the one who let it through."""
    if gate_val is None or pnl_pct is None:
        return None
    if abs(pnl_pct) < NOISE_PCT:
        return None
    if gate_val == "block":
        return pnl_pct < 0
    return pnl_pct > 0


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
    """True when the stance matched the realized RAW price move, False
    when it opposed it, None ONLY when the signal is unmappable (e.g.
    an apex trade-set string, graded at set level via `agreement`).

    A neutral stance is GRADED, not skipped: HOLD/PASS is right when
    the move stayed inside the noise band and wrong when it missed a
    real move. Symmetrically, a directional stance is wrong when the
    move stayed flat — passing was the better call.

    2026-07-30: this previously returned None for EVERY neutral stance,
    so a primary that said HOLD could not score while a shadow that
    took a direction could. Any more-decisive model — or, once prompt
    variants exist, any more-decisive PROMPT — accumulated wins for
    free against an opponent that was structurally unable to answer.
    That artifact alone produced an apparent 27-5 edge for a shadow
    model on ensemble:adversarial_reviewer that vanished under
    de-duplication. Neither side may be ungradable while the other
    scores.
    """
    if stance_val is None or return_pct is None:
        return None
    flat = abs(return_pct) < NOISE_PCT
    if stance_val == "neutral":
        return flat
    if flat:
        return False
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
    """symbol → sorted [(epoch, return_pct, predicted_signal)] of
    resolved predictions for one profile DB; nearest-within-window
    lookup.

    `predicted_signal` rides along because it says whether a trade was
    actually TAKEN ('BUY'/'SHORT') or passed on ('HOLD') — without it a
    gate specialist's verdict can't be told apart from a moot one.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._by_symbol: Dict[str, List[tuple]] = defaultdict(list)
        try:
            rows = conn.execute(
                "SELECT symbol, timestamp, actual_return_pct, "
                "       predicted_signal "
                "FROM ai_predictions WHERE status = 'resolved' "
                "AND actual_return_pct IS NOT NULL"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # `predicted_signal` missing (older schema) must NOT wipe out
            # every outcome — that would silently empty the page rather
            # than degrade it. Retry without the column: forecast
            # purposes still grade on price; gate purposes become moot
            # because no trade direction is knowable.
            logger.warning(
                "shadow metrics: resolved-prediction query failed (%s: %s) "
                "— retrying without predicted_signal; gate specialists "
                "will read as moot until the column exists",
                type(exc).__name__, exc,
            )
            try:
                rows = [
                    (s, t, r, None) for s, t, r in conn.execute(
                        "SELECT symbol, timestamp, actual_return_pct "
                        "FROM ai_predictions WHERE status = 'resolved' "
                        "AND actual_return_pct IS NOT NULL"
                    ).fetchall()
                ]
            except sqlite3.OperationalError:
                rows = []
        for sym, ts, ret, psig in rows:
            e = _parse_ts(ts)
            if e is not None and sym:
                self._by_symbol[str(sym).upper()].append(
                    (e, float(ret), psig))
        for lst in self._by_symbol.values():
            lst.sort(key=lambda r: r[0])

    def outcome_for(self, symbol: str,
                    epoch: Optional[float]) -> Optional[tuple]:
        """Nearest resolved prediction as `(return_pct,
        predicted_signal)`, or None when nothing lands inside the
        match window."""
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
                    best = (d, lst[j][1], lst[j][2])
        return (best[1], best[2]) if best else None


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
        # Disagreement outcomes. shadow_right/primary_right count every
        # resolved disagreement where that side's stance matched, so
        # both sides CAN be right (opposite stances, one flat outcome
        # is impossible — but a neutral-vs-directional pair on a flat
        # move gives neutral the point and directional the miss).
        "dis_resolved": 0, "shadow_right": 0, "primary_right": 0,
        # The honest head-to-head: rows where exactly one side matched.
        # Read THESE, not shadow_right alone — a side can rack up
        # shadow_right on rows the other side also got right.
        "shadow_only": 0, "primary_only": 0,
        "both_right": 0, "neither_right": 0,
        # Signal unmappable on at least one side (apex trade-set
        # strings), or an outcome inside the noise band: nothing to
        # grade. Excluded from the head-to-head.
        "ungradable": 0,
        # Gate specialists (VETO authority) whose matched prediction
        # records NO trade taken. The verdict gated a position that was
        # never opened, so neither side can be right or wrong about it.
        "moot": 0,
        "dis_pending": 0,
        # Distinct (profile, symbol) pairs behind the resolved
        # disagreements — the EFFECTIVE sample size. The same name is
        # re-reviewed every cycle and resolves against one price move,
        # so raw row counts overstate independence badly (2026-07-30:
        # 68 reviewer rows collapsed to 35 units over 26 symbols, with
        # CVX and TSLA appearing 9 times each). Finalized to the
        # integer `dis_units`.
        "_unit_keys": set(),
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
            match = resolved.outcome_for(
                symbol, _parse_ts(ts)) if symbol else None
            outcome = match[0] if match else None
            predicted_signal = match[1] if match else None
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
                for b in (m, pk, ak):
                    b["dis_resolved"] += 1
                    b["_unit_keys"].add((prof_label, (symbol or "").upper()))
                is_gate = (purpose or "") in _GATE_PURPOSES
                moot = False
                if is_gate:
                    # A VETO-authority verdict gates a trade. Score it
                    # against that trade's P&L, and only when a trade
                    # was actually taken.
                    pnl = trade_pnl(predicted_signal, outcome)
                    if pnl is None:
                        moot = True
                        p_right = s_right = None
                    else:
                        p_right = grade_gate(gate_call(primary_signal), pnl)
                        s_right = grade_gate(gate_call(parsed_signal), pnl)
                else:
                    p_right = grade(p_st, outcome)
                    s_right = grade(s_st, outcome)
                if moot:
                    for b in (m, pk, ak):
                        b["moot"] += 1
                    dis_row["who_right"] = "moot (no trade taken)"
                elif p_right is None or s_right is None:
                    # Set-level / unmappable on one side, or a move
                    # inside the noise band — no basis to compare. Never
                    # credit the gradable side here: that asymmetry is
                    # exactly the bug fixed on 2026-07-30.
                    for b in (m, pk, ak):
                        b["ungradable"] += 1
                    dis_row["who_right"] = "ungradable"
                else:
                    if s_right:
                        for b in (m, pk, ak):
                            b["shadow_right"] += 1
                    if p_right:
                        for b in (m, pk, ak):
                            b["primary_right"] += 1
                    if s_right and p_right:
                        for b in (m, pk, ak):
                            b["both_right"] += 1
                        dis_row["who_right"] = "both"
                    elif s_right:
                        for b in (m, pk, ak):
                            b["shadow_only"] += 1
                        dis_row["who_right"] = "shadow"
                    elif p_right:
                        for b in (m, pk, ak):
                            b["primary_only"] += 1
                        dis_row["who_right"] = "primary"
                    else:
                        for b in (m, pk, ak):
                            b["neither_right"] += 1
                        dis_row["who_right"] = "neither"
            recent_dis.append(dis_row)

    def _finalize(b: Dict) -> Dict:
        g = b["agree"] + b["disagree"]
        b["agreement_pct"] = round(b["agree"] / g * 100, 1) if g else None
        b["avg_latency_ms"] = (b["latency_ms"] // b["latency_n"]
                               if b["latency_n"] else None)
        b["cost"] = round(b["cost"], 4)
        b["dis_units"] = len(b.pop("_unit_keys", ()) or ())
        # Head-to-head share, over rows where exactly one side matched.
        # None when no such row exists — never 0%, which would read as
        # "measured and lost" rather than "not measured yet".
        h2h = b["shadow_only"] + b["primary_only"]
        b["h2h_n"] = h2h
        b["shadow_win_pct"] = (round(b["shadow_only"] / h2h * 100, 1)
                               if h2h else None)
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
