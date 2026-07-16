"""`kmt` CLI skeleton (Phase 0-1).

Commands are wired to their implementations phase by phase; until then each
prints a clear not-implemented notice and exits non-zero so cron jobs fail
loudly rather than silently succeeding.

Exit codes: 0 success, 1 unexpected/hard failure, 2 not implemented,
3 STOP -- operator decision required (Step Zero found a required endpoint
needs authentication).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import typer

from kalshi_mt.util import load_config, setup_logging, use_stable_event_loop

use_stable_event_loop()

_log = logging.getLogger("kalshi_mt.crash")


def _log_uncaught_exception(exc_type, exc_value, exc_tb) -> None:
    """Last-resort handler: any exception gets one guaranteed line -- with
    full traceback -- in data/logs/kmt.jsonl before the process exits."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    _log.critical("uncaught exception -- process is about to exit", exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _log_uncaught_exception

app = typer.Typer(
    name="kmt",
    help="Kalshi Makers & Takers Replication -- read-only research instrument.",
    no_args_is_help=True,
)


def _not_implemented(command: str, phase: str) -> None:
    typer.secho(
        f"`kmt {command}` is not implemented yet (arrives in {phase}).",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


@app.callback()
def main() -> None:
    """Initialize config and logging for every command."""
    from dotenv import load_dotenv

    load_dotenv()
    setup_logging(load_config())


@app.command(name="step-zero")
def step_zero() -> None:
    """Verify Kalshi's public API has what this replication needs -- the hard gate (spec S3)."""
    from kalshi_mt.stepzero.report import run_step_zero, write_findings

    config = load_config()

    async def _run():
        return await run_step_zero(config)

    report = asyncio.run(_run())

    for c in report.checks:
        color = {
            "PASS": typer.colors.GREEN, "PARTIAL": typer.colors.YELLOW,
            "FAIL": typer.colors.RED, "AUTH_REQUIRED": typer.colors.RED,
        }[c.status]
        typer.secho(f"Check {c.id} [{c.status}]: {c.name}", fg=color)

    md_path = write_findings(report)

    if report.verdict == "STOP":
        typer.secho("", err=True)
        typer.secho("=" * 70, fg=typer.colors.RED, bold=True, err=True)
        typer.secho("STOP -- OPERATOR DECISION REQUIRED".center(70), fg=typer.colors.RED, bold=True, err=True)
        typer.secho("=" * 70, fg=typer.colors.RED, bold=True, err=True)
        typer.secho(f"Reason: {report.stop_reason}", fg=typer.colors.RED, err=True)
        typer.secho(
            "This repository registers nothing on its own. Read "
            f"{md_path} and .env.example, then make a deliberate decision.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=3)

    typer.secho(f"\nGO -- findings written to {md_path}", fg=typer.colors.GREEN, bold=True)
    raise typer.Exit(code=0)


@app.command()
def status() -> None:
    """Show the last Step Zero verdict and basic config sanity."""
    from pathlib import Path

    from kalshi_mt.util import PROJECT_ROOT

    findings_path = PROJECT_ROOT / "reports" / "step_zero" / "findings.json"
    if not findings_path.exists():
        typer.echo("step-zero: not yet run (no reports/step_zero/findings.json)")
        raise typer.Exit(code=0)

    data = json.loads(findings_path.read_text(encoding="utf-8"))
    typer.echo(f"step-zero: {data['verdict']} at {data['ts']} (base_url={data['base_url']})")
    for c in data["checks"]:
        typer.echo(f"  check {c['id']} [{c['status']}]: {c['name']}")
    if data.get("stop_reason"):
        typer.secho(f"  stop_reason: {data['stop_reason']}", fg=typer.colors.RED)
    raise typer.Exit(code=0)


fetch_app = typer.Typer(help="Two-pass fetch pipeline (spec S3, Phase 2).")
app.add_typer(fetch_app, name="fetch")


@fetch_app.command("pass1")
def fetch_pass1(
    max_series: int | None = typer.Option(
        None, help="Bound how many not-yet-done series the historical scan touches this run "
                    "(omit for no bound -- the full ~12k-series universe, hours-long)."
    ),
    market_limit: int | None = typer.Option(
        None, help="Bound how many markets get a price-panel/quote fetch this run."
    ),
    live_max_pages: int | None = typer.Option(
        None, help="Bound how many cursor pages the live 2023-2026 discovery sweep fetches "
                    "this run (1000 markets/page) -- omit for no bound (tens of thousands of "
                    "markets across the full window; ALWAYS set this for a quick/verification run)."
    ),
    resolve_batch_size: int = typer.Option(
        200, help="Bound how many markets get series_ticker/category resolved "
                   "(one GET /events call per distinct event_ticker, cached) this run."
    ),
) -> None:
    """Universe discovery (live sweep + historical series scan) + boundary-tick
    price panel + closing quotes. Resumable -- safe to re-run; each sub-phase
    picks up where it left off (store/db.py's series_scan_state /
    pass2_progress checkpoints)."""
    from kalshi_mt.api.http import TokenBucket
    from kalshi_mt.api.kalshi import KalshiClient
    from kalshi_mt.fetch.pass1 import run_pass1
    from kalshi_mt.store import db

    config = load_config()

    async def _run():
        bucket = TokenBucket(
            rate=config["kalshi"]["rate_limit"]["requests_per_second"],
            burst=config["kalshi"]["rate_limit"]["burst"],
        )
        client = KalshiClient(bucket, base_url=config["kalshi"]["base_url"])
        conn = db.connect(config["storage"]["db_path"])
        try:
            return await run_pass1(
                client, conn, max_series_this_run=max_series, market_processing_limit=market_limit,
                live_max_pages=live_max_pages, series_resolution_batch_size=resolve_batch_size,
            )
        finally:
            await client.aclose()
            conn.close()

    stats = asyncio.run(_run())
    typer.echo(json.dumps(stats, indent=2, default=str))


@fetch_app.command("pass2")
def fetch_pass2(
    ticker_limit: int | None = typer.Option(
        None, help="Bound how many in-scope markets get a full trade-tape fetch this run."
    ),
    max_pages: int | None = typer.Option(
        None, help="Bound pages fetched per market this run (for a resumable, incremental pass)."
    ),
) -> None:
    """Full trade tape for in-scope (volume/spread/duration-filtered) contracts
    only. Resumable per-market via pass2_progress."""
    from kalshi_mt.api.http import TokenBucket
    from kalshi_mt.api.kalshi import KalshiClient
    from kalshi_mt.fetch.pass2 import run_pass2
    from kalshi_mt.store import db
    from kalshi_mt.store.parquet import TradeStore

    config = load_config()

    async def _run():
        bucket = TokenBucket(
            rate=config["kalshi"]["rate_limit"]["requests_per_second"],
            burst=config["kalshi"]["rate_limit"]["burst"],
        )
        client = KalshiClient(bucket, base_url=config["kalshi"]["base_url"])
        conn = db.connect(config["storage"]["db_path"])
        trade_store = TradeStore(config["storage"]["parquet_dir"])
        try:
            return await run_pass2(
                client, conn, trade_store, ticker_limit=ticker_limit, max_pages_per_market=max_pages
            )
        finally:
            await client.aclose()
            conn.close()

    stats = asyncio.run(_run())
    typer.echo(json.dumps({k: v for k, v in stats.items() if k != "results"}, indent=2, default=str))


@app.command()
def build() -> None:
    """R1 filters, panel construction, count-reconciliation gate, and the
    frozen calendar-2024 category-mix artifact Phase 7 depends on."""
    from kalshi_mt.r1.filters import apply_and_log
    from kalshi_mt.r1.panel import basis_counts, build_doubled_panel, build_yes_only_panel
    from kalshi_mt.r1.reconcile import compute_calendar_2024_mix, reconcile_counts, write_frozen_2024_mix
    from kalshi_mt.store import db
    from kalshi_mt.util import PROJECT_ROOT

    config = load_config()
    conn = db.connect(config["storage"]["db_path"])
    try:
        filter_summary = apply_and_log(conn, window="r1")
        in_scope = {
            r[0] for r in conn.execute(
                "SELECT ticker FROM markets m WHERE m.in_r1_window = 1 "
                "AND m.ticker NOT IN (SELECT ticker FROM universe_log WHERE window = 'r1')"
            ).fetchall()
        }

        yes_only = build_yes_only_panel(conn, in_scope)
        doubled = build_doubled_panel(yes_only)
        reconciliation = reconcile_counts(conn, yes_only, doubled)

        mix = compute_calendar_2024_mix(yes_only)
        mix_path = write_frozen_2024_mix(mix, PROJECT_ROOT / "data" / "frozen_2024_mix.json")

        result = {
            "filters": filter_summary,
            "panel": basis_counts(yes_only, doubled),
            "reconciliation": reconciliation["deltas"],
            "frozen_2024_mix": {"path": str(mix_path), "categories": len(mix)},
        }
    finally:
        conn.close()

    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def r1() -> None:
    """R1 MZ regression + reproduction report: by-year/by-category psi vs
    BDW Tables 8-9, win-rate curve vs Fig 3, returns-by-band vs Fig 5,
    maker/taker split vs Fig 6/Table 10, divergence log."""
    from kalshi_mt.fees.schedule import load_fee_schedule
    from kalshi_mt.r1.panel import build_doubled_panel, build_yes_only_panel
    from kalshi_mt.r1.regression import verify_two_way_equals_one_way_clustering
    from kalshi_mt.r1.reproduction import (
        by_category_psi,
        by_year_psi,
        maker_taker_split,
        returns_by_band,
        win_rate_by_band,
        write_divergence_log,
    )
    from kalshi_mt.store import db
    from kalshi_mt.store.parquet import TradeStore
    from kalshi_mt.util import PROJECT_ROOT

    config = load_config()
    conn = db.connect(config["storage"]["db_path"])
    try:
        in_scope = {
            r[0] for r in conn.execute(
                "SELECT ticker FROM markets m WHERE m.in_r1_window = 1 "
                "AND m.ticker NOT IN (SELECT ticker FROM universe_log WHERE window = 'r1')"
            ).fetchall()
        }
        yes_only = build_yes_only_panel(conn, in_scope)
        doubled = build_doubled_panel(yes_only)
        fee_schedule = load_fee_schedule()

        by_year = by_year_psi(yes_only)
        by_category = by_category_psi(yes_only)
        clustering_check = verify_two_way_equals_one_way_clustering(yes_only)

        resolutions = {
            r[0]: r[1] for r in conn.execute(
                f"SELECT ticker, result FROM markets WHERE ticker IN "
                f"({','.join('?' * len(in_scope))})", list(in_scope)
            ).fetchall()
        } if in_scope else {}
        trade_store = TradeStore(config["storage"]["parquet_dir"])
        trades = trade_store.read_all()
        mt_split = maker_taker_split(trades, resolutions, in_scope)

        log_path = write_divergence_log(
            {"by_year_psi": by_year, "by_category_psi": by_category},
            PROJECT_ROOT / "reports" / "r1" / "divergence_log.md",
        )

        def _fit_summary(entry):
            fit = entry.get("fit")
            out = {k: v for k, v in entry.items() if k != "fit"}
            out["fit"] = None if fit is None else {
                "n": fit.n, "n_clusters": fit.n_clusters, "alpha": fit.alpha,
                "alpha_se": fit.alpha_se, "psi": fit.psi, "psi_se": fit.psi_se,
            }
            return out

        result = {
            "in_scope_markets": len(in_scope),
            "by_year_psi": {y: _fit_summary(e) for y, e in by_year.items()},
            "by_category_psi": {c: _fit_summary(e) for c, e in by_category.items()},
            "clustering_verification": clustering_check,
            "win_rate_by_band": win_rate_by_band(doubled),
            "returns_by_band": returns_by_band(yes_only, fee_schedule),
            "maker_taker_split": mt_split,
            "divergence_log": str(log_path),
        }
    finally:
        conn.close()

    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def r2() -> None:
    """R2 pooled regression, decomposition, verdict binding (Phase 7)."""
    _not_implemented("r2", "Phase 7")
