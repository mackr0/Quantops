"""CAUTION LONG on a dark-cloud-cover pattern — yesterday green,
today red opening above yesterday's high but closing back below
the midpoint of yesterday's body. Mirror of piercing pattern."""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "candle_dark_cloud_cover"
DESCRIPTION = "CAUTION LONG on dark-cloud-cover (partial bearish engulfing)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    candle = candidate.get("candle") or {}
    today = candle.get("today") or {}
    prior = candle.get("prior") or {}
    if not today or not prior:
        return None
    if today.get("is_green") or not prior.get("is_green"):
        return None
    p_open = prior.get("open", 0)
    p_close = prior.get("close", 0)
    p_high = prior.get("high", 0)
    t_open = today.get("open", 0)
    t_close = today.get("close", 0)
    # Today opened above yesterday's high AND closed below midpoint of body
    if t_open <= p_high:
        return None
    midpoint = (p_open + p_close) / 2
    if t_close <= midpoint and t_close > p_open:
        return {"severity": "CAUTION",
                "reasoning": "Dark-cloud cover — today gapped above yesterday's high but closed back inside yesterday's body. Partial reversal."}
    return None
