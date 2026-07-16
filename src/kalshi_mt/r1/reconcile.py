"""R1 count-reconciliation gate (spec S1/S2) + the frozen calendar-2024
category-mix artifact R2's decomposition depends on (Correction 2 of the
approved implementation plan).

Reconciliation compares our own construction against BDW's pinned integers
BEFORE any estimate comparison -- divergence on overlapping deterministic
data is a coverage/filter question, not a sampling question, so BDW's own
standard errors are never the tolerance here (docs/analysis_plan.md S1).

The frozen-mix artifact is R1-window data (2024 falls entirely inside
2021-01-01..2025-04-30) computed once and persisted; Phase 7 (R2's
composition decomposition) consumes it and never recomputes weights from R2
data -- fixing the weights from a pre-treatment period, frozen to disk
before any R2 estimate, is the pre-registration discipline the paper claims.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from kalshi_mt.util import now_utc_iso

BDW_TARGETS: dict[str, int] = {
    "events": 12_403,
    "yes_contracts": 46_282,
    "yes_prices": 156_986,       # Yes-only basis -- the regression n
    "doubled_prices": 313_972,   # doubled Yes+No basis
    "tail_1_10c": 106_209,       # doubled basis
    "tail_90_99c": 106_209,      # doubled basis
}

CALENDAR_2024_START = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
CALENDAR_2024_END = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())


def reconcile_counts(conn, yes_only: pl.DataFrame, doubled: pl.DataFrame) -> dict[str, Any]:
    """Count deltas first, estimate deltas only after this passes (or is at
    least reviewed) -- spec's own sequencing rule."""
    n_events = 0
    if not yes_only.is_empty():
        tickers = yes_only["ticker"].unique().to_list()
        placeholders = ",".join("?" * len(tickers))
        n_events = conn.execute(
            f"SELECT COUNT(DISTINCT event_ticker) FROM markets "
            f"WHERE ticker IN ({placeholders}) AND event_ticker IS NOT NULL",
            tickers,
        ).fetchone()[0]

    n_contracts = yes_only["ticker"].n_unique() if not yes_only.is_empty() else 0
    n_yes_prices = len(yes_only)
    n_doubled_prices = len(doubled)
    n_tail_low = doubled.filter((pl.col("p") > 0) & (pl.col("p") <= 0.10)).height if not doubled.is_empty() else 0
    n_tail_high = doubled.filter((pl.col("p") > 0.90) & (pl.col("p") <= 0.99)).height if not doubled.is_empty() else 0

    actual = {
        "events": n_events, "yes_contracts": n_contracts, "yes_prices": n_yes_prices,
        "doubled_prices": n_doubled_prices, "tail_1_10c": n_tail_low, "tail_90_99c": n_tail_high,
    }
    deltas = {}
    for key, target in BDW_TARGETS.items():
        actual_val = actual[key]
        deltas[key] = {
            "bdw_target": target, "actual": actual_val, "delta": actual_val - target,
            "delta_pct": round((actual_val - target) / target * 100, 2) if target else None,
        }
    return {"actual": actual, "targets": BDW_TARGETS, "deltas": deltas}


def compute_calendar_2024_mix(yes_only: pl.DataFrame) -> dict[str, float]:
    """Per-category share of in-scope, R1-window contracts closing in
    calendar 2024, by CONTRACT count (dedup to one row per ticker -- the
    Yes-only panel has up to 11 price rows per contract, which would
    over-weight contracts with deeper lookback coverage if left un-deduped).
    """
    if yes_only.is_empty():
        return {}
    contracts = yes_only.unique(subset=["ticker"]).filter(
        (pl.col("close_time_epoch") >= CALENDAR_2024_START)
        & (pl.col("close_time_epoch") < CALENDAR_2024_END)
    )
    if contracts.is_empty():
        return {}
    counts = contracts.group_by("category").len()
    total = counts["len"].sum()
    return {
        (row["category"] or "unknown"): row["len"] / total
        for row in counts.iter_rows(named=True)
    }


def write_frozen_2024_mix(mix: dict[str, float], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "computed_ts": now_utc_iso(),
        "basis": "yes_only_contract_count",
        "source_window": "calendar_2024",
        "weights": mix,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_frozen_2024_mix(path: str | Path) -> dict[str, float]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing -- Phase 7 (R2 decomposition) requires the frozen calendar-2024 "
            "category mix to already exist (Phase 3's own reconcile.write_frozen_2024_mix). "
            "It is never recomputed from R2 data."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["weights"]
