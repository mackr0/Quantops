"""Options lifecycle management — sweep expired contracts.

Item 1a follow-up of COMPETITIVE_GAP_PLAN.md: when an option contract
expires, the open trade row in the journal becomes stale unless we
sweep it. This module:

  1. Finds option trades (signal_type='OPTIONS') with status='open'
     whose expiry has passed.
  2. Queries the broker for the actual outcome (filled? canceled?
     position still held?) — broker is source of truth for paper
     accounts where Alpaca handles assignment automatically.
  3. Marks the row status='closed' with realized P&L, where the
     outcome (worthless / assigned / exercised) comes from the
     broker's OPASN/OPXRC/OPEXP activity stream — never from a
     bar-close ITM estimate (2026-07-20 invariant: the sweep must
     not be able to fabricate share movements; see
     _resolve_expired_option).
  4. Books broker-confirmed assignment/exercise share movements as
     equity-leg journal rows (idempotent), flipping fully-consumed
     stock entries to closed so the reconciler stays coherent.

Rows the broker hasn't ruled on yet are DEFERRED (stay open) for up
to ASSIGNMENT_CONFIRM_GRACE_DAYS, then flagged needs_review for the
operator.
"""
from __future__ import annotations

import logging
from contextlib import closing
from datetime import date as _date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def find_expired_open_options(db_path: str,
                                  today: Optional[_date] = None) -> List[Dict[str, Any]]:
    """Return open option trade rows (single-leg AND multileg) whose
    expiry has passed.

    2026-06-17 — ORPHAN-PROOF FILTER. The pre-fix query matched only
    `signal_type='OPTIONS'`, so every MULTILEG / MULTILEG_OPEN spread
    leg was skipped: after expiry the broker zeroes the contract but
    the leg stays status='open' forever, and get_virtual_positions
    keeps reporting its OCC as a held lot — a permanent orphan (the
    operator's "journal shows open, broker does not"). The filter now
    keys on `occ_symbol IS NOT NULL` instead of a signal_type allow-
    list, because:
      • EVERY option position carries an OCC symbol; nothing else does
        (stocks are NULL; OPTION_EXERCISE synthetic equity legs are
        logged with the underlying symbol and occ_symbol=NULL, so they
        are excluded automatically).
      • A signal_type allow-list silently falls behind — multileg
        entries already use BOTH 'MULTILEG' and 'MULTILEG_OPEN'. Keying
        on occ_symbol covers every present and future option
        signal_type, so a new option type can never re-open this orphan
        class. (Pinned by a structural test.)
    `status='open'` is exact: pending_fill / pending_protective /
    needs_review / closed rows are not swept here.

    Returns rows shaped:
      {id, symbol, side, qty, occ_symbol, option_strategy, expiry,
       strike, price, decision_price, ai_confidence, signal_type, reason}
    """
    today = today or _date.today()
    from journal import _get_conn
    with closing(_get_conn(db_path)) as conn:
        cur = conn.execute(
            """SELECT id, symbol, side, qty, occ_symbol, option_strategy,
                      expiry, strike, price, decision_price, ai_confidence,
                      signal_type, reason
               FROM trades
               WHERE occ_symbol IS NOT NULL
                 AND status='open'
                 AND expiry IS NOT NULL
                 AND expiry < ?
                 -- Exclude CLOSE rows masquerading as expired entries.
                 -- A partner-rollback close (multi_scheduler) is a NEW
                 -- MULTILEG row with the partner's past expiry; if its
                 -- pending_fill decays to 'open' the entry-P&L logic
                 -- would mis-book it. It is a close, not an expired
                 -- entry — skip it here. (O2, 2026-06-17.)
                 AND COALESCE(reason,'') NOT LIKE 'Auto-rollback:%'""",
            (today.isoformat(),),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _option_position_at_broker(api, occ_symbol: str) -> Optional[Dict[str, Any]]:
    """Return broker position for the OCC contract, or None if no
    position exists.

    Alpaca lists option positions alongside equity positions in
    api.list_positions() — the symbol is the OCC string.
    """
    try:
        positions = api.list_positions()
    except Exception as exc:
        logger.warning("Could not list positions for option lookup: %s", exc)
        return None
    for p in positions:
        if getattr(p, "symbol", None) == occ_symbol:
            return {
                "symbol": p.symbol,
                "qty": float(getattr(p, "qty", 0) or 0),
                "avg_entry_price": float(getattr(p, "avg_entry_price", 0) or 0),
                "market_value": float(getattr(p, "market_value", 0) or 0),
            }
    return None


def _underlying_close_at_expiry(symbol: str,
                                    expiry: _date) -> Optional[float]:
    """Fetch the underlying's close price at the expiry date so we can
    determine ITM vs OTM. Falls back to last available close.

    Best-effort: returns None on data-fetch failure (caller treats as
    "can't determine ITM/OTM" → marks needs_review).
    """
    try:
        from market_data import get_bars
        bars = get_bars(symbol, limit=120)
        if bars is None or len(bars) == 0:
            return None
        # bars is a DataFrame with tz-aware index; find the close on or
        # closest before expiry. tz-naive .index lookup is fragile, so
        # iterate.
        for ts, row in bars.iterrows():
            try:
                ts_date = ts.date()
            except (KeyError, ValueError, AttributeError, TypeError) as _bt_exc:
                # Per-bar timestamp parse loop; skip rows with
                # malformed index. Surface for follow-up.
                logger.debug(
                    "options_lifecycle bar timestamp parse failed: %s: %s",
                    type(_bt_exc).__name__, _bt_exc,
                )
                continue
            if ts_date == expiry:
                return float(row["close"])
        # No exact match — use the last bar before expiry
        before = [ts for ts in bars.index if hasattr(ts, "date")
                  and ts.date() <= expiry]
        if before:
            return float(bars.loc[before[-1], "close"])
        return float(bars["close"].iloc[-1])
    except Exception as exc:
        logger.debug("Could not fetch close for %s @ %s: %s",
                     symbol, expiry, exc)
        return None


def _occ_right(option_row: Dict[str, Any]) -> str:
    """FORMAT-AGNOSTIC right (C/P) extraction for a journal option row.

    The journal stores BOTH OCC forms: format_occ_symbol emits the
    OSI space-padded root ("GOOG  260717C00180000") but every submit
    path snaps to Alpaca's compact listed symbol ("GOOG260717C00180000")
    before journaling, so `occ[12]` is the right letter ONLY for
    6-char roots. Reading a fixed index was the 2026-07-20 opex bug:
    compact-form rows silently got right='' (a strike digit), so the
    assignment branch skipped the equity leg AND booked premium-only
    P&L on ITM shorts. parse_occ_symbol parses from the RIGHT of the
    string and handles both forms; option_strategy is the fallback.
    """
    occ = option_row.get("occ_symbol") or ""
    if occ:
        try:
            from options_trader import parse_occ_symbol
            r = (parse_occ_symbol(occ).get("right") or "").upper()
            if r in ("C", "P"):
                return r
        except (ValueError, KeyError, ImportError) as exc:
            logger.debug(
                "_occ_right: parse_occ_symbol(%r) failed (%s: %s) — "
                "falling back to option_strategy inference",
                occ, type(exc).__name__, exc,
            )
    strat = (option_row.get("option_strategy") or "").lower()
    if "call" in strat:
        return "C"
    if "put" in strat:
        return "P"
    return ""


def _occ_compact(occ: str) -> str:
    """Normalize an OCC symbol for comparison: strip the OSI root
    padding so padded and compact forms of the same contract match."""
    return (occ or "").replace(" ", "").upper()


def _is_itm_at_expiry(option_row: Dict[str, Any],
                          underlying_close: float) -> Optional[bool]:
    """Decide if an expired option finished ITM.

    Calls ITM if underlying > strike; puts ITM if underlying < strike.
    Returns None when the OCC right can't be determined.

    NOTE: this is a market-data ESTIMATE (bar close, not settlement
    price). It is only trusted for the safe direction — closing OTM
    rows worthless when the broker no longer holds the contract. It
    must NEVER be the basis for synthesizing share movements; that
    requires a broker OPASN/OPXRC activity (see sweep_expired_options).
    """
    strike = float(option_row.get("strike") or 0)
    if strike <= 0 or underlying_close is None or underlying_close <= 0:
        return None
    right = _occ_right(option_row)
    if right == "C":
        return underlying_close > strike
    if right == "P":
        return underlying_close < strike
    return None


# Broker-truth expiry verdicts -------------------------------------------

# How long after expiry we keep waiting for the broker's OPASN/OPXRC/
# OPEXP activity before flagging the row for the operator. Alpaca
# posts expiration-processing activities over the weekend following a
# Friday expiry; 3 calendar days covers Fri-expiry → Mon-morning.
ASSIGNMENT_CONFIRM_GRACE_DAYS = 3

_EXPIRY_ACTIVITY_TYPES = ("OPEXP", "OPASN", "OPXRC")


def _fetch_expiry_activities(api, since_iso: str) -> Optional[List[Any]]:
    """Fetch OPEXP/OPASN/OPXRC activities from the broker.

    Fetches PER TYPE (2026-07-20): Alpaca rejected the comma-joined
    form as one unknown type ("invalid activity type: ..."), which
    would make this function permanently return None and every ITM row
    defer forever. One call per type survives that API shape, and one
    type failing doesn't blind the others.

    Returns None (not []) when ANY type's fetch failed so callers can
    distinguish "broker said nothing happened" from "we couldn't ask
    completely" — an incomplete answer must DEFER, never guess. (A
    partial list would risk closing an assigned contract as worthless
    because the OPASN fetch happened to be the one that failed.)
    """
    out: List[Any] = []
    all_ok = True
    for a_type in _EXPIRY_ACTIVITY_TYPES:
        try:
            acts = api.get_activities(
                activity_types=a_type, after=since_iso,
            )
            out.extend(list(acts) if acts is not None else [])
        except Exception as exc:
            all_ok = False
            logger.warning(
                "options_lifecycle: get_activities(%s) failed (%s) — "
                "ITM-candidate rows will defer this pass rather than "
                "resolve on incomplete broker data", a_type, exc,
            )
    return out if all_ok else None


def _journal_expiry_activities(conn, occ: str) -> Dict[str, float]:
    """Activities already captured into the journal by
    activities_capture (order_id = Alpaca activity id). Belt for the
    api fetch: the capture job's 7-day window may have already rolled
    past an old expiry that the live get_activities call can't see."""
    out: Dict[str, float] = {}
    try:
        for st, qty in conn.execute(
            "SELECT signal_type, qty FROM trades "
            "WHERE occ_symbol IS NOT NULL AND signal_type IN "
            "      ('OPEXP', 'OPASN', 'OPXRC') "
            "AND REPLACE(UPPER(occ_symbol), ' ', '') = ?",
            (_occ_compact(occ),),
        ).fetchall():
            out[st] = out.get(st, 0.0) + float(qty or 0)
    except Exception as exc:
        logger.debug("journal activity lookup failed for %s: %s", occ, exc)
    return out


def _activities_for_occ(activities: Optional[List[Any]],
                        occ: str) -> Dict[str, float]:
    """Aggregate broker activities for one contract: {type: contracts}."""
    out: Dict[str, float] = {}
    if not activities:
        return out
    target = _occ_compact(occ)
    for a in activities:
        try:
            if _occ_compact(getattr(a, "symbol", "") or "") != target:
                continue
            a_type = getattr(a, "activity_type", "") or ""
            if a_type not in _EXPIRY_ACTIVITY_TYPES:
                continue
            qty = float(getattr(a, "qty", 0) or 0)
            out[a_type] = out.get(a_type, 0.0) + abs(qty)
        except (ValueError, TypeError, AttributeError) as exc:
            logger.warning(
                "_activities_for_occ: malformed broker activity %r "
                "skipped (%s: %s) — if this contract's verdict depends "
                "on it, the row will defer/needs_review rather than "
                "resolve on partial data",
                getattr(a, "id", "?"), type(exc).__name__, exc,
            )
    return out


def _resolve_expired_option(row: Dict[str, Any],
                            broker_position: Optional[Dict[str, Any]],
                            underlying_close: Optional[float],
                            occ_acts: Dict[str, float],
                            activities_available: bool,
                            days_past_expiry: int) -> Dict[str, Any]:
    """Decide the outcome of an expired option row from BROKER TRUTH.

    Returns {"pnl_dollars", "outcome", "reason", "equity_leg"} where
    outcome is one of:
      "expired_worthless" — no assignment/exercise activity; OTM (or
                              broker-confirmed OPEXP). ±premium P&L,
                              no share movement.
      "assigned"          — broker reported OPASN for this contract.
                              Premium P&L on the option row; the share
                              movement is booked as an equity leg
                              sized from the ACTIVITY, at strike.
      "exercised"         — broker reported OPXRC. Option-row P&L is
                              -premium (cost); the intrinsic value
                              lives in the equity leg's strike basis,
                              so it is never double-counted.
      "deferred"          — broker hasn't reported the outcome yet
                              (or we couldn't ask). Row stays open;
                              retried next sweep. NO fabrication.
      "needs_review"      — still unconfirmed after the grace window,
                              or bad input data. Operator review.

    INVARIANT (2026-07-20 opex incident): a share movement is only
    ever booked when the broker's own activity stream (OPASN/OPXRC)
    confirms it happened — never from a bar-close ITM estimate. The
    pre-fix code synthesized "called away" SELL legs from a
    market-data close and fabricated short stock positions the broker
    never held (the certify findings: journal short GOOG/WMT/GOOGL
    with broker flat), tripping the kill switch. An estimate may only
    close rows in the direction that moves NO shares (OTM worthless).
    """
    strategy = row.get("option_strategy", "")
    contracts = int(row.get("qty") or 0)
    premium = float(row.get("decision_price") or row.get("price") or 0)
    side = (row.get("side") or "").lower()
    strike = float(row.get("strike") or 0)
    multiplier = contracts * 100  # 100 shares per contract
    right = _occ_right(row)

    if side not in ("buy", "sell") or contracts <= 0:
        return {
            "pnl_dollars": 0.0, "outcome": "needs_review",
            "reason": (
                f"Unresolvable expired option row: side={side!r} "
                f"qty={contracts} — review manually"
            ),
            "equity_leg": None,
        }

    # ── Broker-confirmed outcomes ────────────────────────────────
    if side == "sell" and occ_acts.get("OPASN"):
        assigned_contracts = min(float(contracts), occ_acts["OPASN"])
        shares = int(round(assigned_contracts * 100))
        if right == "C":
            leg = {"side": "sell", "shares": shares, "price": strike,
                   "note": (
                       f"Short call assigned (broker OPASN "
                       f"x{assigned_contracts:.0f}) — called away "
                       f"{shares} shares at ${strike:.2f}"
                   )}
        elif right == "P":
            leg = {"side": "buy", "shares": shares, "price": strike,
                   "note": (
                       f"Short put assigned (broker OPASN "
                       f"x{assigned_contracts:.0f}) — bought "
                       f"{shares} shares at ${strike:.2f}"
                   )}
        else:
            return {
                "pnl_dollars": None, "outcome": "needs_review",
                "reason": (
                    f"Broker reported OPASN for {row.get('occ_symbol')} "
                    f"but the option right (C/P) is unparseable — "
                    f"cannot book the share movement. Review manually."
                ),
                "equity_leg": None,
            }
        pnl = premium * multiplier
        return {
            "pnl_dollars": pnl, "outcome": "assigned",
            "reason": (
                f"Short {strategy} ASSIGNED — broker OPASN activity "
                f"x{assigned_contracts:.0f} of {contracts}. Premium "
                f"realized: +${pnl:,.2f}. Equity leg booked at strike "
                f"${strike:.2f} from broker activity (never estimated)."
            ),
            "equity_leg": leg,
        }

    if side == "buy" and occ_acts.get("OPXRC"):
        exercised_contracts = min(float(contracts), occ_acts["OPXRC"])
        shares = int(round(exercised_contracts * 100))
        if right == "C":
            leg = {"side": "buy", "shares": shares, "price": strike,
                   "note": (
                       f"Long call exercised (broker OPXRC "
                       f"x{exercised_contracts:.0f}) — bought "
                       f"{shares} shares at ${strike:.2f}"
                   )}
        elif right == "P":
            leg = {"side": "sell", "shares": shares, "price": strike,
                   "note": (
                       f"Long put exercised (broker OPXRC "
                       f"x{exercised_contracts:.0f}) — sold "
                       f"{shares} shares at ${strike:.2f}"
                   )}
        else:
            return {
                "pnl_dollars": None, "outcome": "needs_review",
                "reason": (
                    f"Broker reported OPXRC for {row.get('occ_symbol')} "
                    f"but the option right (C/P) is unparseable — "
                    f"cannot book the share movement. Review manually."
                ),
                "equity_leg": None,
            }
        # Option-row P&L is the premium COST only. The exercised
        # value arrives via the equity leg's strike basis (shares
        # worth more/less than strike), so intrinsic is realized on
        # the stock side exactly once. The pre-fix (intrinsic −
        # premium) here PLUS a strike-basis stock leg double-counted
        # the intrinsic.
        pnl = -premium * multiplier
        return {
            "pnl_dollars": pnl, "outcome": "exercised",
            "reason": (
                f"Long {strategy} EXERCISED — broker OPXRC activity "
                f"x{exercised_contracts:.0f} of {contracts}. Premium "
                f"cost ${abs(pnl):,.2f} realized on this row; intrinsic "
                f"value carried by the equity leg at strike "
                f"${strike:.2f} (booked from broker activity)."
            ),
            "equity_leg": leg,
        }

    if occ_acts.get("OPEXP"):
        pnl = premium * multiplier if side == "sell" else -premium * multiplier
        return {
            "pnl_dollars": pnl, "outcome": "expired_worthless",
            "reason": (
                f"{'Short' if side == 'sell' else 'Long'} {strategy} "
                f"expired worthless — broker OPEXP activity confirms no "
                f"assignment/exercise: {'+' if pnl >= 0 else '-'}"
                f"${abs(pnl):,.2f}"
            ),
            "equity_leg": None,
        }

    # ── No broker activity for this contract ─────────────────────
    is_itm = None
    if underlying_close is not None:
        is_itm = _is_itm_at_expiry(row, underlying_close)
    broker_still_holds = bool(
        broker_position is not None and broker_position.get("qty", 0) != 0)

    if is_itm is False and not broker_still_holds:
        # OTM by close and the contract is gone from the broker: an
        # OTM expiry moves NO shares, so closing worthless is safe
        # even without an OPEXP activity (activity feeds can lag or
        # roll out of the fetch window). If this estimate were ever
        # wrong, an assignment would surface as broker share drift —
        # the certify gate's job — instead of us fabricating shares.
        pnl = premium * multiplier if side == "sell" else -premium * multiplier
        return {
            "pnl_dollars": pnl, "outcome": "expired_worthless",
            "reason": (
                f"{'Short' if side == 'sell' else 'Long'} {strategy} "
                f"expired OTM (close ${underlying_close:.2f} vs strike "
                f"${strike:.2f}; no broker assignment activity): "
                f"{'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}"
            ),
            "equity_leg": None,
        }

    # ITM (or undeterminable, or broker still shows the contract):
    # broker truth hasn't arrived. Wait for it — do NOT guess.
    if days_past_expiry <= ASSIGNMENT_CONFIRM_GRACE_DAYS:
        why = ("activities fetch failed"
               if not activities_available else
               "no OPASN/OPXRC/OPEXP activity yet")
        return {
            "pnl_dollars": None, "outcome": "deferred",
            "reason": (
                f"Expired {'ITM' if is_itm else 'undetermined'} "
                f"({why}; broker_holds_contract={broker_still_holds}) — "
                f"deferring to next sweep for broker confirmation "
                f"(day {days_past_expiry}/{ASSIGNMENT_CONFIRM_GRACE_DAYS})"
            ),
            "equity_leg": None,
        }

    return {
        "pnl_dollars": None, "outcome": "needs_review",
        "reason": (
            f"Expired {days_past_expiry}d ago with no broker "
            f"OPASN/OPXRC/OPEXP activity and "
            f"{'ITM' if is_itm else 'undetermined ITM/OTM'} by close "
            f"(${underlying_close if underlying_close else 0:.2f} vs "
            f"strike ${strike:.2f}) — REVIEW MANUALLY. The sweep never "
            f"books share movements without broker confirmation."
        ),
        "equity_leg": None,
    }


def sweep_expired_options(api, db_path: str,
                              today: Optional[_date] = None) -> Dict[str, Any]:
    """Sweep expired open option trades and mark them closed —
    outcomes decided by BROKER TRUTH, never by price estimates.

    For each expired option:
      1. Ask the broker what happened: one OPASN/OPXRC/OPEXP
         activities fetch per sweep (plus activities already captured
         into the journal by activities_capture).
      2. Decide outcome:
         - broker OPASN (short row)  → assigned + equity leg sized
           from the activity, at strike
         - broker OPXRC (long row)   → exercised + equity leg
         - broker OPEXP              → expired_worthless
         - no activity, OTM by close AND contract gone from broker
                                     → expired_worthless (moves no
           shares, so an estimate is safe here — and ONLY here)
         - no activity, ITM/unknown  → DEFERRED (row stays open) for
           up to ASSIGNMENT_CONFIRM_GRACE_DAYS, then needs_review +
           loud log. The sweep NEVER books a share movement the
           broker didn't confirm — fabricated "called away" legs from
           bar-close guesses are the 2026-07-20 kill-switch incident.
      3. Update the option row's status + pnl + reason.
      4. Book the equity leg idempotently (order_id 'OPX-EQ-<row id>')
         and flip fully-consumed stock entries to closed so the
         reconciler's open-row scan stays coherent.

    Returns summary: {expired_found, closed_worthless, assigned,
    exercised, needs_review, deferred, errors, equity_legs_logged,
    details}.
    """
    today = today or _date.today()
    summary = {
        "expired_found": 0, "closed_worthless": 0, "assigned": 0,
        "exercised": 0, "needs_review": 0, "deferred": 0, "errors": 0,
        "equity_legs_logged": 0, "details": [],
    }

    rows = find_expired_open_options(db_path, today=today)
    summary["expired_found"] = len(rows)

    if not rows:
        return summary

    # ONE broker-activities fetch per sweep, windowed from the oldest
    # expiry in the batch. None (vs []) means the fetch FAILED — every
    # ITM-candidate row defers rather than guessing.
    earliest = min((r.get("expiry") or today.isoformat()) for r in rows)
    activities = _fetch_expiry_activities(api, since_iso=earliest)

    from journal import _get_conn
    conn = _get_conn(db_path)
    try:
        for row in rows:
            try:
                broker_pos = _option_position_at_broker(api, row.get("occ_symbol"))
                # Fetch underlying close at the option's expiry date
                try:
                    expiry_d = _date.fromisoformat(row["expiry"])
                except Exception:
                    expiry_d = today
                underlying_close = _underlying_close_at_expiry(
                    row.get("symbol", ""), expiry_d,
                )

                # Broker verdict for THIS contract: live activities
                # fetch, plus activities already captured into the
                # journal by activities_capture (whose 7-day window
                # may have seen what the live call no longer can).
                occ_acts = _activities_for_occ(
                    activities, row.get("occ_symbol") or "")
                for a_type, qty in _journal_expiry_activities(
                        conn, row.get("occ_symbol") or "").items():
                    occ_acts.setdefault(a_type, qty)

                outcome = _resolve_expired_option(
                    row, broker_pos, underlying_close,
                    occ_acts=occ_acts,
                    activities_available=activities is not None,
                    days_past_expiry=(today - expiry_d).days,
                )

                if outcome["outcome"] == "deferred":
                    # Row stays open; broker confirmation is pending.
                    summary["deferred"] += 1
                    summary["details"].append({
                        "id": row["id"], "occ": row.get("occ_symbol"),
                        **outcome,
                    })
                    logger.info(
                        "Lifecycle: trade %s (%s) deferred — %s",
                        row["id"], row.get("occ_symbol"),
                        outcome["reason"],
                    )
                    continue

                # Status mapping per outcome
                outcome_status_map = {
                    "expired_worthless": "closed",
                    "assigned": "closed",
                    "exercised": "closed",
                    "needs_review": "needs_review",
                }
                new_status = outcome_status_map.get(
                    outcome["outcome"], "needs_review")
                if new_status == "needs_review":
                    logger.error(
                        "Lifecycle NEEDS_REVIEW: trade %s (%s) — %s",
                        row["id"], row.get("occ_symbol"),
                        outcome["reason"],
                    )

                conn.execute(
                    "UPDATE trades SET status=?, pnl=?, reason=? WHERE id=?",
                    (new_status, outcome["pnl_dollars"], outcome["reason"],
                     row["id"]),
                )
                conn.commit()

                # Book the broker-confirmed equity leg. Idempotent via
                # a row-scoped order_id; called-away/exercised-put SELL
                # legs also flip the fully-consumed stock entry rows to
                # closed so the reconciler's open-row scan stays
                # coherent (open entry + broker flat would otherwise
                # halt as an orphan close).
                leg = outcome.get("equity_leg")
                if leg:
                    try:
                        if _book_equity_leg(conn, db_path, row, leg):
                            summary["equity_legs_logged"] += 1
                    except Exception as exc:
                        logger.warning(
                            "Equity leg booking failed for option "
                            "trade %s: %s", row["id"], exc,
                        )

                # Bookkeeping
                if outcome["outcome"] == "expired_worthless":
                    summary["closed_worthless"] += 1
                elif outcome["outcome"] == "assigned":
                    summary["assigned"] += 1
                elif outcome["outcome"] == "exercised":
                    summary["exercised"] += 1
                elif outcome["outcome"] == "needs_review":
                    summary["needs_review"] += 1

                summary["details"].append({
                    "id": row["id"], "occ": row.get("occ_symbol"),
                    **outcome,
                })
                logger.info(
                    "Lifecycle: trade %s (%s) → %s (%s)",
                    row["id"], row.get("occ_symbol"),
                    outcome["outcome"], outcome["reason"],
                )
            except Exception as exc:
                summary["errors"] += 1
                logger.exception(
                    "Lifecycle sweep failed for trade %s: %s", row.get("id"), exc,
                )
    finally:
        conn.close()

    return summary


def _book_equity_leg(conn, db_path: str, row: Dict[str, Any],
                     leg: Dict[str, Any]) -> bool:
    """Write the broker-confirmed share movement for an assignment /
    exercise, and keep the stock entry rows coherent.

    Idempotent: the leg carries order_id 'OPX-EQ-<option row id>'; a
    re-sweep (or a crash between the option-row UPDATE and this call)
    can never double-book the shares.

    SELL legs (called away / exercised long put): the leg is written
    status='closed' (it is an exit, mirroring the reconciler's
    bracket-child INSERT shape), and open BUY entries it fully
    consumes (FIFO order) are flipped to 'closed' — get_virtual_
    positions nets via the rows either way, but the reconciler's
    orphan scan looks at OPEN entry rows vs the broker, and an open
    entry with broker flat halts the profile. A partially-consumed
    entry stays open (the FIFO derives the remainder), matching the
    reconciler's own partial-fill pattern. Any SELL qty beyond all
    long lots becomes a short lot in the FIFO — which is exactly the
    broker's own position after an under-covered assignment, so the
    books still mirror the broker.

    BUY legs (assigned short put / exercised long call): a new long
    lot at strike basis, status='open' — the broker holds the shares.
    """
    from journal import log_trade
    marker = f"OPX-EQ-{row['id']}"
    already = conn.execute(
        "SELECT 1 FROM trades WHERE order_id = ? LIMIT 1", (marker,),
    ).fetchone()
    if already:
        return False
    is_sell = leg["side"] == "sell"
    log_trade(
        symbol=row.get("symbol", ""),
        side=leg["side"],
        qty=leg["shares"],
        price=leg["price"],
        order_id=marker,
        signal_type="OPTION_EXERCISE",
        strategy=row.get("option_strategy", ""),
        reason=leg["note"],
        # Inherit the option entry's AI metadata so the equity leg
        # carries the original conviction.
        ai_reasoning=row.get("ai_reasoning"),
        ai_confidence=row.get("ai_confidence"),
        status="closed" if is_sell else "open",
        decision_price=leg["price"],
        fill_price=leg["price"],
        db_path=db_path,
    )
    if is_sell:
        _close_consumed_stock_entries(
            conn, row.get("symbol", ""), leg["shares"],
            note=f"consumed by assignment leg {marker}",
        )
    return True


def _close_consumed_stock_entries(conn, symbol: str, shares: float,
                                  note: str) -> int:
    """Flip open stock BUY entries that a SELL of `shares` fully
    consumes (FIFO order) to status='closed'. Returns rows flipped."""
    remaining = float(shares)
    flipped = 0
    rows = conn.execute(
        "SELECT id, qty FROM trades "
        "WHERE symbol = ? AND side = 'buy' AND occ_symbol IS NULL "
        "AND COALESCE(status, 'open') = 'open' "
        "ORDER BY timestamp ASC, id ASC",
        (symbol,),
    ).fetchall()
    for rid, qty in rows:
        qty = float(qty or 0)
        if qty <= 0 or remaining < qty - 1e-9:
            break  # partial consumption → entry stays open
        conn.execute(
            "UPDATE trades SET status='closed', "
            "reason = COALESCE(reason || ' | ', '') || ? WHERE id = ?",
            (note, rid),
        )
        remaining -= qty
        flipped += 1
    conn.commit()
    return flipped
