"""Rich HTML email notifications for trade events, vetoes, exits, and daily summaries."""

import json
import logging
import sqlite3
import threading
import urllib.request
import urllib.error
from contextlib import closing
from datetime import date, datetime
from typing import Dict

import config
from client import get_account_info, get_positions
from journal import get_trade_history, get_performance_summary
from ai_tracker import get_ai_performance
from portfolio_manager import get_risk_summary

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared HTML helpers
# ---------------------------------------------------------------------------

_HEADER_BG = "#1a1a2e"
_ACCENT = "#16213e"
_GREEN = "#00c853"
_RED = "#ff1744"
_MUTED = "#8a8a9a"
_BODY_BG = "#f4f4f8"
_CARD_BG = "#ffffff"


def _color_pnl(value):
    """Return an inline-styled span coloring *value* green or red."""
    if value is None:
        return '<span style="color:#8a8a9a">--</span>'
    color = _GREEN if value >= 0 else _RED
    sign = "+" if value > 0 else ""
    return f'<span style="color:{color};font-weight:bold">{sign}{value:,.2f}</span>'


def _color_pct(value, invert=False):
    """Return an inline-styled span coloring a percentage green or red.

    When invert=True, NEGATIVE values are green and positive are red — used
    for metrics where a negative outcome is the desired direction (e.g.
    'Avg Move on SELL predictions': price going DOWN is the AI being right).
    """
    if value is None:
        return '<span style="color:#8a8a9a">--</span>'
    is_good = value <= 0 if invert else value >= 0
    color = _GREEN if is_good else _RED
    sign = "+" if value > 0 else ""
    return f'<span style="color:{color};font-weight:bold">{sign}{value:.2f}%</span>'


