"""CLI for edgar_form4.

DAILY:
  python -m edgar_form4.cli refresh-tickers       # update CIK map
  python -m edgar_form4.cli daily --tickers AAPL,MSFT,...

Individual:
  python -m edgar_form4.cli refresh --ticker AAPL  # one symbol
  python -m edgar_form4.cli show --ticker AAPL     # aggregate insider summary
  python -m edgar_form4.cli counts                  # filings by day
  python -m edgar_form4.cli runs                    # scrape-run history
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime

import click

from .scrape import (
    EdgarSession,
    refresh_ticker_cik_map,
    scrape_company,
    scrape_universe,
)
from .store import (
    connect,
    counts_by_date,
    finish_run,
    get_recent_insider_activity,
    recent_runs,
    start_run,
)


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@click.group()
def cli():
    """edgar_form4 — SEC Form 4 (insider transactions) scraper."""
    pass


@cli.command()
@click.option("--verbose", is_flag=True)
def refresh_tickers(verbose):
    """Refresh the ticker → CIK mapping from SEC's published list.

    Runs in under 10 seconds; safe to run weekly or per-deploy.
    Always runs at least once before `daily` to seed the map.
    """
    _setup_logging(verbose)
    session = EdgarSession()
    with connect() as conn:
        run_id = start_run(conn, "refresh_tickers")
        try:
            n = refresh_ticker_cik_map(session, conn)
            finish_run(conn, run_id, "ok", rows_inserted=n, rows_seen=n)
            click.echo(f"✓ Refreshed {n} ticker→CIK mappings.")
        except Exception as exc:
            finish_run(conn, run_id, "failed", error=str(exc))
            click.echo(f"✗ refresh-tickers failed: {exc}", err=True)
            sys.exit(1)


@cli.command()
@click.option("--ticker", required=True)
@click.option("--max-age-days", type=int, default=90)
@click.option("--verbose", is_flag=True)
def refresh(ticker, max_age_days, verbose):
    """Refresh Form 4 data for ONE ticker."""
    _setup_logging(verbose)
    session = EdgarSession()
    with connect() as conn:
        run_id = start_run(conn, f"refresh:{ticker}")
        try:
            r = scrape_company(session, conn, ticker,
                                max_age_days=max_age_days)
            status = "failed" if r.get("error") else "ok"
            finish_run(
                conn, run_id, status,
                rows_inserted=r.get("txns_inserted", 0),
                rows_seen=r.get("filings_seen", 0),
                error=r.get("error"),
            )
            if r.get("error"):
                click.echo(f"✗ {ticker}: {r['error']}", err=True)
                sys.exit(1)
            click.echo(
                f"✓ {ticker} (CIK {r['cik']}): "
                f"{r['filings_seen']} filings seen, "
                f"{r['txns_inserted']} new transactions"
            )
        except Exception as exc:
            finish_run(conn, run_id, "failed", error=str(exc))
            click.echo(f"✗ refresh failed: {exc}", err=True)
            sys.exit(1)


def _discover_active_tickers() -> list:
    """Best-effort: discover the active ticker universe from the
    parent QuantOpsAI repo. Returns [] if not running inside the
    parent context (e.g., standalone install).

    Sources tried in order:
      1. Env var EDGAR_FORM4_TICKERS (comma-separated)
      2. Parent QuantOpsAI's segments.py active universes
      3. Empty (caller's --tickers must then be supplied)
    """
    import os
    env_val = os.environ.get("EDGAR_FORM4_TICKERS")
    if env_val:
        return [t.strip().upper() for t in env_val.split(",") if t.strip()]
    try:
        import sys
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from segments import list_segments, get_segment
        tickers = set()
        for seg_name in list_segments():
            seg = get_segment(seg_name)
            for sym in seg.get("universe", []) or []:
                # Filter crypto (BTC/USD style) — Form 4 is equities only.
                if "/" in sym:
                    continue
                tickers.add(sym.upper())
        return sorted(tickers)
    except Exception:
        return []


@cli.command()
@click.option("--tickers", default=None,
              help="comma-separated ticker list (e.g., AAPL,MSFT,JPM). "
                    "Omit to auto-discover from EDGAR_FORM4_TICKERS env var "
                    "or parent QuantOpsAI segment universes.")
@click.option("--max-age-days", type=int, default=90)
@click.option("--verbose", is_flag=True)
def daily(tickers, max_age_days, verbose):
    """Refresh Form 4 data for a set of tickers (called from
    altdata/run-altdata-daily.sh)."""
    _setup_logging(verbose)
    if tickers:
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    else:
        ticker_list = _discover_active_tickers()
    if not ticker_list:
        click.echo(
            "No tickers provided and auto-discovery returned empty. "
            "Provide --tickers AAPL,MSFT,... OR set EDGAR_FORM4_TICKERS env var.",
            err=True,
        )
        sys.exit(1)
    click.echo(f"Daily refresh on {len(ticker_list)} tickers…")
    session = EdgarSession()
    with connect() as conn:
        # Pre-filter known-no-CIK tickers (ETFs, delisted, foreign).
        # Pre-2026-05-17 these were 63/525 "ticker error(s)" every
        # day for a stable population. Now we recompute the no-CIK
        # set against the current SEC mapping and skip them quietly.
        # If any previously-no-CIK ticker NOW has a CIK (rare but
        # possible — re-listing, SEC catalog update), it's removed
        # from no_cik_tickers so it gets included.
        from .store import (
            cik_for_ticker, mark_no_cik, is_known_no_cik,
            clear_known_no_cik,
        )
        tickers_to_scrape = []
        newly_no_cik = []
        for tk in ticker_list:
            cik = cik_for_ticker(conn, tk)
            if cik:
                # Has CIK — if it was previously no-CIK, clear that.
                if is_known_no_cik(conn, tk):
                    clear_known_no_cik(conn, tk)
                tickers_to_scrape.append(tk)
            else:
                if not is_known_no_cik(conn, tk):
                    newly_no_cik.append(tk)
                mark_no_cik(
                    conn, tk,
                    reason="not in SEC company_tickers.json",
                )
        conn.commit()
        n_skipped = len(ticker_list) - len(tickers_to_scrape)
        if n_skipped:
            click.echo(
                f"  Skipping {n_skipped} known-no-CIK tickers "
                f"(ETFs, delisted, foreign). See `runs` for the "
                f"full list."
            )
        if newly_no_cik:
            click.echo(
                f"  NEW no-CIK tickers this run ({len(newly_no_cik)}): "
                f"{', '.join(newly_no_cik[:10])}"
                + (f", +{len(newly_no_cik)-10} more" if len(newly_no_cik) > 10 else "")
            )
        run_id = start_run(
            conn, f"daily:{len(tickers_to_scrape)} of "
                  f"{len(ticker_list)} tickers ({n_skipped} no-CIK)",
        )
        try:
            results = scrape_universe(
                session, conn, tickers_to_scrape,
                max_age_days=max_age_days,
            )
            total_filings = sum(r.get("filings_seen", 0) for r in results)
            total_txns = sum(r.get("txns_inserted", 0) for r in results)
            errors = [r for r in results if r.get("error")]
            # Persist per-item error detail as JSON in the error
            # column so the /issues page (and `runs` CLI) can show
            # WHICH tickers failed and WHY. Pre-2026-05-16 only a
            # count was stored ("63 ticker error(s)") — the per-item
            # text was printed to stderr (lost when cron captures
            # only stdout). Format is backward-compatible: readers
            # that expect plain text still see the leading summary
            # via the `summary` field; readers that decode JSON
            # get the full list in `items`.
            import json
            error_payload = None
            if errors:
                error_payload = json.dumps({
                    "summary": f"{len(errors)} ticker error(s)",
                    "items": [
                        {"label": r.get("ticker", "?"),
                         "reason": r.get("error", "?")}
                        for r in errors
                    ],
                })
            finish_run(
                conn, run_id,
                "ok" if not errors else "ok_with_errors",
                rows_inserted=total_txns,
                rows_seen=total_filings,
                error=error_payload,
            )
            click.echo(
                f"✓ Daily refresh: {len(ticker_list)} tickers, "
                f"{total_filings} filings seen, {total_txns} new txns, "
                f"{len(errors)} errors"
            )
            for r in errors:
                click.echo(f"  - {r['ticker']}: {r['error']}", err=True)
        except Exception as exc:
            finish_run(conn, run_id, "failed", error=str(exc))
            click.echo(f"✗ daily refresh failed: {exc}", err=True)
            sys.exit(1)


@cli.command()
@click.option("--ticker", required=True)
@click.option("--lookback-days", type=int, default=90)
def show(ticker, lookback_days):
    """Show the aggregate insider-activity summary for a ticker."""
    with connect() as conn:
        data = get_recent_insider_activity(
            conn, ticker, lookback_days=lookback_days,
        )
    click.echo(f"{ticker} insider activity (last {lookback_days}d):")
    click.echo(f"  Recent buys:        {data['recent_buys']}")
    click.echo(f"  Recent sells:       {data['recent_sells']}")
    click.echo(f"  Net direction:      {data['net_direction']}")
    click.echo(f"  Total buy value:    ${data['total_buy_value']:,.2f}")
    click.echo(f"  Total sell value:   ${data['total_sell_value']:,.2f}")
    click.echo(f"  Cluster (14d):      {data['cluster_count']} distinct buyers")
    if data["notable"]:
        click.echo(f"  Notable:            {data['notable']}")


@cli.command()
@click.option("--days", type=int, default=30)
def counts(days):
    """Filings ingested per day for the last N days."""
    with connect() as conn:
        rows = counts_by_date(conn, days=days)
    if not rows:
        click.echo("(no filings ingested)")
        return
    for r in rows:
        click.echo(f"  {r['d']}: {r['n']} filings")


@cli.command()
@click.option("--limit", type=int, default=20)
def runs(limit):
    """Recent scrape-run history (for observability)."""
    with connect() as conn:
        rows = recent_runs(conn, limit=limit)
    if not rows:
        click.echo("(no runs yet)")
        return
    for r in rows:
        click.echo(
            f"  {r['started_at']}  {r['source']:35}  "
            f"{r['status']:18} inserted={r['rows_inserted']}  "
            f"seen={r['rows_seen']}"
            + (f"  ERR={r['error']}" if r.get("error") else "")
        )


@cli.command(name="no-cik")
def no_cik():
    """Show all tickers in the no_cik_tickers table (ETFs, delisted,
    foreign listings). The daily refresh skips these quietly instead
    of generating a 'ticker error' every day."""
    from .store import list_known_no_cik
    with connect() as conn:
        rows = list_known_no_cik(conn)
    if not rows:
        click.echo("(no tickers flagged no-CIK)")
        return
    click.echo(f"{len(rows)} tickers with no SEC CIK mapping:")
    for r in rows:
        click.echo(
            f"  {r['ticker']:8}  first_seen={r['first_seen_at']}  "
            f"last_checked={r['last_checked_at']}  "
            f"reason={r.get('reason') or '(unspecified)'}"
        )


if __name__ == "__main__":
    cli()
