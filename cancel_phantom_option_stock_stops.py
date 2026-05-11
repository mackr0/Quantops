"""One-shot: cancel broker stock-side stop/trailing-stop orders that
are phantom protections placed by `bracket_orders.ensure_protective_stops`
against virtual-profile OPTION positions.

Caught 2026-05-11: ensure_protective_stops reads pos.get("symbol")
which for virtual option positions is the UNDERLYING ticker (not the
OCC). It then submits stock-type trailing stops on the underlying.
Result: 23 phantom stock-side sell stops armed at Alpaca right now,
each waiting to short-sell the underlying if it dips through the
trail level. None protect any actual option contract.

Identification pattern (per Alpaca account):
  - status='open'
  - type IN ('stop', 'trailing_stop', 'stop_limit')
  - side='sell'
  - symbol IS the underlying of an OCC option position held on this
    account (held via the option contract, NOT via a long stock
    position)

Conservative safety: only cancels stops on symbols where THIS
account has zero shares of the underlying stock. Never touches a
real protective stop on a real stock position.

Idempotent: safe to re-run. Logs every cancellation.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def _is_occ_symbol(s):
    if not s or len(s) < 14 or len(s) > 21:
        return False
    if not s[-8:].isdigit():
        return False
    if s[-9] not in ("C", "P"):
        return False
    head = s[:-9].rstrip()
    if len(head) < 7 or not head[-6:].isdigit():
        return False
    return True


def _underlying_of_occ(occ):
    return occ[:-15].rstrip().upper()


def _enabled_profile_ids():
    import sqlite3
    conn = sqlite3.connect("quantopsai.db")
    rows = conn.execute(
        "SELECT id FROM trading_profiles WHERE enabled=1 ORDER BY id"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _phantom_stops_for_account(api):
    """Return list of (order, reason) tuples for orders on this account
    that look like phantom option-protection stock stops."""
    positions = api.list_positions()

    # Underlyings held as option contracts on this account
    underlyings_held_as_options = set()
    # Underlyings held as actual stock (so we DON'T cancel real stops)
    underlyings_held_as_stock = set()
    for p in positions:
        sym = p.symbol or ""
        if _is_occ_symbol(sym):
            underlyings_held_as_options.add(_underlying_of_occ(sym))
        else:
            underlyings_held_as_stock.add(sym.upper())

    orders = api.list_orders(status="open", limit=500)
    phantoms = []
    for o in orders:
        otype = (getattr(o, "type", "") or "").lower()
        oside = (getattr(o, "side", "") or "").lower()
        osym = (getattr(o, "symbol", "") or "").upper()
        if otype not in ("stop", "trailing_stop", "stop_limit"):
            continue
        if oside != "sell":
            continue
        if osym not in underlyings_held_as_options:
            # Not a symbol where we hold options on this account
            continue
        if osym in underlyings_held_as_stock:
            # We DO hold real stock on this symbol — preserve real stop
            continue
        phantoms.append((o, f"phantom: {otype} sell on {osym}, "
                            f"option position held but no stock"))
    return phantoms


def main():
    from models import build_user_context_from_profile
    from client import get_api

    # One ctx per Alpaca account is enough (orders are account-wide,
    # not profile-specific). But profiles map to accounts, so we
    # iterate one profile per account. We dedupe via account-id.
    seen_accounts = set()
    grand_canceled = 0
    grand_failed = 0

    for pid in _enabled_profile_ids():
        try:
            ctx = build_user_context_from_profile(pid)
            api = get_api(ctx)
            acct_id = getattr(ctx, "alpaca_account_id", None) or pid
            if acct_id in seen_accounts:
                continue
            seen_accounts.add(acct_id)

            phantoms = _phantom_stops_for_account(api)
            if not phantoms:
                log.info("acct %s (via profile %d): no phantoms",
                         acct_id, pid)
                continue

            log.info("acct %s (via profile %d): %d phantom(s) to cancel",
                     acct_id, pid, len(phantoms))
            for order, reason in phantoms:
                try:
                    api.cancel_order(order.id)
                    log.info("  CANCELED %s %s qty=%s side=%s type=%s — %s",
                             order.id[:8], order.symbol, order.qty,
                             order.side, order.type, reason)
                    grand_canceled += 1
                except Exception as exc:
                    log.error("  FAILED to cancel %s: %s",
                              order.id[:8], exc)
                    grand_failed += 1
        except Exception as exc:
            log.error("profile %d: %s", pid, exc)

    log.info("TOTAL: canceled=%d failed=%d", grand_canceled, grand_failed)
    return 0 if grand_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
