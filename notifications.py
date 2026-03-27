"""Rich HTML email notifications for trade events, vetoes, exits, and daily summaries."""

import smtplib
import logging
from datetime import date, datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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


def _color_pct(value):
    """Return an inline-styled span coloring a percentage green or red."""
    if value is None:
        return '<span style="color:#8a8a9a">--</span>'
    color = _GREEN if value >= 0 else _RED
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
    <span style="color:#fff;font-size:22px;font-weight:bold;letter-spacing:1px">QUANTOPS</span>
    <span style="color:{_MUTED};font-size:13px;margin-left:12px">{title}</span>
  </td></tr>
  <!-- Body -->
  <tr><td style="background:{_CARD_BG};padding:24px;border-radius:0 0 8px 8px;border:1px solid #e0e0e0;border-top:none">
    {body_content}
  </td></tr>
  <!-- Footer -->
  <tr><td style="padding:12px 24px;text-align:center">
    <span style="color:{_MUTED};font-size:11px">Quantops Automated Trading System &bull; {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</span>
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

def send_email(subject, html_body):
    """Send an HTML email via SMTP with TLS.

    Returns True on success, False on failure.  Never raises.
    """
    smtp_user = config.SMTP_USER
    smtp_password = config.SMTP_PASSWORD

    if not smtp_user or not smtp_password:
        logger.warning("SMTP credentials not configured — skipping email notification.")
        return False

    recipient = config.NOTIFICATION_EMAIL

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, [recipient], msg.as_string())
        logger.info("Email sent: %s", subject)
        return True
    except Exception as exc:
        logger.error("Failed to send email '%s': %s", subject, exc)
        return False


# ---------------------------------------------------------------------------
# 2. Trade notification
# ---------------------------------------------------------------------------

def notify_trade(trade_result, signal, ai_result=None):
    """Send a rich notification after a trade is executed.

    Args:
        trade_result: Dict returned by aggressive_execute_trade (or similar).
        signal: The strategy signal dict that triggered the trade.
        ai_result: Optional AI analysis dict.
    """
    symbol = trade_result.get("symbol", "???")
    action = trade_result.get("action", "NONE")
    qty = trade_result.get("qty", 0)
    price = signal.get("price", 0)
    estimated_cost = trade_result.get("estimated_cost", qty * price)

    subject = f"Quantops: {action} {qty} {symbol} @ ${price:,.2f}"

    # -- Trade details -------------------------------------------------------
    details = (
        _kv_row("Symbol", symbol)
        + _kv_row("Side", action)
        + _kv_row("Quantity", f"{qty:,}")
        + _kv_row("Price", f"${price:,.2f}")
        + _kv_row("Estimated Cost", f"${estimated_cost:,.2f}")
        + _kv_row("Strategy", trade_result.get("strategy", "aggressive"))
        + _kv_row("Order ID", trade_result.get("order_id", "--"))
    )
    body = _section("Trade Details", details)

    # -- Signal info ---------------------------------------------------------
    score = signal.get("score", "--")
    sig_info = (
        _kv_row("Signal", signal.get("signal", "--"))
        + _kv_row("Score", score)
        + _kv_row("Reason", signal.get("reason", "--"))
    )
    body += _section("Strategy Signal", sig_info)

    # -- AI analysis ---------------------------------------------------------
    if ai_result:
        ai_signal = ai_result.get("signal", "--")
        ai_conf = ai_result.get("confidence", "--")
        ai_reasoning = ai_result.get("reasoning", "--")
        risk_factors = ai_result.get("risk_factors", [])
        risk_str = ", ".join(risk_factors) if risk_factors else "None listed"
        ai_info = (
            _kv_row("AI Signal", ai_signal)
            + _kv_row("AI Confidence", f"{ai_conf}%")
            + _kv_row("Reasoning", ai_reasoning)
            + _kv_row("Risk Factors", risk_str)
        )
        body += _section("AI Analysis", ai_info)

    # -- Account snapshot ----------------------------------------------------
    try:
        account = get_account_info()
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
        positions = get_positions()
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
    return send_email(subject, html)


# ---------------------------------------------------------------------------
# 3. AI veto notification
# ---------------------------------------------------------------------------

