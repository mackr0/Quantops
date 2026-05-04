"""CLI for biotechevents.

DAILY:
  python -m biotechevents.cli daily             # last 7 days of trial updates

Individual:
  python -m biotechevents.cli refresh --days 30  # bigger window
  python -m biotechevents.cli sponsor "Moderna"  # backfill one sponsor
  python -m biotechevents.cli show               # query current trials
  python -m biotechevents.cli changes            # recent status changes
  python -m biotechevents.cli counts             # trials by phase
  python -m biotechevents.cli runs               # scrape history
"""

from __future__ import annotations

import logging
from datetime import datetime

import click
from rich.console import Console
from rich.table import Table

from .scrape_clinicaltrials import (
    fetch_for_ticker,
    fetch_recently_updated,
)
from .scrape_fda import scrape_pdufa_calendar
from .store import (
    connect,
    counts_by_phase,
    query_trials,
    recent_changes,
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
    """biotechevents — clinical-trial milestones + FDA event tracker."""
    _setup_logging(verbose)


@cli.command(name="daily")
@click.option("--days", type=int, default=7,
              help="Pull trial updates from the last N days (default 7)")
@click.option("--max-pages", type=int, default=None)
def daily_(days, max_pages):
    """One-button daily refresh — recently-updated trials + (stubbed) FDA."""
    console.print(f"[bold]Daily refresh — last {days} days of trial updates[/bold]")
    console.print(f"  Started at {datetime.utcnow().strftime('%H:%M UTC')}")

    with connect() as conn:
        try:
            stats = fetch_recently_updated(conn, days_back=days, max_pages=max_pages)
            console.print(f"[green]✓ ClinicalTrials done[/green]: {stats}")
        except Exception as exc:
            console.print(f"[red]✗ ClinicalTrials failed[/red]: {exc}")

        try:
            fda_stats = scrape_pdufa_calendar(conn)
            console.print(f"[dim]• FDA PDUFA[/dim]: {fda_stats}")
        except Exception as exc:
            console.print(f"[red]✗ FDA failed[/red]: {exc}")


@cli.command()
@click.option("--days", type=int, default=30)
@click.option("--max-pages", type=int, default=None)
def refresh(days, max_pages):
    """Manual refresh — wider window than `daily` if you need backfill."""
    with connect() as conn:
        stats = fetch_recently_updated(conn, days_back=days, max_pages=max_pages)
    console.print(f"[green]✓ done[/green]: {stats}")


@cli.command()
@click.argument("sponsor")
@click.option("--max-pages", type=int, default=5)
def sponsor(sponsor, max_pages):
    """Backfill all trials for a specific sponsor company."""
    console.print(f"[bold cyan]→[/bold cyan] {sponsor}")
    with connect() as conn:
        stats = fetch_for_ticker(conn, sponsor, max_pages=max_pages)
    console.print(f"[green]✓ done[/green]: {stats}")


@cli.command()
@click.option("--ticker")
@click.option("--sponsor")
@click.option("--phase")
@click.option("--status")
@click.option("--completion-after", help="YYYY-MM-DD")
@click.option("--completion-before", help="YYYY-MM-DD")
@click.option("--limit", type=int, default=30)
def show(ticker, sponsor, phase, status, completion_after, completion_before, limit):
    """Query trials with optional filters."""
    with connect() as conn:
        rows = query_trials(
            conn, ticker=ticker, sponsor=sponsor, phase=phase,
            status=status, completion_after=completion_after,
            completion_before=completion_before, limit=limit,
        )
    if not rows:
        console.print("[yellow]No trials matching filters.[/yellow]")
        return

    table = Table(title=f"Trials ({len(rows)} rows)")
    table.add_column("NCT ID", width=12)
    table.add_column("Phase", width=14)
    table.add_column("Status", width=18)
    table.add_column("Sponsor", width=24)
    table.add_column("Ticker", width=7)
    table.add_column("Primary Completion", width=11)
    table.add_column("Title", overflow="fold")

    for r in rows:
        table.add_row(
            r["nct_id"],
            r["phase"] or "—",
            r["overall_status"] or "—",
            (r["sponsor_name"] or "")[:24],
            r["ticker"] or "—",
            r["primary_completion_date"] or "—",
            (r["brief_title"] or "")[:60],
        )
    console.print(table)


@cli.command()
@click.option("--days", type=int, default=7)
@click.option("--limit", type=int, default=30)
def changes(days, limit):
    """Show recent trial-status / phase / completion-date changes."""
    with connect() as conn:
        rows = recent_changes(conn, days=days, limit=limit)
    if not rows:
        console.print(f"[yellow]No changes in last {days} days.[/yellow]")
        return
    table = Table(title=f"Recent changes (last {days} days)")
    table.add_column("Detected", width=19)
    table.add_column("NCT ID", width=12)
    table.add_column("Sponsor", width=20)
    table.add_column("Ticker", width=7)
    table.add_column("Field", width=24)
    table.add_column("Old → New", overflow="fold")
    for r in rows:
        table.add_row(
            r["detected_at"][:19], r["nct_id"],
            (r["sponsor_name"] or "")[:20],
            r["ticker"] or "—",
            r["field"],
            f"{r['old_value'] or '—'} → {r['new_value'] or '—'}",
        )
    console.print(table)


@cli.command()
def counts():
    """Trials by phase."""
    with connect() as conn:
        cts = counts_by_phase(conn)
    if not cts:
        console.print("[yellow]DB is empty. Run `daily` first.[/yellow]")
        return
    table = Table(title="Trials by phase")
    table.add_column("Phase")
    table.add_column("Count", justify="right")
    for phase, n in cts.items():
        table.add_row(phase, f"{n:,}")
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
