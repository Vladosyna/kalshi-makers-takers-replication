from __future__ import annotations

import pytest

from kalshi_mt.r1.panel import build_doubled_panel, build_yes_only_panel
from kalshi_mt.r1.reconcile import (
    BDW_TARGETS,
    compute_calendar_2024_mix,
    coverage_gap_breakdown,
    load_frozen_2024_mix,
    reconcile_counts,
    write_frozen_2024_mix,
)
from kalshi_mt.store import db


def _seed(conn, ticker, *, event_ticker, category, close_epoch, result="yes", prices=None):
    prices = prices if prices is not None else {0: 0.9}
    db.upsert_market(conn, {
        "ticker": ticker, "event_ticker": event_ticker, "category": category,
        "result": result, "close_time_epoch": close_epoch, "in_r1_window": 1,
    })
    for day, p in prices.items():
        db.upsert_price_panel_row(conn, {
            "ticker": ticker, "lookback_day": day, "trade_id": f"{ticker}-{day}",
            "yes_price_dollars": p, "created_time": "2022-01-01T00:00:00Z", "source": "live",
        })
    conn.commit()


# ---------------------------------------------------------------------------
# reconcile_counts
# ---------------------------------------------------------------------------

def test_reconcile_counts_basic_shape(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "A-1", event_ticker="EVT-1", category="Weather", close_epoch=1000,
          prices={0: 0.05, 1: 0.95})  # one in each tail band
    yes_only = build_yes_only_panel(conn, {"A-1"})
    doubled = build_doubled_panel(yes_only)

    result = reconcile_counts(conn, yes_only, doubled)
    assert result["actual"]["events"] == 1
    assert result["actual"]["yes_contracts"] == 1
    assert result["actual"]["yes_prices"] == 2
    assert result["actual"]["doubled_prices"] == 4
    assert result["targets"] == BDW_TARGETS
    assert result["deltas"]["yes_prices"]["bdw_target"] == 156_986
    assert result["deltas"]["yes_prices"]["delta"] == 2 - 156_986


def test_reconcile_counts_tail_bins_on_doubled_basis(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "A-1", event_ticker="EVT-1", category="Weather", close_epoch=1000,
          prices={0: 0.05})  # Yes side 0.05 (tail-low), No side 0.95 (tail-high)
    yes_only = build_yes_only_panel(conn, {"A-1"})
    doubled = build_doubled_panel(yes_only)
    result = reconcile_counts(conn, yes_only, doubled)
    assert result["actual"]["tail_1_10c"] == 1
    assert result["actual"]["tail_90_99c"] == 1


