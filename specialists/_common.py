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
    """One-line bare summary — used only when no specialist-specific
    formatter is requested. The specialist ensemble path always goes
    through format_candidate_for_specialist."""
    sym = c.get("symbol", "?")
    signal = c.get("signal", "?")
    price = c.get("price", 0) or 0
    reason = c.get("reason", "")[:120]
    return f"- {sym} [{signal} @ ${price}]: {reason}"


# ─────────────────────────────────────────────────────────────────────
# Per-specialist alt-data routing (2026-05-17 #175 fix)
# ─────────────────────────────────────────────────────────────────────
# Before this, format_candidate_brief stripped alt-data out completely.
# Specialists were asked things like "are insiders buying?" without
# being shown the insider data — they were guessing from the 120-char
# reason string. Per-specialist routing fixes that:
#   - earnings_analyst   sees earnings + biotech + insider_earnings + 8-K 2.02 + fundamentals
#   - pattern_recognizer sees intraday + options + short + dark_pool
#   - sentiment_narrative sees insider + sentiment + congressional + 13D/G + 8-K (1.01, 5.02, 7.01)
#   - risk_assessor      sees fundamentals + 8-K (1.03, 4.02, 5.02) + risk_factor_diff + FDA/NHTSA/FAA/EPA + macro
#   - adversarial_reviewer sees all the negative-catalyst signals (mirror of risk_assessor)
#   - iv_skew_specialist sees options + macro cboe_skew + macro cross_asset_vol
#   - gamma_pin_specialist sees options + intraday
#   - option_spread_risk  sees options + fundamentals + intraday
#
# Each specialist gets its OWN per-candidate render to keep token
# cost bounded — feeding all 34 alt-data keys × 15 candidates × 5
# stock specialists would be a ~10x prompt-size explosion.

_SPECIALIST_ALT_KEYS = {
    "earnings_analyst": (
        "earnings_surprise", "biotech_milestones", "insider_earnings",
        "fundamentals", "analyst_estimates",
    ),
    "pattern_recognizer": (
        "intraday", "options", "short", "finra_short_vol", "dark_pool",
    ),
    "sentiment_narrative": (
        "insider", "insider_cluster", "stocktwits_sentiment",
        "congressional_recent", "google_trends",
        "wikipedia_pageviews", "wikipedia_edits", "app_store_ranking",
        "activist_13dg", "insider_track_records",
        "star_manager_holdings",
    ),
    "risk_assessor": (
        "fundamentals", "short", "risk_factor_diff",
        "fda_inspections", "nhtsa_recalls",
        "epa_osha_violations", "macro",
    ),
    "adversarial_reviewer": (
        "fundamentals", "short", "dark_pool", "risk_factor_diff",
        "recent_8k_events", "fda_inspections", "nhtsa_recalls",
        "epa_osha_violations",
    ),
    "iv_skew_specialist": ("options", "macro"),
    "gamma_pin_specialist": ("options", "intraday"),
    "option_spread_risk": ("options", "fundamentals", "intraday"),
}


