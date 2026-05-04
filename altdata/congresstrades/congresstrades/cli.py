"""Command-line interface for congresstrades.

DAILY ROUTINE:
  python -m congresstrades.cli daily       # one-button refresh of everything

Individual commands:
  python -m congresstrades.cli refresh --year 2025           # one chamber, one year
  python -m congresstrades.cli prices                        # refresh price cache
  python -m congresstrades.cli pnl --chamber senate --year 2025
  python -m congresstrades.cli show --ticker NVDA
  python -m congresstrades.cli show --member "Pelosi"
  python -m congresstrades.cli runs
  python -m congresstrades.cli counts
  python -m congresstrades.cli export --format csv --out trades.csv
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .scrape_house import scrape_year as scrape_house_year
from .scrape_senate import scrape_year as scrape_senate_year
from .store import (
    connect,
    counts_by_chamber,
    query_trades,
    recent_runs,
)
from .pnl import (
    list_all_members,
    load_trades_for_member,
    match_fifo_lots,
)


console = Console()


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Debug logging")
@click.pass_context
def cli(ctx, verbose):
    """congresstrades — local scraper for US congressional PTRs."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)


@cli.command()
@click.option("--year", type=int, default=datetime.utcnow().year,
              help="Calendar year to scrape (default: current year)")
@click.option("--house/--no-house", default=True,
              help="Scrape House (default on)")
@click.option("--senate/--no-senate", default=False,
              help="Scrape Senate for the given --year")
@click.option("--limit", type=int, default=None,
              help="Max filings to process (smoke-test mode, e.g. --limit 10)")
@click.option("--force-zip-refresh", is_flag=True,
              help="Re-download the yearly zip even if cached (House only)")
def refresh(year: int, house: bool, senate: bool, limit, force_zip_refresh: bool):
    """Fetch fresh data from disclosure sites into the local DB."""
    with connect() as conn:
        if house:
            console.print(f"[bold cyan]→ House[/bold cyan] "
                          f"scraping year {year}"
                          + (f", limit {limit}" if limit else "")
                          + (", force-refresh" if force_zip_refresh else ""))
            try:
                stats = scrape_house_year(
                    year, conn,
                    max_filings=limit,
                    force_zip_refresh=force_zip_refresh,
                )
                console.print(f"[green]✓ House done[/green]: {stats}")
            except Exception as exc:
                console.print(f"[red]✗ House failed[/red]: {exc}")
        if senate:
            console.print(f"[bold cyan]→ Senate[/bold cyan] scraping year {year}"
                          + (f", limit {limit}" if limit else ""))
            try:
                stats = scrape_senate_year(year, conn, max_filings=limit)
                console.print(f"[green]✓ Senate done[/green]: {stats}")
            except Exception as exc:
                console.print(f"[red]✗ Senate failed[/red]: {exc}")


@cli.command()
@click.option("--ticker", help="Filter by ticker (exact, uppercase)")
@click.option("--member", help="Filter by member name (substring, case-insensitive)")
@click.option("--chamber", type=click.Choice(["house", "senate"]))
@click.option("--since", help="Filter filings on or after YYYY-MM-DD")
@click.option("--limit", type=int, default=50,
              help="Max rows to show (default 50)")
def show(ticker, member, chamber, since, limit):
    """Query the local DB and print results as a table."""
    with connect() as conn:
        rows = query_trades(
            conn,
            ticker=ticker, member=member, chamber=chamber, since=since,
            limit=limit,
        )

    if not rows:
        console.print("[yellow]No trades found matching filters.[/yellow]")
        return

    table = Table(
        title=f"Congressional trades ({len(rows)} rows, filters applied)",
        show_lines=False,
    )
    table.add_column("Chamber", style="dim", width=7)
    table.add_column("Filed", width=10)
    table.add_column("Txn date", width=10)
    table.add_column("Member", width=26)
    table.add_column("Ticker", width=7)
    table.add_column("Type", width=7)
    table.add_column("Amount", width=20)
    table.add_column("Asset", overflow="fold")

    for r in rows:
        amount_str = r["amount_range"] or (
            f"${r['amount_low']:,} - ${r['amount_high']:,}"
            if r["amount_low"] and r["amount_high"] else "—"
        )
        table.add_row(
            r["chamber"],
            r["filing_date"] or "—",
            r["transaction_date"] or "—",
            (r["member_name"] or "—")[:26],
            r["ticker"] or "—",
            r["transaction_type"] or "—",
            amount_str,
            (r["asset_description"] or "")[:80],
        )

    console.print(table)


