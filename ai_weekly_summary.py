"""Weekly AI-work digest — single email covering all trading profiles.

Runs once per week (Friday ~5 PM ET, after the final self-tune of the
trading week). Builds a consolidated HTML summary of:

  - Week's headline numbers per profile: P&L, trades, win rate, AI cost
  - Self-tuning changes (with "why" and 7-day "effect" rollup)
  - Strategy deprecations/restorations (Phase 3 alpha decay)
  - Auto-strategy lifecycle transitions (Phase 7)
  - Crisis state transitions (Phase 10)
  - Top/bottom trades of the week with AI reasoning + outcome

Data sources:
  - Master DB (quantopsai.db): trading_profiles, tuning_history, users
  - Per-profile DBs (quantopsai_profile_{id}.db): trades, ai_predictions,
    ai_cost_ledger, deprecated_strategies, auto_generated_strategies,
    crisis_state_history
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------

def _week_window(end: Optional[datetime] = None) -> tuple:
    """Return (start_str, end_str) UTC timestamps for the week ending `end`.

    Default end = now. Window = 7 days back from end.
    """
    if end is None:
        end = datetime.utcnow()
    start = end - timedelta(days=7)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _prev_week_window(end: Optional[datetime] = None) -> tuple:
    """Return UTC window [end-14d, end-7d] for before/after comparisons."""
    if end is None:
        end = datetime.utcnow()
    start = end - timedelta(days=14)
    mid = end - timedelta(days=7)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        mid.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _safe_table_exists(conn, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Per-profile stats gathering
# ---------------------------------------------------------------------------

def _gather_profile_stats(
    master_db_path: str,
    profile: Dict[str, Any],
    start: str,
    end: str,
) -> Dict[str, Any]:
    """Pull all the per-profile week metrics into a dict."""
    pid = profile["id"]
    db_path = f"quantopsai_profile_{pid}.db"

    stats: Dict[str, Any] = {
        "profile_id": pid,
        "name": profile["name"],
        "market_type": profile["market_type"],
        "db_path": db_path,
        "trades": [],
        "buys": 0,
        "sells": 0,
        "realized_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "resolved_predictions": 0,
        "win_rate": None,
        "ai_cost": 0.0,
        "cost_by_purpose": {},
        "tuning_changes": [],
        "deprecated_strategies": [],
        "restored_strategies": [],
        "auto_strategy_transitions": [],
        "crisis_transitions": [],
    }

    # --- per-profile DB queries ---
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        if _safe_table_exists(conn, "trades"):
            # Aggregate buys/sells/pnl
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) buys,
                    SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) sells,
                    COALESCE(SUM(CASE WHEN side='sell' THEN pnl ELSE 0 END), 0) pnl
                FROM trades
                WHERE timestamp BETWEEN ? AND ?
                """,
                (start, end),
            ).fetchone()
            stats["buys"] = row["buys"] or 0
            stats["sells"] = row["sells"] or 0
            stats["realized_pnl"] = float(row["pnl"] or 0)

            # All week's trades with AI reasoning — used for top/bottom picks
            rows = conn.execute(
                """
                SELECT timestamp, symbol, side, qty, price, pnl, ai_reasoning,
                       ai_confidence, signal_type, reason
                FROM trades
                WHERE timestamp BETWEEN ? AND ?
                  AND pnl IS NOT NULL
                ORDER BY ABS(COALESCE(pnl, 0)) DESC
                """,
                (start, end),
            ).fetchall()
            stats["trades"] = [dict(r) for r in rows]

        if _safe_table_exists(conn, "ai_predictions"):
            row = conn.execute(
                """
                SELECT
                    COUNT(*) n,
                    SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) wins,
                    SUM(CASE WHEN actual_outcome='loss' THEN 1 ELSE 0 END) losses
                FROM ai_predictions
                WHERE resolved_at IS NOT NULL
                  AND resolved_at BETWEEN ? AND ?
                """,
                (start, end),
            ).fetchone()
            n = row["n"] or 0
            wins = row["wins"] or 0
            losses = row["losses"] or 0
            stats["resolved_predictions"] = n
            stats["wins"] = wins
            stats["losses"] = losses
            denom = wins + losses
            stats["win_rate"] = (wins / denom) if denom else None

        if _safe_table_exists(conn, "ai_cost_ledger"):
            rows = conn.execute(
                """
                SELECT purpose, SUM(estimated_cost_usd) cost
                FROM ai_cost_ledger
                WHERE timestamp BETWEEN ? AND ?
                GROUP BY purpose
                """,
                (start, end),
            ).fetchall()
            for r in rows:
                c = float(r["cost"] or 0)
                stats["ai_cost"] += c
                stats["cost_by_purpose"][r["purpose"]] = c

        if _safe_table_exists(conn, "deprecated_strategies"):
            for r in conn.execute(
                """
                SELECT strategy_type, deprecated_at, reason,
                       rolling_sharpe_at_deprecation, lifetime_sharpe
                FROM deprecated_strategies
                WHERE deprecated_at BETWEEN ? AND ?
                """,
                (start, end),
            ).fetchall():
                stats["deprecated_strategies"].append(dict(r))
            for r in conn.execute(
                """
                SELECT strategy_type, restored_at
                FROM deprecated_strategies
                WHERE restored_at IS NOT NULL
                  AND restored_at BETWEEN ? AND ?
                """,
                (start, end),
            ).fetchall():
                stats["restored_strategies"].append(dict(r))

        if _safe_table_exists(conn, "auto_generated_strategies"):
            # Any transition in the window counts (validated_at, shadow_started_at,
            # promoted_at, retired_at falls within [start,end])
            for r in conn.execute(
                """
                SELECT name, status, validated_at, shadow_started_at,
                       promoted_at, retired_at, retirement_reason
                FROM auto_generated_strategies
                WHERE (validated_at BETWEEN ? AND ?)
                   OR (shadow_started_at BETWEEN ? AND ?)
                   OR (promoted_at BETWEEN ? AND ?)
                   OR (retired_at BETWEEN ? AND ?)
                """,
                (start, end, start, end, start, end, start, end),
            ).fetchall():
                stats["auto_strategy_transitions"].append(dict(r))

        if _safe_table_exists(conn, "crisis_state_history"):
            for r in conn.execute(
                """
                SELECT transitioned_at, from_level, to_level, signals_json,
                       size_multiplier
                FROM crisis_state_history
                WHERE transitioned_at BETWEEN ? AND ?
                ORDER BY transitioned_at
                """,
                (start, end),
            ).fetchall():
                stats["crisis_transitions"].append(dict(r))

        conn.close()
    except Exception as exc:
        logger.warning("weekly summary: per-profile query failed for %s: %s",
                       db_path, exc)

    # --- tuning_history from MASTER DB ---
    try:
        conn = sqlite3.connect(master_db_path)
        conn.row_factory = sqlite3.Row
        if _safe_table_exists(conn, "tuning_history"):
            for r in conn.execute(
                """
                SELECT timestamp, adjustment_type, parameter_name,
                       old_value, new_value, reason, win_rate_at_change,
                       outcome_after, win_rate_after
                FROM tuning_history
                WHERE profile_id = ?
                  AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp DESC
                """,
                (pid, start, end),
            ).fetchall():
                stats["tuning_changes"].append(dict(r))
        conn.close()
    except Exception as exc:
        logger.warning("weekly summary: tuning_history query failed: %s", exc)

    return stats


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def build_weekly_summary(
    master_db_path: str = "quantopsai.db",
    end: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build cross-profile summary dict for the 7 days ending `end`."""
    start, end_str = _week_window(end)
    end_obj = end or datetime.utcnow()
    start_obj = end_obj - timedelta(days=7)

    profiles: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(master_db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, name, market_type, user_id, enabled "
            "FROM trading_profiles WHERE enabled=1 ORDER BY id"
        ).fetchall()
        conn.close()
        profiles = [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("weekly summary: trading_profiles query failed: %s", exc)

    per_profile: List[Dict[str, Any]] = []
    totals = {
        "realized_pnl": 0.0,
        "buys": 0,
        "sells": 0,
        "resolved_predictions": 0,
        "wins": 0,
        "losses": 0,
        "ai_cost": 0.0,
        "tuning_changes": 0,
        "deprecated_strategies": 0,
        "restored_strategies": 0,
        "auto_strategy_transitions": 0,
        "crisis_transitions": 0,
    }

    for p in profiles:
        stats = _gather_profile_stats(master_db_path, p, start, end_str)
        per_profile.append(stats)
        totals["realized_pnl"] += stats["realized_pnl"]
        totals["buys"] += stats["buys"]
        totals["sells"] += stats["sells"]
        totals["resolved_predictions"] += stats["resolved_predictions"]
        totals["wins"] += stats["wins"]
        totals["losses"] += stats["losses"]
        totals["ai_cost"] += stats["ai_cost"]
        totals["tuning_changes"] += len(stats["tuning_changes"])
        totals["deprecated_strategies"] += len(stats["deprecated_strategies"])
        totals["restored_strategies"] += len(stats["restored_strategies"])
        totals["auto_strategy_transitions"] += len(
            stats["auto_strategy_transitions"]
        )
        totals["crisis_transitions"] += len(stats["crisis_transitions"])

    totals_denom = totals["wins"] + totals["losses"]
    totals["win_rate"] = (
        totals["wins"] / totals_denom if totals_denom else None
    )

    return {
        "start_utc": start,
        "end_utc": end_str,
        "start_label": start_obj.strftime("%a %b %d"),
        "end_label": end_obj.strftime("%a %b %d, %Y"),
        "profiles": per_profile,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_html(summary: Dict[str, Any]) -> tuple:
    """Render the digest as (subject, html_body)."""
    from notifications import (
        _wrap_html, _section, _table, _kv_row,
        _color_pnl, _color_pct,
    )

    subject = (
        f"QuantOpsAI Weekly Digest — {summary['start_label']} → "
        f"{summary['end_label']}"
    )

    t = summary["totals"]
    profiles = summary["profiles"]

    # --- Headline ---
    wr = f"{t['win_rate']*100:.1f}%" if t["win_rate"] is not None else "—"
    headline = (
        _kv_row("Realized P&amp;L", _color_pnl(t["realized_pnl"]))
        + _kv_row(
            "Trades",
            f"{t['buys']} buys / {t['sells']} sells across "
            f"{len(profiles)} active profile(s)",
        )
        + _kv_row(
            "Resolved predictions",
            f"{t['resolved_predictions']} "
            f"({t['wins']}W / {t['losses']}L, win rate {wr})",
        )
        + _kv_row("AI cost", f"${t['ai_cost']:.2f}")
        + _kv_row(
            "Autonomous changes made",
            f"{t['tuning_changes']} tuning adjustments, "
            f"{t['deprecated_strategies']} strategies deprecated, "
            f"{t['restored_strategies']} restored, "
            f"{t['auto_strategy_transitions']} auto-strategy transitions, "
            f"{t['crisis_transitions']} crisis-state transitions",
        )
    )

    # --- Per-profile table ---
    rows = []
    for p in profiles:
        pwr = (
            f"{p['win_rate']*100:.1f}%"
            if p["win_rate"] is not None else "—"
        )
        rows.append([
            p["name"],
            f"{p['buys']}/{p['sells']}",
            f"{p['resolved_predictions']} ({pwr})",
            _color_pnl(p["realized_pnl"]),
            f"${p['ai_cost']:.2f}",
        ])
    profile_table = _table(
        ["Profile", "B/S", "Resolved (WR)", "Realized P&amp;L", "AI cost"],
        rows,
    )

    # --- Self-tuning changes ---
    tuning_html = _render_tuning_changes(profiles)

    # --- Alpha decay changes ---
    decay_html = _render_decay_changes(profiles)

    # --- Auto-strategy lifecycle ---
    auto_html = _render_auto_strategies(profiles)

    # --- Crisis transitions ---
    crisis_html = _render_crisis(profiles)

    # --- Top/bottom trades with AI reasoning ---
    trades_html = _render_top_bottom_trades(profiles)

    autonomy_html = _render_autonomy_summary(summary)

    body = (
        _section("This Week at a Glance", headline)
        + _section("Autonomy Activity This Week", autonomy_html)
        + _section("Per-Profile Summary", profile_table)
        + _section("Self-Tuning Changes", tuning_html)
        + _section("Strategy Deprecations & Restorations", decay_html)
        + _section("Auto-Strategy Lifecycle", auto_html)
        + _section("Crisis-State Transitions", crisis_html)
        + _section("Trading Narrative — Top & Bottom Trades", trades_html)
    )

    html = _wrap_html(
        f"Weekly Digest &bull; {summary['start_label']} → "
        f"{summary['end_label']}",
        body,
    )
    return subject, html


def _render_tuning_changes(profiles: List[Dict[str, Any]]) -> str:
    from display_names import display_name, format_param_value
    rows = []
    for p in profiles:
        for t in p["tuning_changes"]:
            pname = t.get("parameter_name", "")
            old_raw = t.get("old_value")
            new_raw = t.get("new_value")
            old = format_param_value(pname, old_raw) if old_raw not in (None, "") else "—"
            new = format_param_value(pname, new_raw) if new_raw not in (None, "") else "—"
            outcome = t.get("outcome_after") or "pending"
            outcome_badge = _outcome_badge(outcome)
            wr_after = t.get("win_rate_after")
            wr_str = (
                f" (WR after: {wr_after*100:.1f}%)"
                if wr_after is not None else ""
            )
            rows.append([
                p["name"],
                display_name(pname),
                f"{old} → {new}",
                (t.get("reason") or "")[:140],
                outcome_badge + wr_str,
            ])
    if not rows:
        return (
            '<div style="color:#8a8a9a;font-style:italic">'
            "No self-tuning changes this week. The tuner reviews daily; "
            "when it finds no adjustment worth making, it logs "
            "\"evaluated, no changes needed.\""
            "</div>"
        )
    from notifications import _table
    return _table(
        ["Profile", "Parameter", "Old → New", "Why", "Effect"],
        rows,
    )


def _render_decay_changes(profiles: List[Dict[str, Any]]) -> str:
    from display_names import display_name
    rows = []
    for p in profiles:
        for d in p["deprecated_strategies"]:
            rs = d.get("rolling_sharpe_at_deprecation")
            lt = d.get("lifetime_sharpe")
            sharpe_str = (
                f"rolling {rs:.2f} vs lifetime {lt:.2f}"
                if rs is not None and lt is not None else "—"
            )
            rows.append([
                p["name"],
                "DEPRECATED",
                display_name(d.get("strategy_type", "")),
                sharpe_str,
                (d.get("reason") or "")[:100],
            ])
        for r in p["restored_strategies"]:
            rows.append([
                p["name"],
                "RESTORED",
                display_name(r.get("strategy_type", "")),
                "—",
                "Rolling Sharpe recovered for 14+ days",
            ])
    if not rows:
        return (
            '<div style="color:#8a8a9a;font-style:italic">'
            "No strategy deprecations or restorations this week."
            "</div>"
        )
    from notifications import _table
    return _table(
        ["Profile", "Action", "Strategy", "Sharpe", "Detail"],
        rows,
    )


def _render_auto_strategies(profiles: List[Dict[str, Any]]) -> str:
    rows = []
    for p in profiles:
        for a in p["auto_strategy_transitions"]:
            rows.append([
                p["name"],
                a.get("name", ""),
                a.get("status", ""),
                (a.get("retirement_reason") or "")[:80],
            ])
    if not rows:
        return (
            '<div style="color:#8a8a9a;font-style:italic">'
            "No auto-strategy transitions this week. The weekly proposer "
            "runs Sundays; daily lifecycle task promotes/retires based on "
            "shadow track record."
            "</div>"
        )
    from notifications import _table
    return _table(
        ["Profile", "Strategy", "Current Status", "Retirement Reason"],
        rows,
    )


def _render_crisis(profiles: List[Dict[str, Any]]) -> str:
    rows = []
    for p in profiles:
        for c in p["crisis_transitions"]:
            rows.append([
                p["name"],
                c.get("transitioned_at", "")[:16],
                f"{c.get('from_level', '—')} → {c.get('to_level', '—')}",
                f"size × {c.get('size_multiplier', 1.0)}",
            ])
    if not rows:
        return (
            '<div style="color:#8a8a9a;font-style:italic">'
            "No crisis-state transitions this week. (VIX stayed below "
            "elevated thresholds, cross-asset correlation within norm.)"
            "</div>"
        )
    from notifications import _table
    return _table(
        ["Profile", "When", "Transition", "Size multiplier"],
        rows,
    )


def _render_autonomy_summary(summary: Dict[str, Any]) -> str:
    """High-level summary of the week's autonomous activity:
      - count of changes by category
      - currently-active overrides across all profiles
      - cost guard status (today's spend / ceiling, ceiling source)
      - post-mortem patterns extracted this week
    """
    from notifications import _table

    profiles = summary.get("profiles", [])
    user_id = None
    for p in profiles:
        if "user_id" in p and p["user_id"]:
            user_id = p["user_id"]
            break
    # Profile dicts in summary are stats — fall back to looking up
    # user_id from master DB on the first profile.
    if user_id is None:
        try:
            conn = sqlite3.connect("quantopsai.db")
            row = conn.execute(
                "SELECT user_id FROM trading_profiles WHERE enabled=1 LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                user_id = row[0]
        except Exception:
            pass

    # ─── Counts of autonomous changes this week ───
    totals = summary.get("totals", {})
    rows_kv = []
    rows_kv.append(["Parameter tunings applied",
                     str(totals.get("tuning_changes", 0))])
    rows_kv.append(["Strategies deprecated",
                     str(totals.get("deprecated_strategies", 0))])
    rows_kv.append(["Strategies restored",
                     str(totals.get("restored_strategies", 0))])
    rows_kv.append(["Auto-strategy lifecycle transitions",
                     str(totals.get("auto_strategy_transitions", 0))])
    rows_kv.append(["Crisis-state transitions",
                     str(totals.get("crisis_transitions", 0))])

    # ─── Currently-active overrides (snapshot, not historical) ───
    active_signal_weights = 0
    active_regime_overrides = 0
    active_tod_overrides = 0
    active_symbol_overrides = 0
    profiles_with_capital_scale = 0
    try:
        conn = sqlite3.connect("quantopsai.db")
        prof_rows = conn.execute(
            "SELECT id, signal_weights, regime_overrides, tod_overrides, "
            " symbol_overrides, capital_scale "
            "FROM trading_profiles WHERE enabled=1"
        ).fetchall()
        conn.close()
        from signal_weights import get_all_weights
        from regime_overrides import get_all_overrides as get_regime
        from tod_overrides import get_all_overrides as get_tod
        from symbol_overrides import get_all_overrides as get_sym
        for r in prof_rows:
            pdict = {
                "signal_weights": r[1],
                "regime_overrides": r[2],
                "tod_overrides": r[3],
                "symbol_overrides": r[4],
                "capital_scale": r[5],
            }
            active_signal_weights += len(get_all_weights(pdict))
            active_regime_overrides += sum(
                len(v) for v in get_regime(pdict).values())
            active_tod_overrides += sum(
                len(v) for v in get_tod(pdict).values())
            active_symbol_overrides += sum(
                len(v) for v in get_sym(pdict).values())
            if float(r[5] or 1.0) != 1.0:
                profiles_with_capital_scale += 1
    except Exception as exc:
        logger.debug("autonomy summary overrides snapshot failed: %s", exc)

    rows_kv.append(["—", "—"])  # separator-ish
    rows_kv.append(["Active signal weights (across profiles)",
                     str(active_signal_weights)])
    rows_kv.append(["Active per-regime overrides",
                     str(active_regime_overrides)])
    rows_kv.append(["Active per-time-of-day overrides",
                     str(active_tod_overrides)])
    rows_kv.append(["Active per-symbol overrides",
                     str(active_symbol_overrides)])
    rows_kv.append(["Profiles with non-default capital scale",
                     str(profiles_with_capital_scale)])

    # ─── Post-mortem patterns extracted this week ───
    pm_extracted = 0
    pm_pattern_examples = []
    try:
        import os
        for p in profiles:
            pid = p.get("profile_id") or p.get("id")
            if not pid:
                continue
            db_path = f"quantopsai_profile_{pid}.db"
            if not os.path.exists(db_path):
                continue
            conn = sqlite3.connect(db_path)
            tbl = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='learned_patterns'"
            ).fetchone()
            if not tbl:
                conn.close()
                continue
            rows = conn.execute(
                "SELECT pattern_text FROM learned_patterns "
                "WHERE datetime(created_at) >= datetime('now', '-7 days') "
                "ORDER BY created_at DESC LIMIT 3"
            ).fetchall()
            conn.close()
            pm_extracted += len(rows)
            for r in rows[:1]:  # one example per profile, max
                pm_pattern_examples.append(
                    (p.get("name", f"Profile {pid}"), r[0]))
    except Exception as exc:
        logger.debug("autonomy summary post-mortem fetch failed: %s", exc)

    rows_kv.append(["—", "—"])
    rows_kv.append(["Losing-week post-mortems extracted",
                     str(pm_extracted)])

    # ─── Cost guard status ───
    cost_status = None
    if user_id:
        try:
            from cost_guard import status as _cost_status
            cost_status = _cost_status(user_id)
        except Exception as exc:
            logger.debug("autonomy summary cost-guard fetch failed: %s", exc)

    if cost_status:
        rows_kv.append(["—", "—"])
        rows_kv.append([
            "Today's API spend",
            f"${cost_status['today_usd']:.2f}",
        ])
        rows_kv.append([
            f"Daily ceiling ({cost_status.get('ceiling_source', 'auto')}-set)",
            f"${cost_status['ceiling_usd']:.2f}",
        ])
        rows_kv.append([
            "7-day average",
            f"${cost_status['trailing_7d_avg_usd']:.2f}/day",
        ])

    main = _table(["Metric", "Value"], rows_kv)

    # Pattern examples in their own block below the kv table
    extras = ""
    if pm_pattern_examples:
        extras += (
            '<div style="margin-top:0.75rem;padding:0.5rem 0.75rem;'
            'background:#f3e5f5;border-left:3px solid #9c27b0;'
            'border-radius:0 4px 4px 0;font-size:0.85rem;">'
            '<strong>This week\'s post-mortem learnings '
            '(now in the AI prompt):</strong><ul style="margin:0.25rem 0 0 1.25rem;padding:0;">'
        )
        for prof_name, pattern in pm_pattern_examples:
            extras += (
                f'<li><strong>{_escape(prof_name)}:</strong> '
                f'{_escape(pattern)}</li>'
            )
        extras += '</ul></div>'

    return main + extras


def _render_top_bottom_trades(profiles: List[Dict[str, Any]]) -> str:
    # Flatten all trades across profiles, sort by |pnl|
    all_trades = []
    for p in profiles:
        for t in p["trades"]:
            pnl = t.get("pnl")
            if pnl is None:
                continue
            all_trades.append({**t, "profile": p["name"]})
    if not all_trades:
        return (
            '<div style="color:#8a8a9a;font-style:italic">'
            "No closed trades with P&amp;L this week."
            "</div>"
        )

    winners = sorted(
        [t for t in all_trades if (t.get("pnl") or 0) > 0],
        key=lambda t: t["pnl"], reverse=True,
    )[:5]
    losers = sorted(
        [t for t in all_trades if (t.get("pnl") or 0) < 0],
        key=lambda t: t["pnl"],
    )[:3]

    parts = []
    if winners:
        parts.append('<div style="font-weight:bold;color:#1b7a3a;margin:8px 0 4px">TOP 5 WINNERS</div>')
        parts.append(_render_trade_list(winners))
    if losers:
        parts.append('<div style="font-weight:bold;color:#a82020;margin:12px 0 4px">BOTTOM 3 LOSERS</div>')
        parts.append(_render_trade_list(losers))
    return "".join(parts)


def _render_trade_list(trades: List[Dict[str, Any]]) -> str:
    from notifications import _color_pnl
    out = []
    for t in trades:
        symbol = t.get("symbol", "")
        side = (t.get("side") or "").upper()
        price = t.get("price")
        price_str = f"${price:.2f}" if price is not None else "—"
        qty = t.get("qty") or 0
        pnl_html = _color_pnl(t.get("pnl") or 0)
        ai_conf = t.get("ai_confidence")
        conf_str = f" (conf {ai_conf:.0f}%)" if ai_conf else ""
        reasoning = (t.get("ai_reasoning") or t.get("reason") or "").strip()
        reasoning = reasoning[:300]
        profile = t.get("profile", "")
        out.append(
            f'<div style="padding:6px 10px;margin:4px 0;background:#f7f7fa;border-left:3px solid #4a4a8a">'
            f'<div><strong>[{profile}]</strong> {pnl_html} &nbsp; {side} {qty} {symbol} @ {price_str}{conf_str}</div>'
            f'<div style="color:#555;font-size:12px;margin-top:3px;font-style:italic">'
            f'{_escape(reasoning) or "(no AI reasoning recorded)"}</div>'
            f'</div>'
        )
    return "".join(out)


def _outcome_badge(outcome: str) -> str:
    color = {
        "improved": "#1b7a3a",
        "worsened": "#a82020",
        "neutral": "#888",
        "pending": "#888",
    }.get(outcome, "#888")
    return (
        f'<span style="background:{color};color:#fff;padding:2px 6px;'
        f'border-radius:3px;font-size:11px;font-weight:bold">'
        f'{outcome.upper()}</span>'
    )


def _escape(s: str) -> str:
    """Minimal HTML escape for user-visible strings."""
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )
