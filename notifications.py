"""Rich HTML email notifications for trade events, vetoes, exits, and daily summaries."""

import json
import logging
import threading
import urllib.request
import urllib.error
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
        except Exception:
            pass

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
        conn = _get_conn(db_path)
        vetoes = conn.execute(
            "SELECT * FROM signals WHERE acted_on = 0 AND timestamp LIKE ? "
            "AND signal IN ('BUY','STRONG_BUY','SELL','STRONG_SELL') "
            "ORDER BY timestamp DESC",
            (f"{today_str}%",),
        ).fetchall()
        conn.close()
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
# 6. Error notification
# ---------------------------------------------------------------------------

def notify_error(error_msg, context="", ctx=None):
    """Send a critical error notification.

    Args:
        error_msg: The error message or traceback string.
        context: Short label for what was happening when the error occurred.
        ctx: UserContext, optional.
    """
    ctx_label = context if context else "General"
    subject = f"QuantOpsAI ERROR: {ctx_label}"

    error_block = (
        f'<div style="padding:14px;background:#ffebee;border-left:4px solid {_RED};'
        f'font-family:monospace;font-size:13px;white-space:pre-wrap;word-break:break-all">'
        f"{error_msg}</div>"
    )
    body = _section(f"Error in: {ctx_label}", error_block)

    # Timestamp
    body += _kv_row("Occurred at", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))

    html = _wrap_html("Error Alert", body)
    return send_email(subject, html, ctx=ctx)
