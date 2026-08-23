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
import math
import os
import random
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


# ---------------------------------------------------------------------------
# The bottom line: would following this arm have made more money?
# ---------------------------------------------------------------------------
#
# Counting who was "right" answers a different question than the one the
# operator asks, because being right on a 0.2% drift and being right on
# an 8% collapse count the same. These functions score each side by the
# RETURN POINTS you'd have banked by acting on its call, so the summary
# can state an edge in P&L rather than in debating points.
#
# Deliberately NOT converted to dollars: position sizing isn't recorded
# on the shadow row, so any dollar figure would be an invented constant
# multiplied by a real number — the exact species of fake precision this
# module spent 2026-07-30 removing.

# Below this many scored decisions no verdict is issued, regardless of
# how lopsided the split looks. At a few dozen units a "clear winner" is
# routinely a handful of repeated names.
MIN_DECISIONS_FOR_VERDICT = 30
VERDICT_ALPHA = 0.05

# The daily-trend table shows this many most-recent days. Display-only:
# every aggregate metric on the page is computed over the full history
# (see collect_fleet_metrics — days=None since 2026-08-21).
DAILY_TREND_DAYS = 30


def decision_value(stance_val: Optional[str],
                   return_pct: Optional[float]) -> Optional[float]:
    """Return points banked by ACTING on a forecaster's stance: long
    earns the move, short earns its inverse, neutral takes no position
    and earns nothing."""
    if stance_val is None or return_pct is None:
        return None
    if stance_val == "bullish":
        return float(return_pct)
    if stance_val == "bearish":
        return -float(return_pct)
    return 0.0


def gate_value(gate_val: Optional[str],
               pnl_pct: Optional[float]) -> Optional[float]:
    """Return points banked by FOLLOWING a gate verdict on a trade that
    was taken: blocking it banks nothing (and risks nothing), allowing
    it banks the position's P&L."""
    if gate_val is None or pnl_pct is None:
        return None
    return 0.0 if gate_val == "block" else float(pnl_pct)


def _sign_test_p(wins: int, losses: int) -> Optional[float]:
    """Two-sided exact binomial p-value for `wins` vs `losses` under a
    fair coin. Answers "could a split this lopsided be luck?" without
    assuming the per-decision returns are normally distributed — they
    are visibly not. Falls back to a normal approximation above 2000
    trials, where the exact computation stops being worth its cost."""
    n = wins + losses
    if n == 0:
        return None
    k = min(wins, losses)
    if n > 2000:
        z = abs(wins - losses) / math.sqrt(n)
        return max(0.0, min(1.0, math.erfc(z / math.sqrt(2))))
    tail = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, 2.0 * (tail / (2 ** n)))


_BOOTSTRAP_RESAMPLES = 2000
_BOOTSTRAP_SEED = 20260730   # fixed: the page must not shuffle its own
                             # verdict between two refreshes