def _render_alt_value(key: str, value: Any) -> str:
    """Render one alt-data block as a compact `key=...` snippet.
    Returns empty string when value carries no signal (so the
    rendered candidate line stays tight)."""
    if not isinstance(value, dict) or not value:
        return ""
    # Macro is nested — render sub-keys that matter for the
    # specialist asking for it.
    if key == "macro":
        bits = []
        cav = (value.get("cross_asset_vol") or {})
        if cav:
            for vk in ("move", "ovx", "gvz"):
                v = cav.get(vk) or {}
                if v.get("p30d_label"):
                    bits.append(f"{vk}={v['p30d_label']}")
        skew = (value.get("cboe_skew") or {}).get("skew_signal")
        if skew:
            bits.append(f"skew={skew}")
        yc = (value.get("yield_curve") or {}).get("curve_signal")
        if yc:
            bits.append(f"yc={yc}")
        return f"macro({','.join(bits)})" if bits else ""
    # 8-K events: render high-signal tags only
    if key == "recent_8k_events":
        events = value.get("events") or []
        high = [e for e in events if e.get("item_tags")][:3]
        if not high:
            return ""
        tags = "/".join(
            t for e in high for t in (e.get("item_tags") or [])
        )
        return f"8K({tags})"
    # 13D/G: signal whether activist (13D) or passive (13G)
    if key == "activist_13dg":
        if not value.get("count"):
            return ""
        flag = "13D" if value.get("has_13d") else "13G"
        return f"{flag}({value['count']})"
    # Special signals that don't use the has_data flag
    if key == "risk_factor_diff" and value.get("has_new_risks"):
        return f"newRisks({value.get('added_risk_count', 0)})"
    # has_data-style binary signals — compact one-word render
    if (value.get("has_data") is True
            or value.get("count", 0) > 0
            or value.get("has_new_risks") is True):
        # Per-key short rendering
        if key == "insider":
            return f"insider({value.get('recent_buys', 0)}B/{value.get('recent_sells', 0)}S)"
        if key == "insider_cluster":
            if value.get("cluster_detected"):
                return "insider_CLUSTER"
            return ""
        if key == "stocktwits_sentiment":
            ns = value.get("net_sentiment_7d")
            if ns is None:
                return ""
            return f"twits({ns:+.2f})"
        if key == "short":
            si = value.get("short_interest_pct")
            return f"short({si:.1f}%)" if si else ""
        if key == "finra_short_vol":
            sv = value.get("short_vol_ratio")
            return f"finraSV({sv:.2f})" if sv else ""
        if key == "fda_inspections":
            return f"FDA({value.get('recent_citations_count', 0)})"
        if key == "nhtsa_recalls":
            return f"NHTSA({value.get('recalls_recent_years', 0)})"
        if key == "risk_factor_diff":
            return f"newRisks({value.get('added_risk_count', 0)})"
        if key == "options":
            iv = value.get("iv_rank")
            return f"opts(IV{iv:.0f})" if iv else "opts"
        if key == "intraday":
            patt = value.get("pattern")
            return f"intraday({patt})" if patt else ""
        if key == "earnings_surprise":
            return f"earnSurp({value.get('surprise_pct', 0):+.1f}%)"
        if key == "fundamentals":
            pe = value.get("pe_ratio")
            return f"PE({pe:.1f})" if pe else ""
        # Generic fallback for anything with has_data=True
        return key
    return ""


