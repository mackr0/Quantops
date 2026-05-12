"""Position model — single canonical representation for an open broker
or virtual position.

Why this exists
---------------
The system was built stock-first. Options were bolted on later. As of
2026-05-11 we discovered SIX places where downstream code did
`pos.get("symbol")` and assumed the value was the right thing to send
to the broker:

- `bracket_orders.ensure_protective_stops` submitted STOCK-side
  trailing-stops on the UNDERLYING for option positions. 23 phantom
  stock-stops were armed at Alpaca (each ready to short-sell the
  underlying if triggered).
- `trader._entry_order_filled_at_broker` searched broker positions
  by underlying — never matched OCC option positions → every option
  exit deferred forever.
- `portfolio_manager.check_*` ran stock-style stop-loss/take-profit
  %-of-price math on option premiums.
- `virtual_audit` flagged every legitimate short option leg as a
  data-integrity issue.
- `_record_multileg_legs` used the combo's signed net premium as the
  per-leg price → 14 multileg legs invisible from the AI's view.
- `_enriched_positions` metadata lookup filtered out option SELL
  rows (multileg short legs) → dashboard rows missing AI conf + ts.

All the same root cause: `symbol` meant TWO different things
depending on whether the position came from `client.get_positions`
(virtual = underlying, real = OCC) and downstream code couldn't tell
which one it was looking at.

The Position class fixes that at the type level. Two factories own
the OCC-vs-underlying decision; every consumer reads attributes that
are unambiguous.

Phase 1 (this file)
-------------------
- `Position` dataclass + `from_alpaca(p)` + `from_virtual_row(row)`.
- `__getitem__` / `.get()` / `__contains__` shim so existing
  `pos["symbol"]` / `pos.get("qty")` / `"foo" in pos` calls keep
  working unchanged. Every key supported maps to an attribute via
  `_DICT_KEY_MAP`.
- `client.get_positions` and `journal.get_virtual_positions` start
  returning `List[Position]`. No behavior change for consumers — the
  shim makes them indistinguishable from the old dicts at the call
  sites.

Phase 2-5 (later)
-----------------
- Consumers migrate to attribute access (`pos.broker_symbol`,
  `pos.is_option`, etc.) one path at a time.
- After full migration, the `__getitem__` shim is removed and a
  static guardrail blocks `pos["symbol"]`-style access in production
  code outside this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Optional


def _is_occ_symbol(s: Any) -> bool:
    """OCC option symbol detection. Padded ('AAPL  260612C00150000',
    21 chars) and unpadded ('AAPL260612C00150000', ~14-21 chars) both
    accepted. Same heuristic as `client._is_occ_symbol` — duplicated
    to keep this module dependency-free for testing."""
    if not s or not isinstance(s, str):
        return False
    if len(s) < 14 or len(s) > 21:
        return False
    if not s[-8:].isdigit():
        return False
    if s[-9] not in ("C", "P"):
        return False
    head = s[:-9].rstrip()
    if len(head) < 7:
        return False
    if not head[-6:].isdigit():
        return False
    return True


def _underlying_from_occ(occ: str) -> str:
    """Strip the date+right+strike from an OCC symbol to get the
    underlying ticker. Works on padded and unpadded forms."""
    return occ[:-15].rstrip().upper()


# Canonical attribute names by dict key. Old code used many dict
# keys; the shim below maps each to an attribute. New code uses
# attributes directly.
_DICT_KEY_MAP = {
    # broker-facing identity
    "symbol": "_legacy_symbol",
    "occ_symbol": "occ_symbol",
    # quantity + sign
    "qty": "qty_signed",
    "abs_qty": "abs_qty",
    # economics
    "avg_entry_price": "avg_entry_price",
    "current_price": "current_price",
    "market_value": "market_value",
    "unrealized_pl": "unrealized_pl",
    "unrealized_plpc": "unrealized_plpc",
    # AI metadata (only present on enriched positions; default None)
    "ai_confidence": "ai_confidence",
    "ai_reasoning": "ai_reasoning",
    "stop_loss": "stop_loss",
    "take_profit": "take_profit",
    "decision_price": "decision_price",
    "fill_price": "fill_price",
    "slippage_pct": "slippage_pct",
    "timestamp": "timestamp",
    "side": "side_label",
    "reason": "reason",
    "pnl": "pnl",
    # 2026-05-12 — explicit per-trade TP/SL PRICES.
    "take_profit_price": "take_profit_price",
    "stop_loss_price": "stop_loss_price",
}


@dataclass
class Position:
    """One open position — stock or option leg.

    Constructed by ONE of the two factories below. Every other code
    path consumes Positions and reads attributes (or, during Phase 1
    migration, the dict-style shim).

    Attribute meanings:
      instrument_kind   "stock" | "option"
      underlying        Always the underlying ticker (display name).
                          PCG, AAPL, etc. Same for stocks and options.
      occ_symbol        OCC string for option positions, None for stocks.
      qty_signed        Position size with sign. Long → positive, short
                          → negative. Both stock and option.
      avg_entry_price   For stocks: per-share. For options: per-contract
                          premium. NEVER negative (multileg combo-net
                          bug fixed 2026-05-11 via per-leg lookup).
      current_price     Same units as avg_entry_price.
      market_value      Total dollar exposure (positive for long,
                          negative for short).
      unrealized_pl     Dollar-denominated, accounts for short-side
                          sign correctly.
      unrealized_plpc   Pct-of-cost, sign-aware.

    Optional metadata (set when enriched from journal; None otherwise):
      ai_confidence, ai_reasoning, stop_loss, take_profit,
      decision_price, fill_price, slippage_pct, timestamp,
      side_label (display "buy"/"sell"), reason, pnl.

    Properties for option-vs-stock decisions in consumer code:
      .is_option, .is_stock, .is_short, .is_long, .broker_symbol,
      .display_symbol, .abs_qty.
    """
    instrument_kind: Literal["stock", "option"]
    underlying: str
    occ_symbol: Optional[str]
    qty_signed: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float
    unrealized_plpc: float

    # Optional enrichment metadata
    ai_confidence: Optional[float] = None
    ai_reasoning: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    decision_price: Optional[float] = None
    fill_price: Optional[float] = None
    slippage_pct: Optional[float] = None
    timestamp: Optional[str] = None
    side_label: Optional[str] = None
    reason: Optional[str] = None
    pnl: Optional[float] = None
    # 2026-05-12 — explicit per-trade TP/SL PRICES (unambiguous name
    # — `stop_loss`/`take_profit` above are historically overloaded).
    # `check_stop_loss_take_profit` reads these to fire at the AI's
    # actual target instead of the profile-level percentage.
    take_profit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None

    # ------------------------------------------------------------------
    # The properties consumer code should use after Phase 2 migration.
    # ------------------------------------------------------------------

    @property
    def is_option(self) -> bool:
        return self.instrument_kind == "option"

    @property
    def is_stock(self) -> bool:
        return self.instrument_kind == "stock"

    @property
    def is_short(self) -> bool:
        return self.qty_signed < 0

    @property
    def is_long(self) -> bool:
        return self.qty_signed > 0

    @property
    def abs_qty(self) -> float:
        return abs(self.qty_signed)

    @property
    def broker_symbol(self) -> str:
        """The string `api.submit_order(symbol=...)` should receive.
        OCC for options, underlying for stocks. THE SINGLE SOURCE OF
        TRUTH for what to send to the broker."""
        if self.is_option:
            assert self.occ_symbol, (
                "Option position constructed without occ_symbol — "
                "factory bug"
            )
            return self.occ_symbol
        return self.underlying

    @property
    def display_symbol(self) -> str:
        """The thing humans recognize. Always the underlying ticker."""
        return self.underlying

    # ------------------------------------------------------------------
    # Back-compat shim. Existing code does `pos["symbol"]` /
    # `pos.get("symbol")` / `"qty" in pos`. Phase 1 keeps that working
    # so we can ship the factories without touching every consumer.
    # Phase 5 deletes this shim and adds a guardrail.
    # ------------------------------------------------------------------

    @property
    def _legacy_symbol(self):
        """The legacy `symbol` field meant DIFFERENT things in the two
        producers — `get_virtual_positions` returned the underlying,
        `client.get_positions` returned the OCC for options. To
        preserve back-compat with EVERY existing consumer, the shim
        returns the underlying (matches the virtual producer that
        most code was written against; the dashboard's `is_option`
        check + occ_symbol field still work exactly as before).
        """
        return self.underlying

    def __getitem__(self, key: str) -> Any:
        attr = _DICT_KEY_MAP.get(key)
        if attr is None:
            raise KeyError(key)
        return getattr(self, attr)

    def get(self, key: str, default: Any = None) -> Any:
        attr = _DICT_KEY_MAP.get(key)
        if attr is None:
            return default
        return getattr(self, attr, default)

    def __contains__(self, key: str) -> bool:
        return key in _DICT_KEY_MAP

    def keys(self) -> Iterator[str]:
        """Lets `dict(pos)` work for any consumer that does that."""
        return iter(_DICT_KEY_MAP.keys())

    def __iter__(self) -> Iterator[str]:
        return iter(_DICT_KEY_MAP.keys())

    # ------------------------------------------------------------------
    # Factories — the ONLY two places that decide stock-vs-option.
    # ------------------------------------------------------------------

    @classmethod
    def from_alpaca(cls, p: Any, **enrichment) -> "Position":
        """Build from an Alpaca SDK position object (whatever
        `api.list_positions()` returns). Detects option positions via
        OCC pattern on `p.symbol`."""
        sym = getattr(p, "symbol", "") or ""
        if _is_occ_symbol(sym):
            kind = "option"
            occ = sym
            underlying = _underlying_from_occ(sym)
        else:
            kind = "stock"
            occ = None
            underlying = sym.upper()

        return cls(
            instrument_kind=kind,
            underlying=underlying,
            occ_symbol=occ,
            qty_signed=float(getattr(p, "qty", 0) or 0),
            avg_entry_price=float(getattr(p, "avg_entry_price", 0) or 0),
            current_price=float(getattr(p, "current_price", 0) or 0),
            market_value=float(getattr(p, "market_value", 0) or 0),
            unrealized_pl=float(getattr(p, "unrealized_pl", 0) or 0),
            unrealized_plpc=float(getattr(p, "unrealized_plpc", 0) or 0),
            **enrichment,
        )

    @classmethod
    def from_virtual_row(cls, row: dict, **enrichment) -> "Position":
        """Build from a `get_virtual_positions` output dict. Both
        symbol (underlying) and occ_symbol are present in the row."""
        occ = row.get("occ_symbol")
        if occ:
            kind = "option"
            underlying = (row.get("symbol") or
                          _underlying_from_occ(occ)).upper()
        else:
            kind = "stock"
            underlying = (row.get("symbol") or "").upper()

        return cls(
            instrument_kind=kind,
            underlying=underlying,
            occ_symbol=occ,
            qty_signed=float(row.get("qty", 0) or 0),
            avg_entry_price=float(row.get("avg_entry_price", 0) or 0),
            current_price=float(row.get("current_price", 0) or 0),
            market_value=float(row.get("market_value", 0) or 0),
            unrealized_pl=float(row.get("unrealized_pl", 0) or 0),
            unrealized_plpc=float(row.get("unrealized_plpc", 0) or 0),
            take_profit_price=row.get("take_profit_price"),
            stop_loss_price=row.get("stop_loss_price"),
            **enrichment,
        )
