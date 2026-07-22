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
    min_volume: float = typer.Option(
        1000.0, help="Skip series/category resolution AND the panel/quote fetch for markets "
                     "below this volume_fp -- matches R1/R2's own $1k filter (fetch/pass2.py's "
                     "MIN_VOLUME_FP). A full live sweep discovers hundreds of thousands of thin "
                     "markets that would never survive Phase 3 anyway (confirmed live, 2026-07: "
                     "576k+ from the live sweep alone); pass 0 to disable and process everything."
    ),
    min_open_hours: float = typer.Option(
        24.0, help="Skip series/category resolution AND the panel/quote fetch for markets open "
                   "fewer than this many hours -- matches R1/R2's own hourly-reset exclusion "
                   "(fetch/pass2.py's MIN_OPEN_SECONDS). Confirmed live, 2026-07-21: of ~2.56M "
                   "markets clearing $1k volume, only ~510k also clear 24h; the rest are "
                   "hourly-reset crypto/index sub-markets no downstream phase uses. Pass 0 to "
                   "disable and process every volume-qualifying market regardless of duration."
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
    min_volume_fp = None if min_volume <= 0 else min_volume
    min_open_duration_s = None if min_open_hours <= 0 else min_open_hours * 3600.0

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
                min_volume_fp=min_volume_fp, min_open_duration_s=min_open_duration_s,
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
    from kalshi_mt.r1.reconcile import (
        compute_calendar_2024_mix,
        coverage_gap_breakdown,
        reconcile_counts,
        write_frozen_2024_mix,
    )
    from kalshi_mt.store import db
    from kalshi_mt.store.parquet import TradeStore
    from kalshi_mt.util import PROJECT_ROOT

    config = load_config()
    conn = db.connect(config["storage"]["db_path"])
    try:
        trade_store = TradeStore(config["storage"]["parquet_dir"])
        # The TRUE $1k dollar-notional gate (2026-07-21 audit -- volume_fp
        # is a contract count, not dollars); computed from Pass 2's full
        # trade tape, not the cheap volume_fp proxy Pass 1/2 use only to
        # SCOPE which markets get an expensive fetch in the first place.
        dollar_volume_by_ticker = trade_store.dollar_volume_by_ticker()
        filter_summary = apply_and_log(conn, window="r1", dollar_volume_by_ticker=dollar_volume_by_ticker)
        in_scope = {
            r[0] for r in conn.execute(
                "SELECT ticker FROM markets m WHERE m.in_r1_window = 1 "
                "AND m.ticker NOT IN (SELECT ticker FROM universe_log WHERE window = 'r1')"
            ).fetchall()
        }

        yes_only = build_yes_only_panel(conn, in_scope)
        doubled = build_doubled_panel(yes_only)
        reconciliation = reconcile_counts(conn, yes_only, doubled)
        gap_breakdown = coverage_gap_breakdown(conn, window="r1")

        mix = compute_calendar_2024_mix(yes_only)
        mix_path = write_frozen_2024_mix(mix, PROJECT_ROOT / "data" / "frozen_2024_mix.json")

        result = {
            "filters": filter_summary,
            "panel": basis_counts(yes_only, doubled),
            "reconciliation": reconciliation["deltas"],
            "coverage_gap_breakdown": gap_breakdown,
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
    from kalshi_mt.r1.field_population import field_population_by_era
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
        field_population = field_population_by_era(trades)

        log_path = write_divergence_log(
            {
                "by_year_psi": by_year, "by_category_psi": by_category,
                "taker_field_population_by_era": field_population,
            },
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
            "taker_field_population_by_era": field_population,
            "divergence_log": str(log_path),
        }
    finally:
        conn.close()

    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def r2() -> None:
    """R2 pooled category-interacted regression, composition decomposition,
    verdict binding, and horizon robustness (Phase 7, docs/analysis_plan.md
    S2). Refuses to run without R1's frozen calendar-2024 category-mix
    artifact (data/frozen_2024_mix.json) -- Phase 7's own hard dependency,
    never recomputed from R2 data (spec's pre-registration firewall)."""
    from kalshi_mt.r1.filters import apply_and_log
    from kalshi_mt.r1.panel import build_yes_only_panel
    from kalshi_mt.r1.reconcile import load_frozen_2024_mix
    from kalshi_mt.r1.regression import fit_mz_regression
    from kalshi_mt.r2.decomposition import category_weights_from_panel, decompose, delta_bar_with_ci
    from kalshi_mt.r2.horizon import run_horizon_robustness
    from kalshi_mt.r2.regression import fit_all_categories
    from kalshi_mt.r2.report import build_r2_report, load_r2_report, write_r2_report
    from kalshi_mt.r2.verdicts import determine_verdict
    from kalshi_mt.store import db
    from kalshi_mt.store.parquet import TradeStore
    from kalshi_mt.util import PROJECT_ROOT

    config = load_config()
    mix_path = PROJECT_ROOT / "data" / "frozen_2024_mix.json"
    try:
        frozen_2024_mix = load_frozen_2024_mix(mix_path)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    conn = db.connect(config["storage"]["db_path"])
    try:
        # R2 reuses R1's own filter thresholds (volume/spread/duration/
        # settlement-mismatch), applied to the R2 window -- r1/filters.py's
        # apply_and_log is idempotent (re-running just re-derives the same
        # exclusions), so calling it here rather than requiring a separate
        # `kmt build --window r2` step keeps `kmt r2` runnable on its own.
        trade_store = TradeStore(config["storage"]["parquet_dir"])
        dollar_volume_by_ticker = trade_store.dollar_volume_by_ticker()
        r2_filter_summary = apply_and_log(
            conn, window="r2", dollar_volume_by_ticker=dollar_volume_by_ticker
        )
        r1_scope = {
            r[0] for r in conn.execute(
                "SELECT ticker FROM markets m WHERE m.in_r1_window = 1 "
                "AND m.ticker NOT IN (SELECT ticker FROM universe_log WHERE window = 'r1')"
            ).fetchall()
        }
        r2_scope = {
            r[0] for r in conn.execute(
                "SELECT ticker FROM markets m WHERE m.in_r2_window = 1 "
                "AND m.ticker NOT IN (SELECT ticker FROM universe_log WHERE window = 'r2')"
            ).fetchall()
        }
        r1_panel = build_yes_only_panel(conn, r1_scope)
        r2_panel = build_yes_only_panel(conn, r2_scope)
        # The boundary-interacted regression (r2/regression.py) needs data
        # on BOTH sides of the fee boundary to estimate delta_fee at all --
        # R2's own window STARTS exactly at 2025-05-01 (the fee boundary),
        # so an R2-only panel would make the fee dummy constant (always 1)
        # and the design matrix singular. R1 union R2 gives every category
        # a genuine pre-boundary baseline (alpha_c/psi_c, per
        # r2/decomposition.py's own docstring: "psi_bar_c is category c's
        # own baseline (pre-boundary) slope") plus both boundary shifts in
        # one fit -- this pooled scope is ONLY the regression's input;
        # category_weights_from_panel below stays R2-window-only, per spec
        # S2.3's own "R2-window weight" definition.
        pooled_panel = build_yes_only_panel(conn, r1_scope | r2_scope)
    finally:
        conn.close()

    r1_fit = fit_mz_regression(r1_panel)
    psi_bar_r1 = r1_fit.psi if r1_fit is not None else None

    category_fits = fit_all_categories(pooled_panel)
    r2_weights = category_weights_from_panel(r2_panel)

    decomposition = {
        "fee": decompose(category_fits, frozen_2024_mix, r2_weights, boundary="fee"),
        "publication": decompose(category_fits, frozen_2024_mix, r2_weights, boundary="publication"),
    }

    delta_bar = {
        "fee": delta_bar_with_ci(category_fits, frozen_2024_mix, boundary="fee"),
        "publication": delta_bar_with_ci(category_fits, frozen_2024_mix, boundary="publication"),
    }
    verdict = {}
    for boundary, estimate in delta_bar.items():
        if psi_bar_r1 is not None and estimate is not None:
            verdict[boundary] = determine_verdict(estimate, psi_bar_r1)
        else:
            verdict[boundary] = None

    horizon = run_horizon_robustness(pooled_panel, frozen_2024_mix)

    report = build_r2_report(
        r2_filters=r2_filter_summary, psi_bar_r1=psi_bar_r1,
        r1_panel_n=len(r1_panel), r2_panel_n=len(r2_panel), pooled_panel_n=len(pooled_panel),
        categories_fit=sorted(category_fits.keys()), delta_bar=delta_bar, verdict=verdict,
        decomposition=decomposition, horizon=horizon,
    )
    lock_path = write_r2_report(report, PROJECT_ROOT / "reports" / "r2" / "verdict_lock.json")
    # Re-read the persisted payload (rather than re-stamping a second
    # locked_ts here) so stdout shows exactly what was written to disk --
    # the single locked_ts write_r2_report already made, not a second,
    # slightly later clock call.
    locked_report = load_r2_report(lock_path)
    locked_report["locked_artifact"] = str(lock_path)

    typer.echo(json.dumps(locked_report, indent=2, default=str))


@app.command(name="r3-check")
def r3_check() -> None:
    """R3's firewall gate (Phase 9, docs/analysis_plan.md S4): reports
    whether R3 code may proceed -- R2's verdict must already be locked to
    disk, and nothing in the rest of the repo may import kalshi_mt.r3.
    This command performs no R3 analysis itself (none exists yet); it only
    checks the gate."""
    from kalshi_mt.r3.firewall import R3FirewallError, check_no_r3_imports_outside_r3, require_r2_locked

    import_violations = check_no_r3_imports_outside_r3()
    try:
        r2_report = require_r2_locked()
        locked_ok = True
        locked_error = None
    except R3FirewallError as exc:
        r2_report = None
        locked_ok = False
        locked_error = str(exc)

    result = {
        "r2_locked": locked_ok,
        "r2_locked_ts": r2_report.get("locked_ts") if r2_report else None,
        "r2_verdict": r2_report.get("verdict") if r2_report else None,
        "import_violations": import_violations,
        "firewall_clear": locked_ok and not import_violations,
    }
    if locked_error:
        result["locked_error"] = locked_error

    typer.echo(json.dumps(result, indent=2, default=str))
    if not result["firewall_clear"]:
        raise typer.Exit(code=1)


def _sourced_maker_rate(fee_schedule: dict) -> float | None:
    maker_default_rows = [
        r for r in fee_schedule.get("schedule", [])
        if r.get("role") == "maker" and r.get("category") == "default"
    ]
    if not maker_default_rows:
        return None
    return float(max(maker_default_rows, key=lambda r: r["effective_from"])["rate"])


def _maker_rate_schedule(base_schedule: dict, maker_rate: float) -> dict:
    """A copy of base_schedule with every post-2025-05-01 maker/default
    row's rate replaced by `maker_rate` -- sweeps the [VERIFY]-flagged
    sourced maker rate (data/fees.yaml's own header) for the
    fee-sensitivity ribbon (S3.3), leaving the taker rate (well-sourced,
    no known revision) and the pre-boundary maker row (0.0, definitional --
    "Kalshi began to charge fees on Makers after April 2025") untouched."""
    import copy

    schedule = copy.deepcopy(base_schedule)
    for row in schedule.get("schedule", []):
        if row.get("role") == "maker" and row.get("effective_from", "") >= "2025-05-01":
            row["rate"] = maker_rate
    return schedule


def _compute_escalation(config: dict) -> dict:
    """Shared by `kmt escalate` and `kmt report`: loads R2's locked
    verdict, computes the maker >=50c margin and its fee-sensitivity
    ribbon on R2-window trades, and runs the S5 escalation determination.
    Returns a dict bundling everything both commands need -- callers
    should not duplicate this assembly."""
    from kalshi_mt.fees.ribbon import compute_ribbon, default_fee_grid
    from kalshi_mt.fees.schedule import load_fee_schedule
    from kalshi_mt.r1.filters import apply_and_log
    from kalshi_mt.r2.maker_margin import compute_maker_margin_ge_50c
    from kalshi_mt.r2.report import load_r2_report
    from kalshi_mt.r2.verdicts import DeltaBarEstimate
    from kalshi_mt.report.escalation import determine_escalation
    from kalshi_mt.store import db
    from kalshi_mt.store.parquet import TradeStore
    from kalshi_mt.util import PROJECT_ROOT

    r2_report = load_r2_report(PROJECT_ROOT / "reports" / "r2" / "verdict_lock.json")

    fee_bar = r2_report.get("delta_bar", {}).get("fee")
    pub_bar = r2_report.get("delta_bar", {}).get("publication")
    delta_bar_fee = DeltaBarEstimate(**fee_bar) if fee_bar else None
    delta_bar_pub = DeltaBarEstimate(**pub_bar) if pub_bar else None

    fee_schedule = load_fee_schedule()
    trade_store = TradeStore(config["storage"]["parquet_dir"])
    conn = db.connect(config["storage"]["db_path"])
    try:
        dollar_volume_by_ticker = trade_store.dollar_volume_by_ticker()
        apply_and_log(conn, window="r2", dollar_volume_by_ticker=dollar_volume_by_ticker)
        r2_scope = {
            r[0] for r in conn.execute(
                "SELECT ticker FROM markets m WHERE m.in_r2_window = 1 "
                "AND m.ticker NOT IN (SELECT ticker FROM universe_log WHERE window = 'r2')"
            ).fetchall()
        }
        resolutions, categories = {}, {}
        if r2_scope:
            placeholders = ",".join("?" * len(r2_scope))
            for row in conn.execute(
                f"SELECT ticker, result, category FROM markets WHERE ticker IN ({placeholders})",
                list(r2_scope),
            ).fetchall():
                resolutions[row["ticker"]] = row["result"]
                categories[row["ticker"]] = row["category"]
    finally:
        conn.close()

    trades = trade_store.read_all()
    maker_margin = compute_maker_margin_ge_50c(trades, resolutions, categories, fee_schedule, r2_scope)

    ribbon = None
    sourced_rate = _sourced_maker_rate(fee_schedule)
    if sourced_rate is not None and maker_margin.n_maker_b > 0 and maker_margin.n_taker_b > 0:
        def _margin_fn(rate: float) -> float:
            synthetic = _maker_rate_schedule(fee_schedule, rate)
            swept = compute_maker_margin_ge_50c(trades, resolutions, categories, synthetic, r2_scope)
            return swept.layer_b if swept.layer_b is not None else 0.0

        ribbon = compute_ribbon(_margin_fn, default_fee_grid(sourced_rate))

    escalation = determine_escalation(
        delta_bar_fee=delta_bar_fee, delta_bar_pub=delta_bar_pub,
        maker_margin_layer_a=maker_margin.layer_a, maker_margin_layer_c=maker_margin.layer_c,
        ribbon=ribbon,
    )
    return {
        "r2_report": r2_report, "maker_margin": maker_margin, "ribbon": ribbon, "escalation": escalation,
    }


@app.command()
def escalate() -> None:
    """S5 escalation determination: does R2 (plus the maker >=50c margin's
    fee-sensitivity ribbon) trigger escalation from a replication note to
    a standalone short paper? Refuses to run without R2's locked verdict
    artifact (same dependency `kmt r2` writes for `kmt r3-check`)."""
    from dataclasses import asdict

    config = load_config()
    try:
        ctx = _compute_escalation(config)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    result = {
        "maker_margin": asdict(ctx["maker_margin"]),
        "ribbon": asdict(ctx["ribbon"]) if ctx["ribbon"] is not None else None,
        "escalation": asdict(ctx["escalation"]),
    }
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command(name="report")
def final_report() -> None:
    """Assembles the final note-format (or standalone-short-paper, if
    escalated) draft, pulling together R1, R2, the maker-margin ribbon,
    the Polymarket control venue (if its bootstrap files have been
    downloaded), and the S5 escalation determination. Writes
    reports/final/draft.md."""
    from kalshi_mt.control.polymarket import CAVEATS, COVERAGE_GAP_STATEMENT, build_polymarket_panel, monthly_psi_path
    from kalshi_mt.report.final import build_final_report_markdown, write_final_report
    from kalshi_mt.util import PROJECT_ROOT

    config = load_config()
    try:
        ctx = _compute_escalation(config)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    bootstrap_dir = PROJECT_ROOT / "data" / "bootstrap"
    quant_path, markets_path = bootstrap_dir / "quant.parquet", bootstrap_dir / "markets.parquet"
    control_monthly_psi = None
    if quant_path.exists() and markets_path.exists():
        panel = build_polymarket_panel(quant_path, markets_path)
        control_monthly_psi = [
            {"month": r.month, "result": None if r.result is None else {
                "psi": r.result.psi, "n": r.result.n, "n_clusters": r.result.n_clusters,
            }}
            for r in monthly_psi_path(panel)
        ]

    divergence_log_path = PROJECT_ROOT / "reports" / "r1" / "divergence_log.md"

    markdown = build_final_report_markdown(
        r2_report=ctx["r2_report"], escalation=ctx["escalation"],
        maker_margin=ctx["maker_margin"], ribbon=ctx["ribbon"],
        control_monthly_psi=control_monthly_psi, control_caveats=CAVEATS,
        control_coverage_gap_statement=COVERAGE_GAP_STATEMENT,
        r1_divergence_log_path=str(divergence_log_path) if divergence_log_path.exists() else None,
    )
    path = write_final_report(markdown, PROJECT_ROOT / "reports" / "final" / "draft.md")
    typer.secho(f"Final report draft written to {path}", fg=typer.colors.GREEN)
    typer.echo(json.dumps({"venue": ctx["escalation"].escalate and "standalone_short_paper" or "replication_note",
                            "escalate": ctx["escalation"].escalate, "path": str(path)}, indent=2))
