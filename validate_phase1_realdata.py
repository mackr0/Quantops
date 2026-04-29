"""End-to-end validation of LONG_SHORT_PLAN Phase 1 against real prod data.

Simulates each Phase 1 capability with real symbols / real predictions /
real universe so we know what will actually work tomorrow vs what
silently no-ops because of an empty data path.

Usage:  /opt/quantopsai/venv/bin/python3 validate_phase1_realdata.py
        Run from /opt/quantopsai. Exit 0 = no issues; exit 1 = issues found.
"""
import os
import sys
import sqlite3
import traceback

sys.path.insert(0, ".")

issues = []  # collect for end-of-run summary
warnings = []


def section(title):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def issue(label, detail):
    issues.append(f"{label}: {detail}")
    print(f"  [ISSUE] {label}: {detail}")


def warn(label, detail):
    warnings.append(f"{label}: {detail}")
    print(f"  [WARN] {label}: {detail}")


def ok(label):
    print(f"  [OK] {label}")


# ---------------------------------------------------------------------------
# 1. Bearish strategies — RUN each against the actual scan universe
# ---------------------------------------------------------------------------
section("1. Bearish strategies — exercise against real universe")

conn = sqlite3.connect("quantopsai_profile_10.db")
cur = conn.execute(
    "SELECT DISTINCT symbol FROM trades ORDER BY id DESC LIMIT 30"
)
universe = [r[0] for r in cur.fetchall() if r[0]]
conn.close()
print(f"  Sample universe ({len(universe)} symbols): {universe[:10]}...")

from models import build_user_context_from_profile
ctx = build_user_context_from_profile(10)

bearish_modules = [
    "strategies.breakdown_support",
    "strategies.distribution_at_highs",
    "strategies.failed_breakout",
    "strategies.parabolic_exhaustion",
    "strategies.relative_weakness_in_strong_sector",
]

import importlib
strategy_results = {}
for mod_name in bearish_modules:
    try:
        mod = importlib.import_module(mod_name)
        results = mod.find_candidates(ctx, universe)
        strategy_results[mod.NAME] = len(results)
        if results:
            print(f"  {mod.NAME}: {len(results)} candidates, "
                  f"sample reason={results[0].get('reason', '')[:80]}")
        else:
            print(f"  {mod.NAME}: 0 candidates (may be normal in current regime)")
    except Exception as exc:
        issue(f"{mod.NAME} crashed", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()

total_bearish = sum(strategy_results.values())
if total_bearish == 0:
    warn("Zero bearish candidates",
         "All 5 strategies returned 0 — could be regime, could be data path. "
         "Watch tomorrow.")
else:
    ok(f"Bearish strategies produced {total_bearish} candidates total")


# ---------------------------------------------------------------------------
# 2. Regime classifier
# ---------------------------------------------------------------------------
section("2. Regime classifier — current SPY regime")

from trade_pipeline import _classify_market_regime, _CATALYST_SHORT_STRATEGIES
regime = _classify_market_regime()
print(f"  current regime: {regime}")
print(f"  catalyst-allowed strategies in strong_bull: "
      f"{sorted(_CATALYST_SHORT_STRATEGIES)}")

if regime == "strong_bull":
    catalyst_in_new = _CATALYST_SHORT_STRATEGIES & {
        "breakdown_support", "distribution_at_highs", "failed_breakout",
        "parabolic_exhaustion", "relative_weakness_in_strong_sector"
    }
    if catalyst_in_new:
        ok(f"Catalyst-tagged new bearish strategy: {catalyst_in_new}")
    else:
        warn("Strong-bull regime suppresses ALL new bearish strategies",
             "The 5 new technical bearish strategies will be filtered "
             "tomorrow because none are catalyst-tagged. Consider tagging "
             "distribution_at_highs at minimum.")


# ---------------------------------------------------------------------------
# 3. Backfill spot-check
# ---------------------------------------------------------------------------
section("3. Backfill spot-check — SELL predictions classification")

found_sells = []
for pid in [1, 3, 4, 5, 6, 7, 8, 9, 10, 11]:
    db = f"quantopsai_profile_{pid}.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, symbol, predicted_signal, prediction_type, "
        "substr(reasoning, 1, 120) AS reason "
        "FROM ai_predictions WHERE predicted_signal = 'SELL'"
    ).fetchall()
    conn.close()
    for r in rows:
        found_sells.append((pid, dict(r)))

