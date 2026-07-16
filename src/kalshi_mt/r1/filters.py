"""R1 filters (spec S1): volume>=$1k, spread<=20c, open>=24h, and the
63-mismatch settlement-vs-last-trade check. Operates on markets already
discovered by Pass 1 (store/db.py's markets/quotes/price_panel tables),
restricted to the R1 window (2021-01-01..2025-04-30).

Judgment call, documented rather than silently guessed (spec's own
placeholder: "pin the exact field behind 'separately-reported' during
implementation -- settlement price vs last price"): the 63-mismatch check
compares Kalshi's own authoritative `result` field (categorical yes/no --
Kalshi's settlement determination) against the price panel's closing-day
last-trade price rounded to an implied side (yes_price>=0.5 implies "yes"
will win, else "no"). A mismatch is a contract whose very last trade implied
the opposite of how it actually settled -- exactly BDW's own "dropped for
mismatch" construction, without needing a second endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kalshi_mt.store import db

MIN_VOLUME_FP = 1000.0
MAX_SPREAD = 0.20
MIN_OPEN_SECONDS = 24 * 3600


@dataclass
class FilterResult:
    ticker: str
    passed: bool
    reason_codes: list[str] = field(default_factory=list)


def _implied_side(yes_price: float | None) -> str | None:
    if yes_price is None:
        return None
    return "yes" if yes_price >= 0.5 else "no"


def apply_r1_filters(conn) -> list[FilterResult]:
    """One row per R1-window market that Pass 1 has reached at least once
    (has a markets row). A market with no quote row yet is reported as
    failing on 'no_quote_available' -- not silently skipped -- since
    incomplete Pass 1 coverage should be visible in the reconciliation
    counts, not swallowed."""
    rows = conn.execute(
        """
        SELECT m.ticker, m.volume_fp, m.open_time_epoch, m.close_time_epoch, m.result,
               q.spread
        FROM markets m
        LEFT JOIN quotes q ON q.ticker = m.ticker
        WHERE m.in_r1_window = 1
        """
    ).fetchall()

    day0_price = {
        r["ticker"]: r["yes_price_dollars"]
        for r in conn.execute(
            "SELECT ticker, yes_price_dollars FROM price_panel WHERE lookback_day = 0"
        ).fetchall()
    }

    results = []
    for row in rows:
        reasons = []
        if row["volume_fp"] is None or row["volume_fp"] < MIN_VOLUME_FP:
            reasons.append("volume_below_1000")
        if row["spread"] is None:
            reasons.append("no_quote_available")
        elif row["spread"] > MAX_SPREAD:
            reasons.append("spread_above_20c")
        if row["open_time_epoch"] is None or row["close_time_epoch"] is None:
            reasons.append("missing_open_or_close_time")
        elif (row["close_time_epoch"] - row["open_time_epoch"]) < MIN_OPEN_SECONDS:
            reasons.append("open_below_24h")
        implied = _implied_side(day0_price.get(row["ticker"]))
        if row["result"] and implied and row["result"] != implied:
            reasons.append("settlement_last_trade_mismatch")
        results.append(FilterResult(ticker=row["ticker"], passed=not reasons, reason_codes=reasons))
    return results


def summarize(results: list[FilterResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    reason_counts: dict[str, int] = {}
    for r in results:
        for code in r.reason_codes:
            reason_counts[code] = reason_counts.get(code, 0) + 1
    return {"total": total, "passed": passed, "failed": total - passed, "reason_counts": reason_counts}


def apply_and_log(conn, window: str = "r1") -> dict[str, Any]:
    """Runs the filters and persists every exclusion to universe_log
    (spec-wide defense against selection-bias claims -- see db.py's own
    universe_log docstring)."""
    results = apply_r1_filters(conn)
    exclusions = [
        (r.ticker, code) for r in results if not r.passed for code in r.reason_codes
    ]
    db.log_universe_exclusions(conn, window, exclusions)
    conn.commit()
    return summarize(results)