def _bootstrap_p(deltas: List[float]) -> Optional[float]:
    """Two-sided bootstrap p-value for "is the mean edge zero?".

    Per-decision returns are heavy-tailed — a handful of 8% moves
    dominate a pile of 0.3% ones — so a t-test's normality assumption
    is not met at these sample sizes. Resampling makes no such
    assumption. Seeded, so the same data always yields the same
    verdict.
    """
    n = len(deltas)
    # Only run it where it can change the answer. Below the verdict
    # threshold the result is "insufficient" whatever the p-value says,
    # and this runs for every bucket on every page load.
    if n < MIN_DECISIONS_FOR_VERDICT:
        return None
    rng = random.Random(_BOOTSTRAP_SEED)
    observed = sum(deltas) / n
    # Centre the sample so the resampling distribution is the null
    # ("true mean is zero"), then ask how often it reaches the
    # observed effect.
    centred = [d - observed for d in deltas]
    at_least = 0
    for _ in range(_BOOTSTRAP_RESAMPLES):
        m = sum(rng.choice(centred) for _ in range(n)) / n
        if abs(m) >= abs(observed):
            at_least += 1
    return (at_least + 1) / (_BOOTSTRAP_RESAMPLES + 1)


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
    """symbol → sorted [(epoch, resolved, return_pct, predicted_signal)]
    of ALL predictions for one profile DB; nearest-within-window lookup
    that grades a shadow row against ITS OWN decision only.

    2026-08-03 — the index previously held only RESOLVED predictions,
    so a review whose own same-cycle prediction was still pending got
    matched to the nearest resolved NEIGHBOR — a different cycle's
    decision. Caught live on p210 GOOGL: the 15:38 review's own
    prediction (SHORT, pending) sat at the same minute, but the
    matcher graded the disagreement "moot" against a resolved HOLD
    from a different cycle 27 minutes away — and the same mechanism
    could just as easily SCORE a row against a neighboring decision's
    P&L. Now the nearest prediction of ANY status is the match (that
    is the decision the review actually gated); if it is pending, the
    disagreement is PENDING — never silently reassigned to a
    neighbor.

    `predicted_signal` rides along because it says whether a trade was
    actually TAKEN ('BUY'/'SHORT') or passed on ('HOLD') — without it a
    gate specialist's verdict can't be told apart from a moot one.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._by_symbol: Dict[str, List[tuple]] = defaultdict(list)
        # decision_id → (is_resolved, return_pct, predicted_signal):
        # the EXACT join (2026-08-06). Populated when the column
        # exists; empty on pre-migration DBs.
        self._by_decision: Dict[str, tuple] = {}
        try:
            rows = conn.execute(
                "SELECT symbol, timestamp, status, actual_return_pct, "
                "       predicted_signal, decision_id "
                "FROM ai_predictions"
            ).fetchall()
        except sqlite3.OperationalError:
            try:
                rows = [
                    (s, t, st, r, p, None) for s, t, st, r, p in
                    conn.execute(
                        "SELECT symbol, timestamp, status, "
                        "actual_return_pct, predicted_signal "
                        "FROM ai_predictions"
                    ).fetchall()
                ]
            except sqlite3.OperationalError as exc:
                # `predicted_signal` missing (older schema) must NOT
                # wipe out every outcome — that would silently empty
                # the page rather than degrade it. Retry without the
                # column: forecast purposes still grade on price; gate
                # purposes become moot because no trade direction is
                # knowable.
                logger.warning(
                    "shadow metrics: prediction query failed (%s: %s) "
                    "— retrying without predicted_signal; gate "
                    "specialists will read as moot until the column "
                    "exists",
                    type(exc).__name__, exc,
                )
                try:
                    rows = [
                        (s, t, st, r, None, None) for s, t, st, r in
                        conn.execute(
                            "SELECT symbol, timestamp, status, "
                            "actual_return_pct FROM ai_predictions"
                        ).fetchall()
                    ]
                except sqlite3.OperationalError:
                    rows = []
        for sym, ts, status, ret, psig, did in rows:
            e = _parse_ts(ts)
            if e is None or not sym:
                continue
            is_resolved = (str(status or "").lower() == "resolved"
                           and ret is not None)
            entry = (e, is_resolved,
                     float(ret) if ret is not None else None, psig)
            self._by_symbol[str(sym).upper()].append(entry)
            if did:
                self._by_decision[str(did)] = (
                    is_resolved, entry[2], psig)
        for lst in self._by_symbol.values():
            lst.sort(key=lambda r: r[0])

    def outcome_for(self, symbol: str,
                    epoch: Optional[float]) -> Optional[tuple]:
        """Outcome of the review's OWN decision: the nearest prediction
        of ANY status inside the match window. Returns `(return_pct,
        predicted_signal)` when that row is resolved, or None when the
        row is still pending / nothing lands inside the window. On an
        exact-distance tie the resolved row wins (never discard an
        available grade to a same-second pending twin)."""
        if not symbol or epoch is None:
            return None
        lst = self._by_symbol.get(symbol.upper())
        if not lst:
            return None
        i = bisect_left(lst, (epoch,))
        best = None
        # i+1 included: same-epoch twins (dual-class dedupe writes two
        # rows in one second) both sort at/after the bisect point, so a
        # two-neighbor scan can miss the resolved twin.
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(lst):
                d = abs(lst[j][0] - epoch)
                if d > _MATCH_WINDOW_SEC:
                    continue
                cand = (d, not lst[j][1], lst[j])
                if best is None or cand[:2] < best[:2]:
                    best = cand
        if best is None:
            return None
        _e, resolved, ret, psig = best[2]
        if not resolved:
            return None  # own decision not resolved yet → pending
        return (ret, psig)

    def match_for(self, symbol: str, epoch: Optional[float],
                  decision_id: Optional[str]) -> tuple:
        """Preferred lookup (2026-08-06): `(outcome, predicted_signal,
        method)` where method is 'exact' (joined by decision_id — an
        identity, zero ambiguity), 'window' (nearest-in-time fallback
        for rows predating the id plumbing), or None (no match at
        all). outcome is None when the matched decision is still
        pending."""
        if decision_id:
            hit = self._by_decision.get(str(decision_id))
            if hit is not None:
                is_resolved, ret, psig = hit
                if not is_resolved:
                    return (None, None, "exact")  # pending, exactly
                return (ret, psig, "exact")
            # id present on the shadow row but no prediction carries
            # it (prediction write raced/failed) — fall back rather
            # than lose the row.
        m = self.outcome_for(symbol, epoch)
        if m is None:
            return (None, None, None)
        return (m[0], m[1], "window")


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
        "cost": 0.0, "cost_30d": 0.0, "latency_ms": 0, "latency_n": 0,
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
        # 2026-08-06 — how each RESOLVED disagreement found its
        # outcome: 'exact' = joined by decision_id (an identity),
        # 'window' = nearest-in-time fallback (rows predating the
        # decision-id plumbing). Rendered on /shadow so the operator
        # can watch inference-matched evidence age out.
        "match_exact": 0,
        "match_window": 0,
        # 2026-08-11 — prompt-variant process metrics: on VETO-
        # authority calls, how often did this arm block vs the primary
        # ON THE SAME CALLS? For a variant arm this IS the experiment
        # (the rewrite exists to veto less, with evidence); the page
        # showed no stat for it.
        "gate_calls": 0,
        "gate_blocks": 0,
        "gate_primary_blocks": 0,
        # Distinct (profile, symbol) pairs behind the resolved
        # disagreements — the EFFECTIVE sample size. The same name is
        # re-reviewed every cycle and resolves against one price move,
        # so raw row counts overstate independence badly (2026-07-30:
        # 68 reviewer rows collapsed to 35 units over 26 symbols, with
        # CVX and TSLA appearing 9 times each). Finalized to the
        # integer `dis_units`.
        "_unit_keys": set(),
        # (profile, symbol) -> [return-point deltas of following the
        # shadow instead of the primary]. Collapsed to ONE figure per
        # unit at finalize, so nine reviews of CVX can't count as nine
        # independent bets on CVX.
        "_edge_by_unit": {},
    }


def _finalize_edge(edge_by_unit: Dict[tuple, List[float]]) -> Dict[str, Any]:
    """Turn per-unit return-point deltas into the plain-English verdict
    the /shadow page leads with.

    One figure per (profile, symbol) unit — the mean of that unit's
    deltas — so a name reviewed nine times contributes one bet, not
    nine. `verdict` is 'insufficient' unless the split is both large
    enough (MIN_DECISIONS_FOR_VERDICT) and unlikely enough under a fair
    coin (VERDICT_ALPHA). Refusing to call it is the DEFAULT, not an
    error state: this page previously reported a 27-5 rout that was
    entirely measurement artifact.
    """
    unit_deltas = [sum(v) / len(v) for v in edge_by_unit.values() if v]
    n = len(unit_deltas)
    empty = {"edge_points": None, "edge_per_decision": None,
             "edge_n": 0, "edge_wins": 0, "edge_losses": 0,
             "edge_ties": 0, "edge_p": None, "edge_p_sign": None,
             "verdict": "insufficient", "verdict_leader": None,
             "verdict_line": "No scored decisions yet"}
    if not n:
        return empty
    # A delta under 0.05 points is a rounding artefact, not a decision
    # that went either way.
    wins = sum(1 for d in unit_deltas if d > 0.05)
    losses = sum(1 for d in unit_deltas if d < -0.05)
    ties = n - wins - losses
    total = sum(unit_deltas)
    mean = total / n
    # TWO different questions, and they can disagree:
    #   p_sign  — does this arm win more OFTEN? (fair-coin test)
    #   p_money — does it win more MONEY? (is mean edge != 0?)
    # An arm can lose most decisions and still be ahead on points by
    # catching the few large moves, which is what the operator actually
    # cares about — so the VERDICT is gated on p_money, and p_sign is
    # reported beside it so a lopsided-but-lucky split stays visible.
    p_sign = _sign_test_p(wins, losses)
    p_money = _bootstrap_p(unit_deltas)
    decisive = (n >= MIN_DECISIONS_FOR_VERDICT
                and p_money is not None and p_money < VERDICT_ALPHA)
    if not decisive:
        leader, verdict = None, "insufficient"
        if n < MIN_DECISIONS_FOR_VERDICT or p_money is None:
            line = (f"Not enough evidence — {n} scored decision"
                    f"{'' if n == 1 else 's'}, need at least "
                    f"{MIN_DECISIONS_FOR_VERDICT}")
        else:
            line = (f"Not enough evidence — {mean:+.2f} pts/decision "
                    f"over {n} is within chance "
                    f"(p={p_money:.2f})")
    elif total > 0:
        leader, verdict = "shadow", "shadow_better"
        line = (f"Shadow arm is ahead: {mean:+.2f} return points per "
                f"decision, {total:+.0f} total over {n} decisions "
                f"({wins}W/{losses}L, p={p_money:.3f})")
    else:
        leader, verdict = "primary", "primary_better"
        line = (f"Primary is ahead: {-mean:+.2f} return points per "
                f"decision, {-total:+.0f} total over {n} decisions "
                f"({losses}W/{wins}L, p={p_money:.3f})")
    return {"edge_points": round(total, 2),
            "edge_per_decision": round(mean, 3),
            "edge_n": n, "edge_wins": wins, "edge_losses": losses,
            "edge_ties": ties,
            "edge_p": (round(p_money, 4) if p_money is not None else None),
            "edge_p_sign": (round(p_sign, 4)
                            if p_sign is not None else None),
            "verdict": verdict, "verdict_leader": leader,
            "verdict_line": line}


def collect_fleet_metrics(profile_dbs: List[str],
                          days: Optional[int] = None,
                          profile_names: Optional[Dict[str, str]] = None,
                          ) -> Dict[str, Any]:
    """Aggregate the full metric set across profile DBs.

    `days=None` (the default since 2026-08-21) reads the ENTIRE shadow
    history: the page states the experiment's full current standing, so
    a rolling window that silently drops the earliest evidence off the
    back is wrong for it — verdicts would weaken as scored decisions
    age out, looking like regression when nothing changed. Pass an int
    only for a deliberately bounded slice. Two windowed figures remain
    regardless: `cost_30d` (spend is judged as a run-rate, not a
    lifetime total) and the daily trend (capped at DAILY_TREND_DAYS
    rows — a per-day table can't grow a row per trading day forever).

    `profile_names` (2026-08-11): optional {db_path: display name} so
    the page shows the profile's NAME instead of the obtuse pNNN
    label; missing entries fall back to pNNN.

    Returns a dict the /shadow template renders directly:
      overview, per_model, by_purpose, by_primary_action,
      recent_disagreements, daily, standings.
    """
    cutoff = None
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)
                  ).strftime("%Y-%m-%d %H:%M:%S")
    cost_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)
                   ).strftime("%Y-%m-%d %H:%M:%S")
    per_model: Dict[str, Dict] = defaultdict(_model_bucket)
    by_purpose: Dict[tuple, Dict] = defaultdict(_model_bucket)
    by_action: Dict[tuple, Dict] = defaultdict(_model_bucket)
    daily: Dict[tuple, Dict] = defaultdict(
        lambda: {"graded": 0, "agree": 0})
    recent_dis: List[Dict] = []
    overview = {"calls": 0, "graded": 0, "cost": 0.0, "cost_30d": 0.0,
                "profiles": 0, "since_days": days,
                "daily_trend_days": DAILY_TREND_DAYS}

    for db in profile_dbs:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            where = "WHERE timestamp >= ? " if cutoff else ""
            params = (cutoff,) if cutoff else ()
            try:
                rows = conn.execute(
                    "SELECT timestamp, purpose, provider, model, "
                    " parsed_signal, agreement, error, cost_usd, "
                    " latency_ms, primary_parsed, decision_id "
                    "FROM ai_shadow_calls " + where +
                    "ORDER BY id",
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                # decision_id may be missing on a pre-migration DB —
                # degrade to the legacy shape (window matching only)
                # rather than dropping the profile from the page.
                try:
                    rows = [
                        tuple(r) + (None,) for r in conn.execute(
                            "SELECT timestamp, purpose, provider, "
                            " model, parsed_signal, agreement, error, "
                            " cost_usd, latency_ms, primary_parsed "
                            "FROM ai_shadow_calls " + where +
                            "ORDER BY id",
                            params,
                        ).fetchall()
                    ]
                except sqlite3.OperationalError:
                    continue
            if not rows:
                continue
            overview["profiles"] += 1
            resolved = _ResolvedIndex(conn)
        finally:
            conn.close()

        # basename FIRST: callers pass absolute paths, and replacing only
        # the filename part left the directory attached, rendering
        # "/opt/quantopsai/p212" in the page's Profile column.
        prof_label = os.path.basename(db).replace(
            "quantopsai_profile_", "p").replace(".db", "")
        # 2026-08-11 — the operator's display name beats the pNNN
        # shorthand wherever the caller can supply it.
        if profile_names:
            prof_label = (profile_names.get(db)
                          or profile_names.get(prof_label)
                          or prof_label)
        for (ts, purpose, provider, model, parsed_signal, agreement,
             err, cost, latency, primary_parsed, row_decision_id) in rows:
            mkey = f"{provider}:{model}"
            m = per_model[mkey]
            m["calls"] += 1
            overview["calls"] += 1
            m["cost"] += float(cost or 0)
            overview["cost"] += float(cost or 0)
            # Run-rate spend: the standings' cost column answers "what
            # is this arm costing me NOW", so it stays a 30-day figure
            # even though every evidence metric is all-history.
            if str(ts) >= cost_cutoff:
                m["cost_30d"] += float(cost or 0)
                overview["cost_30d"] += float(cost or 0)
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
            # Prompt-variant process metrics: same-call block rates on
            # gate purposes (2026-08-11).
            if (purpose or "") in _GATE_PURPOSES:
                _arm_gate = gate_call(parsed_signal)
                _pri_gate = gate_call(primary_signal)
                if _arm_gate is not None and _pri_gate is not None:
                    m["gate_calls"] += 1
                    if _arm_gate == "block":
                        m["gate_blocks"] += 1
                    if _pri_gate == "block":
                        m["gate_primary_blocks"] += 1
            p_st = stance(primary_signal)
            # Group by what the primary actually DID. `stance()` maps
            # VETO to "bearish", so grouping gate specialists by stance
            # filed every reviewer veto under a directional heading —
            # the same category error the SCORING was fixed for: a veto
            # is not a forecast that the price will fall. Gate purposes
            # get their own allow/block rows.
            if (purpose or "") in _GATE_PURPOSES:
                _gc = gate_call(primary_signal)
                if _gc:
                    action_key = "gate: " + _gc
                elif (primary_signal or "").strip().upper() in (
                        "SELL", "STRONG_SELL"):
                    # Advice about closing a DIFFERENT, already-held
                    # position — not a ruling on the candidate under
                    # review, so it never counts as a block on it.
                    action_key = "gate: exit advice"
                else:
                    action_key = "gate: unrecognised"
            else:
                action_key = p_st or "set-level"
            ak = by_action[(action_key, mkey)]
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
            # 2026-08-23 (docs/25 item 1.3) — the apex batch_select
            # call was "set-level / ungradable": its decision is a
            # whole trade SET. Explode the two sets into per-symbol
            # stances (a symbol one side picked and the other didn't
            # is a directional call vs a HOLD) and score each symbol
            # against its own outcome, so the most important decision
            # in the system is finally compared.
            if (purpose or "") == "batch_select":
                n_scored = _score_batch_select_pair(
                    primary_signal, parsed_signal, resolved,
                    _parse_ts(ts), prof_label, (m, pk, ak))
                if n_scored == 0:
                    for b in (m, pk, ak):
                        b["dis_pending"] += 1
                recent_dis.append({
                    "ts": str(ts)[:16], "profile": prof_label,
                    "symbol": "(trade set)", "purpose": purpose,
                    "model": mkey,
                    "primary": primary_signal or "?",
                    "shadow": parsed_signal or "?",
                    "outcome_pct": None,
                    "who_right": (f"{n_scored} symbols scored"
                                  if n_scored else "pending"),
                })
                continue
            outcome, predicted_signal, match_method = resolved.match_for(
                symbol, _parse_ts(ts), row_decision_id)
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
                    if match_method == "exact":
                        b["match_exact"] += 1
                    elif match_method == "window":
                        b["match_window"] += 1
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
                # P&L edge: what following the shadow instead of the
                # primary would have banked on THIS decision. Computed
                # for every scored row (including both-right and
                # neither-right, where the size of the move is exactly
                # what the win/loss counters throw away) but never for
                # moot rows — no position, no P&L.
                if not moot:
                    if is_gate:
                        pnl = trade_pnl(predicted_signal, outcome)
                        p_val = gate_value(gate_call(primary_signal), pnl)
                        s_val = gate_value(gate_call(parsed_signal), pnl)
                    else:
                        p_val = decision_value(p_st, outcome)
                        s_val = decision_value(s_st, outcome)
                    if p_val is not None and s_val is not None:
                        key = (prof_label, (symbol or "").upper())
                        for b in (m, pk, ak):
                            b["_edge_by_unit"].setdefault(
                                key, []).append(s_val - p_val)
                        dis_row["edge_pts"] = round(s_val - p_val, 2)
            recent_dis.append(dis_row)

    def _finalize(b: Dict) -> Dict:
        g = b["agree"] + b["disagree"]
        b["agreement_pct"] = round(b["agree"] / g * 100, 1) if g else None
        b["avg_latency_ms"] = (b["latency_ms"] // b["latency_n"]
                               if b["latency_n"] else None)
        b["cost"] = round(b["cost"], 4)
        b["cost_30d"] = round(b.get("cost_30d", 0.0), 4)
        b["dis_units"] = len(b.pop("_unit_keys", ()) or ())
        # Head-to-head share, over rows where exactly one side matched.
        # None when no such row exists — never 0%, which would read as
        # "measured and lost" rather than "not measured yet".
        h2h = b["shadow_only"] + b["primary_only"]
        b["h2h_n"] = h2h
        b["shadow_win_pct"] = (round(b["shadow_only"] / h2h * 100, 1)
                               if h2h else None)
        # Same-call gate block rates (2026-08-11): None when the arm
        # saw no gate calls — never 0%, which would read as measured.
        gc = b.get("gate_calls", 0)
        b["gate_block_pct"] = (round(b["gate_blocks"] / gc * 100, 1)
                               if gc else None)
        b["gate_primary_block_pct"] = (
            round(b["gate_primary_blocks"] / gc * 100, 1)
            if gc else None)
        b.update(_finalize_edge(b.pop("_edge_by_unit", {}) or {}))
        return b

    per_model_out = {k: _finalize(dict(v))
                     for k, v in sorted(per_model.items())}
    by_purpose_out: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for (purpose, mkey), v in sorted(by_purpose.items()):
        by_purpose_out[purpose][mkey] = _finalize(dict(v))
    # Purposes-covered scope per arm (2026-08-11): a variant that fires
    # on ONE purpose by design must read as scoped, not absent.
    for mk, v in per_model_out.items():
        pv = sorted(p for (p, k2) in by_purpose if k2 == mk)
        v["purposes_covered"] = len(pv)
        v["purposes_list"] = pv
    by_action_out: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for (act, mkey), v in sorted(by_action.items()):
        by_action_out[act][mkey] = _finalize(dict(v))
    daily_out: Dict[str, Dict[str, Any]] = defaultdict(dict)
    for (day, mkey), v in sorted(daily.items()):
        pct = (round(v["agree"] / v["graded"] * 100, 1)
               if v["graded"] else None)
        daily_out[day][mkey] = {"graded": v["graded"],
                                "agreement_pct": pct}
    # The daily trend is a recent-activity view, not an evidence store:
    # cap it at the latest DAILY_TREND_DAYS days so the all-history
    # page doesn't grow a table row per trading day forever. Every
    # aggregate above is computed over the FULL row set.
    recent_days = set(sorted(daily_out.keys())[-DAILY_TREND_DAYS:])
    daily_out = {d: v for d, v in daily_out.items() if d in recent_days}
    recent_dis.sort(key=lambda r: r["ts"], reverse=True)
    overview["cost"] = round(overview["cost"], 4)
    overview["cost_30d"] = round(overview["cost_30d"], 4)
    return {
        "verdict_min_decisions": MIN_DECISIONS_FOR_VERDICT,
        "overview": overview,
        "per_model": per_model_out,
        "by_purpose": dict(by_purpose_out),
        "by_primary_action": dict(by_action_out),
        "recent_disagreements": _recent_selection(recent_dis),
        "daily": dict(daily_out),
        "standings": _standings(per_model_out),
    }


def _parse_trade_set(sig: Optional[str]) -> Dict[str, str]:
    """'AAPL:BUY,MSFT:SELL' -> {'AAPL': 'BUY', 'MSFT': 'SELL'}; 'PASS' or
    anything unparseable -> {} (an empty set is a deliberate
    'no trades' decision)."""
    out: Dict[str, str] = {}
    if not sig or sig.strip().upper() == "PASS":
        return out
    for part in sig.split(","):
        if ":" not in part:
            continue
        sym, _, act = part.partition(":")
        sym = sym.strip().upper()
        if sym and sym != "?":
            out[sym] = act.strip().upper()
    return out


def _score_batch_select_pair(primary_signal: Optional[str],
                             shadow_signal: Optional[str],
                             resolved: "_ResolvedIndex",
                             epoch: Optional[float],
                             prof_label: str,
                             buckets) -> int:
    """Score an apex-call disagreement symbol by symbol. For every
    symbol in the union of the two trade sets, each side's stance is
    its action if it picked the symbol, else neutral (it chose NOT to
    trade the name — a real decision, graded like a HOLD). Outcomes
    come from the primary's own prediction rows by symbol and time
    (batch_select spans many candidates, so it carries no single
    decision id). Returns the number of symbols scored; unresolved
    symbols are left for a later render."""
    p_set = _parse_trade_set(primary_signal)
    s_set = _parse_trade_set(shadow_signal)
    scored = 0
    for sym in sorted(set(p_set) | set(s_set)):
        p_st = stance(p_set[sym]) if sym in p_set else "neutral"
        s_st = stance(s_set[sym]) if sym in s_set else "neutral"
        if p_st is None or s_st is None or p_st == s_st:
            continue
        outcome, _psig, match_method = resolved.match_for(sym, epoch, None)
        if outcome is None:
            continue
        p_right = grade(p_st, outcome)
        s_right = grade(s_st, outcome)
        p_val = decision_value(p_st, outcome)
        s_val = decision_value(s_st, outcome)
        if p_right is None or s_right is None or p_val is None or s_val is None:
            for b in buckets:
                b["ungradable"] += 1
            continue
        scored += 1
        key = (prof_label, sym)
        for b in buckets:
            b["dis_resolved"] += 1
            b["_unit_keys"].add(key)
            if match_method == "exact":
                b["match_exact"] += 1
            elif match_method == "window":
                b["match_window"] += 1
            if s_right:
                b["shadow_right"] += 1
            if p_right:
                b["primary_right"] += 1
            if s_right and p_right:
                b["both_right"] += 1
            elif s_right:
                b["shadow_only"] += 1
            elif p_right:
                b["primary_only"] += 1
            else:
                b["neither_right"] += 1
            b["_edge_by_unit"].setdefault(key, []).append(s_val - p_val)
    return scored


def _recent_selection(recent_dis: List[Dict],
                      per_arm: int = 20, cap: int = 60) -> List[Dict]:
    """Latest disagreements with EVERY arm represented. 2026-08-11 —
    two defects: the old `[:60]` slice was in profile-then-insertion
    order (not newest-first), and a 14:1 call-volume mismatch meant a
    low-volume arm (the prompt variant) effectively never appeared.
    Now: newest-first per arm, up to `per_arm` rows each, merged
    newest-first, capped at `cap`."""
    by_model: Dict[str, List[Dict]] = defaultdict(list)
    for d in recent_dis:
        by_model[d.get("model") or "?"].append(d)
    picked: List[Dict] = []
    for rows in by_model.values():
        rows.sort(key=lambda d: d.get("ts") or "", reverse=True)
        picked.extend(rows[:per_arm])
    picked.sort(key=lambda d: d.get("ts") or "", reverse=True)
    return picked[:cap]


def _standings(per_model: Dict[str, Dict]) -> Dict[str, list]:
    """Rank arms relative to the shared opponent (the primary), from
    each arm's own pairwise verdict. 2026-08-11 — the operator read
    'Primary is better' on one row and 'This arm is better' on
    another and reasonably asked which model wins overall: every row
    is a separate head-to-head against the SAME primary, so the
    ordering falls out of chaining them — but the page must state it,
    not leave the reader to infer transitively.

    Returns {'better': [(model, edge/dec)...] sorted best-first,
    'worse': [...] sorted worst-first, 'undecided': [model...]}.
    Undecided arms are never ranked — refusing is the page's whole
    ethic. The ranking is THROUGH the common opponent (each arm is
    scored on its own disagreement set), not a direct arm-vs-arm
    trial; the template says so."""
    better, worse, undecided = [], [], []
    for mk, v in per_model.items():
        verdict = v.get("verdict")
        edge = v.get("edge_per_decision")
        if verdict == "shadow_better":
            better.append((mk, edge))
        elif verdict == "primary_better":
            worse.append((mk, edge))
        else:
            undecided.append(mk)
    better.sort(key=lambda x: -(x[1] or 0))
    worse.sort(key=lambda x: (x[1] or 0))
    return {"better": better, "worse": worse,
            "undecided": sorted(undecided)}