print(f"  Total SELL predictions: {len(found_sells)}")
exit_count = sum(1 for _, r in found_sells if r["prediction_type"] == "exit_long")
short_count = sum(1 for _, r in found_sells
                  if r["prediction_type"] == "directional_short")
print(f"  Classified: {exit_count} exit_long + {short_count} directional_short")

# Look for unclassified exit-y reasoning
for pid, r in found_sells:
    text = (r["reason"] or "").lower()
    has_exit = any(kw in text for kw in
                    ["exit", "lock in", "take profit", "close existing"])
    if has_exit and r["prediction_type"] != "exit_long":
        issue(f"profile_{pid} #{r['id']} {r['symbol']}",
              f"Reasoning has exit phrasing but classified as "
              f"{r['prediction_type']}. Expected exit_long.")
if exit_count > 0:
    ok(f"{exit_count} exit_long classifications match exit phrasing")


# ---------------------------------------------------------------------------
# 4. Borrow info
# ---------------------------------------------------------------------------
section("4. Borrow info — sample real candidates")

from client import get_borrow_info
sample_symbols = ["AAPL", "TSLA", "GME", "AMC", "NVDA", "PLTR"]
shortable_count = 0
htb_count = 0
for sym in sample_symbols:
    info = get_borrow_info(sym, ctx=ctx)
    print(f"  {sym}: {info}")
    if info.get("shortable"):
        shortable_count += 1
        if not info.get("easy_to_borrow"):
            htb_count += 1

if shortable_count == 0:
    issue("No symbols shortable", "Alpaca asset endpoint may be misconfigured.")
elif shortable_count == len(sample_symbols) and htb_count == 0:
    warn("All symbols easy_to_borrow",
         "Alpaca paper says everything's easy. HTB penalty won't engage "
         "in paper but will live. Acceptable.")


# ---------------------------------------------------------------------------
# 5. _rank_candidates simulation
# ---------------------------------------------------------------------------
section("5. _rank_candidates — full pipeline simulation")

from trade_pipeline import _rank_candidates


def fake_signal(sym, sig, score):
    return {"symbol": sym, "signal": sig, "score": score,
            "votes": {"strat": sig}, "rsi": 50}


mixed = (
    [fake_signal(f"L{i}", "BUY", 4) for i in range(12)] +
    [fake_signal(f"S{i}", "SHORT", 2) for i in range(6)]
)
long_out = _rank_candidates(mixed, held_symbols=set(), enable_shorts=False)
long_short_count = sum(1 for c in long_out if c["signal"] == "SHORT")
if long_short_count > 0:
    issue("Long-only profile leaked SHORT candidates",
          f"got {long_short_count}, expected 0")
else:
    ok("Long-only profile filters out SHORTs")

import unittest.mock as _mock
with _mock.patch("trade_pipeline._classify_market_regime", return_value="neutral"), \
     _mock.patch("trade_pipeline._squeeze_risk", return_value="LOW"), \
     _mock.patch("client.get_borrow_info",
                  return_value={"shortable": True, "easy_to_borrow": True}):
    short_out = _rank_candidates(mixed, held_symbols=set(), enable_shorts=True)
    sc = sum(1 for c in short_out if c["signal"] == "SHORT")
    lc = sum(1 for c in short_out if c["signal"] == "BUY")
    if sc < 1 or sc > 5:
        issue("Reserved-slot logic broken", f"got {sc} shorts, expected 1-5")
    else:
        ok(f"Reserved {sc} short slots, {lc} long slots")