def test_reconcile_counts_distinct_events_not_contracts(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    # Two contracts (strikes) under the SAME event -- events count must be 1, contracts 2.
    _seed(conn, "A-1", event_ticker="EVT-1", category="Weather", close_epoch=1000)
    _seed(conn, "A-2", event_ticker="EVT-1", category="Weather", close_epoch=1000)
    yes_only = build_yes_only_panel(conn, {"A-1", "A-2"})
    doubled = build_doubled_panel(yes_only)
    result = reconcile_counts(conn, yes_only, doubled)
    assert result["actual"]["events"] == 1
    assert result["actual"]["yes_contracts"] == 2


def test_reconcile_counts_empty_panel(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    import polars as pl
    from kalshi_mt.r1.panel import PANEL_SCHEMA
    empty = pl.DataFrame(schema=PANEL_SCHEMA)
    result = reconcile_counts(conn, empty, empty)
    assert result["actual"]["events"] == 0
    assert result["actual"]["yes_prices"] == 0


# ---------------------------------------------------------------------------
# coverage_gap_breakdown
# ---------------------------------------------------------------------------

def test_coverage_gap_breakdown_splits_structural_vs_operational_vs_other(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.log_universe_exclusions(conn, "r1", [
        ("A", "spread_filter_not_computable"),
        ("B", "spread_filter_not_computable"),
        ("C", "spread_filter_not_yet_fetched"),
        ("D", "volume_below_1000"),
        ("D", "open_below_24h"),
    ])
    conn.commit()
    result = coverage_gap_breakdown(conn, window="r1")
    assert result["structural_spread_filter_not_computable"] == 2
    assert result["operational_spread_filter_not_yet_fetched"] == 1
    assert result["other_filter_exclusions"] == 2
    assert result["reason_counts"]["volume_below_1000"] == 1
    assert result["reason_counts"]["open_below_24h"] == 1


def test_coverage_gap_breakdown_empty_log(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    result = coverage_gap_breakdown(conn, window="r1")
    assert result == {
        "structural_spread_filter_not_computable": 0,
        "operational_spread_filter_not_yet_fetched": 0,
        "other_filter_exclusions": 0,
        "reason_counts": {},
    }


def test_coverage_gap_breakdown_respects_window(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.log_universe_exclusions(conn, "r1", [("A", "spread_filter_not_computable")])
    db.log_universe_exclusions(conn, "r2", [("B", "spread_filter_not_computable")])
    conn.commit()
    r1_result = coverage_gap_breakdown(conn, window="r1")
    assert r1_result["structural_spread_filter_not_computable"] == 1


# ---------------------------------------------------------------------------
# compute_calendar_2024_mix
# ---------------------------------------------------------------------------

def _epoch_2024(month=6):
    from datetime import datetime, timezone
    return int(datetime(2024, month, 1, tzinfo=timezone.utc).timestamp())


def _epoch_2023():
    from datetime import datetime, timezone
    return int(datetime(2023, 6, 1, tzinfo=timezone.utc).timestamp())


def test_compute_calendar_2024_mix_basic(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "W-1", event_ticker="E1", category="Weather", close_epoch=_epoch_2024())
    _seed(conn, "W-2", event_ticker="E2", category="Weather", close_epoch=_epoch_2024())
    _seed(conn, "E-1", event_ticker="E3", category="Economics", close_epoch=_epoch_2024())
    yes_only = build_yes_only_panel(conn, {"W-1", "W-2", "E-1"})
    mix = compute_calendar_2024_mix(yes_only)
    assert abs(mix["Weather"] - 2 / 3) < 1e-9
    assert abs(mix["Economics"] - 1 / 3) < 1e-9


def test_compute_calendar_2024_mix_excludes_other_years(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "OLD", event_ticker="E1", category="Weather", close_epoch=_epoch_2023())
    yes_only = build_yes_only_panel(conn, {"OLD"})
    mix = compute_calendar_2024_mix(yes_only)
    assert mix == {}


def test_compute_calendar_2024_mix_dedups_by_ticker_not_price_rows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    # One contract with 3 price-panel rows -- must count as ONE contract, not 3.
    _seed(conn, "W-1", event_ticker="E1", category="Weather", close_epoch=_epoch_2024(),
          prices={0: 0.5, 1: 0.4, 2: 0.3})
    yes_only = build_yes_only_panel(conn, {"W-1"})
    assert len(yes_only) == 3  # sanity: 3 price rows
    mix = compute_calendar_2024_mix(yes_only)
    assert mix == {"Weather": 1.0}


def test_compute_calendar_2024_mix_empty_input():
    import polars as pl
    from kalshi_mt.r1.panel import PANEL_SCHEMA
    assert compute_calendar_2024_mix(pl.DataFrame(schema=PANEL_SCHEMA)) == {}


# ---------------------------------------------------------------------------
# frozen mix artifact read/write
# ---------------------------------------------------------------------------

def test_write_and_load_frozen_mix_roundtrip(tmp_path):
    mix = {"Weather": 0.6, "Economics": 0.4}
    path = write_frozen_2024_mix(mix, tmp_path / "frozen_2024_mix.json")
    assert path.exists()
    loaded = load_frozen_2024_mix(path)
    assert loaded == mix


def test_load_frozen_mix_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_frozen_2024_mix(tmp_path / "does_not_exist.json")
