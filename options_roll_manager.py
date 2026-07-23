"""Phase C1 of OPTIONS_PROGRAM_PLAN.md — roll mechanics.

When an option position is near expiry, three decisions can apply:

  1. AUTO-CLOSE: short premium positions (covered_call, CSP, credit
     spreads) at ≥AUTO_CLOSE_PROFIT_PCT of max profit get closed
     early. Reasoning: theta decay rate flattens late in the cycle
     and gamma risk rises sharply — locking in 80% of max gain
     beats holding for the last 20% in the assignment-risk zone.

  2. ROLL_RECOMMEND: the position is profitable but the original
     thesis (per the AI's `ai_reasoning` on the row) still holds.
     Surface to the AI prompt: "consider rolling to next expiry."
     The AI proposes via MULTILEG_OPEN if it agrees.

  3. HOLD: long-premium losers, or credit positions far from max
     profit, just expire as-is. The lifecycle sweep handles them.

Out of scope here (separate sessions): wheel automation (C3), which
is the natural follow-up — when a CSP gets closed via auto-close
or assigned, the wheel state machine takes over the cycle.
"""
from __future__ import annotations

import logging
from contextlib import closing
from datetime import date as _date, timedelta
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Roll-window threshold defaults. OPEN_ITEMS #10 — these are now
# per-profile tunable knobs (UserContext fields, settings UI). Module
# constants stay as fallbacks when a function is called without ctx.
ROLL_WINDOW_DAYS = 7
AUTO_CLOSE_PROFIT_PCT = 0.80
ROLL_RECOMMEND_PROFIT_PCT = 0.50


def _close_pnl_estimate(entry_side: str, premium_in: float,
                        cur_price: float, qty: int) -> float:
    """Direction-aware realized-P&L estimate for closing one option leg.

    2026-07-22 — the p212 GOOG-spread incident: the old inline formula
    `(premium_in - cur_price) * mult` is only correct for a SHORT leg.
    Applying it to the long 380C booked +2,148 where the truth was
    −2,148 — fabricating +4,608 of realized P&L and latching the
    integrity kill switch for a full trading day. Delegates to
    journal.realized_option_close_pnl — the SINGLE source of the sign
    convention (the bug existed precisely because this path had its
    own inline copy). Returns None when inputs are missing/non-sane;
    callers journal pnl NULL and the fill-true recompute books it."""
    from journal import realized_option_close_pnl
    return realized_option_close_pnl(premium_in, cur_price, qty,
                                     entry_side)


