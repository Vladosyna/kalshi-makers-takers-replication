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


def apply_r1_filters(
    conn, window: str = "r1", dollar_volume_by_ticker: dict[str, float] | None = None,
) -> list[FilterResult]:
    """One row per market in the given window (`in_r1_window` or
    `in_r2_window`) that Pass 1 has reached at least once (has a markets
    row). Same filter thresholds for both windows -- analysis_plan.md S2's
    R2 spec is described as an extension of R1's own construction, with no
    separate filter definition restated, so R2 reuses R1's volume/spread/
    duration/settlement-mismatch criteria unchanged, applied to the R2
    window's markets. A market with no quote row yet fails on
    'spread_filter_not_yet_fetched' (operational -- Pass 1 hasn't attempted
    it); a market whose quote WAS attempted (live+historical) but Kalshi had
    no bid/ask history fails on 'spread_filter_not_computable' (structural --
    won't resolve by fetching more, per Step Zero Check 5's own finding).
    Neither is silently skipped -- incomplete Pass 1 coverage should be
    visible in the reconciliation counts, split by which of the two it is
    (reconcile.py's coverage_gap_breakdown), not swallowed into one bucket.

    A market whose stored `result` is neither 'yes' nor 'no' -- Pass 1's
    live sweep upserts status/result fields that are documented as
    "frequently stale for older markets" (fetch/pass1.py's module
    docstring), and no re-derivation from trade/settlement evidence is
    implemented yet (2026-07-21 audit finding, deferred pending a design
    decision on what "re-derive" means) -- now fails on
    'result_missing_or_invalid' rather than silently passing here only to
    be dropped later, invisibly, by r1/panel.py's `WHERE result IN
    ('yes','no')`. This does not change which contracts end up in the
    final panel; it makes an already-happening drop visible in
    universe_log/reconcile.py's coverage_gap_breakdown instead of an
    unattributed shortfall against BDW's 156,986.

    `dollar_volume_by_ticker` (store/parquet.py's TradeStore.
    dollar_volume_by_ticker()) is the TRUE $1k volume gate (spec S1: dollar
    notional), replacing the earlier proxy that thresholded Kalshi's own
    `volume_fp` -- a CONTRACT COUNT, not dollars (2026-07-21 audit: since
    every trade price is <$1, count>=1000 admits real notional under $1000,
    concentrated in exactly the cheap tail bins the FLB headline depends
    on). A market Pass 2 hasn't finished (no 'done' pass2_progress row)
    fails 'dollar_volume_not_yet_fetched' (operational, mirrors the
    spread_filter split) rather than being silently treated as below
    threshold. Passing None (the default) falls back to the old
    contract-count proxy against `volume_fp` -- an approximation, correct
    only for a lightweight/preview call made before Pass 2 has run; the
    production R1/R2 gate (cli.py's `build` command) always threads the
    real dict through."""
    window_column = {"r1": "in_r1_window", "r2": "in_r2_window"}[window]
    rows = conn.execute(
        f"""
        SELECT m.ticker, m.volume_fp, m.open_time_epoch, m.close_time_epoch, m.result,
               q.spread, (q.ticker IS NOT NULL) AS quote_attempted,
               p.status AS pass2_status
        FROM markets m
        LEFT JOIN quotes q ON q.ticker = m.ticker
        LEFT JOIN pass2_progress p ON p.ticker = m.ticker
        WHERE m.{window_column} = 1
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
        if dollar_volume_by_ticker is None:
            if row["volume_fp"] is None or row["volume_fp"] < MIN_VOLUME_FP:
                reasons.append("volume_below_1000")
        elif row["pass2_status"] != "done":
            reasons.append("dollar_volume_not_yet_fetched")
        elif dollar_volume_by_ticker.get(row["ticker"], 0.0) < MIN_VOLUME_FP:
            reasons.append("volume_below_1000")
        if not row["quote_attempted"]:
            reasons.append("spread_filter_not_yet_fetched")
        elif row["spread"] is None:
            reasons.append("spread_filter_not_computable")
        elif row["spread"] > MAX_SPREAD:
            reasons.append("spread_above_20c")
        if row["open_time_epoch"] is None or row["close_time_epoch"] is None:
            reasons.append("missing_open_or_close_time")
        elif (row["close_time_epoch"] - row["open_time_epoch"]) < MIN_OPEN_SECONDS:
            reasons.append("open_below_24h")
        if row["result"] not in ("yes", "no"):
            reasons.append("result_missing_or_invalid")
        else:
            implied = _implied_side(day0_price.get(row["ticker"]))
            if implied and row["result"] != implied:
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


def apply_and_log(
    conn, window: str = "r1", dollar_volume_by_ticker: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Runs the filters and persists every exclusion to universe_log
    (spec-wide defense against selection-bias claims -- see db.py's own
    universe_log docstring). See apply_r1_filters for
    `dollar_volume_by_ticker`."""
    results = apply_r1_filters(conn, window=window, dollar_volume_by_ticker=dollar_volume_by_ticker)
    exclusions = [
        (r.ticker, code) for r in results if not r.passed for code in r.reason_codes
    ]
    db.log_universe_exclusions(conn, window, exclusions)
    conn.commit()
    return summarize(results)