def _get_or_compute_panel(c: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    """Lazy-compute the deterministic-rule panel for this candidate and
    cache the result on the candidate dict so subsequent specialists
    in the same cycle don't re-run all 147 rules.

    2026-05-18 — Phase 3 re-scope. The LLM specialists used to derive
    facts themselves; now the deterministic library has the facts and
    the LLM's job is synthesis. Surfacing the panel verdicts in the
    candidate render is the plumbing that makes the re-scope possible.

    Fail-soft: any error returns an empty list so the prompt is still
    well-formed.
    """
    if "_panel_verdicts" in c:
        return c["_panel_verdicts"]
    try:
        from deterministic_specialists import run_panel
        verdicts = run_panel(c, ctx)
    except Exception:
        verdicts = []
    c["_panel_verdicts"] = verdicts
    return verdicts


def _format_panel_compact(verdicts: List[Dict[str, Any]]) -> str:
    """One-line `[SEV]name; [SEV]name; ...` summary of fired
    verdicts. Compact form for inlining into the candidate render —
    the full reasoning text is omitted here because the candidate
    line has a length budget. Specialists that need the reasoning
    can ask the deterministic library directly."""
    if not verdicts:
        return ""
    sev_order = {"VETO": 0, "CAUTION": 1, "CONFIRM": 2}
    ranked = sorted(verdicts, key=lambda v: sev_order.get(v.get("severity"), 9))
    bits = [f"[{v['severity'][0]}]{v['name']}" for v in ranked[:12]]
    return "  |  RULES: " + " ".join(bits)


def _fmt_opt_money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "unavailable"


def _format_option_proposal(c: Dict[str, Any]) -> str:
    """Render an OPTION proposal with its actual economics.

    Until 2026-07-15 option proposals fell through the stock-shaped
    render below, which reads only symbol/signal/price/reason — none of
    which an option proposal has — so every specialist reviewed
    `- SYM [? @ $0]:` and option_spread_risk truthfully vetoed 78
    fully-priced spreads for "missing strike and premium data". This
    branch renders the fields the option specialists' prompts actually
    interrogate: strategy, strikes, expiry/DTE, contracts, net premium,
    max loss x contracts, max gain, breakeven, spot, IV rank."""
    econ = c.get("_specialist_econ") or {}
    sym = c.get("symbol", "?")
    action = (c.get("action") or "").upper() or "OPTION"
    strategy = (econ.get("strategy") or c.get("strategy_name")
                or c.get("option_strategy") or "?")
    contracts = econ.get("contracts") or c.get("contracts") or "?"
    bits = [f"- {sym} {strategy} [{action} x{contracts}]:"]

    prem = econ.get("entry_net_premium")
    if prem is not None:
        side_word = ("CREDIT" if econ.get("is_credit")
                     else "DEBIT" if econ.get("is_credit") is not None
                     else "premium")
        bits.append(f"{side_word} {_fmt_opt_money(prem)}/contract")
    else:
        bits.append("net premium: unavailable")

    strikes = c.get("strikes")
    if isinstance(strikes, dict) and strikes:
        ks = ", ".join(f"{k} {v}" for k, v in strikes.items()
                       if v is not None)
        bits.append(f"strikes: {ks}")
    elif c.get("strike") is not None:
        bits.append(f"strike: {c.get('strike')}")

    expiry = econ.get("expiry") or c.get("expiry")
    dte = econ.get("dte")
    if expiry:
        bits.append(f"exp {expiry}" + (f" ({dte} DTE)"
                                       if dte is not None else ""))
    width = econ.get("spread_width_points")
    if width:
        bits.append(f"width {width:g} pts")

    ml = econ.get("max_loss_per_contract")
    if ml is not None:
        total = ""
        try:
            total = f" ({_fmt_opt_money(float(ml) * int(contracts))} total)"
        except (TypeError, ValueError):
            pass
        bits.append(f"max loss {_fmt_opt_money(ml)}/contract{total}")
    else:
        bits.append("max loss: unavailable")
    mg = econ.get("max_gain_per_contract")
    if mg is not None:
        bits.append(f"max gain {_fmt_opt_money(mg)}/contract")
    be = econ.get("breakeven")
    if be is not None:
        bits.append(f"breakeven {be:g}")

    spot = econ.get("spot")
    bits.append(f"spot {_fmt_opt_money(spot)}" if spot
                else "spot: unavailable")
    ivr = econ.get("iv_rank")
    bits.append(f"IV rank {ivr:g}" if ivr is not None
                else "IV rank: unavailable")

    conf = c.get("confidence")
    if conf is not None:
        bits.append(f"AI conf {conf}")
    reasoning = (c.get("reasoning") or c.get("reason") or "")[:160]
    line = " | ".join([bits[0] + " " + bits[1]] + bits[2:])
    return f"{line} — {reasoning}" if reasoning else line


def format_candidate_for_specialist(
    c: Dict[str, Any], specialist_name: str, ctx: Any = None,
) -> str:
    """Per-specialist candidate render. Falls back to the bare brief
    when the specialist isn't in the routing table or when the
    candidate has no alt-data dict.

    Option proposals (action MULTILEG_OPEN / OPTIONS, or an attached
    `_specialist_econ` from the pipeline preflight) take the option
    branch — the stock-shaped render below has none of their fields
    and blinded the option specialists until 2026-07-15.

    When `ctx` is provided, appends a compact RULES summary of the
    deterministic panel verdicts (Phase 3 of docs/17) so the LLM can
    synthesize from facts the rule layer already established. (The
    RULES panel is stock-shaped and is not computed for option
    proposals.)"""
    _action = str(c.get("action") or "").upper()
    if c.get("_specialist_econ") or _action in ("MULTILEG_OPEN", "OPTIONS"):
        return _format_option_proposal(c)
    sym = c.get("symbol", "?")
    signal = c.get("signal", "?")
    price = c.get("price", 0) or 0
    reason = c.get("reason", "")[:120]
    keys = _SPECIALIST_ALT_KEYS.get(specialist_name)
    alt = c.get("alt_data") or {}
    panel_suffix = ""
    if ctx is not None:
        panel_suffix = _format_panel_compact(_get_or_compute_panel(c, ctx))
    if not keys or not alt:
        return f"- {sym} [{signal} @ ${price}]: {reason}{panel_suffix}"
    bits = []
    for k in keys:
        rendered = _render_alt_value(k, alt.get(k))
        if rendered:
            bits.append(rendered)
    if not bits:
        return f"- {sym} [{signal} @ ${price}]: {reason}{panel_suffix}"
    return (f"- {sym} [{signal} @ ${price}]: {reason}  "
            f"|  {' '.join(bits)}{panel_suffix}")


def candidates_block(candidates: List[Dict[str, Any]],
                     limit: int = 20,
                     specialist_name: str = "",
                     ctx: Any = None) -> str:
    """Render a candidate list as a markdown bullet block for prompts.

    When `specialist_name` is set, each candidate gets a per-
    specialist alt-data view (insider data for sentiment_narrative,
    risk-factor diff for risk_assessor, etc.). Otherwise falls back
    to the bare brief.

    When `ctx` is provided, each rendered candidate also carries a
    compact summary of the deterministic-rule panel verdicts so the
    LLM specialist can synthesize from the facts the rule layer
    has already established (Phase 3 of docs/17, 2026-05-18 re-scope).
    """
    if not candidates:
        return "(no candidates)"
    if specialist_name:
        return "\n".join(
            format_candidate_for_specialist(c, specialist_name, ctx=ctx)
            for c in candidates[:limit]
        )
    return "\n".join(format_candidate_brief(c) for c in candidates[:limit])
