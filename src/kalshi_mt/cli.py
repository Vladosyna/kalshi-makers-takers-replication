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


@app.command()
def fetch(
    pass_: str = typer.Argument(..., metavar="PASS", help="pass1 | pass2"),
) -> None:
    """Two-pass fetch pipeline (Phase 2)."""
    _not_implemented(f"fetch {pass_}", "Phase 2")


@app.command()
def build() -> None:
    """R1 filters, panel construction, count reconciliation (Phase 3)."""
    _not_implemented("build", "Phase 3")


@app.command()
def r1() -> None:
    """R1 MZ regression + reproduction report (Phase 5)."""
    _not_implemented("r1", "Phase 5")


@app.command()
def r2() -> None:
    """R2 pooled regression, decomposition, verdict binding (Phase 7)."""
    _not_implemented("r2", "Phase 7")
