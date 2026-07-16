"""Step Zero orchestration: run all 5 checks, render findings, decide GO/STOP.

Verdict is STOP iff any check trips AUTH_REQUIRED. Everything else --
PARTIAL/FAIL on checks 2-5 -- is a documented limitation for later phases,
never the specific gate the spec reserves for authentication.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from kalshi_mt.api.http import TokenBucket
from kalshi_mt.api.kalshi import KalshiClient
from kalshi_mt.stepzero.checks import (
    CheckResult,
    _http_status_evidence,
    check_2021_2022_positive,
    check_quote_availability,
    check_taker_field_population,
    check_timestamp_bracketing,
    check_unauthenticated_access,
)
from kalshi_mt.util import PROJECT_ROOT, now_utc_iso

log = logging.getLogger(__name__)

PLACEHOLDER_MAP = {
    1: "Unauthenticated access to required Kalshi endpoints",
    2: "2021-2022 historical trade-tape depth (positive check, not inferred from a 200 response)",
    3: "taker_outcome_side/taker_book_side population vs legacy taker_side",
    4: "/trades min_ts/max_ts timestamp bracketing support",
    5: "Historical bid/ask (yes_bid/yes_ask) availability for the spread<=20c filter",
}


@dataclass
class StepZeroReport:
    ts: str
    base_url: str
    verdict: str  # "GO" | "STOP"
    checks: list[CheckResult] = field(default_factory=list)
    stop_reason: str | None = None


async def _safe_check(coro, check_id: int, check_name: str) -> CheckResult:
    """Runs one check, converting any unexpected 401/403 mid-check into
    AUTH_REQUIRED (the hard gate) rather than letting it crash the run."""
    try:
        return await coro
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            return CheckResult(
                check_id, check_name, "AUTH_REQUIRED", _http_status_evidence(exc),
                "Unexpected auth requirement encountered mid-check.",
            )
        return CheckResult(check_id, check_name, "FAIL", _http_status_evidence(exc),
                            f"Unhandled HTTP error: {exc}")
    except Exception as exc:
        return CheckResult(check_id, check_name, "FAIL", {"error": str(exc)}, f"Unhandled error: {exc}")


async def run_step_zero(
    config: dict[str, Any], client: KalshiClient | None = None
) -> StepZeroReport:
    base_url = config["kalshi"]["base_url"]
    owns_client = client is None
    if client is None:
        bucket = TokenBucket(
            rate=config["kalshi"]["rate_limit"]["requests_per_second"],
            burst=config["kalshi"]["rate_limit"]["burst"],
        )
        client = KalshiClient(bucket, base_url=base_url)

    checks: list[CheckResult] = []

    def _stop(c: CheckResult) -> StepZeroReport:
        return StepZeroReport(now_utc_iso(), base_url, "STOP", checks, f"{c.name}: {c.notes}")

    try:
        c1 = await check_unauthenticated_access(client)
        checks.append(c1)
        if c1.status == "AUTH_REQUIRED":
            return _stop(c1)

        c2 = await _safe_check(check_2021_2022_positive(client), 2, "2021-2022 positive-check")
        checks.append(c2)
        if c2.status == "AUTH_REQUIRED":
            return _stop(c2)

        era_2021_2022_tickers = [
            m["ticker"] for m in c2.evidence.get("markets", []) if m.get("ok")
        ]

        c3 = await _safe_check(
            check_taker_field_population(client, era_2021_2022_tickers), 3, "Taker-field population"
        )
        checks.append(c3)
        if c3.status == "AUTH_REQUIRED":
            return _stop(c3)

        eras3 = c3.evidence.get("eras", {})
        probe_ticker = era_2021_2022_tickers[0] if era_2021_2022_tickers else eras3.get("2023", {}).get("ticker")
        if probe_ticker:
            c4 = await _safe_check(
                check_timestamp_bracketing(client, probe_ticker), 4, "/trades timestamp bracketing"
            )
        else:
            c4 = CheckResult(4, "/trades timestamp bracketing", "FAIL", {}, "No probe ticker available.")
        checks.append(c4)
        if c4.status == "AUTH_REQUIRED":
            return _stop(c4)

        era_tickers = {
            "2021-2022": era_2021_2022_tickers[0] if era_2021_2022_tickers else None,
            "2023": eras3.get("2023", {}).get("ticker"),
            "2024": eras3.get("2024", {}).get("ticker"),
            "2025-jan-apr": eras3.get("2025-jan-apr", {}).get("ticker"),
        }
        c5 = await _safe_check(
            check_quote_availability(client, era_tickers), 5, "Quote availability for spread filter"
        )
        checks.append(c5)
        if c5.status == "AUTH_REQUIRED":
            return _stop(c5)
    finally:
        if owns_client:
            await client.aclose()

    return StepZeroReport(now_utc_iso(), base_url, "GO", checks, None)


def render_findings_markdown(report: StepZeroReport) -> str:
    lines: list[str] = []

    if report.verdict == "STOP":
        lines += [
            "=" * 70,
            "STOP -- OPERATOR DECISION REQUIRED".center(70),
            "=" * 70,
            "",
            f"**Reason:** {report.stop_reason}",
            "",
            "A required Kalshi endpoint demanded authentication. This repository "
            "registers nothing on its own -- see `.env.example` and "
            "`tests/test_scope.py`. Read the check evidence below, then make a "
            "deliberate decision about whether to register for API access.",
            "",
        ]

    lines += [
        f"# Step Zero findings -- {report.ts}",
        "",
        f"- Base URL: `{report.base_url}`",
        f"- Verdict: **{report.verdict}**",
        "",
    ]

    for c in report.checks:
        lines += [
            f"## Check {c.id}: {c.name} -- {c.status}",
            "",
            c.notes,
            "",
            "```json",
            json.dumps(c.evidence, indent=2, default=str),
            "```",
            "",
        ]

    lines += ["## Placeholder inventory resolved", "", "| Spec `[VERIFY]` item | Check | Status |", "|---|---|---|"]
    for c in report.checks:
        item = PLACEHOLDER_MAP.get(c.id, c.name)
        lines.append(f"| {item} | Check {c.id} | {c.status} |")
    lines.append("")

    return "\n".join(lines)


def _report_to_dict(report: StepZeroReport) -> dict[str, Any]:
    return {
        "ts": report.ts,
        "base_url": report.base_url,
        "verdict": report.verdict,
        "stop_reason": report.stop_reason,
        "checks": [
            {"id": c.id, "name": c.name, "status": c.status, "evidence": c.evidence, "notes": c.notes}
            for c in report.checks
        ],
    }


def write_findings(report: StepZeroReport, reports_dir: Path | None = None) -> Path:
    reports_dir = reports_dir or (PROJECT_ROOT / "reports" / "step_zero")
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / "findings.md"
    json_path = reports_dir / "findings.json"
    md_path.write_text(render_findings_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(_report_to_dict(report), indent=2, default=str), encoding="utf-8")
    return md_path
