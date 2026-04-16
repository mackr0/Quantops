"""Auto-strategy generator — Phase 7 of the Quant Fund Evolution roadmap.

Most quant systems have a fixed strategy library that ages out over time.
Ours evolves: the AI proposes new strategy *specs* (pure JSON, not code),
we validate the spec against a closed allowlist of fields and operators,
render it into a deterministic Python module, backtest it, and if it clears
the Phase 2 gate it enters shadow-trading. This module never evaluates
AI-written code — it translates structured specs into safe Python via a
fixed template.

Spec schema (all fields required unless noted):
    {
      "name":        "snake_case_identifier",
      "description": "one-line intent",
      "applicable_markets": ["small", "midcap", ...],
      "direction":   "BUY" | "SELL",
      "score":       1|2|3,
      "conditions":  [ {"field": F, "op": OP, "value": V}, ... ]  # ALL must hold
    }

Conditions are AND-combined. Each condition references either a concrete
indicator column produced by `market_data.add_indicators` or one of the
derived fields the generator knows how to compute. Comparisons against
another field use `"field_ref"` in place of `"value"`.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Allowlist — the only fields the AI is permitted to reference in a spec.
# Anything outside this list is rejected at validation time, which means an
# AI-proposed spec cannot smuggle in arbitrary Python, data, or side effects.
# ---------------------------------------------------------------------------

ALLOWED_INDICATOR_FIELDS = {
    # Raw OHLCV
    "open", "high", "low", "close", "volume",
    # Moving averages
    "sma_10", "sma_20", "sma_50", "ema_12",
    # Momentum
    "rsi", "macd", "macd_signal", "macd_histogram",
    # Volatility bands
    "bb_upper", "bb_middle", "bb_lower",
    # Volume context
    "volume_sma_20",
    # Range structure
    "high_10", "high_20", "low_5", "low_10",
}

# Derived fields the generator computes from the bars on the fly.
ALLOWED_DERIVED_FIELDS = {
    "price_pct_vs_sma20",   # (close / sma_20 - 1) * 100
    "price_pct_vs_sma50",   # (close / sma_50 - 1) * 100
    "volume_ratio",         # volume / volume_sma_20
    "gap_pct",              # (open - prev_close) / prev_close * 100
    "pct_change_1d",        # (close - prev_close) / prev_close * 100
    "range_position",       # (close - low_20) / (high_20 - low_20) in [0, 1]
    "rsi_change_5d",        # rsi - rsi.shift(5)
}

ALLOWED_FIELDS = ALLOWED_INDICATOR_FIELDS | ALLOWED_DERIVED_FIELDS

ALLOWED_OPS = {">", ">=", "<", "<=", "==", "!="}
ALLOWED_MARKETS = {"micro", "small", "midcap", "largecap", "crypto"}
ALLOWED_DIRECTIONS = {"BUY", "SELL"}

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
STRATEGIES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategies")


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------

class SpecError(ValueError):
    """Raised when a proposed strategy spec violates the allowlist."""


def validate_spec(spec: Dict[str, Any]) -> None:
    """Reject any spec that doesn't conform to the allowlisted grammar.

    Raises SpecError with a human-readable message on the first violation.
    Does not mutate the spec.
    """
    if not isinstance(spec, dict):
        raise SpecError("spec must be a dict")

    for required in ("name", "description", "applicable_markets",
                     "direction", "score", "conditions"):
        if required not in spec:
            raise SpecError(f"missing required field: {required}")

    name = spec["name"]
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise SpecError(
            "name must be lowercase snake_case, 3-64 chars, starting with a letter"
        )
    if not name.startswith("auto_"):
        raise SpecError("auto-generated strategy names must start with 'auto_'")

    markets = spec["applicable_markets"]
    if (not isinstance(markets, list) or not markets
            or any(m not in ALLOWED_MARKETS for m in markets)):
        raise SpecError(
            f"applicable_markets must be a non-empty subset of {sorted(ALLOWED_MARKETS)}"
        )

    if spec["direction"] not in ALLOWED_DIRECTIONS:
        raise SpecError(f"direction must be one of {sorted(ALLOWED_DIRECTIONS)}")

    score = spec["score"]
    if not isinstance(score, int) or score not in (1, 2, 3):
        raise SpecError("score must be an integer 1, 2, or 3")

    conditions = spec["conditions"]
    if (not isinstance(conditions, list) or not conditions
            or len(conditions) > 6):
        raise SpecError("conditions must be a list of 1-6 entries")

    for i, cond in enumerate(conditions):
        _validate_condition(cond, i)


def _validate_condition(cond: Dict[str, Any], idx: int) -> None:
    if not isinstance(cond, dict):
        raise SpecError(f"condition[{idx}] must be a dict")
    field = cond.get("field")
    op = cond.get("op")
    if field not in ALLOWED_FIELDS:
        raise SpecError(
            f"condition[{idx}].field '{field}' not in allowlist "
            f"({len(ALLOWED_FIELDS)} permitted)"
        )
    if op not in ALLOWED_OPS:
        raise SpecError(f"condition[{idx}].op '{op}' not in {sorted(ALLOWED_OPS)}")

    has_value = "value" in cond
    has_field_ref = "field_ref" in cond
    if has_value == has_field_ref:
        raise SpecError(
            f"condition[{idx}] must supply exactly one of 'value' or 'field_ref'"
        )
    if has_value:
        if not isinstance(cond["value"], (int, float)):
            raise SpecError(f"condition[{idx}].value must be numeric")
    else:
        if cond["field_ref"] not in ALLOWED_FIELDS:
            raise SpecError(
                f"condition[{idx}].field_ref '{cond['field_ref']}' not in allowlist"
            )


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------

_TEMPLATE = '''"""Auto-generated strategy — {NAME}.