def find_near_expiry_options(db_path: str,
                                today: Optional[_date] = None,
                                window_days: int = ROLL_WINDOW_DAYS
                                ) -> List[Dict[str, Any]]:
    """Return open option trade rows whose expiry is within
    `window_days` from `today` (and not already past)."""
    today = today or _date.today()
    cutoff = today + timedelta(days=window_days)
    from journal import _get_conn
    with closing(_get_conn(db_path)) as conn:
        cur = conn.execute(
            """SELECT id, symbol, side, qty, occ_symbol, option_strategy,
                      expiry, strike, price, decision_price,
                      ai_confidence, ai_reasoning, signal_type, timestamp,
                      order_id
               FROM trades
               WHERE signal_type IN ('OPTIONS', 'MULTILEG')
                 AND status='open'
                 AND expiry IS NOT NULL
                 AND expiry >= ?
                 AND expiry <= ?""",
            (today.isoformat(), cutoff.isoformat()),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _is_credit_position(option_row: Dict[str, Any]) -> bool:
    """Heuristic: short side OR strategy name implies credit."""
    side = option_row.get("side", "").lower()
    strategy = (option_row.get("option_strategy") or "").lower()
    if side == "sell":
        return True
    if any(name in strategy for name in (
        "covered_call", "cash_secured_put",
        "bull_put_spread", "bear_call_spread",
        "iron_condor", "iron_butterfly",
    )):
        return True
    return False


def _profit_pct_of_max(option_row: Dict[str, Any],
                          current_market_value_per_contract: float
                          ) -> Optional[float]:
    """For CREDIT positions: what fraction of max profit have we
    captured? 1.0 = full profit, 0.0 = no profit yet, negative = at loss.

    For credit: we collected premium; max profit is the premium itself.
    Captured = (premium - current_value) / premium. When current option
    price drops to $0, we have full profit (=1.0).
    """
    premium_collected = float(
        option_row.get("decision_price") or option_row.get("price") or 0)
    if premium_collected <= 0:
        return None
    captured = (premium_collected - current_market_value_per_contract) / premium_collected
    return captured


def evaluate_for_roll(option_row: Dict[str, Any],
                          current_market_value_per_contract: Optional[float],
                          auto_close_profit_pct: float = AUTO_CLOSE_PROFIT_PCT,
                          roll_recommend_profit_pct: float = ROLL_RECOMMEND_PROFIT_PCT,
                          ) -> Dict[str, Any]:
    """Decide what to do with one near-expiry option position.

    Args:
        option_row: row from `find_near_expiry_options`.
        current_market_value_per_contract: per-share quote of the
            option (mid or last). When None, returns "HOLD" (we
            can't make an informed decision without a price).

    Returns: {action, reason, profit_pct} where action is:
        AUTO_CLOSE        — short credit position at ≥80% max profit
        ROLL_RECOMMEND    — profitable; surface to AI for roll proposal
        HOLD              — not enough edge to act
    """
    if current_market_value_per_contract is None:
        return {
            "action": "HOLD", "reason": "No quote for current option price",
            "profit_pct": None,
        }

    is_credit = _is_credit_position(option_row)
    if not is_credit:
        # Long premium positions — let them expire or roll if AI wants
        # a future-dated entry. We don't auto-close longs because
        # there's no "max profit" boundary to anchor against.
        return {
            "action": "HOLD",
            "reason": "Long-premium position — expiry handled by lifecycle",
            "profit_pct": None,
        }

    profit_pct = _profit_pct_of_max(option_row,
                                       current_market_value_per_contract)
    if profit_pct is None:
        return {
            "action": "HOLD",
            "reason": "Could not compute profit percentage (premium=0?)",
            "profit_pct": None,
        }

    if profit_pct >= auto_close_profit_pct:
        return {
            "action": "AUTO_CLOSE",
            "reason": (
                f"Credit position at {profit_pct*100:.0f}% of max profit "
                f"(≥{auto_close_profit_pct*100:.0f}% threshold). Close "
                f"early to avoid late-cycle assignment risk."
            ),
            "profit_pct": profit_pct,
        }

    if profit_pct >= roll_recommend_profit_pct:
        return {
            "action": "ROLL_RECOMMEND",
            "reason": (
                f"Credit position at {profit_pct*100:.0f}% of max profit. "
                f"Consider rolling to next expiry if thesis still holds."
            ),
            "profit_pct": profit_pct,
        }

    return {
        "action": "HOLD",
        "reason": f"Profit at {profit_pct*100:.0f}% (below roll threshold)",
        "profit_pct": profit_pct,
    }


def render_roll_recommendations_for_prompt(
    db_path: str,
    quote_lookup: Optional[Callable[[str], Optional[float]]] = None,
    today: Optional[_date] = None,
) -> str:
    """Build a NEAR-EXPIRY POSITIONS prompt section listing positions
    where the AI should consider rolling. Empty when nothing is in
    the roll window.

    Args:
        db_path: profile journal DB.
        quote_lookup: callable(occ_symbol) → current option mid price
            per share. None → use the row's last `price` field as a
            stale fallback.
        today: override for testing.
    """
    rows = find_near_expiry_options(db_path, today=today)
    if not rows:
        return ""

    actionable: List[str] = []
    for row in rows:
        occ = row.get("occ_symbol")
        # Get current option price
        cur_price = None
        if quote_lookup and occ:
            try:
                cur_price = quote_lookup(occ)
            except Exception:
                cur_price = None
        if cur_price is None:
            # Stale fallback
            cur_price = float(row.get("price") or 0) or None

        outcome = evaluate_for_roll(row, cur_price)
        if outcome["action"] == "ROLL_RECOMMEND":
            actionable.append(
                f"  - {row.get('symbol')} {row.get('option_strategy')} "
                f"({row.get('expiry')}, {row.get('qty')}× contracts) "
                f"at {outcome['profit_pct']*100:.0f}% max profit → "
                f"consider rolling forward"
            )
        elif outcome["action"] == "AUTO_CLOSE":
            # Auto-close fires server-side; surface for visibility only
            actionable.append(
                f"  - {row.get('symbol')} {row.get('option_strategy')} "
                f"({row.get('expiry')}) — auto-closing at "
                f"{outcome['profit_pct']*100:.0f}% max profit"
            )

    if not actionable:
        return ""
    return (
        "NEAR-EXPIRY OPTION POSITIONS (within "
        f"{ROLL_WINDOW_DAYS} days of expiry):\n"
        + "\n".join(actionable)
        + "\n  → Roll candidates: propose MULTILEG_OPEN at next "
          "expiry if the original thesis still holds."
    )


def _combo_sibling_legs(conn, closed_row):
    """Return the open, filled sibling leg(s) of the SAME multileg combo
    as `closed_row`. Pairing is EXACT when possible: a combo's legs all
    carry the SAME entry order_id (the combo/parent id; distinguished by
    occ_symbol — see the dup-order_id invariant), so we pair by that id.
    Only sequential (non-combo) multileg fills lack a shared id; for
    those we fall back to the option_strategy + underlying + 60s-window
    heuristic. `closed_row` is the pre-close snapshot, so its order_id is
    still the original combo id even though the DB row was just
    overwritten with the close id.
    """
    combo_oid = closed_row.get("order_id")
    occ = closed_row.get("occ_symbol")
    # price/fill_price/decision_price let _close_combo_partner_legs
    # compute the sibling's entry premium so it can stamp realized P&L
    # at close time (else the in-place close row rots at pnl=NULL).
    _cols = ("id, side, qty, occ_symbol, price, fill_price, "
             "decision_price")
    # EXACT path — pair by the shared combo order_id.
    if combo_oid:
        siblings = conn.execute(
            f"SELECT {_cols} FROM trades "
            "WHERE order_id = ? AND signal_type='MULTILEG' "
            "  AND COALESCE(occ_symbol,'') != ? "
            "  AND COALESCE(status,'open')='open' "
            "  AND fill_price IS NOT NULL",
            (combo_oid, occ or ""),
        ).fetchall()
        if siblings:
            return siblings
    # FALLBACK — sequential legs (distinct per-leg order_ids) have no
    # shared id; pair by option_strategy + underlying + 60s window.
    return conn.execute(
        f"SELECT {_cols} FROM trades "
        "WHERE signal_type='MULTILEG' AND option_strategy=? "
        "  AND symbol=? AND id != ? "
        "  AND COALESCE(occ_symbol,'') != ? "
        "  AND COALESCE(status,'open')='open' "
        "  AND fill_price IS NOT NULL "
        "  AND ABS(strftime('%s', timestamp) - strftime('%s', ?)) < 60",
        (closed_row.get("option_strategy"), closed_row.get("symbol"),
         closed_row["id"], occ or "", closed_row.get("timestamp")),
    ).fetchall()


def _close_combo_partner_legs(api, conn, closed_row, summary,
                              quote_lookup=None) -> int:
    """O6 — close the surviving sibling leg(s) of the SAME multileg
    combo as `closed_row` (a credit leg just auto-closed). Siblings are
    paired EXACTLY by the shared combo order_id (heuristic only for
    sequential legs — see _combo_sibling_legs). Each sibling is closed
    by ITS OWN OCC + qty and stamped with ITS OWN close order_id — never
    by broker position qty, never reusing the credit leg's id — so every
    leg stays profile-attributable on the shared Alpaca conduit. Returns
    the count closed. A submit failure is logged loudly (never silent)
    and leaves the sibling open.

    2026-07-22 — closes journal their OWN pending_fill row (the old
    in-place status flip overwrote the partner OPEN row's order_id
    with the close order's id — the second instance of the p212
    clobber). The open row is never mutated; the fill state machine
    flips it when the close confirms. Routing: a short partner's
    buy-close hits the net-short buy-to-close branch; a long partner's
    sell-close hits the sell/cover FIFO branch — pnl NULL is safe in
    both (the fill-true recompute books it).
    """
    from journal import realized_option_close_pnl
    swept = 0
    try:
        siblings = _combo_sibling_legs(conn, closed_row)
    except Exception as exc:
        logger.warning("O6 partner-sweep query failed for combo %s: %s",
                       closed_row.get("option_strategy"), exc)
        return 0
    for sib in siblings:
        sib_id, sib_side, sib_qty_raw, sib_occ = (
            sib[0], sib[1], sib[2], sib[3])
        sib_price = sib[4] if len(sib) > 4 else None
        sib_fill = sib[5] if len(sib) > 5 else None
        sib_dp = sib[6] if len(sib) > 6 else None
        try:
            sib_qty = int(sib_qty_raw or 0)
        except (TypeError, ValueError):
            sib_qty = 0
        if not sib_occ or sib_qty <= 0:
            continue
        rev_side = "buy" if (sib_side or "").lower() == "sell" else "sell"
        try:
            o = api.submit_order(symbol=sib_occ, qty=sib_qty,
                                 side=rev_side, type="market",
                                 time_in_force="day")
            sib_close_id = getattr(o, "id", None)
        except Exception as exc:
            summary["errors"] += 1
            summary["details"].append({
                "id": sib_id, "occ": sib_occ,
                "error": f"O6 partner close submit failed: {exc}",
            })
            logger.error(
                "O6 partner close FAILED for combo leg %s (%s): %s — "
                "leaving it open (never falsely closed).",
                sib_id, sib_occ, exc,
            )
            continue
        # Close-time realized-P&L estimate (entry premium vs fresh
        # quote). entry_px prefers the actual fill, then submit price,
        # then decision price. close_px from the quote; None when no
        # quote → est stays None and we leave pnl NULL (the orphan
        # backstop is the safety net), never fabricating a price.
        entry_px = next(
            (float(v) for v in (sib_fill, sib_price, sib_dp)
             if v is not None and float(v) > 0), None)
        close_px = None
        if quote_lookup is not None:
            try:
                _q = quote_lookup(sib_occ)
                close_px = float(_q) if _q is not None else None
            except Exception:
                close_px = None
        est = realized_option_close_pnl(entry_px, close_px, sib_qty,
                                        sib_side)
        if est is None:
            logger.info(
                "O6: partner leg %s (%s) closing with pnl NULL pending "
                "fill-true recompute (entry_px=%s close_px=%s)",
                sib_id, sib_occ, entry_px, close_px,
            )
        # 2026-07-22 — same corruption fix as the primary leg: the old
        # in-place UPDATE overwrote the partner OPEN row's order_id
        # with the close order's id (the second instance of the p212
        # clobber). The close now journals its OWN pending_fill row;
        # the open row is never mutated. Routing is safe both ways:
        # a short partner's buy-close hits the net-short buy-to-close
        # branch; a long partner's sell-close hits the sell/cover FIFO
        # branch — pnl NULL allowed in both (fill-true recompute books
        # it).
        try:
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                " price, pnl, status, order_id, occ_symbol, "
                " signal_type, option_strategy, reason) "
                "VALUES (datetime('now'), ?, ?, ?, ?, ?, "
                " 'pending_fill', ?, ?, 'OPTIONS', ?, ?)",
                (closed_row.get("symbol"), rev_side, sib_qty,
                 close_px, est, sib_close_id, sib_occ,
                 closed_row.get("option_strategy"),
                 "Auto-close partner: combo %s leg closed alongside "
                 "its credit leg (#%s)" % (
                     closed_row.get("option_strategy"),
                     closed_row["id"])),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("O6 partner-close journal failed for %s: %s",
                           sib_id, exc)
            continue
        swept += 1
        logger.info("O6: auto-closed combo partner leg %s (%s) "
                    "alongside credit leg #%s",
                    sib_id, sib_occ, closed_row["id"])
    return swept


def auto_close_high_profit_credits(
    api,
    db_path: str,
    quote_lookup: Optional[Callable[[str], Optional[float]]] = None,
    today: Optional[_date] = None,
    window_days: int = ROLL_WINDOW_DAYS,
    auto_close_profit_pct: float = AUTO_CLOSE_PROFIT_PCT,
    roll_recommend_profit_pct: float = ROLL_RECOMMEND_PROFIT_PCT,
) -> Dict[str, Any]:
    """For each near-expiry credit position at ≥auto_close_profit_pct
    of max profit, submit a closing order and update the journal.

    OPEN_ITEMS #10 — window + thresholds parameterizable per profile.
    Defaults match the legacy module-constant values.

    Returns: {evaluated, auto_closed, errors, details}
    """
    summary = {
        "evaluated": 0, "auto_closed": 0, "errors": 0, "details": [],
    }
    rows = find_near_expiry_options(db_path, today=today,
                                       window_days=window_days)
    summary["evaluated"] = len(rows)
    if not rows:
        return summary

    from journal import _get_conn
    conn = _get_conn(db_path)
    try:
        for row in rows:
            try:
                # LIVE RE-CHECK — a partner sweep earlier this run may
                # have already closed this leg (the `rows` snapshot is
                # stale). Re-processing it would submit a SECOND close
                # for the same leg (double-close / over-sell). Skip it.
                try:
                    _st = conn.execute(
                        "SELECT status FROM trades WHERE id=?",
                        (row["id"],)).fetchone()
                    if _st and (_st[0] or "open") != "open":
                        continue
                except Exception as _rc_exc:
                    # Re-check is best-effort; on failure fall through
                    # and process the row normally (the apply UPDATE is
                    # the source of truth). Surface for follow-up.
                    logger.debug(
                        "roll-manager live status re-check failed for "
                        "#%s: %s", row.get("id"), _rc_exc,
                    )
                cur_price = None
                occ = row.get("occ_symbol")
                if quote_lookup and occ:
                    try:
                        cur_price = quote_lookup(occ)
                    except Exception:
                        cur_price = None
                if cur_price is None:
                    continue

                outcome = evaluate_for_roll(
                    row, cur_price,
                    auto_close_profit_pct=auto_close_profit_pct,
                    roll_recommend_profit_pct=roll_recommend_profit_pct,
                )
                if outcome["action"] != "AUTO_CLOSE":
                    continue

                # Submit closing order — opposite side of the entry
                entry_side = row.get("side", "").lower()
                close_side = "buy" if entry_side == "sell" else "sell"
                qty = int(row.get("qty") or 0)
                if qty <= 0:
                    continue
                # RESUBMIT DEDUP (2026-07-22) — the old code's in-place
                # status flip doubled as the retry guard; now that the
                # open row stays untouched, an in-flight close from an
                # earlier cycle must be detected explicitly or every
                # cycle would submit another close. Skip when a pending
                # close row for this OCC+side has a live (or
                # unverifiable) order; a terminal-unfilled one falls
                # through to a fresh submit and the phantom sweep voids
                # the stale row.
                try:
                    _pending = conn.execute(
                        "SELECT order_id FROM trades WHERE occ_symbol=? "
                        "AND side=? AND status='pending_fill' "
                        "AND order_id IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1", (occ, close_side),
                    ).fetchone()
                except Exception:
                    _pending = None
                if _pending and _pending[0]:
                    try:
                        from order_status_cache import get_order_cached
                        _po = get_order_cached(api, _pending[0])
                        _ps = (getattr(_po, "status", "") or "").lower()
                    except Exception:
                        _ps = "unverifiable"
                    if _ps not in ("canceled", "expired", "rejected",
                                   "done_for_day", "filled"):
                        continue  # close already in flight — don't stack
                    if _ps == "filled":
                        continue  # fill machinery is mid-flight — defer
                try:
                    order = api.submit_order(
                        symbol=occ, qty=qty, side=close_side,
                        type="market", time_in_force="day",
                    )
                    order_id = getattr(order, "id", None)
                except Exception as exc:
                    summary["errors"] += 1
                    summary["details"].append({
                        "id": row["id"], "occ": occ,
                        "error": f"close submit failed: {exc}",
                    })
                    continue

                # 2026-07-22 — THE p212 GOOG-SPREAD CORRUPTION FIX.
                # The old code did an in-place
                #   UPDATE trades SET status='pending_fill', pnl=?,
                #   order_id=? WHERE id=<open row>
                # which (a) CLOBBERED the open row — its order_id was
                # overwritten with the close order's id (re-stamped on
                # every retry, so a week of 429-failed closes left the
                # final close ids on the open rows), and (b) computed
                # pnl with a SHORT-LEG-ONLY formula — a LONG leg's sign
                # came out inverted (+2,148 booked where truth was
                # −2,148), fabricating +4,608 realized P&L and latching
                # the integrity kill switch for a full trading day.
                # Now: the open row is NEVER mutated; the close gets its
                # OWN pending_fill row carrying the direction-aware
                # estimate (pnl on close rows is the house convention
                # AND the fill state machine's discriminator for option
                # closes); the fill machinery flips both rows and trues
                # the pnl when the broker confirms.
                premium_in = float(
                    row.get("decision_price") or row.get("price") or 0)
                realized_est = _close_pnl_estimate(
                    entry_side, premium_in, cur_price, qty)
                from journal import log_trade
                log_trade(
                    symbol=row.get("symbol"),
                    side=close_side,
                    qty=qty,
                    price=cur_price,
                    order_id=order_id,
                    signal_type="OPTIONS",
                    option_strategy=row.get("option_strategy"),
                    expiry=row.get("expiry"),
                    strike=row.get("strike"),
                    occ_symbol=occ,
                    status="pending_fill",
                    pnl=realized_est,
                    reason=outcome["reason"],
                    db_path=db_path,
                )
                realized_pnl = realized_est
                summary["auto_closed"] += 1
                summary["details"].append({
                    "id": row["id"], "occ": occ,
                    "profit_pct": outcome["profit_pct"],
                    "pnl": realized_pnl, "close_order_id": order_id,
                })
                logger.info(
                    "Auto-closed %s at %.0f%% max profit; pnl=%+.2f",
                    occ, outcome["profit_pct"] * 100, realized_pnl,
                )

                # O6 (2026-06-17) — PARTNER SWEEP. A credit SPREAD is N
                # MULTILEG legs but AUTO_CLOSE only fires on the
                # short/credit leg (the long protective leg hits the
                # HOLD early-return). Closing only the credit leg leaves
                # the long partner naked at the broker (a single-leg leg
                # the combo never intended to hold). Close the surviving
                # sibling leg(s) of THIS combo in the same pass — by the
                # sibling's OWN OCC/qty and its OWN close order_id (never
                # by broker position qty, never reusing this leg's id),
                # so each leg stays profile-attributable on the shared
                # conduit. Reason prefix is distinct from 'Auto-rollback:'
                # so the expiry sweep's exclusion still behaves.
                if (row.get("signal_type") == "MULTILEG"
                        and row.get("timestamp")):
                    _swept = _close_combo_partner_legs(
                        api, conn, row, summary, quote_lookup=quote_lookup,
                    )
                    summary.setdefault("partner_legs_closed", 0)
                    summary["partner_legs_closed"] += _swept
            except Exception as exc:
                summary["errors"] += 1
                logger.exception(
                    "Roll-manager evaluation failed for %s: %s",
                    row.get("id"), exc,
                )
    finally:
        conn.close()
    return summary
