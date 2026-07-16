from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from kalshi_mt.control.polymarket import (
    CONTROL_END,
    CONTROL_START,
    MonthlyPsiResult,
    _detect_column,
    _outcome_to_float,
    _to_epoch,
    build_polymarket_panel,
    load_category_map,
    monthly_psi_path,
)


def _epoch(y, m, d=1, hh=0):
    return int(datetime(y, m, d, hh, tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# _detect_column
# ---------------------------------------------------------------------------

def test_detect_column_matches_case_insensitively():
    assert _detect_column(["Condition_ID", "Price"], ["condition_id"]) == "Condition_ID"


def test_detect_column_returns_first_matching_candidate_in_priority_order():
    assert _detect_column(["market_id", "id"], ["condition_id", "id", "market_id"]) == "id"


def test_detect_column_none_when_no_candidate_present():
    assert _detect_column(["foo", "bar"], ["condition_id", "market_id"]) is None


# ---------------------------------------------------------------------------
# _outcome_to_float
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (1, 1.0), (0, 0.0), (1.0, 1.0), (0.0, 0.0),
    ("yes", 1.0), ("Yes", 1.0), ("NO", 0.0), ("true", 1.0), ("false", 0.0),
    (None, None), ("maybe", None), ("multi_outcome_3", None),
])
def test_outcome_to_float(raw, expected):
    assert _outcome_to_float(raw) == expected


# ---------------------------------------------------------------------------
# _to_epoch
# ---------------------------------------------------------------------------

def test_to_epoch_from_int():
    assert _to_epoch(1234567) == 1234567


def test_to_epoch_from_iso_string():
    assert _to_epoch("2025-06-15T00:00:00Z") == _epoch(2025, 6, 15)


def test_to_epoch_from_datetime():
    dt = datetime(2025, 6, 15, tzinfo=timezone.utc)
    assert _to_epoch(dt) == _epoch(2025, 6, 15)


def test_to_epoch_invalid_returns_none():
    assert _to_epoch("not-a-date") is None
    assert _to_epoch(None) is None


# ---------------------------------------------------------------------------
# load_category_map
# ---------------------------------------------------------------------------

def test_load_category_map_missing_file_returns_empty(tmp_path):
    assert load_category_map(tmp_path / "nope.yaml") == {}


def test_load_category_map_reads_map_key(tmp_path):
    p = tmp_path / "map.yaml"
    p.write_text("version: 1\nmap:\n  Weather: Climate and Weather\n  Politics: Politics\n", encoding="utf-8")
    result = load_category_map(p)
    assert result == {"Weather": "Climate and Weather", "Politics": "Politics"}


# ---------------------------------------------------------------------------
# build_polymarket_panel -- synthetic fixture files, never the real HF dataset
# ---------------------------------------------------------------------------

def _write_markets_parquet(path, rows):
    pl.DataFrame(rows).write_parquet(path)


def _write_quant_parquet(path, rows):
    pl.DataFrame(rows).write_parquet(path)


def test_build_polymarket_panel_joins_and_filters_resolved_binary_in_window(tmp_path):
    markets_path = tmp_path / "markets.parquet"
    quant_path = tmp_path / "quant.parquet"

    _write_markets_parquet(markets_path, [
        {"condition_id": "M1", "outcome": "yes", "end_date_iso": "2025-06-15T00:00:00Z", "category": "Weather"},
        {"condition_id": "M2", "outcome": "no", "end_date_iso": "2025-07-01T00:00:00Z", "category": "Politics"},
        # Outside the control window entirely -- must be dropped.
        {"condition_id": "M3", "outcome": "yes", "end_date_iso": "2026-03-01T00:00:00Z", "category": "Weather"},
        # Unresolved (still open) -- must be dropped.
        {"condition_id": "M4", "outcome": None, "end_date_iso": "2025-06-20T00:00:00Z", "category": "Weather"},
    ])
    _write_quant_parquet(quant_path, [
        # M1: two trades, later one is the closing price used.
        {"condition_id": "M1", "price": 0.3, "timestamp": _epoch(2025, 6, 10)},
        {"condition_id": "M1", "price": 0.7, "timestamp": _epoch(2025, 6, 14)},
        {"condition_id": "M2", "price": 0.2, "timestamp": _epoch(2025, 6, 25)},
        {"condition_id": "M3", "price": 0.5, "timestamp": _epoch(2026, 2, 20)},
        {"condition_id": "M4", "price": 0.4, "timestamp": _epoch(2025, 6, 15)},
    ])

    category_map = {"Weather": "Climate and Weather", "Politics": "Politics"}
    panel = build_polymarket_panel(quant_path, markets_path, category_map=category_map)

    assert set(panel["ticker"].to_list()) == {"M1", "M2"}
    m1 = panel.filter(pl.col("ticker") == "M1").row(0, named=True)
    assert m1["p"] == 0.7  # the LATER trade, not the earlier one
    assert m1["y"] == 1.0
    assert m1["category"] == "Climate and Weather"
    assert m1["lookback_day"] == 0
    assert m1["event_ticker"] == "M1"
    assert m1["source"] == "polymarket_archive"