@cli.command()
def counts():
    """Show total trade counts by chamber."""
    with connect() as conn:
        cts = counts_by_chamber(conn)
    if not cts:
        console.print("[yellow]DB is empty. Run `refresh` first.[/yellow]")
        return
    table = Table(title="Trade counts by chamber")
    table.add_column("Chamber")
    table.add_column("Trades", justify="right")
    total = 0
    for chamber, n in sorted(cts.items()):
        table.add_row(chamber, f"{n:,}")
        total += n
    table.add_row("[bold]TOTAL[/bold]", f"[bold]{total:,}[/bold]")
    console.print(table)


@cli.command()
@click.option("--limit", type=int, default=10, help="Runs to show")
def runs(limit):
    """Show recent scrape-run history + status."""
    with connect() as conn:
        rs = recent_runs(conn, limit=limit)
    if not rs:
        console.print("[yellow]No scrape runs yet. Run `refresh` first.[/yellow]")
        return

    table = Table(title="Recent scrape runs")
    table.add_column("Started", width=19)
    table.add_column("Chamber")
    table.add_column("Status")
    table.add_column("Rows inserted", justify="right")
    table.add_column("Filings seen", justify="right")
    table.add_column("Error")

    for r in rs:
        table.add_row(
            r["started_at"],
            r["chamber"],
            r["status"],
            str(r["rows_inserted"] or 0),
            str(r["rows_seen"] or 0),
            (r["error"] or "")[:40],
        )
    console.print(table)


@cli.command()
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]),
              default="csv")
@click.option("--out", type=click.Path(), required=True,
              help="Output file path")
@click.option("--chamber", type=click.Choice(["house", "senate"]),
              help="Filter by chamber")
@click.option("--since", help="Filter on or after YYYY-MM-DD")
def export(fmt, out, chamber, since):
    """Export the local DB to CSV or JSON."""
    with connect() as conn:
        rows = query_trades(conn, chamber=chamber, since=since, limit=1_000_000)

    if not rows:
        console.print("[yellow]Nothing to export.[/yellow]")
        return

    rows_as_dicts = [dict(r) for r in rows]

    if fmt == "csv":
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows_as_dicts[0].keys())
            w.writeheader()
            w.writerows(rows_as_dicts)
    else:
        import json
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rows_as_dicts, f, indent=2, default=str)

    console.print(f"[green]✓[/green] Exported {len(rows)} rows to {out}")


# ---------------------------------------------------------------------------
# P&L command
# ---------------------------------------------------------------------------

def _build_price_lookup(price_cache_dir: Path):
    """Load whatever the earlier never_lost.py cached (CSV per ticker).

    Returns two callables matching the pnl.match_fifo_lots signature:
      price_at_date(ticker, iso_date) -> Optional[float]
      current_price(ticker) -> Optional[float]

    Returns (None, None) when the cache dir doesn't exist; callers get
    functions that always return None and the P&L estimator reports
    "price unresolved" for affected trades.
    """
    if not price_cache_dir.exists():
        return (lambda sym, date: None, lambda sym: None)

    import pandas as pd

    # Lazy-load CSVs on first access per ticker
    cached_series: dict = {}

    def _load(ticker: str):
        if ticker in cached_series:
            return cached_series[ticker]
        path = price_cache_dir / f"{ticker}.csv"
        if not path.exists():
            cached_series[ticker] = None
            return None
        try:
            df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
            if df.empty or "Close" not in df.columns:
                cached_series[ticker] = None
                return None
            cached_series[ticker] = df["Close"].dropna()
            return cached_series[ticker]
        except Exception:
            cached_series[ticker] = None
            return None

    def price_at_date(ticker: str, iso_date: str):
        s = _load(ticker)
        if s is None or s.empty:
            return None
        try:
            ts = pd.Timestamp(iso_date)
        except Exception:
            return None
        # Match any time-zone from cached index
        if s.index.tz is not None and ts.tz is None:
            ts = ts.tz_localize(s.index.tz)
        elif s.index.tz is None and ts.tz is not None:
            ts = ts.tz_localize(None)
        # Closest trading day on or after
        after = s[s.index >= ts]
        return float(after.iloc[0]) if not after.empty else None

    def current_price(ticker: str):
        s = _load(ticker)
        if s is None or s.empty:
            return None
        return float(s.iloc[-1])

    return (price_at_date, current_price)


