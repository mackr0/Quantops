"""CONFIRM LONG when insider buying cluster detected.

A cluster (3+ insiders buying in a tight window) is one of the
strongest documented bullish signals in academic literature
(Cohen, Malloy, Pomorski 2012 — "Decoding Insider Information").
Insider sells are noisy; insider BUY clusters are signal.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

NAME = "insider_cluster_buying"
DESCRIPTION = "CONFIRM LONG on insider buying cluster (academic-strong signal)"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    alt = candidate.get("alt_data") or {}
    cluster = alt.get("insider_cluster") or {}
    if not cluster.get("is_cluster") and not cluster.get("cluster_detected"):
        return None
    direction = cluster.get("cluster_direction", "")
    if direction != "buying":
        return None
    n = cluster.get("insider_count", 0) or 0
    val = cluster.get("total_value", 0) or 0
    return {
        "severity": "CONFIRM",
        "reasoning": (
            f"Insider buying cluster: {n} insiders bought ~${val:,.0f}. "
            "Cluster buys are one of the strongest documented bullish "
            "signals in the academic literature."
        ),
    }
