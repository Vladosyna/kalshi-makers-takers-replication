"""Full-tape taker-field population by era, over Pass 2's real trade tape.

Step Zero's Check 3 (stepzero/checks.py) samples ONE representative market
per era via a live API probe to answer "is taker_outcome_side/
taker_book_side populated" -- a fast, cheap, necessarily-thin diagnostic
that must run before any fetch. Three of its four eras (2023, 2024,
2025-jan-apr) returned zero trades for their sampled candidate in practice,
so its reported PASS verdict rests on population rates measured from a
single 2021 weather market's 152 trades, not one sample per era as the
verdict's own wording implies.

This module recomputes the identical three population rates over EVERY
trade Pass 2 has actually fetched into the Parquet tape (store/parquet.py),
bucketed by the same era boundaries -- at zero extra API cost, since the
tape already exists once Pass 2 has run. It turns Check 3's single-market
probe into full-sample evidence for the same era labels, so the two can be
compared directly rather than the probe standing in for eras it never
actually sampled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import polars as pl

from kalshi_mt.util import iso_to_epoch

# Same boundaries as stepzero/checks.py's check_taker_field_population,
# plus a fifth R2-window bucket -- Pass 2 also fetches R2-era trades, and
# there is no reason to leave them out of a full-sample report just because
# Check 3 (a pre-fetch diagnostic) never had R2 data to sample from.
ERA_BOUNDARIES: list[tuple[str, int, int]] = [
    ("2021-2022", int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()),
     int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())),
    ("2023", int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()),
     int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())),
    ("2024", int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()),
     int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())),
    ("2025-jan-apr", int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()),
     int(datetime(2025, 5, 1, tzinfo=timezone.utc).timestamp())),
    ("2025-may-onward", int(datetime(2025, 5, 1, tzinfo=timezone.utc).timestamp()),
     int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())),
]


def _era_label(created_time: str | None) -> str | None:
    epoch = iso_to_epoch(created_time)
    if epoch is None:
        return None
    for label, start, end in ERA_BOUNDARIES:
        if start <= epoch < end:
            return label
    return None


UNASSIGNED_KEY = "_unassigned"


def _bucket_stats(bucket_df: pl.DataFrame) -> dict[str, Any]:
    n = len(bucket_df)
    if n == 0:
        return {"trade_count": 0}
    return {
        "trade_count": n,
        "taker_outcome_side_population": round(bucket_df["taker_outcome_side"].is_not_null().sum() / n, 4),
        "taker_book_side_population": round(bucket_df["taker_book_side"].is_not_null().sum() / n, 4),
        "taker_side_legacy_population": round(bucket_df["taker_side"].is_not_null().sum() / n, 4),
    }


def field_population_by_era(trades: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """`trades` is Pass 2's raw tape (store/parquet.py's TradeStore.read_all()
    or read_for_ticker) -- every column it needs (created_time,
    taker_outcome_side, taker_book_side, taker_side) is already part of
    fetch/pass2.py's own row shape, so this is a pure aggregation with no
    new fetch required. Eras with zero trades in the tape still appear in
    the result (trade_count=0) rather than being omitted, so a reader can
    see which eras Pass 2 simply hasn't reached yet.

    Also always reports a reserved `_unassigned` bucket -- trades whose
    created_time is unparseable, or whose epoch falls outside every span in
    ERA_BOUNDARIES (today: before 2021-01-01 or at/after 2027-01-01) -- so a
    reader can tell "this era genuinely has zero trades" apart from "some
    trades exist but couldn't be placed," rather than both looking like the
    same silent zero (the same absence-must-never-be-ambiguous principle
    this diff's spread-filter fix is built on)."""
    era_labels = [label for label, _, _ in ERA_BOUNDARIES]
    if trades.is_empty():
        return {label: {"trade_count": 0} for label in era_labels + [UNASSIGNED_KEY]}

    df = trades.with_columns(
        pl.col("created_time").map_elements(_era_label, return_dtype=pl.String).alias("era")
    )

    results: dict[str, dict[str, Any]] = {}
    for label in era_labels:
        results[label] = _bucket_stats(df.filter(pl.col("era") == label))
    results[UNASSIGNED_KEY] = _bucket_stats(df.filter(pl.col("era").is_null()))
    return results