@cli.command()
@click.option("--chamber", type=click.Choice(["house", "senate"]),
              help="Restrict to one chamber (default: both)")
@click.option("--year", type=int,
              help="Only count trades with transaction_date in this year")
@click.option("--member", help="Single-member detail view (substring match)")
@click.option("--min-trades", type=int, default=5,
              help="Skip members with fewer than this many trades")
@click.option("--top", type=int, default=20,
              help="How many members to show on the summary")
def pnl(chamber, year, member, min_trades, top):
    """Estimate realized + unrealized P&L per member.

    Uses range midpoints for position size + cached daily closes for
    return %. Output bounds reflect the inherent uncertainty of STOCK
    Act range-only disclosures."""
    from pathlib import Path as _P
    price_dir = _P(__file__).resolve().parent.parent / "data" / "cache" / "prices"
    price_at_date, current_price = _build_price_lookup(price_dir)

    start = f"{year}-01-01" if year else None
    end = f"{year}-12-31" if year else None

    with connect() as conn:
        if member:
            # Single-member detail
            rows = conn.execute(
                "SELECT DISTINCT member_name FROM trades "
                "WHERE member_name LIKE ? ORDER BY member_name",
                (f"%{member}%",),
            ).fetchall()
            matches = [r["member_name"] for r in rows]
            if not matches:
                console.print(f"[yellow]No member matching '{member}'[/yellow]")
                return
            for name in matches:
                trades = load_trades_for_member(
                    conn, name, start_date=start, end_date=end, chamber=chamber,
                )
                perf = match_fifo_lots(trades, price_at_date, current_price)
                _print_member_detail(perf)
            return

        # Leaderboard mode
        members = list_all_members(conn, chamber=chamber, start_date=start,
                                    min_trades=min_trades)
        if not members:
            console.print("[yellow]No members meet --min-trades threshold.[/yellow]")
            return

        results = []
        for name in members:
            trades = load_trades_for_member(
                conn, name, start_date=start, end_date=end, chamber=chamber,
            )
            perf = match_fifo_lots(trades, price_at_date, current_price)
            realized = perf.realized_bounds()
            unrealized = perf.unrealized_bounds()
            total = perf.total_bounds()
            wr = perf.closed_win_rate()
            results.append({
                "member": name,
                "n_buys": perf.n_buys,
                "n_sells": perf.n_sells,
                "closed": len(perf.closed_roundtrips),
                "open": len(perf.open_positions),
                "realized": realized,
                "unrealized": unrealized,
                "total": total,
                "win_rate": wr,
            })

        results.sort(key=lambda r: r["total"][1], reverse=True)

        scope = []
        if chamber: scope.append(chamber)
        if year: scope.append(str(year))
        scope_str = " · ".join(scope) if scope else "all data"
        table = Table(
            title=f"Estimated P&L by member — {scope_str} "
                  f"(top {top}, ≥{min_trades} trades)",
            show_lines=False,
        )
        table.add_column("Member", width=28)
        table.add_column("B/S", width=9)
        table.add_column("Closed", justify="right", width=7)
        table.add_column("Open", justify="right", width=5)
        table.add_column("Realized est.", width=24)
        table.add_column("Unrealized est.", width=24)
        table.add_column("Total mid", justify="right", width=12)
        table.add_column("WR", justify="right", width=6)

        for r in results[:top]:
            rl, rm, rh = r["realized"]
            ul, um, uh = r["unrealized"]
            tl, tm, th = r["total"]
            wr = f"{r['win_rate']*100:.0f}%" if r["win_rate"] is not None else "—"
            color_total = "green" if tm > 0 else ("red" if tm < 0 else "white")
            table.add_row(
                r["member"][:28],
                f"{r['n_buys']}/{r['n_sells']}",
                str(r["closed"]),
                str(r["open"]),
                _fmt_bounds(rl, rm, rh),
                _fmt_bounds(ul, um, uh),
                f"[{color_total}]${tm:+,.0f}[/{color_total}]",
                wr,
            )
        console.print(table)
        console.print(
            "[dim]Bounds reflect STOCK Act range-only disclosures "
            "(low/mid/high come from applying observed % return to the "
            "amount-range ends + midpoint). Midpoint is the point estimate; "
            "low/high are the uncertainty band.[/dim]"
        )


