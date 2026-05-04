"""CLI for stocktwits.

DAILY:
  python -m stocktwits.cli daily          # full watchlist refresh + trending

Individual:
  python -m stocktwits.cli ticker NVDA    # one ticker
  python -m stocktwits.cli trending       # current top-trending tickers
  python -m stocktwits.cli show           # recent messages
  python -m stocktwits.cli sentiment      # daily sentiment leaderboard
  python -m stocktwits.cli runs

Watchlist: ~/stocktwits_watchlist.txt (one ticker per line). Falls
back to a default of mega-cap tech if the file isn't present.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List

import click
from rich.console import Console
from rich.table import Table

from .scrape import fetch_messages_for_ticker, fetch_trending, fetch_watchlist
from .store import (
    connect,
    latest_trending,
    query_daily_sentiment,
    query_messages,
    recent_runs,
)


console = Console()


WATCHLIST_FILE = Path.home() / "stocktwits_watchlist.txt"

DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    "AMD", "AVGO", "QCOM", "INTC",
    "JPM", "BAC", "GS", "MS", "V", "MA",
    "NFLX", "DIS", "UBER", "ABNB",
    "COIN", "MARA", "RIOT",
    "PLTR", "SOFI", "HOOD", "AFRM",
    "MRNA", "PFE", "LLY", "JNJ",
    "XOM", "CVX",
    "WMT", "COST", "TGT",
]


def _load_watchlist() -> List[str]:
    if WATCHLIST_FILE.exists():
        text = WATCHLIST_FILE.read_text().strip()
        return [
            line.strip().upper() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    return DEFAULT_WATCHLIST


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True)
@click.pass_context
def cli(ctx, verbose):
    """stocktwits — local sentiment cache for a custom watchlist."""
    _setup_logging(verbose)


@cli.command(name="daily")
@click.option("--max-tickers", type=int, default=None,
              help="Cap watchlist size (smoke test mode)")
@click.option("--skip-trending", is_flag=True,
              help="Skip the trending-snapshot fetch")
def daily_(max_tickers, skip_trending):
    """One-button refresh — every ticker on the watchlist + trending.

    Default watchlist is ~37 tickers. With 20s/req politeness, full
    daily run takes ~13-14 minutes. Override by writing one ticker per
    line to ~/stocktwits_watchlist.txt.
    """
    watchlist = _load_watchlist()
    if max_tickers:
        watchlist = watchlist[:max_tickers]

    console.print(f"[bold]Daily refresh — {len(watchlist)} tickers[/bold]")
    console.print(f"  Started at {datetime.utcnow().strftime('%H:%M UTC')}")

    with connect() as conn:
        if not skip_trending:
            console.print(f"\n[bold cyan][1/2] Trending[/bold cyan]")
            try:
                tickers = fetch_trending(conn)
                console.print(f"  [green]✓[/green] {len(tickers)} trending: "
                              f"{', '.join(tickers[:10])}")
            except Exception as exc:
                console.print(f"  [red]✗ trending failed[/red]: {exc}")

        console.print(f"\n[bold cyan][2/2] Watchlist[/bold cyan]")
        try:
            overall = fetch_watchlist(conn, watchlist)
            console.print(f"  [green]✓ done[/green]: {overall}")
        except Exception as exc:
            console.print(f"  [red]✗ watchlist failed[/red]: {exc}")


@cli.command()
@click.argument("ticker")
def ticker(ticker):
    """Fetch messages for one ticker right now."""
    with connect() as conn:
        try:
            stats = fetch_messages_for_ticker(conn, ticker)
            console.print(f"[green]✓ {ticker.upper()}[/green]: {stats}")
        except Exception as exc:
            console.print(f"[red]✗ failed[/red]: {exc}")


@cli.command()
def trending():
    """Snapshot + show current top-trending StockTwits tickers."""
    with connect() as conn:
        try:
            tickers = fetch_trending(conn)
        except Exception as exc:
            console.print(f"[red]✗ failed[/red]: {exc}")
            return
        rows = latest_trending(conn, limit=30)

    if not rows:
        console.print("[yellow]No trending data.[/yellow]")
        return

    table = Table(title="StockTwits trending — current snapshot")
    table.add_column("Rank", justify="right")
    table.add_column("Ticker")
    table.add_column("Snapshot at")
    for r in rows:
        table.add_row(str(r["rank"]), r["ticker"], r["snapshot_at"])
    console.print(table)


@cli.command()
@click.option("--ticker")
@click.option("--sentiment", type=click.Choice(["bullish", "bearish"]))
@click.option("--since", help="ISO timestamp (UTC)")
@click.option("--limit", type=int, default=30)
def show(ticker, sentiment, since, limit):
    """Recent messages with optional filters."""
    with connect() as conn:
        rows = query_messages(conn, ticker=ticker, sentiment=sentiment,
                              since=since, limit=limit)
    if not rows:
        console.print("[yellow]No messages.[/yellow]")
        return
    table = Table(title=f"Messages ({len(rows)})")
    table.add_column("Created", width=20)
    table.add_column("Ticker", width=7)
    table.add_column("Sent", width=8)
    table.add_column("Likes", justify="right", width=5)
    table.add_column("User", width=18)
    table.add_column("Body", overflow="fold")
    for r in rows:
        sent = r["sentiment"] or "—"
        sent_color = "green" if sent == "bullish" else (
            "red" if sent == "bearish" else "white"
        )
        table.add_row(
            (r["created_at"] or "")[:19],
            r["ticker"],
            f"[{sent_color}]{sent}[/{sent_color}]",
            str(r["like_count"] or 0),
            (r["user_name"] or "?")[:18],
            (r["body"] or "")[:80],
        )
    console.print(table)


@cli.command()
@click.option("--ticker")
@click.option("--since", help="YYYY-MM-DD")
@click.option("--limit", type=int, default=30)
def sentiment(ticker, since, limit):
    """Daily aggregated sentiment (rolled up from messages)."""
    with connect() as conn:
        rows = query_daily_sentiment(conn, ticker=ticker, since=since, limit=limit)
    if not rows:
        console.print("[yellow]No daily sentiment data.[/yellow]")
        return
    table = Table(title=f"Daily sentiment ({len(rows)})")
    table.add_column("Date", width=10)
    table.add_column("Ticker", width=7)
    table.add_column("Msgs", justify="right", width=6)
    table.add_column("Bull", justify="right", width=5)
    table.add_column("Bear", justify="right", width=5)
    table.add_column("Net", justify="right", width=8)
    table.add_column("Avg likes", justify="right", width=8)
    for r in rows:
        net = r["net_sentiment"] or 0
        color = "green" if net > 0 else ("red" if net < 0 else "white")
        table.add_row(
            r["date"], r["ticker"],
            str(r["n_messages"]),
            str(r["n_bullish"]), str(r["n_bearish"]),
            f"[{color}]{net:+.2f}[/{color}]",
            f"{(r['avg_likes'] or 0):.1f}",
        )
    console.print(table)


@cli.command()
@click.option("--limit", type=int, default=10)
def runs(limit):
    """Recent scrape runs."""
    with connect() as conn:
        rs = recent_runs(conn, limit=limit)
    if not rs:
        console.print("[yellow]No runs yet.[/yellow]")
        return
    table = Table(title="Recent scrape runs")
    table.add_column("Started", width=19)
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Rows inserted", justify="right")
    table.add_column("Error")
    for r in rs:
        table.add_row(
            r["started_at"], r["source"], r["status"],
            str(r["rows_inserted"] or 0), (r["error"] or "")[:40],
        )
    console.print(table)


if __name__ == "__main__":
    cli()
