"""R1 price panel construction (spec S1): turns Pass 1's raw boundary-tick
fetches (the `price_panel` SQLite table) into the two panels the rest of R1
consumes -- Yes-only (the regression basis) and doubled Yes+No (the
descriptive-statistics basis, spec's own basis-tagging rule).

The 11-row-per-market boundary-tick FETCH itself (last trade on close day +
up to 10 prior ET-calendar-day lookbacks, skip-no-backfill) is Pass 1's job
(fetch/pass1.py's fetch_price_panel -- it needs live API access and the ET
timezone arithmetic lives there). This module makes no API calls; it only
reads what Pass 1 already wrote and shapes it.
"""

from __future__ import annotations

from typing import Any

import polars as pl

PANEL_SCHEMA = {
    "ticker": pl.String,
    "event_ticker": pl.String,  # the event-clustering key (spec S4's clustering unit)
    "lookback_day": pl.Int64,
    "category": pl.String,
    "close_time_epoch": pl.Int64,
    "side": pl.String,  # 'yes' | 'no'
    "y": pl.Float64,    # realized outcome for this side, 0.0 or 1.0
    "p": pl.Float64,    # market probability for this side, (0, 1)
    "source": pl.String,  # 'live' | 'historical' -- which endpoint family answered
}


def build_yes_only_panel(conn, in_scope_tickers: set[str]) -> pl.DataFrame:
    """One row per (ticker, lookback_day) price-panel observation, Yes-only
    basis -- spec S1's regression n (156,986 in BDW's own reproduction
    target). Requires a resolved result (yes/no); a market still pending or
    disputed-without-resolution contributes no rows, same as BDW's own
    resolved-markets-only construction."""
    if not in_scope_tickers:
        return pl.DataFrame(schema=PANEL_SCHEMA)

    rows = conn.execute(
        """
        SELECT p.ticker, p.lookback_day, p.yes_price_dollars, p.source,
               m.result, m.close_time_epoch, m.category, m.event_ticker
        FROM price_panel p
        JOIN markets m ON m.ticker = p.ticker
        WHERE m.result IN ('yes', 'no')
        """
    ).fetchall()

    records: list[dict[str, Any]] = []
    for r in rows:
        if r["ticker"] not in in_scope_tickers or r["yes_price_dollars"] is None:
            continue
        records.append({
            "ticker": r["ticker"], "event_ticker": r["event_ticker"],
            "lookback_day": r["lookback_day"], "category": r["category"],
            "close_time_epoch": r["close_time_epoch"], "side": "yes",
            "y": 1.0 if r["result"] == "yes" else 0.0, "p": r["yes_price_dollars"],
            "source": r["source"],
        })
    return pl.DataFrame(records, schema=PANEL_SCHEMA) if records else pl.DataFrame(schema=PANEL_SCHEMA)


def build_doubled_panel(yes_only: pl.DataFrame) -> pl.DataFrame:
    """Yes-only panel plus its complementary No-side rows (No price = 1 -
    Yes price, No outcome = 1 - Yes outcome) -- spec's doubled basis for
    descriptive statistics (win-rate curve, tail-bin counts, maker/taker
    split). Never the MZ regression's own input -- that stays Yes-only."""
    if yes_only.is_empty():
        return yes_only
    no_side = yes_only.with_columns([
        pl.lit("no").alias("side"),
        (1.0 - pl.col("y")).alias("y"),
        (1.0 - pl.col("p")).alias("p"),
    ])
    return pl.concat([yes_only, no_side], how="vertical")


def price_band(p: float) -> str:
    """10c band label, e.g. '1-10c', '90-99c' -- BDW's own Fig 3/5 binning.
    Prices are probabilities in (0,1); bands follow their 1-indexed cent
    convention (a price of exactly 0.10 falls in the 1-10c band, matching
    "contracts <=10c" language, not a fresh 10-20c band)."""
    cents = p * 100.0
    if cents <= 10:
        return "1-10c"
    if cents <= 20:
        return "11-20c"
    if cents <= 30:
        return "21-30c"
    if cents <= 40:
        return "31-40c"
    if cents <= 50:
        return "41-50c"
    if cents <= 60:
        return "51-60c"
    if cents <= 70:
        return "61-70c"
    if cents <= 80:
        return "71-80c"
    if cents <= 90:
        return "81-90c"
    return "91-99c"


def basis_counts(yes_only: pl.DataFrame, doubled: pl.DataFrame) -> dict[str, int]:
    """The basis-tagging invariant, checkable at build time: doubled count
    must be exactly 2x the Yes-only count (spec S1: 156,986 -> 313,972)."""
    return {
        "yes_only_n": len(yes_only),
        "doubled_n": len(doubled),
        "doubled_equals_2x_yes_only": len(doubled) == 2 * len(yes_only),
    }