def test_build_polymarket_panel_drops_trades_after_resolution():
    """A trade timestamped AFTER the market's own resolution can't be the
    closing price -- it would be looking past resolution."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        from pathlib import Path
        markets_path = Path(d) / "markets.parquet"
        quant_path = Path(d) / "quant.parquet"
        _write_markets_parquet(markets_path, [
            {"condition_id": "M1", "outcome": "yes", "end_date_iso": "2025-06-15T00:00:00Z", "category": "Weather"},
        ])
        _write_quant_parquet(quant_path, [
            {"condition_id": "M1", "price": 0.6, "timestamp": _epoch(2025, 6, 10)},
            {"condition_id": "M1", "price": 0.99, "timestamp": _epoch(2025, 6, 20)},  # after resolution
        ])
        panel = build_polymarket_panel(quant_path, markets_path, category_map={})
        assert len(panel) == 1
        assert panel["p"][0] == 0.6


def test_build_polymarket_panel_unmapped_category_stays_none_not_dropped():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        markets_path = Path(d) / "markets.parquet"
        quant_path = Path(d) / "quant.parquet"
        _write_markets_parquet(markets_path, [
            {"condition_id": "M1", "outcome": "yes", "end_date_iso": "2025-06-15T00:00:00Z", "category": "SomeNewTag"},
        ])
        _write_quant_parquet(quant_path, [
            {"condition_id": "M1", "price": 0.5, "timestamp": _epoch(2025, 6, 10)},
        ])
        panel = build_polymarket_panel(quant_path, markets_path, category_map={"Weather": "Climate and Weather"})
        assert len(panel) == 1
        assert panel["category"][0] is None


def test_build_polymarket_panel_rejects_window_outside_control_coverage():
    with pytest.raises(ValueError):
        build_polymarket_panel("x.parquet", "y.parquet", start=CONTROL_START, end=CONTROL_END + 1_000_000)


def test_build_polymarket_panel_missing_columns_raises(tmp_path):
    markets_path = tmp_path / "markets.parquet"
    quant_path = tmp_path / "quant.parquet"
    pl.DataFrame({"totally_unrelated_column": [1, 2]}).write_parquet(markets_path)
    pl.DataFrame({"condition_id": ["M1"], "price": [0.5], "timestamp": [_epoch(2025, 6, 1)]}).write_parquet(quant_path)
    with pytest.raises(ValueError):
        build_polymarket_panel(quant_path, markets_path)


def test_build_polymarket_panel_empty_result_returns_empty_schema_frame(tmp_path):
    markets_path = tmp_path / "markets.parquet"
    quant_path = tmp_path / "quant.parquet"
    _write_markets_parquet(markets_path, [
        {"condition_id": "M1", "outcome": None, "end_date_iso": "2025-06-15T00:00:00Z", "category": "Weather"},
    ])
    _write_quant_parquet(quant_path, [{"condition_id": "M1", "price": 0.5, "timestamp": _epoch(2025, 6, 1)}])
    panel = build_polymarket_panel(quant_path, markets_path, category_map={})
    assert panel.is_empty()
    assert set(panel.columns) >= {"ticker", "event_ticker", "y", "p"}


# ---------------------------------------------------------------------------
# monthly_psi_path
# ---------------------------------------------------------------------------

def _panel_row(ticker, close_epoch, p, y, category="Weather"):
    return {
        "ticker": ticker, "event_ticker": ticker, "lookback_day": 0, "category": category,
        "close_time_epoch": close_epoch, "side": "yes", "y": y, "p": p, "source": "polymarket_archive",
    }


def test_monthly_psi_path_produces_one_result_per_month_in_window():
    import numpy as np
    from kalshi_mt.r1.panel import PANEL_SCHEMA

    rng = np.random.default_rng(0)
    rows = []
    for month in range(5, 13):  # 2025-05 .. 2025-12
        for i in range(20):
            p = 0.1 + 0.8 * (i / 19)
            y = 1.0 if rng.random() < p else 0.0
            rows.append(_panel_row(f"M{month}-{i}", _epoch(2025, month, 10), p, y))
    panel = pl.DataFrame(rows, schema=PANEL_SCHEMA)

    results = monthly_psi_path(panel)
    assert [r.month for r in results] == [f"2025-{m:02d}" for m in range(5, 13)]
    assert all(r.result is not None for r in results)


def test_monthly_psi_path_thin_month_reports_none_not_dropped():
    from kalshi_mt.r1.panel import PANEL_SCHEMA
    rows = [_panel_row("M6-1", _epoch(2025, 6, 5), 0.5, 1.0)]  # single row -- can't fit
    panel = pl.DataFrame(rows, schema=PANEL_SCHEMA)
    results = monthly_psi_path(panel, start=_epoch(2025, 6, 1), end=_epoch(2025, 6, 30))
    assert len(results) == 1
    assert results[0].month == "2025-06"
    assert results[0].result is None


def test_monthly_psi_path_rejects_window_outside_control_coverage():
    from kalshi_mt.r1.panel import PANEL_SCHEMA
    panel = pl.DataFrame(schema=PANEL_SCHEMA)
    with pytest.raises(ValueError):
        monthly_psi_path(panel, start=CONTROL_START - 1)


def test_monthly_psi_path_empty_panel_still_walks_months():
    from kalshi_mt.r1.panel import PANEL_SCHEMA
    panel = pl.DataFrame(schema=PANEL_SCHEMA)
    results = monthly_psi_path(panel, start=_epoch(2025, 5, 1), end=_epoch(2025, 7, 31))
    assert [r.month for r in results] == ["2025-05", "2025-06", "2025-07"]
    assert all(r.result is None for r in results)