{DESCRIPTION}

Generated by strategy_generator on {GENERATED_AT} from spec id={SPEC_ID}.
DO NOT EDIT — regenerate from the JSON spec instead.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = {NAME_REPR}
APPLICABLE_MARKETS = {MARKETS_REPR}
AUTO_GENERATED = True
SPEC_ID = {SPEC_ID}


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars, add_indicators
    from strategy_generator import evaluate_conditions

    spec_conditions = {CONDITIONS_REPR}
    direction = {DIRECTION_REPR}
    score = {SCORE}

    out: List[Dict[str, Any]] = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=60)
            if df is None or len(df) < 30:
                continue
            if "rsi" not in df.columns:
                df = add_indicators(df)
            if not evaluate_conditions(df, spec_conditions):
                continue
            price = float(df["close"].iloc[-1])
            out.append({{
                "symbol": symbol,
                "signal": direction,
                "score": score,
                "votes": {{NAME: direction}},
                "price": price,
                "reason": {REASON_REPR},
            }})
        except Exception:
            continue
    return out
'''


def render_strategy_module(spec: Dict[str, Any], spec_id: int) -> str:
    """Render a validated spec into the Python source for a strategy module.

    Raises SpecError if the spec is invalid. Output is deterministic for a
    given (spec, spec_id) pair.
    """
    validate_spec(spec)
    name = spec["name"]
    reason = f"Auto-strategy {name}: {spec['description']}"
    return _TEMPLATE.format(
        NAME=name,
        DESCRIPTION=spec["description"],
        GENERATED_AT=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        SPEC_ID=spec_id,
        NAME_REPR=repr(name),
        MARKETS_REPR=repr(list(spec["applicable_markets"])),
        CONDITIONS_REPR=repr(spec["conditions"]),
        DIRECTION_REPR=repr(spec["direction"]),
        SCORE=int(spec["score"]),
        REASON_REPR=repr(reason),
    )


def write_strategy_module(spec: Dict[str, Any], spec_id: int,
                          dest_dir: Optional[str] = None) -> str:
    """Render and write the module to strategies/<name>.py. Returns the path."""
    source = render_strategy_module(spec, spec_id)
    # Resolve STRATEGIES_DIR at call time so tests can monkeypatch it.
    target_dir = dest_dir if dest_dir is not None else STRATEGIES_DIR
    path = os.path.join(target_dir, f"{spec['name']}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(source)
    return path


# ---------------------------------------------------------------------------
# Condition evaluation — imported and called by every auto-generated module.
# Keeping the logic here instead of inlining it into every generated file
# means we can fix bugs in one place and every auto-strategy picks up the fix.
# ---------------------------------------------------------------------------

def evaluate_conditions(df, conditions: List[Dict[str, Any]]) -> bool:
    """Return True iff every condition is satisfied by the latest bar.

    `df` must be the output of add_indicators (or equivalent). Derived
    fields are computed on the fly from the existing columns.
    """
    if df is None or len(df) == 0:
        return False
    try:
        for cond in conditions:
            lhs = _resolve_field(df, cond["field"])
            if lhs is None:
                return False
            if "value" in cond:
                rhs = float(cond["value"])
            else:
                rhs = _resolve_field(df, cond["field_ref"])
                if rhs is None:
                    return False
            if not _apply_op(lhs, rhs, cond["op"]):
                return False
        return True
    except Exception:
        return False


def _resolve_field(df, field: str) -> Optional[float]:
    """Return the latest-bar value for an indicator or derived field."""
    if field in ALLOWED_INDICATOR_FIELDS:
        if field not in df.columns:
            return None
        val = df[field].iloc[-1]
        return None if val is None else float(val)

    if field == "price_pct_vs_sma20":
        if "sma_20" not in df.columns:
            return None
        s = float(df["sma_20"].iloc[-1])
        return None if s <= 0 else (float(df["close"].iloc[-1]) / s - 1) * 100

    if field == "price_pct_vs_sma50":
        if "sma_50" not in df.columns:
            return None
        s = float(df["sma_50"].iloc[-1])
        return None if s <= 0 else (float(df["close"].iloc[-1]) / s - 1) * 100

    if field == "volume_ratio":
        if "volume_sma_20" not in df.columns:
            return None
        v = float(df["volume_sma_20"].iloc[-1])
        return None if v <= 0 else float(df["volume"].iloc[-1]) / v

    if field == "gap_pct":
        if len(df) < 2:
            return None
        prev_close = float(df["close"].iloc[-2])
        today_open = float(df["open"].iloc[-1])
        return None if prev_close <= 0 else (today_open - prev_close) / prev_close * 100

    if field == "pct_change_1d":
        if len(df) < 2:
            return None
        prev_close = float(df["close"].iloc[-2])
        today_close = float(df["close"].iloc[-1])
        return None if prev_close <= 0 else (today_close - prev_close) / prev_close * 100

    if field == "range_position":
        if "high_20" not in df.columns:
            return None
        high = float(df["high_20"].iloc[-1])
        low = float(df["low"].rolling(20).min().iloc[-1])
        rng = high - low
        return None if rng <= 0 else (float(df["close"].iloc[-1]) - low) / rng

    if field == "rsi_change_5d":
        if "rsi" not in df.columns or len(df) < 6:
            return None
        now = df["rsi"].iloc[-1]
        prev = df["rsi"].iloc[-6]
        if now is None or prev is None:
            return None
        return float(now) - float(prev)

    return None


def _apply_op(lhs: float, rhs: float, op: str) -> bool:
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    if op == "==":
        return abs(lhs - rhs) < 1e-9
    if op == "!=":
        return abs(lhs - rhs) >= 1e-9
    return False


# ---------------------------------------------------------------------------
# Lifecycle persistence
# ---------------------------------------------------------------------------

STATUSES = ("proposed", "validated", "shadow", "active", "retired")


def save_spec(db_path: str, spec: Dict[str, Any],
              parent_id: Optional[int] = None) -> int:
    """Persist a new spec (status=proposed). Returns the auto-assigned id."""
    validate_spec(spec)
    generation = 1
    if parent_id is not None:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT generation FROM auto_generated_strategies WHERE id = ?",
                (parent_id,),
            ).fetchone()
            if row:
                generation = int(row[0]) + 1
        finally:
            conn.close()

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO auto_generated_strategies
                 (name, spec_json, status, generation, parent_id)
               VALUES (?, ?, 'proposed', ?, ?)""",
            (spec["name"], json.dumps(spec), generation, parent_id),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_status(db_path: str, spec_id: int, status: str,
                  retirement_reason: Optional[str] = None,
                  validation_report: Optional[Dict[str, Any]] = None) -> None:
    """Transition a strategy to a new lifecycle state."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status}")

    conn = sqlite3.connect(db_path)
    try:
        now = "datetime('now')"
        if status == "validated":
            sql = ("UPDATE auto_generated_strategies SET status = ?, "
                   "validated_at = datetime('now'), "
                   "validation_report_json = ? WHERE id = ?")
            conn.execute(sql, (status,
                               json.dumps(validation_report) if validation_report else None,
                               spec_id))
        elif status == "shadow":
            conn.execute(
                "UPDATE auto_generated_strategies SET status = ?, "
                "shadow_started_at = datetime('now') WHERE id = ?",
                (status, spec_id),
            )
        elif status == "active":
            conn.execute(
                "UPDATE auto_generated_strategies SET status = ?, "
                "promoted_at = datetime('now') WHERE id = ?",
                (status, spec_id),
            )
        elif status == "retired":
            conn.execute(
                "UPDATE auto_generated_strategies SET status = ?, "
                "retired_at = datetime('now'), retirement_reason = ? WHERE id = ?",
                (status, retirement_reason, spec_id),
            )
        else:
            conn.execute(
                "UPDATE auto_generated_strategies SET status = ? WHERE id = ?",
                (status, spec_id),
            )
        conn.commit()
    finally:
        conn.close()


def list_strategies(db_path: str,
                    status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all auto-generated strategies, optionally filtered by status."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM auto_generated_strategies WHERE status = ? "
                "ORDER BY id DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM auto_generated_strategies ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_strategy(db_path: str, spec_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM auto_generated_strategies WHERE id = ?",
            (spec_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_module_file(spec_name: str,
                       dest_dir: Optional[str] = None) -> bool:
    """Remove the generated .py file. Returns True iff a file was removed."""
    target_dir = dest_dir if dest_dir is not None else STRATEGIES_DIR
    path = os.path.join(target_dir, f"{spec_name}.py")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False
