"""Shared helpers for specialist modules — JSON parsing, candidate summarization."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


VALID_VERDICTS = {"BUY", "SELL", "HOLD", "VETO"}
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def extract_verdict_array(raw: str) -> List[Dict[str, Any]]:
    """Best-effort: find JSON verdict entries in the response and parse them.

    Accepts four shapes the AI might produce despite a "return an array"
    instruction:
      1. A clean JSON array: ``[{...}, {...}]``
      2. A single JSON object: ``{...}`` → wrapped into a one-item list
      3. Multiple concatenated objects: ``{...}\\n{...}`` → each parsed
      4. Any of the above embedded in prose or markdown fences

    Each entry must have `symbol` and `verdict` (BUY/SELL/HOLD/VETO).
    Invalid entries are silently dropped. Returns an empty list on total
    parse failure — callers treat that as "specialist abstains".
    """
    if not raw:
        return []

    parsed: Optional[Any] = None
    # 1. Direct parse
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2. Look for an embedded JSON array
    if parsed is None:
        match = _JSON_ARRAY_RE.search(raw)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = None

    # 3. Wrap a single object into a list
    if isinstance(parsed, dict):
        parsed = [parsed]

    # 4. Last resort: scan for multiple top-level objects in the text
    #    (Haiku sometimes streams one object per line instead of an array)
    if not isinstance(parsed, list):
        found = []
        for m in _JSON_OBJECT_RE.finditer(raw):
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "symbol" in obj and "verdict" in obj:
                found.append(obj)
        if found:
            parsed = found

    if not isinstance(parsed, list):
        return []

    out: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        sym = item.get("symbol")
        verdict = item.get("verdict")
        if not isinstance(sym, str) or verdict not in VALID_VERDICTS:
            continue
        try:
            conf = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(100.0, conf))
        out.append({
            "symbol": sym,
            "verdict": verdict,
            "confidence": conf,
            "reasoning": str(item.get("reasoning", ""))[:400],
        })
    return out


def format_candidate_brief(c: Dict[str, Any]) -> str:
    """One-line summary of a candidate for inclusion in specialist prompts."""
    sym = c.get("symbol", "?")
    signal = c.get("signal", "?")
    price = c.get("price", 0) or 0
    reason = c.get("reason", "")[:120]
    return f"- {sym} [{signal} @ ${price}]: {reason}"


def candidates_block(candidates: List[Dict[str, Any]], limit: int = 20) -> str:
    """Render a candidate list as a markdown bullet block for prompts."""
    if not candidates:
        return "(no candidates)"
    return "\n".join(format_candidate_brief(c) for c in candidates[:limit])
