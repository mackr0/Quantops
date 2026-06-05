"""Compare displayed P&L vs broker truth vs journal truth for
EXP-A3-700K-AggressiveFree. Used to verify whether the dashboard's
~$37K profit reflects real broker realized + unrealized state or
whether drift makes it misleading.
"""
import os
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__),
)))

from models import get_user_profiles, build_user_context_from_profile
from client import get_api

PROFILE_NAME = "EXP-A3-700K-AggressiveFree"

profiles = get_user_profiles(1)
prof = next(
    (p for p in profiles if p.get("name") == PROFILE_NAME),
    None,
)
if prof is None:
    print(f"Profile {PROFILE_NAME!r} not found")
    sys.exit(1)

pid = prof["id"]
acct = prof["alpaca_account_id"]
initial = float(prof.get("initial_capital") or 0)
print(f"Profile: {PROFILE_NAME}  pid={pid}  alpaca_account_id={acct}")
print(f"  initial_capital: ${initial:,.2f}")
print()

ctx = build_user_context_from_profile(pid)
db_path = ctx.db_path

# --- Broker view ---
api = get_api(ctx)
acct_info = api.get_account()
positions = api.list_positions()
broker_equity = float(getattr(acct_info, "equity", 0) or 0)
broker_cash = float(getattr(acct_info, "cash", 0) or 0)
broker_long_mkt = float(getattr(acct_info, "long_market_value", 0) or 0)
broker_short_mkt = float(getattr(acct_info, "short_market_value", 0) or 0)
broker_unrealized = sum(float(getattr(p, "unrealized_pl", 0) or 0)
                         for p in positions)
print("--- BROKER ACCOUNT (Alpaca truth) ---")
print(f"  equity:            ${broker_equity:>12,.2f}")
print(f"  cash:              ${broker_cash:>12,.2f}")
print(f"  long mkt value:    ${broker_long_mkt:>12,.2f}")
print(f"  short mkt value:   ${broker_short_mkt:>12,.2f}")
print(f"  unrealized P&L:    ${broker_unrealized:>12,.2f}")
print(f"  open positions:    {len(positions)}")
print()

# --- Journal-derived view ---
from journal import get_virtual_account_info, get_virtual_positions

vai = get_virtual_account_info(db_path=db_path)
vpos = get_virtual_positions(db_path=db_path)
print("--- VIRTUAL ACCOUNT (journal-derived) ---")
print(f"  equity:            ${vai.get('equity', 0):>12,.2f}")
print(f"  cash:              ${vai.get('cash', 0):>12,.2f}")
print(f"  market_value:      ${vai.get('market_value', 0):>12,.2f}")
print(f"  unrealized P&L:    ${vai.get('unrealized_pl', 0):>12,.2f}")
print(f"  realized P&L:      ${vai.get('realized_pl', 0):>12,.2f}")
print(f"  open positions:    {len(vpos)}")
print()
print(f"  equity - initial:  ${vai.get('equity', 0) - initial:>+12,.2f}  "
      f"(what the dashboard reports as profit)")
print()

# --- Per-position comparison ---
print("--- PER-POSITION (broker vs virtual) ---")
broker_by_sym = {
    p.symbol: float(p.qty) for p in positions
}
virt_by_sym = {
    p.get("symbol", "?"): float(p.get("qty", 0) or 0)
    for p in vpos
}
all_syms = sorted(set(broker_by_sym) | set(virt_by_sym))
mismatches = 0
for sym in all_syms:
    b = broker_by_sym.get(sym, 0)
    v = virt_by_sym.get(sym, 0)
    flag = "" if abs(b - v) < 0.5 else "  <-- MISMATCH"
    if flag:
        mismatches += 1
    print(f"  {sym:<24} broker={b:>10.0f}  virtual={v:>10.0f}{flag}")
print()
print(f"Mismatches: {mismatches} of {len(all_syms)} positions")
print()
print(f"=== Bottom line: dashboard P&L = virtual_equity - initial_capital "
      f"= ${vai.get('equity', 0) - initial:+,.2f} ===")
print(f"=== But broker_equity - initial = "
      f"${broker_equity - initial:+,.2f} ===")
print(f"=== Delta between displayed and true: "
      f"${vai.get('equity', 0) - broker_equity:+,.2f} ===")