def _fmt_bounds(low, mid, high) -> str:
    if mid == 0 and low == 0 and high == 0:
        return "[dim]—[/dim]"
    color = "green" if mid > 0 else ("red" if mid < 0 else "white")
    return f"[{color}]${mid:+,.0f}[/{color}] [dim](${low:+,.0f}…${high:+,.0f})[/dim]"


def _print_member_detail(perf):
    console.print(
        f"\n[bold]{perf.member}[/bold]  "
        f"{perf.n_buys} buys / {perf.n_sells} sells · "
        f"{len(perf.closed_roundtrips)} closed / "
        f"{len(perf.open_positions)} open"
    )
    rl, rm, rh = perf.realized_bounds()
    ul, um, uh = perf.unrealized_bounds()
    tl, tm, th = perf.total_bounds()
    wr = perf.closed_win_rate()
    console.print(f"  Realized:    {_fmt_bounds(rl, rm, rh)}")
    console.print(f"  Unrealized:  {_fmt_bounds(ul, um, uh)}")
    console.print(f"  Total mid:   ${tm:+,.0f}   Win rate (closed): "
                  f"{wr*100:.0f}%" if wr is not None else
                  f"  Total mid:   ${tm:+,.0f}")


@cli.command()
@click.option("--year", type=int, default=datetime.utcnow().year,
              help="Restrict to tickers traded in this year (default: current)")
@click.option("--chamber", type=click.Choice(["house", "senate"]),
              help="Restrict to one chamber")
@click.option("--period", default="1y",
              help="yfinance period string. '1y' for current-year P&L, "
                   "'3y' for multi-year. Default: 1y")
@click.option("--force", is_flag=True,
              help="Re-fetch even for already-cached tickers")
@click.option("--all", "all_tickers", is_flag=True,
              help="Fetch every ticker in the DB, ignoring year/chamber")
def prices(year, chamber, period, force, all_tickers):
    """Refresh the daily-close price cache for tickers in the DB.

    Rate-limited to 1 req/sec — ~10 min for 600 tickers. Safe to re-run;
    cached tickers are skipped unless --force. Required for `pnl` to
    produce accurate marked-to-market estimates.
    """
    from .prices import refresh_prices, tickers_from_db

    with connect() as conn:
        if all_tickers:
            tickers = tickers_from_db(conn)
            label = "all tickers"
        else:
            tickers = tickers_from_db(conn, year=year, chamber=chamber)
            scope = [str(year)] + ([chamber] if chamber else [])
            label = f"tickers active in {' · '.join(scope)}"

    if not tickers:
        console.print("[yellow]No tickers to fetch.[/yellow]")
        return

    console.print(f"[bold cyan]→ Prices[/bold cyan] refreshing {len(tickers)} "
                  f"{label} (period={period}, 1 req/sec)")

    def _progress(n, total):
        if n % 50 == 0 or n == total:
            console.print(f"  {n}/{total} processed")

    stats = refresh_prices(tickers, period=period, force=force,
                           on_progress=_progress)
    console.print(
        f"[green]✓ Prices done[/green]: "
        f"fetched {stats['fetched']}, cached hits {stats['cached']}, "
        f"empty {stats['empty']}, errors {stats['errors']}"
    )