# ---------------------------------------------------------------------------
# 6. AI prompt rendering
# ---------------------------------------------------------------------------
section("6. AI prompt — shorts-enabled section rendering")

from ai_analyst import _build_batch_prompt


class StubCtx:
    enable_short_selling = True
    max_position_pct = 0.10
    max_total_positions = 10
    segment = "small"
    db_path = "quantopsai_profile_10.db"
    signal_weights = "{}"
    prompt_layout = "{}"
    short_max_position_pct = 0.05


candidates_data = [
    {"symbol": "AAPL", "price": 200, "signal": "BUY", "score": 3,
     "rsi": 55, "volume_ratio": 1.2, "_borrow_cost": "low"},
    {"symbol": "GME", "price": 30, "signal": "SHORT", "score": 2,
     "rsi": 75, "volume_ratio": 2.1,
     "_borrow_cost": "high", "_squeeze_risk": "MED"},
]
portfolio_state = {"positions": [], "equity": 100000, "cash": 100000}
market_context = {"vix": 18, "regime": "bullish",
                  "sector_rotation": {}, "macro_data": {}}

try:
    prompt = _build_batch_prompt(candidates_data, portfolio_state,
                                  market_context, ctx=StubCtx())
    checks = [
        ("LONG CANDIDATES section", "LONG CANDIDATES" in prompt),
        ("SHORT CANDIDATES section", "SHORT CANDIDATES" in prompt),
        ("Borrow cost annotation", "BORROW: high cost" in prompt),
        ("Squeeze annotation", "SQUEEZE: MED" in prompt),
        ("BUY | SHORT actions", "BUY | SHORT" in prompt),
        ("Asymmetric sizing copy", "halved for shorts" in prompt),
        ("Long/short balance directive", "high-conviction short beats" in prompt),
    ]
    for label, present in checks:
        if present:
            ok(label)
        else:
            issue(f"AI prompt missing: {label}", "")
except Exception as exc:
    issue("Prompt rendering crashed", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 7. Resolver agreement
# ---------------------------------------------------------------------------
section("7. Resolver — apply per-type criteria on a sample")

from ai_tracker import _resolve_one

spot_rows = []
for pid in [1, 3, 8, 10]:
    db = f"quantopsai_profile_{pid}.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    for ptype in ("directional_long", "directional_short", "exit_long"):
        rows = conn.execute(
            "SELECT predicted_signal, prediction_type, price_at_prediction, "
            "resolution_price, actual_outcome, timestamp FROM ai_predictions "
            "WHERE prediction_type = ? AND status = 'resolved' AND "
            "resolution_price IS NOT NULL LIMIT 2",
            (ptype,),
        ).fetchall()
        for r in rows:
            spot_rows.append(dict(r))
    conn.close()

agree = 0
disagree = 0
for r in spot_rows:
    pred = {
        "predicted_signal": r["predicted_signal"],
        "prediction_type": r["prediction_type"],
        "price_at_prediction": r["price_at_prediction"],
        "timestamp": r["timestamp"],
    }
    result = _resolve_one(pred, r["resolution_price"])
    if result is None:
        continue
    if result[0] == r["actual_outcome"]:
        agree += 1
    else:
        disagree += 1

print(f"  Resolver: {agree} agree, {disagree} disagree out of {agree + disagree}")
if disagree > 0:
    warn("Resolver divergence",
         f"{disagree}/{agree+disagree} historic resolutions diverge from "
         f"the new resolver. Spot-check that those rows are exit_long type "
         f"(expected divergence — different criteria).")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("SUMMARY")
print()
if not issues and not warnings:
    print("  All Phase 1 capabilities validated. No issues, no warnings.")
else:
    if issues:
        print(f"  {len(issues)} ISSUES:")
        for i in issues:
            print(f"    - {i}")
    if warnings:
        print(f"  {len(warnings)} WARNINGS (non-blocking):")
        for w in warnings:
            print(f"    - {w}")
print()
sys.exit(1 if issues else 0)