def notify_veto(symbol, technical_signal, ai_result):
    """Notify when AI vetoes a trade.

    Args:
        symbol: Ticker string.
        technical_signal: The strategy signal dict.
        ai_result: AI analysis dict.
    """
    tech_action = technical_signal.get("signal", "???")
    ai_signal = ai_result.get("signal", "???")
    ai_conf = ai_result.get("confidence", "?")

    subject = f"Quantops: AI Vetoed {tech_action} {symbol}"

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
    return send_email(subject, html)


# ---------------------------------------------------------------------------
# 4. Exit notification (stop-loss / take-profit)
# ---------------------------------------------------------------------------

def notify_exit(symbol, trigger, qty, reason):
    """Notify when a stop-loss or take-profit triggers an exit.

    Args:
        symbol: Ticker string.
        trigger: 'stop_loss' or 'take_profit'.
        qty: Number of shares sold.
        reason: Descriptive reason string (e.g. from portfolio_manager).
    """
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

    subject = f"Quantops: {label} {symbol}{pct_str}"

    exit_info = (
        _kv_row("Symbol", symbol)
        + _kv_row("Trigger", label)
        + _kv_row("Quantity Sold", f"{qty:,}")
        + _kv_row("Reason", reason)
    )
    body = _section("Exit Details", exit_info)

    # -- P&L from the trade --------------------------------------------------
    try:
        trades = get_trade_history(symbol=symbol, limit=5)
        recent_sells = [t for t in trades if t.get("side") == "sell" and t.get("pnl") is not None]
        if recent_sells:
            last = recent_sells[0]
            pnl_info = _kv_row("Realized P&L", _color_pnl(last["pnl"]))
            body += _section("P&L", pnl_info)
    except Exception as exc:
        logger.warning("Could not fetch trade P&L for exit notification: %s", exc)

    # -- Remaining positions --------------------------------------------------
    try:
        positions = get_positions()
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
    return send_email(subject, html)


# ---------------------------------------------------------------------------
# 5. Daily summary
# ---------------------------------------------------------------------------

def notify_daily_summary():
    """Send a comprehensive end-of-day summary email."""
    today_str = date.today().isoformat()
    subject = f"Quantops Daily Summary \u2014 {today_str}"

    body = ""

    # -- Account overview ----------------------------------------------------
    try:
        account = get_account_info()
        positions = get_positions()
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
        all_trades = get_trade_history(limit=200)
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
        conn = _get_conn()
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
        ai_perf = get_ai_performance()
        if ai_perf.get("total_predictions", 0) > 0:
            ai_info = (
                _kv_row("Total Predictions", ai_perf["total_predictions"])
                + _kv_row("Resolved", ai_perf["resolved"])
                + _kv_row("Pending", ai_perf["pending"])
                + _kv_row("Win Rate", f"{ai_perf['win_rate']:.1f}%")
                + _kv_row("Profit Factor", ai_perf["profit_factor"])
                + _kv_row("Avg Return on BUYs", _color_pct(ai_perf["avg_return_on_buys"]))
                + _kv_row("Avg Return on SELLs", _color_pct(ai_perf["avg_return_on_sells"]))
            )
            body += _section("AI Performance", ai_info)
    except Exception as exc:
        logger.warning("Could not fetch AI performance for daily summary: %s", exc)

    # -- Risk summary --------------------------------------------------------
    if account and positions:
        try:
            risk = get_risk_summary(account, positions)
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
        perf = get_performance_summary()
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
    return send_email(subject, html)


# ---------------------------------------------------------------------------
# 6. Error notification
# ---------------------------------------------------------------------------

def notify_error(error_msg, context=""):
    """Send a critical error notification.

    Args:
        error_msg: The error message or traceback string.
        context: Short label for what was happening when the error occurred.
    """
    ctx_label = context if context else "General"
    subject = f"Quantops ERROR: {ctx_label}"

    error_block = (
        f'<div style="padding:14px;background:#ffebee;border-left:4px solid {_RED};'
        f'font-family:monospace;font-size:13px;white-space:pre-wrap;word-break:break-all">'
        f"{error_msg}</div>"
    )
    body = _section(f"Error in: {ctx_label}", error_block)

    # Timestamp
    body += _kv_row("Occurred at", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))

    html = _wrap_html("Error Alert", body)
    return send_email(subject, html)
