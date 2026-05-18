"""Non-AI strategies used as experimental baselines (2026-05-17).

Dispatched from multi_scheduler when a trading_profile's
`strategy_type` column is `buy_hold` or `random` instead of the
default `ai`.

Goals (docs/15_EXPERIMENT_DESIGN_2026_05_17.md):
- `run_buy_hold_spy(ctx)`: null floor for the full-system arm.
  Buys SPY on day 1, holds, rebalances to 100% only when SPY weight
  drifts more than 5% from target. Never sells voluntarily.
- `run_random_stock_of_day(ctx)`: tests whether the AI's stock-picking
  adds value over random selection. Each market day deterministically
  picks 5 symbols from the large-cap universe (seed = profile_id +
  date), closes any held positions not in today's pick, and opens
  today's picks equal-weighted.

Both strategies write through the journal (`log_trade`) carrying the
broker order_id so the perfect-matching invariant (#157) holds.
Neither strategy imports or runs any AI / meta-model / specialist /
alt-data code — that is the entire point.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SPY_SYMBOL = "SPY"
SPY_DRIFT_THRESHOLD = 0.05  # 5% rebalance trigger
RANDOM_PICK_COUNT = 5
CASH_BUFFER = 0.05  # leave 5% as cash to absorb slippage / partial fills


def _fetch_price(api, symbol: str) -> Optional[float]:
    """Latest trade price via Alpaca. Returns None on failure."""
    try:
        trade = api.get_latest_trade(symbol)
        return float(trade.price)
    except (AttributeError, ValueError, TypeError, OSError) as exc:
        logger.warning(
            "simple_strategies: get_latest_trade failed for %s: %s: %s",
            symbol, type(exc).__name__, exc,
        )
        return None


def _empty_summary(strategy: str) -> Dict[str, Any]:
    return {
        "buys": 0, "sells": 0, "shorts": 0,
        "ai_vetoed": 0, "holds": 0, "errors": 0,
        "pre_filtered": 0, "sent_to_ai": 0,
        "strategy": strategy,
    }


def _submit_and_log(api, ctx, symbol, side, qty, price, strategy_name,
                    reason) -> bool:
    """Submit a market order and write to the journal with order_id.
    Returns True on success."""
    from journal import log_trade
    try:
        order = api.submit_order(
            symbol=symbol, qty=int(qty), side=side,
            type="market", time_in_force="day",
        )
    except (AttributeError, ValueError, TypeError, OSError) as exc:
        logger.error(
            "simple_strategies: submit_order failed for %s %s x%d: %s: %s",
            side, symbol, qty, type(exc).__name__, exc,
        )
        return False
    except Exception as exc:
        # alpaca_trade_api raises alpaca_trade_api.rest.APIError for
        # things like `asset "X" not found` (stale tickers no longer
        # tradable) and `insufficient buying power`. These are
        # PER-TICKER failures — they must not propagate up and fail
        # the whole scan loop. Caught 2026-05-18 17:28 ET when GPS
        # (Gap, renamed in 2025) was picked by random and the bare
        # APIError tore down P13's entire 5-pick scan, leaving the
        # profile with fewer-than-intended day-1 positions. Catching
        # broadly here because alpaca_trade_api isn't a stdlib
        # import we want to chase in this hot path; the cost of
        # missing one new exception class < the cost of crashing
        # the loop on one bad symbol.
        logger.warning(
            "simple_strategies: submit_order rejected for %s %s x%d "
            "(skipping pick, continuing scan): %s: %s",
            side, symbol, qty, type(exc).__name__, exc,
        )
        return False
    try:
        log_trade(
            symbol=symbol,
            side=side,
            qty=int(qty),
            price=price,
            order_id=order.id,
            signal_type="BUY" if side == "buy" else "SELL",
            strategy=strategy_name,
            reason=reason,
            decision_price=price,
            status="pending_fill",
            db_path=ctx.db_path,
        )
    except (AttributeError, ValueError, TypeError, OSError) as exc:
        # Order is live at the broker but journal write failed — this
        # IS a perfect-matching-invariant violation. Surface loudly so
        # the audit picks it up; don't swallow.
        logger.error(
            "simple_strategies: log_trade failed for %s %s x%d "
            "(order_id=%s now orphaned): %s: %s",
            side, symbol, qty, order.id, type(exc).__name__, exc,
        )
        return False

    # Log to master activity_log so the dashboard ticker shows the
    # trade alongside AI-pipeline trades. Without this, buy_hold and
    # random profiles' day-1 entries land in the per-profile trades
    # table but never appear in /api/activity → dashboard ticker
    # silently misses them. Caught 2026-05-18 — operator observed
    # AI profiles ticking trades but benchmarks producing nothing
    # visible.
    try:
        from models import log_activity
        action = "BUY" if side == "buy" else "SELL"
        title = f"{action} {int(qty):,} {symbol} @ ${price:,.2f}"
        log_activity(
            profile_id=getattr(ctx, "profile_id", 0),
            user_id=getattr(ctx, "user_id", 0),
            activity_type="trade_executed",
            title=title,
            detail=f"Trade executed: {action} {symbol}\n{reason}",
            symbol=symbol,
        )
    except Exception as exc:
        # Activity log is informational; trade is already real at
        # broker + journal. Surface at warning (not silent) so the
        # /issues audit picks up any future regression.
        logger.warning(
            "simple_strategies: activity_log write failed for %s %s "
            "(order_id=%s already submitted): %s: %s",
            side, symbol, order.id, type(exc).__name__, exc,
        )
    return True


# ─────────────────────────────────────────────────────────────────────
# Buy & Hold SPY
# ─────────────────────────────────────────────────────────────────────

def run_buy_hold_spy(ctx) -> Dict[str, Any]:
    """Buy and hold SPY. Day 1 spends ~100% of equity on SPY; later
    cycles only act if SPY weight has drifted more than 5% from
    100% (e.g. via accumulated cash from dividends). Never sells."""
    summary = _empty_summary("buy_hold")
    seg_label = ctx.display_name or ctx.segment

    from client import get_api, get_account_info, get_positions
    api = get_api(ctx)

    account = get_account_info(api=api, ctx=ctx)
    if not account:
        logger.error("[%s buy_hold] no account info", seg_label)
        summary["errors"] = 1
        return summary

    equity = float(account.get("equity", 0))
    if equity <= 0:
        logger.error("[%s buy_hold] equity=0, nothing to invest",
                     seg_label)
        summary["errors"] = 1
        return summary

    positions = get_positions(api=api, ctx=ctx)
    spy_qty = 0.0
    for p in positions:
        # Positions list may be Position objects or dicts; support both
        sym = getattr(p, "symbol", None) or (
            p.get("symbol") if isinstance(p, dict) else None
        )
        if sym == SPY_SYMBOL:
            q = getattr(p, "qty", None)
            if q is None and isinstance(p, dict):
                q = p.get("qty", 0)
            spy_qty = float(q or 0)
            break

    price = _fetch_price(api, SPY_SYMBOL)
    if not price:
        logger.error("[%s buy_hold] could not fetch SPY price", seg_label)
        summary["errors"] = 1
        return summary

    target_qty = int(equity * (1.0 - CASH_BUFFER) / price)
    qty_to_buy = target_qty - int(spy_qty)
    spy_value = spy_qty * price
    spy_weight = spy_value / equity if equity > 0 else 0.0

    # Skip rebalance if already within drift band AND at least 1 share held
    if spy_qty > 0 and abs(1.0 - spy_weight) <= SPY_DRIFT_THRESHOLD:
        summary["holds"] = 1
        logger.info(
            "[%s buy_hold] SPY weight=%.2f%% within ±%.0f%% drift band — hold",
            seg_label, spy_weight * 100, SPY_DRIFT_THRESHOLD * 100,
        )
        return summary

    if qty_to_buy <= 0:
        summary["holds"] = 1
        return summary

    reason = (
        "buy_hold rebalance: SPY weight %.2f%% → target 100%% "
        "(buy %d shares @ ~$%.2f)"
        % (spy_weight * 100, qty_to_buy, price)
    )
    if _submit_and_log(
        api, ctx, SPY_SYMBOL, "buy", qty_to_buy, price,
        "buy_hold_spy", reason,
    ):
        summary["buys"] = 1
        logger.info("[%s buy_hold] %s", seg_label, reason)
    else:
        summary["errors"] = 1
    return summary


# ─────────────────────────────────────────────────────────────────────
# Random Stock-of-Day
# ─────────────────────────────────────────────────────────────────────

def _pick_random_symbols(profile_id: int, today: str,
                         universe: List[str], n: int) -> List[str]:
    """Deterministic pick: same (profile, date) → same picks. Re-runs
    on the same day don't churn positions."""
    seed = hash((profile_id, today)) & 0xFFFFFFFF
    rng = random.Random(seed)
    return rng.sample(list(universe), min(n, len(universe)))


