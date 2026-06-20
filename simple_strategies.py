"""Non-AI strategies used as experimental baselines (2026-05-17).

Dispatched from multi_scheduler when a trading_profile's
`strategy_type` column is `buy_hold` or `random` instead of the
default `ai`.

**Hard rule (2026-05-19, after live bug):** these are BENCHMARKS,
not active strategies. They MUST fire once on first run and then
HOLD FOREVER. Any subsequent run for the same profile is a no-op.
The fire-once guard is enforced by checking the profile's journal
for prior entries tagged with this strategy. If found → return a
HOLD summary without contacting Alpaca.

Goals (docs/15_EXPERIMENT_DESIGN_2026_05_17.md):
- `run_buy_hold_spy(ctx)`: null floor for the full-system arm.
  Fires once → buys SPY using per-profile VIRTUAL equity (NOT the
  shared Alpaca account equity, which is corrupted by other
  profiles' positions). Holds forever afterward.
- `run_random_stock_of_day(ctx)`: tests whether the AI's stock-picking
  adds value over random selection. Fires once → picks 5 symbols
  deterministically (seed = profile_id), buys equal-weighted from
  per-profile virtual equity. Holds forever. The strategy name
  "stock_of_day" is now a historical artifact — daily re-rolling
  was wrong-by-design for a benchmark.

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
RANDOM_PICK_COUNT = 5
CASH_BUFFER = 0.05  # leave 5% as cash to absorb slippage / partial fills


def _has_prior_strategy_entry(db_path: str, strategy_tag: str) -> bool:
    """True iff the profile's journal has at least one trade row with
    `strategy = <tag>`. Used by both baseline strategies as the
    fire-once guard — once an initial buy has been logged, the
    strategy NEVER re-fires.
    """
    if not db_path:
        return False
    try:
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE strategy = ? LIMIT 1",
                (strategy_tag,),
            ).fetchone()
            return row is not None
    except Exception as exc:
        # Fail-CLOSED: if we can't read the journal, refuse to fire.
        # Better to skip a baseline cycle than to double-buy.
        logger.warning(
            "_has_prior_strategy_entry: journal read failed (%s) — "
            "refusing to fire %s as a safety measure",
            exc, strategy_tag,
        )
        return True


def _virtual_equity(ctx) -> float:
    """Per-profile VIRTUAL equity (initial_capital - net spend +
    portfolio value). Falls back to `ctx.initial_capital` when
    journal can't be read.

    Critical: must NEVER fall back to `get_account_info()`'s Alpaca
    equity — that's the SHARED account, corrupted by other virtual
    profiles' positions, which caused the 2026-05-19 over-allocation
    bug.
    """
    db_path = getattr(ctx, "db_path", None)
    initial_capital = float(getattr(ctx, "initial_capital", 0) or 0)
    if not db_path:
        return initial_capital
    try:
        from journal import get_virtual_account_info
        info = get_virtual_account_info(
            db_path=db_path, initial_capital=initial_capital)
        return float(info.get("equity", initial_capital))
    except Exception as exc:
        logger.warning(
            "_virtual_equity: get_virtual_account_info failed (%s) — "
            "falling back to initial_capital %s",
            exc, initial_capital,
        )
        return initial_capital


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
    # simple_strategies opens positions; a 'sell' pick is a DELIBERATE
    # short entry (sell-to-open), so declare it to the oversell door —
    # otherwise the door refuses it as a naked sell (own journal long=0).
    _entry_intent = {"intent": "open_short"} if side == "sell" else {}
    try:
        order = api.submit_order(
            symbol=symbol, qty=int(qty), side=side,
            type="market", time_in_force="day", **_entry_intent,
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
    """Buy and hold SPY. Fires ONCE per profile lifetime — buys SPY
    using per-profile VIRTUAL equity, then HOLDS FOREVER. Any
    subsequent invocation is a no-op (returns holds=1).

    2026-05-19 — original "rebalance on 5% drift" design was wrong:
    drift was being computed against shared-Alpaca account equity
    (which includes OTHER virtual profiles' positions), causing
    daily re-buys and ~2× over-allocation. A benchmark must NEVER
    re-trigger or drift-rebalance — that's the entire definition of
    "buy and hold."
    """
    summary = _empty_summary("buy_hold")
    seg_label = ctx.display_name or ctx.segment

    # Fire-once guard. Once we've logged ANY 'buy_hold_spy' trade
    # for this profile, we are done forever. No drift rebalance, no
    # re-allocation, no exceptions.
    db_path = getattr(ctx, "db_path", None)
    if _has_prior_strategy_entry(db_path or "", "buy_hold_spy"):
        summary["holds"] = 1
        logger.info(
            "[%s buy_hold] prior buy_hold_spy entry exists — HOLD (fire-once)",
            seg_label,
        )
        return summary

    # First fire: size against PER-PROFILE virtual equity, not the
    # shared Alpaca account.
    equity = _virtual_equity(ctx)
    if equity <= 0:
        logger.error("[%s buy_hold] virtual equity = %s, nothing to invest",
                     seg_label, equity)
        summary["errors"] = 1
        return summary

    from client import get_api
    api = get_api(ctx)
    price = _fetch_price(api, SPY_SYMBOL)
    if not price:
        logger.error("[%s buy_hold] could not fetch SPY price", seg_label)
        summary["errors"] = 1
        return summary

    qty_to_buy = int(equity * (1.0 - CASH_BUFFER) / price)
    if qty_to_buy <= 0:
        summary["holds"] = 1
        return summary

    reason = (
        "buy_hold INITIAL allocation: $%.2f virtual equity → "
        "%d shares of SPY @ ~$%.2f. After this fires, the strategy "
        "HOLDS forever — no rebalance, no re-trigger."
        % (equity, qty_to_buy, price)
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

def _is_alpaca_tradable(api, symbol: str) -> bool:
    """Return True if Alpaca lists the asset as active + tradable.
    Fail-safe: any error returns False so the caller substitutes
    rather than risks a rejection mid-flight."""
    if api is None:
        return True  # tests / no-api path — preserve old behavior
    try:
        asset = api.get_asset(symbol)
    except Exception:
        return False
    status = (getattr(asset, "status", "") or "").lower()
    return status == "active" and bool(getattr(asset, "tradable", False))


def _pick_random_symbols(profile_id: int,
                         universe: List[str], n: int,
                         api=None) -> List[str]:
    """Deterministic pick from the universe, seeded by profile_id
    ALONE (no date component). 2026-05-19 — was previously seeded
    by (profile_id, today_date), which produced different picks
    every day and rotated the portfolio. For a benchmark that
    fires once and holds, the seed must be stable across days.

    2026-06-04 — added `api`-keyed substitution. The raw seed sample
    may land on symbols that Alpaca has marked inactive (delisted /
    halted at the broker), causing submit_order to reject mid-cycle
    and leaving fewer than n holdings. To keep replicas of the
    random benchmark comparable, draw a larger pool from the same
    seeded RNG and take the first n that are tradable at Alpaca.
    Determinism is preserved — same seed + same broker state →
    same picks. When `api` is None (tests without get_asset
    mocking), falls back to the pre-2026-06-04 unfiltered behavior."""
    seed = hash(("random_baseline_v2", profile_id)) & 0xFFFFFFFF
    rng = random.Random(seed)
    universe_list = list(universe)
    n = min(n, len(universe_list))
    if api is None:
        return rng.sample(universe_list, n)
    # Draw a larger pool from the seeded RNG; take first n tradable.
    # Pool size 4× n is conservative — even with ~25% inactive symbols
    # in the universe, this finds n active picks. If somehow the pool
    # is exhausted (would require >75% of universe inactive), returns
    # whatever it found.
    pool_size = min(max(n * 4, 20), len(universe_list))
    candidates = rng.sample(universe_list, pool_size)
    picks: List[str] = []
    for sym in candidates:
        if len(picks) >= n:
            break
        if _is_alpaca_tradable(api, sym):
            picks.append(sym)
    return picks


def run_random_stock_of_day(ctx) -> Dict[str, Any]:
    """Pick 5 random large-cap symbols, buy them ONCE, hold forever.

    2026-05-19 — original design re-picked every day (different
    seed per date) and rotated the portfolio. That's a high-
    turnover strategy, not a benchmark. Fixed: pick once on first
    fire (seed = profile_id alone), buy equal-weighted from per-
    profile virtual equity, then HOLD FOREVER. The function name
    keeps "stock_of_day" for backward-compat but the semantics are
    now "stock_of_baseline" — fire once, hold forever.

    Any subsequent invocation after the initial buy is a no-op
    (returns holds=1). Enforced by the fire-once guard.
    """
    summary = _empty_summary("random")
    seg_label = ctx.display_name or ctx.segment

    # Fire-once guard.
    db_path = getattr(ctx, "db_path", None)
    if _has_prior_strategy_entry(db_path or "", "random_stock_of_day"):
        summary["holds"] = 1
        logger.info(
            "[%s random] prior random_stock_of_day entry exists — "
            "HOLD (fire-once)", seg_label,
        )
        return summary

    # First fire: pick + buy.
    from segments import STOCK_UNIVERSE
    from client import get_api
    api = get_api(ctx)
    picks = _pick_random_symbols(
        getattr(ctx, "profile_id", 0) or 0,
        STOCK_UNIVERSE, RANDOM_PICK_COUNT,
        api=api,
    )
    logger.info("[%s random] INITIAL picks (held forever): %s",
                seg_label, picks)

    equity = _virtual_equity(ctx)
    if equity <= 0:
        logger.error("[%s random] virtual equity = %s, nothing to invest",
                     seg_label, equity)
        summary["errors"] += 1
        return summary
    cash_per_pick = (equity * (1.0 - CASH_BUFFER)) / RANDOM_PICK_COUNT
    for sym in picks:
        price = _fetch_price(api, sym)
        if not price or price <= 0:
            logger.warning("[%s random] skip %s: no price", seg_label, sym)
            summary["errors"] += 1
            continue
        qty = int(cash_per_pick / price)
        if qty <= 0:
            logger.warning(
                "[%s random] skip %s: cash/pick=$%.2f < price=$%.2f",
                seg_label, sym, cash_per_pick, price,
            )
            continue
        reason = (
            "random_baseline INITIAL allocation: $%.2f virtual equity / "
            "%d picks = $%.2f per pick → %d shares of %s @ ~$%.2f. "
            "After this fires, the strategy HOLDS forever."
            % (equity, RANDOM_PICK_COUNT, cash_per_pick, qty, sym, price)
        )
        if _submit_and_log(
            api, ctx, sym, "buy", qty, price,
            "random_stock_of_day", reason,
        ):
            summary["buys"] += 1
        else:
            summary["errors"] += 1

    if summary["buys"] == 0:
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
