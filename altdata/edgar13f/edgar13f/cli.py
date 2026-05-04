"""CLI for edgar13f.

DAILY:
  python -m edgar13f.cli daily          # refresh all starter filers

Individual:
  python -m edgar13f.cli refresh --cik 0001067983      # one filer
  python -m edgar13f.cli show --ticker AAPL            # current holdings of a ticker
  python -m edgar13f.cli counts                         # filings per quarter
  python -m edgar13f.cli filers                         # list configured filers
  python -m edgar13f.cli runs                           # scrape-run history
"""

from __future__ import annotations

import logging
from datetime import datetime

import click
from rich.console import Console
from rich.table import Table

from .scrape import FILERS, scrape_all_filers, scrape_filer
from .store import (
    connect,
    counts_by_period,
    query_holdings,
    recent_runs,
)


console = Console()


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True)
@click.pass_context
def cli(ctx, verbose):
    """edgar13f — SEC 13F institutional holdings scraper."""
    _setup_logging(verbose)


@cli.command(name="daily")
@click.option("--max-filings", type=int, default=None,
              help="Max filings per filer (smoke-test mode)")
def daily_(max_filings):
    """One-button refresh: scrape all configured filers for any new 13F-HR filings."""
    console.print(f"[bold]Daily refresh — {len(FILERS)} configured filers[/bold]")
    console.print(f"  Started at {datetime.utcnow().strftime('%H:%M UTC')}")

    with connect() as conn:
        results = scrape_all_filers(conn, max_filings_per_filer=max_filings)

    total_holdings = 0
    failures = 0
    for cik, stats in results.items():
        name = FILERS.get(cik, ("?", "?"))[0]
        if "error" in stats:
            console.print(f"  [red]✗ {name}[/red]: {stats['error']}")
            failures += 1
        else:
            total_holdings += stats.get("holdings_inserted", 0)
            console.print(
                f"  [green]✓ {name}[/green]: {stats['filings_ok']} filings, "
                f"{stats['holdings_inserted']} holdings"
            )

    console.print(
        f"\n[bold]Done.[/bold] {total_holdings} new holdings inserted, "
        f"{failures} filer(s) failed."
    )


@cli.command()
@click.option("--cik", required=True, help="10-digit CIK, zero-padded")
@click.option("--max-filings", type=int, default=None)
def refresh(cik, max_filings):
    """Refresh one filer by CIK."""
    name = FILERS.get(cik, ("(unknown)", None))[0]
    console.print(f"[bold cyan]→[/bold cyan] {name} (CIK {cik})")
    with connect() as conn:
        stats = scrape_filer(cik, conn, max_filings=max_filings)
    console.print(f"[green]✓ done[/green]: {stats}")


@cli.command()
@click.option("--ticker", help="Filter by ticker")
@click.option("--cusip", help="Filter by CUSIP")
@click.option("--cik", help="Filter by filer CIK")
@click.option("--period", help="Filter by period_of_report (YYYY-MM-DD)")
@click.option("--limit", type=int, default=50)
def show(ticker, cusip, cik, period, limit):
    """Query the DB and print results."""
    with connect() as conn:
        rows = query_holdings(conn, ticker=ticker, cusip=cusip,
                               cik=cik, period=period, limit=limit)
    if not rows:
        console.print("[yellow]No holdings found.[/yellow]")
        return

    table = Table(title=f"Holdings ({len(rows)} rows)")
    table.add_column("Period", width=10)
    table.add_column("Filer", width=28)
    table.add_column("Ticker", width=7)
    table.add_column("Company", width=36)
    table.add_column("Shares", justify="right", width=12)
    table.add_column("Value", justify="right", width=14)
    table.add_column("Put/Call", width=6)

    for r in rows:
        shares_str = f"{r['shares']:,}" if r["shares"] else "—"
        value_str = f"${r['value_usd']:,}" if r["value_usd"] else "—"
        table.add_row(
            r["period_of_report"],
            (r["filer_name"] or "")[:28],
            r["ticker"] or "—",
            (r["company_name"] or "")[:36],
            shares_str,
            value_str,
            r["put_call"] or "",
        )
    console.print(table)


@cli.command()
def counts():
    """Filings per quarter."""
    with connect() as conn:
        cts = counts_by_period(conn)
    if not cts:
        console.print("[yellow]DB is empty. Run `daily` first.[/yellow]")
        return
    table = Table(title="Filings by quarter")
    table.add_column("Period")
    table.add_column("Filings", justify="right")
    for period, n in sorted(cts.items(), reverse=True):
        table.add_row(period, f"{n:,}")
    console.print(table)


@cli.command()
def filers():
    """List the starter filer roster."""
    table = Table(title=f"Configured filers ({len(FILERS)})")
    table.add_column("CIK", width=10)
    table.add_column("Name", width=40)
    table.add_column("Type")
    for cik, (name, ftype) in FILERS.items():
        table.add_row(cik, name, ftype)
    console.print(table)


@cli.command()
@click.option("--limit", type=int, default=10)
def runs(limit):
    """Show recent scrape runs."""
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