def _wrap_html(title, body_content):
    """Wrap *body_content* in a full HTML email template."""
    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{_BODY_BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;color:#222;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{_BODY_BG}">
<tr><td align="center" style="padding:20px 10px">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%">
  <!-- Header -->
  <tr><td style="background:{_HEADER_BG};padding:20px 24px;border-radius:8px 8px 0 0">
    <span style="color:#fff;font-size:22px;font-weight:bold;letter-spacing:1px">QUANTOPSAI</span>
    <span style="color:{_MUTED};font-size:13px;margin-left:12px">{title}</span>
  </td></tr>
  <!-- Body -->
  <tr><td style="background:{_CARD_BG};padding:24px;border-radius:0 0 8px 8px;border:1px solid #e0e0e0;border-top:none">
    {body_content}
  </td></tr>
  <!-- Footer -->
  <tr><td style="padding:12px 24px;text-align:center">
    <span style="color:{_MUTED};font-size:11px">QuantOpsAI Automated Trading System &bull; {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</span>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def _section(heading, content):
    """Return an HTML section block with a heading."""
    return f"""\
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:18px">
  <tr><td style="padding-bottom:6px;border-bottom:2px solid {_ACCENT}">
    <span style="font-size:15px;font-weight:bold;color:{_ACCENT}">{heading}</span>
  </td></tr>
  <tr><td style="padding-top:10px">{content}</td></tr>
</table>"""


def _kv_row(label, value):
    """Return a label: value row for use inside a section."""
    return f'<div style="padding:3px 0"><span style="color:{_MUTED};font-size:12px">{label}:</span> <strong>{value}</strong></div>'


def _table(headers, rows):
    """Build an HTML table from a list of header strings and list-of-lists rows."""
    hdr = "".join(
        f'<th style="text-align:left;padding:6px 10px;background:{_ACCENT};color:#fff;font-size:12px">{h}</th>'
        for h in headers
    )
    body = ""
    for i, row in enumerate(rows):
        bg = "#f9f9fc" if i % 2 == 0 else _CARD_BG
        cells = "".join(
            f'<td style="padding:6px 10px;font-size:13px;border-bottom:1px solid #eee;background:{bg}">{c}</td>'
            for c in row
        )
        body += f"<tr>{cells}</tr>"
    return f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">' \
           f"<tr>{hdr}</tr>{body}</table>"


# ---------------------------------------------------------------------------
# 1. Low-level email sender
# ---------------------------------------------------------------------------

def _sanitize_subject(subject: str, max_len: int = 200) -> str:
    """Resend rejects subjects with newline characters (HTTP 422).
    Convert every whitespace control char to a space, strip other
    control chars, collapse runs of whitespace, and truncate.
    Defense-in-depth — any caller that accidentally includes a
    multi-line block (e.g. the watchdog passing a context paragraph)
    used to silently fail. Now the email lands with a single-line
    subject regardless."""
    if not subject:
        return "QuantOpsAI"
    # Convert newlines / carriage returns / tabs to spaces first so
    # split-and-rejoin can collapse them (rather than concatenating
    # the surrounding words together).
    out = []
    for ch in subject:
        if ch in ("\n", "\r", "\t"):
            out.append(" ")
        elif ch == " " or ch.isprintable():
            out.append(ch)
        # else: drop other control chars
    cleaned = " ".join("".join(out).split())  # collapse runs of whitespace
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len - 1] + "…"
    return cleaned or "QuantOpsAI"


_EMAIL_DEDUP_WINDOW_SECONDS = 3600   # 1 hour — same subject only once
_EMAIL_DEDUP: Dict[str, float] = {}
_EMAIL_DEDUP_LOCK = threading.Lock()


def _is_duplicate_within_window(subject: str) -> bool:
    """Return True when this subject was already sent within the
    dedup window. Process-local cache only — survives within a
    scheduler run but not across restarts. That's intentional: a
    fresh process should be allowed to send its first error email.
    The point is to stop tight-loop spam (599 identical errors in
    24h, 2026-05-04 incident) — not to enforce any global ceiling."""
    import time as _time
    now = _time.time()
    with _EMAIL_DEDUP_LOCK:
        last = _EMAIL_DEDUP.get(subject)
        if last is not None and (now - last) < _EMAIL_DEDUP_WINDOW_SECONDS:
            return True
        _EMAIL_DEDUP[subject] = now
        # Periodic eviction — keep map small in long-running processes.
        if len(_EMAIL_DEDUP) > 200:
            cutoff = now - _EMAIL_DEDUP_WINDOW_SECONDS
            for k in [k for k, v in _EMAIL_DEDUP.items() if v < cutoff]:
                _EMAIL_DEDUP.pop(k, None)
    return False


def send_email(subject, html_body, ctx=None):
    """Send an HTML email via Resend API.

    Parameters
    ----------
    ctx : UserContext, optional
        If provided, uses ctx.resend_api_key and ctx.notification_email
        instead of config globals.

    Returns True on success, False on failure.  Never raises.

    Deduplication: identical subjects are silently dropped within a
    1-hour rolling window per process. Stops crash-loop email spam.
    """
    subject = _sanitize_subject(subject)
    if _is_duplicate_within_window(subject):
        logger.debug("Email deduped (subject sent within last hour): %s", subject)
        return True
    if ctx is not None:
        api_key = ctx.resend_api_key
        recipient = ctx.notification_email
    else:
        api_key = config.RESEND_API_KEY
        recipient = config.NOTIFICATION_EMAIL

    if not api_key:
        logger.warning("RESEND_API_KEY not configured — skipping email notification.")
        return False

    payload = json.dumps({
        "from": "QuantOpsAI <onboarding@resend.dev>",
        "to": [recipient],
        "subject": subject,
        "html": html_body,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "QuantOpsAI/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        logger.info("Email sent: %s", subject)
        return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error("Failed to send email '%s': HTTP %s — %s", subject, exc.code, body)
        return False
    except Exception as exc:
        logger.error("Failed to send email '%s': %s", subject, exc)
        return False


# ---------------------------------------------------------------------------
# 2. Trade notification
# ---------------------------------------------------------------------------

def notify_trade(trade_result, signal=None, ai_result=None, ctx=None):
    """Disabled — too many emails with 10 profiles. Trade activity is
    visible on the dashboard. Only EOD summary + self-tuning emails sent."""
    return


def _notify_trade_disabled(trade_result, signal=None, ai_result=None, ctx=None):
    """Original trade notification — kept for reference.

    All data can come from trade_result alone (signal and ai_result are
    optional overrides for backward compatibility).
    """
    signal = signal or trade_result
    ai_result = ai_result or trade_result

    symbol = trade_result.get("symbol", "???")
    action = trade_result.get("action", "NONE")
    qty = trade_result.get("qty", 0)
    price = trade_result.get("price") or signal.get("price", 0)
    estimated_cost = trade_result.get("estimated_cost") or trade_result.get("estimated_proceeds") or (qty * price)

    subject = f"QuantOpsAI: {action} {qty} {symbol} @ ${price:,.2f}"

    # -- Trade details -------------------------------------------------------
    cost_label = "Estimated Proceeds" if action == "SHORT" else "Estimated Cost"
    details = (
        _kv_row("Symbol", symbol)
        + _kv_row("Side", action)
        + _kv_row("Quantity", f"{qty:,}")
        + _kv_row("Price", f"${price:,.2f}")
        + _kv_row(cost_label, f"${estimated_cost:,.2f}")
        + _kv_row("Strategy", trade_result.get("strategy", "aggressive"))
        + _kv_row("Order ID", trade_result.get("order_id", "--"))
    )
    body = _section("Trade Details", details)

    # -- Signal info ---------------------------------------------------------
    score = trade_result.get("score") or signal.get("score", "--")
    sig_info = (
        _kv_row("Signal", trade_result.get("signal") or signal.get("signal", "--"))
        + _kv_row("Score", score)
        + _kv_row("Reason", trade_result.get("reason") or signal.get("reason", "--"))
    )
    body += _section("Strategy Signal", sig_info)

    # -- AI analysis ---------------------------------------------------------
    ai_signal = trade_result.get("ai_signal") or ai_result.get("signal")
    ai_conf = trade_result.get("ai_confidence") or ai_result.get("confidence")
    ai_reasoning = trade_result.get("ai_reasoning") or ai_result.get("reasoning")
    risk_factors = trade_result.get("ai_risk_factors") or ai_result.get("risk_factors", [])

    if ai_signal:
        risk_str = ", ".join(risk_factors) if risk_factors else "None listed"
        ai_info = (
            _kv_row("AI Signal", ai_signal)
            + _kv_row("AI Confidence", f"{ai_conf}%")
            + _kv_row("Reasoning", ai_reasoning or "--")
            + _kv_row("Risk Factors", risk_str)
        )
        body += _section("AI Analysis", ai_info)

    # -- Account snapshot ----------------------------------------------------
    try:
        account = get_account_info(ctx=ctx)
        acct_info = (
            _kv_row("Equity", f"${account['equity']:,.2f}")
            + _kv_row("Cash", f"${account['cash']:,.2f}")
            + _kv_row("Buying Power", f"${account['buying_power']:,.2f}")
        )
        body += _section("Account Snapshot", acct_info)
    except Exception as exc:
        logger.warning("Could not fetch account for notification: %s", exc)

    # -- Positions table -----------------------------------------------------
    try:
        positions = get_positions(ctx=ctx)
        if positions:
            rows = []
            for p in positions:
                rows.append([
                    p["symbol"],
                    f"{int(p['qty'])}",
                    f"${p['current_price']:,.2f}",
                    f"${p['market_value']:,.2f}",
                    _color_pnl(p["unrealized_pl"]),
                    _color_pct(p["unrealized_plpc"] * 100),
                ])
            body += _section("Current Positions",
                             _table(["Symbol", "Qty", "Price", "Mkt Value", "P&L", "%"], rows))
    except Exception as exc:
        logger.warning("Could not fetch positions for notification: %s", exc)

    # -- Stop-loss / take-profit levels --------------------------------------
    sl_pct = trade_result.get("stop_loss_pct", signal.get("stop_loss_pct"))
    tp_pct = trade_result.get("take_profit_pct", signal.get("take_profit_pct"))
    if sl_pct is not None or tp_pct is not None:
        sl_price = price * (1 - sl_pct) if sl_pct else None
        tp_price = price * (1 + tp_pct) if tp_pct else None
        levels = ""
        if sl_pct is not None:
            levels += _kv_row("Stop-Loss", f"{sl_pct:.0%} &rarr; ${sl_price:,.2f}")
        if tp_pct is not None:
            levels += _kv_row("Take-Profit", f"{tp_pct:.0%} &rarr; ${tp_price:,.2f}")
        body += _section("Risk Levels", levels)

    html = _wrap_html("Trade Executed", body)
    return send_email(subject, html, ctx=ctx)


# ---------------------------------------------------------------------------
# 3. AI veto notification
# ---------------------------------------------------------------------------

def notify_veto(symbol, technical_signal, ai_result, ctx=None):
    """Disabled — veto activity visible on dashboard."""
    return


def _notify_veto_disabled(symbol, technical_signal, ai_result, ctx=None):
    """Original veto notification — kept for reference.

    Args:
        symbol: Ticker string.
        technical_signal: The strategy signal dict.
        ai_result: AI analysis dict.
        ctx: UserContext, optional.
    """
    tech_action = technical_signal.get("signal", "???")
    ai_signal = ai_result.get("signal", "???")
    ai_conf = ai_result.get("confidence", "?")

    subject = f"QuantOpsAI: AI Vetoed {tech_action} {symbol}"

    tech_info = (
        _kv_row("Symbol", symbol)
        + _kv_row("Technical Signal", tech_action)
        + _kv_row("Score", technical_signal.get("score", "--"))
        + _kv_row("Reason", technical_signal.get("reason", "--"))
    )
    body = _section("Technical Analysis Said", tech_info)

    ai_info = (
        _kv_row("AI Signal", ai_signal)
        + _kv_row("Confidence", f"{ai_conf}%")
        + _kv_row("Reasoning", ai_result.get("reasoning", "--"))
    )
    risk_factors = ai_result.get("risk_factors", [])
    if risk_factors:
        ai_info += _kv_row("Risk Factors", ", ".join(risk_factors))
    body += _section("AI Analysis Said", ai_info)

    verdict = (
        f'<div style="padding:12px;background:#fff3e0;border-left:4px solid #ff9800;margin-top:8px;font-size:13px">'
        f"<strong>Trade was NOT executed.</strong> The AI overrode the technical signal."
        f"</div>"
    )
    body += verdict

    html = _wrap_html("AI Veto", body)
    return send_email(subject, html, ctx=ctx)


# ---------------------------------------------------------------------------
# 4. Exit notification (stop-loss / take-profit)
# ---------------------------------------------------------------------------

def notify_exit(symbol, trigger, qty, reason, ctx=None):
    """Disabled — exit activity visible on dashboard."""
    return


def _notify_exit_disabled(symbol, trigger, qty, reason, ctx=None):
    """Original exit notification — kept for reference.

    Args:
        symbol: Ticker string.
        trigger: 'stop_loss' or 'take_profit'.
        qty: Number of shares sold.
        reason: Descriptive reason string (e.g. from portfolio_manager).
        ctx: UserContext, optional.
    """
    # Derive db_path from ctx for downstream calls
    db_path = ctx.db_path if ctx is not None else None

    label = "Stop-Loss" if trigger == "stop_loss" else "Take-Profit"
    # Try to extract the percentage from the reason string
    pct_str = ""
    if "%" in reason:
        try:
            import re
            match = re.search(r'([+-]?\d+\.?\d*)%', reason)
            if match:
                pct_str = f" ({match.group(0)})"
        except (TypeError, AttributeError) as _re_exc:
            # Regex pct extract for subject line; subject still
            # rendered without pct on failure. Surface for follow-up.
            logger.debug(
                "notify subject pct extract failed: %s: %s",
                type(_re_exc).__name__, _re_exc,
            )

    subject = f"QuantOpsAI: {label} {symbol}{pct_str}"

    exit_info = (
        _kv_row("Symbol", symbol)
        + _kv_row("Trigger", label)
        + _kv_row("Quantity Sold", f"{qty:,}")
        + _kv_row("Reason", reason)
    )
    body = _section("Exit Details", exit_info)

    # -- P&L from the trade --------------------------------------------------
    try:
        trades = get_trade_history(symbol=symbol, limit=5, db_path=db_path)
        recent_sells = [t for t in trades if t.get("side") == "sell" and t.get("pnl") is not None]
        if recent_sells:
            last = recent_sells[0]
            pnl_info = _kv_row("Realized P&L", _color_pnl(last["pnl"]))
            body += _section("P&L", pnl_info)
    except Exception as exc:
        logger.warning("Could not fetch trade P&L for exit notification: %s", exc)

    # -- Remaining positions --------------------------------------------------
    try:
        positions = get_positions(ctx=ctx)
        if positions:
            rows = []
            for p in positions:
                rows.append([
                    p["symbol"],
                    f"{int(p['qty'])}",
                    f"${p['current_price']:,.2f}",
                    _color_pnl(p["unrealized_pl"]),
                    _color_pct(p["unrealized_plpc"] * 100),
                ])
            body += _section("Remaining Positions",
                             _table(["Symbol", "Qty", "Price", "P&L", "%"], rows))
        else:
            body += _section("Remaining Positions",
                             '<span style="color:#8a8a9a">No open positions.</span>')
    except Exception as exc:
        logger.warning("Could not fetch positions for exit notification: %s", exc)

    html = _wrap_html(f"{label} Triggered", body)
    return send_email(subject, html, ctx=ctx)


# ---------------------------------------------------------------------------
# 5. Daily summary
# ---------------------------------------------------------------------------

def notify_daily_summary(ctx=None):
    """Send a comprehensive end-of-day summary email."""
    # Derive db_path from ctx for downstream calls
    db_path = ctx.db_path if ctx is not None else None

    from zoneinfo import ZoneInfo
    today_str = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    subject = f"QuantOpsAI Daily Summary \u2014 {today_str}"

    body = ""

    # -- Account overview ----------------------------------------------------
    try:
        account = get_account_info(ctx=ctx)
        positions = get_positions(ctx=ctx)
        total_unrealized = sum(p.get("unrealized_pl", 0) for p in positions)

        acct_info = (
            _kv_row("Equity", f"${account['equity']:,.2f}")
            + _kv_row("Cash", f"${account['cash']:,.2f}")
            + _kv_row("Buying Power", f"${account['buying_power']:,.2f}")
            + _kv_row("Unrealized P&L", _color_pnl(total_unrealized))
        )
        body += _section("Account Overview", acct_info)
    except Exception as exc:
        logger.warning("Could not fetch account for daily summary: %s", exc)
        account = {}
        positions = []

    # -- Positions with P&L --------------------------------------------------
    if positions:
        rows = []
        for p in positions:
            rows.append([
                p["symbol"],
                f"{int(p['qty'])}",
                f"${p['avg_entry_price']:,.2f}",
                f"${p['current_price']:,.2f}",
                f"${p['market_value']:,.2f}",
                _color_pnl(p["unrealized_pl"]),
                _color_pct(p["unrealized_plpc"] * 100),
            ])
        body += _section("Open Positions",
                         _table(["Symbol", "Qty", "Entry", "Price", "Mkt Value", "P&L", "%"], rows))
    else:
        body += _section("Open Positions",
                         '<span style="color:#8a8a9a">No open positions.</span>')

    # -- Trades executed today -----------------------------------------------
    try:
        all_trades = get_trade_history(limit=200, db_path=db_path)
        today_trades = [t for t in all_trades if t.get("timestamp", "").startswith(today_str)]
        if today_trades:
            rows = []
            for t in today_trades:
                pnl_cell = _color_pnl(t["pnl"]) if t.get("pnl") is not None else "--"
                rows.append([
                    t.get("timestamp", "")[-8:],
                    t["symbol"],
                    t["side"].upper(),
                    f"{int(t['qty'])}",
                    f"${t['price']:,.2f}" if t.get("price") else "--",
                    pnl_cell,
                ])
            body += _section("Trades Today",
                             _table(["Time", "Symbol", "Side", "Qty", "Price", "P&L"], rows))
        else:
            body += _section("Trades Today",
                             '<span style="color:#8a8a9a">No trades executed today.</span>')
    except Exception as exc:
        logger.warning("Could not fetch trades for daily summary: %s", exc)

    # -- AI vetoes today -----------------------------------------------------
    try:
        from journal import _get_conn
        with closing(_get_conn(db_path)) as conn:
            vetoes = conn.execute(
                "SELECT * FROM signals WHERE acted_on = 0 AND timestamp LIKE ? "
                "AND signal IN ('BUY','STRONG_BUY','SELL','STRONG_SELL') "
                "ORDER BY timestamp DESC",
                (f"{today_str}%",),
            ).fetchall()
        if vetoes:
            rows = []
            for v in vetoes:
                rows.append([
                    dict(v).get("symbol", ""),
                    dict(v).get("signal", ""),
                    dict(v).get("reason", "")[:80],
                ])
            body += _section("AI Vetoes Today",
                             _table(["Symbol", "Signal", "Reason"], rows))
    except Exception as exc:
        logger.warning("Could not fetch vetoes for daily summary: %s", exc)

    # -- AI performance summary ----------------------------------------------
    try:
        ai_perf = get_ai_performance(db_path=db_path)
        if ai_perf.get("total_predictions", 0) > 0:
            ai_info = (
                _kv_row("Total Predictions", ai_perf["total_predictions"])
                + _kv_row("Resolved", ai_perf["resolved"])
                + _kv_row("Pending", ai_perf["pending"])
                + _kv_row("Win Rate", f"{ai_perf['win_rate']:.1f}%")
                + _kv_row("Profit Factor", ai_perf["profit_factor"])
                + _kv_row("Avg Move on BUYs", _color_pct(ai_perf["avg_return_on_buys"]))
                + _kv_row("Avg Move on SELLs", _color_pct(ai_perf["avg_return_on_sells"], invert=True))
            )
            body += _section("AI Performance", ai_info)
    except Exception as exc:
        logger.warning("Could not fetch AI performance for daily summary: %s", exc)

    # -- Risk summary --------------------------------------------------------
    if account and positions:
        try:
            # Pull risk params from ctx if available
            risk_kwargs = {}
            if ctx is not None:
                risk_kwargs["max_total_positions"] = ctx.max_total_positions
                risk_kwargs["max_position_pct"] = ctx.max_position_pct
            risk = get_risk_summary(account, positions, **risk_kwargs)
            risk_info = (
                _kv_row("Positions", f"{risk['num_positions']} / {risk['max_positions']}")
                + _kv_row("Available Slots", risk["available_slots"])
                + _kv_row("Cash %", f"{risk['cash_pct']:.1f}%")
                + _kv_row("Invested %", f"{risk['invested_pct']:.1f}%")
                + _kv_row("Total Unrealized P&L", _color_pnl(risk["total_unrealized_pnl"]))
            )
            largest = risk.get("largest_position", {})
            if largest.get("symbol"):
                risk_info += _kv_row("Largest Position",
                                     f"{largest['symbol']} ({largest['weight']:.1f}%)")
            body += _section("Risk Summary", risk_info)
        except Exception as exc:
            logger.warning("Could not compute risk summary: %s", exc)

    # -- Trade performance (all-time) ----------------------------------------
    try:
        perf = get_performance_summary(db_path=db_path)
        if perf["total_trades"] > 0:
            perf_info = (
                _kv_row("Total Closed Trades", perf["total_trades"])
                + _kv_row("Win Rate", f"{perf['win_rate']:.1f}%")
                + _kv_row("Total P&L", _color_pnl(perf["total_pnl"]))
                + _kv_row("Avg P&L", _color_pnl(perf["avg_pnl"]))
                + _kv_row("Best Trade", _color_pnl(perf["best_trade"]))
                + _kv_row("Worst Trade", _color_pnl(perf["worst_trade"]))
            )
            body += _section("Trade Performance (All-Time)", perf_info)
    except Exception as exc:
        logger.warning("Could not fetch performance summary: %s", exc)

    html = _wrap_html("Daily Summary", body)
    return send_email(subject, html, ctx=ctx)


# ---------------------------------------------------------------------------
# 5b. Shadow model evaluation daily digest
# ---------------------------------------------------------------------------

_VERDICT_COLORS = {
    "primary": "#1976d2",       # blue
    "shadow": "#ff8f00",        # orange
    "tie": "#8a8a9a",
    "both_wrong": "#b00020",    # dark red
    "unknown": "#8a8a9a",
}


def _render_verdict_for_shadow_row(row, primary_sig, shadow_sig, db_path):
    """Look up the matching prediction's outcome and render an
    inline verdict block. Returns "" when no resolved match is found
    (very common for same-day rows).

    Email render path — one bad row must not break the whole digest.
    Wrapped at the call site so unexpected exceptions are logged with
    the row context, not silently swallowed."""
    from shadow_eval import verdict_for_disagreement, _try_parse_json

    primary_parsed = _try_parse_json(row.get("primary_response"))
    if not isinstance(primary_parsed, dict):
        return ""
    symbol = (primary_parsed.get("symbol")
              or primary_parsed.get("ticker"))
    if not symbol:
        return ""

    try:
        conn = sqlite3.connect(db_path)
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            OSError) as exc:
        logger.warning(
            "shadow verdict: prediction-DB connect failed (%s) "
            "for symbol=%s: %s: %s",
            db_path, symbol, type(exc).__name__, exc,
        )
        return ""
    try:
        conn.row_factory = sqlite3.Row
        try:
            pred = conn.execute(
                "SELECT actual_return_pct, days_held "
                "FROM ai_predictions "
                "WHERE symbol = ? "
                "AND status = 'resolved' "
                "AND actual_return_pct IS NOT NULL "
                "AND ABS(strftime('%s', timestamp) - strftime('%s', ?)) <= 300 "
                "ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) "
                "LIMIT 1",
                (symbol, row.get("timestamp"), row.get("timestamp")),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            logger.warning(
                "shadow verdict: prediction lookup failed for "
                "symbol=%s ts=%s: %s: %s",
                symbol, row.get("timestamp"),
                type(exc).__name__, exc,
            )
            return ""
    finally:
        conn.close()

    if not pred or pred["actual_return_pct"] is None:
        return ""

    verdict = verdict_for_disagreement(
        primary_sig, shadow_sig, pred["actual_return_pct"],
    )
    color = _VERDICT_COLORS.get(verdict["winner"], _MUTED)
    days_part = (f" over {int(pred['days_held'])}d"
                 if pred["days_held"] else "")
    return (
        f'<br><span style="color:{color};font-size:12px;'
        f'font-weight:bold">▸ {verdict["headline"]}{days_part}</span>'
        f' <span style="color:#666;font-size:12px">'
        f'&mdash; {verdict["reason"]}</span>'
    )


def _shadow_resolved_section(db_path):
    """Render the 'Recently Resolved Disagreements' section. Walks
    disagreement rows from the past 7 days whose outcomes are now
    known and tallies winner / reasoning per row.

    Returns "" when no resolved disagreements exist."""
    try:
        from shadow_eval import (
            fetch_recently_resolved_disagreements,
            verdict_for_disagreement, _try_parse_json,
        )
    except Exception:
        return ""

    rows = fetch_recently_resolved_disagreements(db_path, lookback_days=7)
    if not rows:
        return ""

    # Tally by shadow model: who's been winning the recent
    # disagreements?
    from collections import defaultdict
    tally = defaultdict(lambda: {
        "primary_wins": 0, "shadow_wins": 0, "tie": 0, "both_wrong": 0,
    })

    row_blocks = []
    for r in rows:
        primary_parsed = _try_parse_json(r.get("primary_response")) or {}
        primary_sig = (primary_parsed.get("signal")
                       or primary_parsed.get("action")
                       or primary_parsed.get("recommendation") or "—")
        shadow_sig = r.get("parsed_signal") or "—"
        ret = r.get("outcome_return_pct")
        verdict = verdict_for_disagreement(primary_sig, shadow_sig, ret)

        label = f"{r.get('provider', '?')}:{r.get('model', '?')}"
        if verdict["winner"] == "primary":
            tally[label]["primary_wins"] += 1
        elif verdict["winner"] == "shadow":
            tally[label]["shadow_wins"] += 1
        elif verdict["winner"] == "tie":
            tally[label]["tie"] += 1
        elif verdict["winner"] == "both_wrong":
            tally[label]["both_wrong"] += 1

        color = _VERDICT_COLORS.get(verdict["winner"], _MUTED)
        symbol = r.get("outcome_symbol") or "?"
        purpose = r.get("purpose") or "?"
        date_part = (r.get("timestamp") or "")[:10]
        row_blocks.append(
            f'<div style="padding:6px 0;border-bottom:1px solid #eee;font-size:13px">'
            f'<span style="color:{_MUTED};font-size:11px">{date_part}</span> '
            f'<strong>{symbol}</strong> '
            f'<span style="color:{_MUTED};font-size:11px">({purpose})</span> '
            f'&mdash; {label}: <strong>{shadow_sig}</strong> vs '
            f'primary <strong>{primary_sig}</strong>'
            f'<br><span style="color:{color};font-size:12px;font-weight:bold">'
            f'▸ {verdict["headline"]}</span> '
            f'<span style="color:#666;font-size:12px">&mdash; {verdict["reason"]}</span>'
            f'</div>'
        )

    # Tally summary table
    tally_rows = []
    for label, t in sorted(tally.items()):
        scored = t["primary_wins"] + t["shadow_wins"]
        scoreboard = (
            f"{t['primary_wins']} - {t['shadow_wins']}"
            if scored or t["tie"] or t["both_wrong"] else "—"
        )
        tally_rows.append([
            label, scoreboard,
            str(t["tie"]), str(t["both_wrong"]),
        ])

    block = ""
    if tally_rows:
        block += _section(
            "Disagreement Scoreboard (last 7d)",
            _table(
                ["Shadow Model", "Primary - Shadow",
                 "Ties", "Both Wrong"],
                tally_rows,
            ),
        )
    block += _section(
        "Recently Resolved Disagreements (last 7d)",
        "".join(row_blocks),
    )
    return block


def _shadow_disagreement_detail(rows, primary_label="primary", db_path=None):
    """Render an HTML block listing each disagreement, grouped by
    purpose, with field-level diff. `rows` is a list of ai_shadow_calls
    dicts as returned by shadow_eval.fetch_daily_rows.

    When `db_path` is provided, each row is also looked up against
    ai_predictions for an outcome verdict. Most rows in the "today"
    digest will not yet have a resolved outcome, so the verdict block
    only renders for the subset that do.
    """
    from collections import defaultdict
    grouped = defaultdict(list)
    for r in rows:
        # Surface only rows where the comparison was actually scored
        # AND disagreed. Rows with agreement=NULL had no recognisable
        # signal on either side — they go in the "Unscored" bucket.
        if r.get("agreement") == 0:
            grouped[r.get("purpose") or "uncategorized"].append(r)

    if not grouped:
        return '<span style="color:#8a8a9a">No disagreements logged today.</span>'

    parts = []
    for purpose, items in sorted(grouped.items()):
        rows_html = ""
        for r in items:
            shadow_label = f"{r.get('provider', '?')}:{r.get('model', '?')}"
            primary_sig = "—"
            shadow_sig = r.get("parsed_signal") or "—"
            try:
                pj = json.loads(r.get("primary_parsed") or "null")
                if isinstance(pj, dict):
                    primary_sig = (pj.get("signal") or pj.get("action")
                                   or pj.get("recommendation")
                                   or pj.get("direction") or "—")
            except (TypeError, ValueError) as exc:
                # Primary parsed-signal extraction for the digest row.
                # On a malformed primary_parsed payload the row still
                # renders with primary_sig = "—". Logged at debug so
                # the parse failure is discoverable in the journal.
                logger.debug(
                    "shadow eval: primary-parsed extraction failed for "
                    "row id=%s purpose=%s: %s: %s",
                    r.get("id"), purpose, type(exc).__name__, exc,
                )

            diff_bits = []
            try:
                pj_full = json.loads(r.get("primary_response") or "null")
                sj_full = json.loads(r.get("raw_response") or "null")
                if isinstance(pj_full, dict) and isinstance(sj_full, dict):
                    for field in ("confidence", "target_entry",
                                  "target_stop_loss", "target_take_profit"):
                        a = pj_full.get(field)
                        b = sj_full.get(field)
                        if a is None and b is None:
                            continue
                        if a == b:
                            continue
                        diff_bits.append(f"{field}: {a} vs {b}")
                    # Reasoning head — first 80 chars when different
                    ra = (pj_full.get("reasoning") or "")[:80]
                    rb = (sj_full.get("reasoning") or "")[:80]
                    if ra and rb and ra != rb:
                        diff_bits.append(
                            f"reasoning: {primary_label} said \"{ra}\"; "
                            f"shadow said \"{rb}\""
                        )
            except (TypeError, ValueError) as exc:
                # Optional field-level diff for a digest row.
                # If either response isn't valid JSON the row still
                # renders with the top-line signal — the diff is a
                # nice-to-have, not load-bearing. Logged so malformed
                # shadow responses are still discoverable in the
                # journal rather than swallowed.
                logger.debug(
                    "shadow eval: field-diff parse failed for purpose=%s "
                    "shadow=%s: %s: %s",
                    purpose, shadow_label,
                    type(exc).__name__, exc,
                )

            diff_text = "; ".join(diff_bits) if diff_bits else (
                "fields match except for the top-level signal"
            )

            # Verdict: only renders if the prediction has resolved
            # (rare for same-day rows — most resolve days later).
            # Per-row guard so an unexpected exception in one row's
            # verdict lookup doesn't take down the whole digest.
            verdict_html = ""
            if db_path:
                try:
                    verdict_html = _render_verdict_for_shadow_row(
                        r, primary_sig, shadow_sig, db_path,
                    )
                except Exception as exc:
                    logger.warning(
                        "shadow verdict render unexpected error for "
                        "row id=%s symbol=%s: %s: %s",
                        r.get("id"),
                        primary_sig, type(exc).__name__, exc,
                        exc_info=True,
                    )

            rows_html += (
                f'<div style="padding:6px 0;border-bottom:1px solid #eee;font-size:13px">'
                f'<strong>{shadow_label}</strong> &mdash; '
                f'{primary_label}: <strong>{primary_sig}</strong> / '
                f'shadow: <strong>{shadow_sig}</strong>'
                f'<br><span style="color:#666;font-size:12px">{diff_text}</span>'
                f'{verdict_html}'
                f'</div>'
            )
        parts.append(_section(
            f"Disagreements &mdash; {purpose}",
            rows_html,
        ))
    return "".join(parts)


def notify_shadow_eval_daily(ctx=None):
    """Send the shadow-eval daily digest. Separate from the main daily
    summary so the user can mute it independently.

    Skips sending entirely when no shadow_eval rows were logged today.
    """
    db_path = ctx.db_path if ctx is not None else None
    if not db_path:
        return False

    from zoneinfo import ZoneInfo
    today_str = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    try:
        from shadow_eval import fetch_daily_rows
        rows = fetch_daily_rows(db_path, today_str)
    except Exception as exc:
        logger.warning("shadow eval fetch failed: %s", exc)
        return False

    if not rows:
        logger.info("Shadow eval digest skipped — no rows for %s", today_str)
        return False

    profile_label = getattr(ctx, "profile_name", "") or ""
    subject_suffix = f" — {profile_label}" if profile_label else ""
    subject = (
        f"QuantOpsAI Shadow Eval — {today_str}{subject_suffix}"
    )

    # Per-model summary
    from collections import defaultdict
    per_model = defaultdict(lambda: {
        "calls": 0, "agree": 0, "disagree": 0, "unscored": 0,
        "errors": 0, "cost": 0.0, "latency_ms": 0,
    })
    primary_total = 0
    for r in rows:
        key = f"{r.get('provider', '?')}:{r.get('model', '?')}"
        agg = per_model[key]
        agg["calls"] += 1
        agg["cost"] += float(r.get("cost_usd") or 0.0)
        agg["latency_ms"] += int(r.get("latency_ms") or 0)
        if r.get("error"):
            agg["errors"] += 1
        elif r.get("agreement") == 1:
            agg["agree"] += 1
        elif r.get("agreement") == 0:
            agg["disagree"] += 1
        else:
            agg["unscored"] += 1
    primary_total = len(set(r.get("call_id") for r in rows
                            if r.get("call_id")))

    summary_rows = []
    for label, agg in sorted(per_model.items()):
        scored = agg["agree"] + agg["disagree"]
        agreement_pct = (
            f"{(agg['agree'] / scored * 100):.0f}%" if scored else "—"
        )
        avg_lat = (
            f"{agg['latency_ms'] // max(1, agg['calls'])} ms"
        )
        summary_rows.append([
            label,
            f"{agg['calls']}",
            agreement_pct,
            f"{agg['disagree']}",
            f"{agg['errors']}",
            f"${agg['cost']:.4f}",
            avg_lat,
        ])

    body = ""
    intro = (
        f'<div style="color:#666;font-size:13px;padding-bottom:10px">'
        f'{primary_total} primary AI calls had shadow evaluation enabled '
        f'today. Operational behavior is unchanged &mdash; this digest '
        f'is observational only.</div>'
    )
    body += intro

    if summary_rows:
        body += _section(
            "Per-Model Summary",
            _table(
                ["Model", "Calls", "Agreement",
                 "Disagree", "Errors", "Cost", "Avg Latency"],
                summary_rows,
            ),
        )

    primary_label = "primary"
    sample_primary = next(
        (r for r in rows
         if r.get("primary_provider") and r.get("primary_model")),
        None,
    )
    if sample_primary:
        primary_label = (
            f"{sample_primary['primary_provider']}"
            f":{sample_primary['primary_model']}"
        )

    body += _shadow_disagreement_detail(
        rows, primary_label=primary_label, db_path=db_path,
    )

    # Recently resolved disagreements — yesterday's and older calls
    # whose outcomes are now known. This is where most of the
    # "which was right" signal will land, because same-day
    # predictions are rarely resolved by EOD.
    body += _shadow_resolved_section(db_path)

    html = _wrap_html("Shadow Model Evaluation", body)
    return send_email(subject, html, ctx=ctx)


# ---------------------------------------------------------------------------
# 6. Error notification
# ---------------------------------------------------------------------------

# 2026-05-13 — per-subject email debounce. The May 13 incident:
# scheduler crash-looped on a non-critical DB integrity failure,
# sending 145 ERROR emails over 2 hours (one per 30-second restart
# cycle). With this debounce, a given subject can only fire once
# per `_NOTIFY_ERROR_DEBOUNCE_HOURS` window — no matter how many
# times the underlying error recurs. The first hit goes through;
# subsequent hits are silently suppressed.
#
# State is in-process (a module-level dict). When the process
# restarts, debounce resets — which is correct: if a process
# legitimately crashed and a new one is starting fresh, the operator
# should learn about a reproducing error from the FIRST email of
# each new process. The pathology this prevents is a SINGLE process
# (or rapid-restart loop) firing the same email N times.
#
# A debounced suppression IS logged so it's visible in the journal.
import threading as _threading
_NOTIFY_ERROR_DEBOUNCE_HOURS = 1
_notify_error_lock = _threading.Lock()
_notify_error_last_sent = {}  # subject → datetime


def notify_error(error_msg, context="", ctx=None):
    """Send a critical error notification.

    Args:
        error_msg: The error message or traceback string.
        context: Short label for what was happening when the error occurred.
        ctx: UserContext, optional.

    Per-subject debounced — a given subject can only fire once per
    `_NOTIFY_ERROR_DEBOUNCE_HOURS` window (default 1h). Suppressed
    subsequent calls log a warning so the activity is visible.

    The OUTERMOST safety net: this function MUST NEVER raise.
    It's called from many critical-path try/except blocks. If it
    propagates an exception, the original error is replaced by the
    notification error in tracebacks, error visibility collapses,
    and recursion-style retry loops can crash the system. Any
    failure inside (SMTP down, malformed body, debounce dict
    corrupted) is caught and logged-only.
    """
    try:
        ctx_label = (context if context else "General") or ""
        subject = f"QuantOpsAI ERROR: {ctx_label}"

        # Debounce check
        from datetime import timedelta as _timedelta
        now = datetime.utcnow()
        with _notify_error_lock:
            last = _notify_error_last_sent.get(subject)
            if last and (now - last) < _timedelta(
                    hours=_NOTIFY_ERROR_DEBOUNCE_HOURS):
                mins_remaining = int(
                    (_timedelta(hours=_NOTIFY_ERROR_DEBOUNCE_HOURS) -
                     (now - last)).total_seconds() / 60
                )
                logger.warning(
                    "notify_error debounced (last sent %s, %d min until "
                    "next allowed): %s",
                    last.isoformat(timespec="seconds"),
                    mins_remaining, subject,
                )
                return False
            _notify_error_last_sent[subject] = now

        # Build the body. Defensive str() in case error_msg is None.
        error_block = (
            f'<div style="padding:14px;background:#ffebee;border-left:4px solid {_RED};'
            f'font-family:monospace;font-size:13px;white-space:pre-wrap;word-break:break-all">'
            f"{str(error_msg) if error_msg is not None else '(no message)'}</div>"
        )
        body = _section(f"Error in: {ctx_label}", error_block)
        body += _kv_row(
            "Occurred at",
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        html = _wrap_html("Error Alert", body)
        return send_email(subject, html, ctx=ctx)
    except Exception as exc:
        # Outermost safety net — log + return False, never raise.
        # 2026-05-13 — pinned by tests/test_notify_error_never_raises.py
        # after that test caught notify_error propagating
        # send_email/_wrap_html/_kv_row failures.
        try:
            logger.warning(
                "notify_error itself failed (this is the safety net "
                "preventing error-handler crash loop): %s: %s",
                type(exc).__name__, exc,
            )
        # SILENT_OK: even the logger failed — there's nothing
        # left to do. The outer notify_error MUST never raise per
        # contract, so a failed-logger swallow is the right ending.
        except Exception:
            pass
        return False