def run_random_stock_of_day(ctx) -> Dict[str, Any]:
    """Pick 5 random large-cap symbols, hold N days (until next pick).
    Each cycle:
    1. Compute today's picks deterministically.
    2. Sell any held position not in today's picks.
    3. Buy any pick not currently held, equal-weighted from equity.
    """
    summary = _empty_summary("random")
    seg_label = ctx.display_name or ctx.segment

    from client import get_api, get_account_info, get_positions
    from segments import LARGE_CAP_UNIVERSE
    api = get_api(ctx)

    today = datetime.now(tz=timezone.utc).date().isoformat()
    picks = _pick_random_symbols(
        getattr(ctx, "profile_id", 0) or 0,
        today, LARGE_CAP_UNIVERSE, RANDOM_PICK_COUNT,
    )
    pick_set = set(picks)
    logger.info("[%s random] today=%s picks=%s", seg_label, today, picks)

    positions = get_positions(api=api, ctx=ctx)
    held: Dict[str, float] = {}
    for p in positions:
        sym = getattr(p, "symbol", None) or (
            p.get("symbol") if isinstance(p, dict) else None
        )
        if not sym:
            continue
        q = getattr(p, "qty", None)
        if q is None and isinstance(p, dict):
            q = p.get("qty", 0)
        held[sym] = float(q or 0)

    # Step 1: close positions not in today's pick.
    for sym, qty in list(held.items()):
        if sym in pick_set or qty <= 0:
            continue
        price = _fetch_price(api, sym) or 0.0
        reason = "random_stock_of_day: %s not in today's pick" % sym
        if _submit_and_log(
            api, ctx, sym, "sell", qty, price,
            "random_stock_of_day", reason,
        ):
            summary["sells"] += 1
        else:
            summary["errors"] += 1

    # Step 2: open new picks. Re-fetch account so just-closed
    # positions are reflected.
    account = get_account_info(api=api, ctx=ctx)
    if not account:
        summary["errors"] += 1
        return summary
    equity = float(account.get("equity", 0))
    if equity <= 0:
        summary["errors"] += 1
        return summary
    cash_per_pick = (equity * (1.0 - CASH_BUFFER)) / RANDOM_PICK_COUNT

    for sym in picks:
        if sym in held and held[sym] > 0:
            continue  # already holding (carried over from yesterday)
        price = _fetch_price(api, sym)
        if not price or price <= 0:
            logger.warning(
                "[%s random] skip %s: no price", seg_label, sym,
            )
            summary["errors"] += 1
            continue
        qty = int(cash_per_pick / price)
        if qty <= 0:
            logger.warning(
                "[%s random] skip %s: cash/pick=$%.2f < price=$%.2f",
                seg_label, sym, cash_per_pick, price,
            )
            continue
        reason = ("random_stock_of_day: today's pick (date=%s, "
                  "equal-weighted from $%.2f / %d picks)"
                  % (today, equity, RANDOM_PICK_COUNT))
        if _submit_and_log(
            api, ctx, sym, "buy", qty, price,
            "random_stock_of_day", reason,
        ):
            summary["buys"] += 1
        else:
            summary["errors"] += 1

    if summary["buys"] == 0 and summary["sells"] == 0:
        summary["holds"] = 1
    return summary


# ─────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────

def dispatch(ctx) -> Optional[Dict[str, Any]]:
    """Return a summary dict if the profile's strategy_type is a
    non-AI mode; return None if the AI pipeline should run instead."""
    st = getattr(ctx, "strategy_type", "ai") or "ai"
    if st == "buy_hold":
        return run_buy_hold_spy(ctx)
    if st == "random":
        return run_random_stock_of_day(ctx)
    if st == "ai":
        return None
    logger.error(
        "Unknown strategy_type=%r for profile %s — falling back to AI pipeline",
        st, getattr(ctx, "display_name", "?"),
    )
    return None