@cli.command(name="daily")
@click.option("--year", type=int, default=datetime.utcnow().year,
              help="Year to refresh (default: current calendar year)")
@click.option("--skip-prices", is_flag=True,
              help="Skip the price-cache refresh step (faster, ~5 min)")
@click.option("--force-prices", is_flag=True,
              help="Re-fetch every ticker in the price cache, even if cached")
def daily_(year, skip_prices, force_prices):
    """One-button daily refresh: House → Senate → Prices.

    Order matters — we scrape both chambers first (populates new tickers
    in the DB), then refresh prices for every ticker we have. Rate-limited
    throughout (House 0.4s/req, Senate 2s/req, Prices 1s/req). Total
    ~15-25 min depending on how much new data has been disclosed.
    """
    from .prices import refresh_prices, tickers_from_db

    console.print(f"[bold]Daily refresh — year {year}[/bold]")
    console.print(f"  Started at {datetime.utcnow().strftime('%H:%M UTC')}")

    summary = {}

    with connect() as conn:
        # Snapshot counts so we can report deltas at the end
        before_h = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE chamber='house'"
        ).fetchone()[0]
        before_s = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE chamber='senate'"
        ).fetchone()[0]

    # Step 1: House (force ZIP refresh — current-year ZIP grows over time)
    console.print(f"\n[bold cyan][1/3] House {year}[/bold cyan]  "
                  f"(force-zip-refresh)")
    try:
        with connect() as conn:
            house_stats = scrape_house_year(
                year, conn, force_zip_refresh=True,
            )
        console.print(f"  [green]✓ House done[/green]: {house_stats}")
        summary["house"] = house_stats
    except Exception as exc:
        console.print(f"  [red]✗ House failed[/red]: {exc}")
        summary["house_error"] = str(exc)

    # Step 2: Senate
    console.print(f"\n[bold cyan][2/3] Senate {year}[/bold cyan]")
    try:
        with connect() as conn:
            senate_stats = scrape_senate_year(year, conn)
        console.print(f"  [green]✓ Senate done[/green]: {senate_stats}")
        summary["senate"] = senate_stats
    except Exception as exc:
        console.print(f"  [red]✗ Senate failed[/red]: {exc}")
        summary["senate_error"] = str(exc)

    # Step 3: Prices (covers both chambers' tickers for the year)
    if skip_prices:
        console.print(f"\n[dim][3/3] Prices skipped (--skip-prices)[/dim]")
    else:
        with connect() as conn:
            tickers = tickers_from_db(conn, year=year)
        console.print(f"\n[bold cyan][3/3] Prices[/bold cyan]  "
                      f"refreshing {len(tickers)} tickers active in {year}")
        if tickers:
            def _progress(n, total):
                if n % 50 == 0 or n == total:
                    console.print(f"  {n}/{total} processed")
            price_stats = refresh_prices(
                tickers, period="1y", force=force_prices,
                on_progress=_progress,
            )
            console.print(f"  [green]✓ Prices done[/green]: {price_stats}")
            summary["prices"] = price_stats

    # Delta summary
    with connect() as conn:
        after_h = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE chamber='house'"
        ).fetchone()[0]
        after_s = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE chamber='senate'"
        ).fetchone()[0]

    delta_h = after_h - before_h
    delta_s = after_s - before_s
    console.print(f"\n[bold]Done.[/bold] New rows inserted this run: "
                  f"House +{delta_h}, Senate +{delta_s}")
    if delta_h == 0 and delta_s == 0:
        console.print("  [dim](no new disclosures since last refresh — "
                      "both chambers steady)[/dim]")


if __name__ == "__main__":
    cli()
