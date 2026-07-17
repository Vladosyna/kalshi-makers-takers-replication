from __future__ import annotations

import polars as pl

from kalshi_mt.r1.field_population import ERA_BOUNDARIES, UNASSIGNED_KEY, field_population_by_era
from kalshi_mt.store.parquet import TRADE_SCHEMA


def _trade(ticker="T-1", created_time="2022-06-01T00:00:00Z", taker_outcome_side="yes",
           taker_book_side="yes", taker_side="yes", trade_id=None):
    return {
        "trade_id": trade_id or f"{ticker}-{created_time}", "ticker": ticker, "count_fp": 1.0,
        "yes_price_dollars": 0.5, "no_price_dollars": 0.5,
        "taker_outcome_side": taker_outcome_side, "taker_book_side": taker_book_side,
        "taker_side": taker_side, "created_time": created_time,
        "is_block_trade": False, "source": "live",
    }


def _df(rows):
    return pl.DataFrame(rows, schema=TRADE_SCHEMA) if rows else pl.DataFrame(schema=TRADE_SCHEMA)


def test_empty_trades_reports_every_era_at_zero():
    result = field_population_by_era(_df([]))
    assert set(result) == {label for label, _, _ in ERA_BOUNDARIES} | {UNASSIGNED_KEY}
    assert all(entry == {"trade_count": 0} for entry in result.values())


def test_buckets_by_era_correctly():
    rows = [
        _trade(created_time="2022-06-01T00:00:00Z", trade_id="a"),  # 2021-2022
        _trade(created_time="2023-06-01T00:00:00Z", trade_id="b"),  # 2023
        _trade(created_time="2024-06-01T00:00:00Z", trade_id="c"),  # 2024
        _trade(created_time="2025-03-01T00:00:00Z", trade_id="d"),  # 2025-jan-apr
        _trade(created_time="2025-06-01T00:00:00Z", trade_id="e"),  # 2025-may-onward
    ]
    result = field_population_by_era(_df(rows))
    assert result["2021-2022"]["trade_count"] == 1
    assert result["2023"]["trade_count"] == 1
    assert result["2024"]["trade_count"] == 1
    assert result["2025-jan-apr"]["trade_count"] == 1
    assert result["2025-may-onward"]["trade_count"] == 1


def test_population_rates_computed_correctly():
    rows = [
        _trade(created_time="2022-01-01T00:00:00Z", trade_id="a",
               taker_outcome_side="yes", taker_book_side=None, taker_side="yes"),
        _trade(created_time="2022-01-02T00:00:00Z", trade_id="b",
               taker_outcome_side=None, taker_book_side=None, taker_side="no"),
    ]
    result = field_population_by_era(_df(rows))["2021-2022"]
    assert result["trade_count"] == 2
    assert result["taker_outcome_side_population"] == 0.5
    assert result["taker_book_side_population"] == 0.0
    assert result["taker_side_legacy_population"] == 1.0


def test_era_with_no_trades_reports_zero_not_omitted():
    rows = [_trade(created_time="2022-01-01T00:00:00Z")]
    result = field_population_by_era(_df(rows))
    assert result["2024"] == {"trade_count": 0}


def test_trade_outside_all_eras_lands_in_unassigned_not_silently_dropped():
    rows = [_trade(created_time="2019-01-01T00:00:00Z", trade_id="pre-2021")]
    result = field_population_by_era(_df(rows))
    assert all(entry.get("trade_count", 0) == 0 for label, entry in result.items() if label != UNASSIGNED_KEY)
    assert result[UNASSIGNED_KEY]["trade_count"] == 1


def test_unparseable_created_time_lands_in_unassigned_not_silently_dropped():
    rows = [_trade(created_time="not-a-timestamp", trade_id="bad")]
    result = field_population_by_era(_df(rows))
    assert all(entry.get("trade_count", 0) == 0 for label, entry in result.items() if label != UNASSIGNED_KEY)
    assert result[UNASSIGNED_KEY]["trade_count"] == 1


def test_unassigned_is_zero_when_every_trade_placed():
    rows = [_trade(created_time="2022-06-01T00:00:00Z")]
    result = field_population_by_era(_df(rows))
    assert result[UNASSIGNED_KEY] == {"trade_count": 0}


# ---------------------------------------------------------------------------
# Exact era-boundary instants -- each of the five ERA_BOUNDARIES cut points,
# including the fee-regime boundary (2025-05-01T00:00:00Z) the whole R2
# verdict design pivots on. A trade at exactly `end` must land in the NEXT
# era, not be double-counted into both (half-open [start, end) semantics).
# ---------------------------------------------------------------------------

def test_boundary_instant_2021_01_01_starts_2021_2022():
    rows = [_trade(created_time="2021-01-01T00:00:00Z", trade_id="b0")]
    result = field_population_by_era(_df(rows))
    assert result["2021-2022"]["trade_count"] == 1
    assert result[UNASSIGNED_KEY]["trade_count"] == 0


def test_boundary_instant_2023_01_01_belongs_to_2023_not_2021_2022():
    rows = [_trade(created_time="2023-01-01T00:00:00Z", trade_id="b1")]
    result = field_population_by_era(_df(rows))
    assert result["2023"]["trade_count"] == 1
    assert result["2021-2022"]["trade_count"] == 0


def test_boundary_instant_2024_01_01_belongs_to_2024_not_2023():
    rows = [_trade(created_time="2024-01-01T00:00:00Z", trade_id="b2")]
    result = field_population_by_era(_df(rows))
    assert result["2024"]["trade_count"] == 1
    assert result["2023"]["trade_count"] == 0


def test_boundary_instant_2025_01_01_belongs_to_jan_apr_not_2024():
    rows = [_trade(created_time="2025-01-01T00:00:00Z", trade_id="b3")]
    result = field_population_by_era(_df(rows))
    assert result["2025-jan-apr"]["trade_count"] == 1
    assert result["2024"]["trade_count"] == 0


def test_boundary_instant_2025_05_01_fee_regime_boundary_belongs_to_may_onward():
    """The fee-regime boundary itself (spec: maker fees begin 2025-05-01) --
    the single most consequential instant this module's bucketing could get
    wrong."""
    rows = [_trade(created_time="2025-05-01T00:00:00Z", trade_id="b4")]
    result = field_population_by_era(_df(rows))
    assert result["2025-may-onward"]["trade_count"] == 1
    assert result["2025-jan-apr"]["trade_count"] == 0
