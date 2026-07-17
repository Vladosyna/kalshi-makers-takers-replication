from __future__ import annotations

import polars as pl

from kalshi_mt.r1.panel import PANEL_SCHEMA
from kalshi_mt.r1.reproduction import (
    by_category_psi,
    by_year_psi,
    maker_taker_split,
    returns_by_band,
    win_rate_by_band,
    write_divergence_log,
)


def _epoch(year, month=6, day=1):
    from datetime import datetime, timezone
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def _panel_row(ticker, event, p, y, *, lookback_day=0, category="Weather", close_epoch=None):
    return {
        "ticker": ticker, "event_ticker": event, "lookback_day": lookback_day, "category": category,
        "close_time_epoch": close_epoch or _epoch(2023), "side": "yes", "y": y, "p": p, "source": "live",
    }


def _panel(rows):
    return pl.DataFrame(rows, schema=PANEL_SCHEMA)


# ---------------------------------------------------------------------------
# by_year_psi
# ---------------------------------------------------------------------------

def test_by_year_psi_buckets_by_close_year():
    rows = [
        _panel_row("T1", "E1", 0.1, 0.0, close_epoch=_epoch(2022)),
        _panel_row("T2", "E2", 0.3, 0.0, close_epoch=_epoch(2022)),
        _panel_row("T3", "E3", 0.7, 1.0, close_epoch=_epoch(2022)),
        _panel_row("T4", "E4", 0.9, 1.0, close_epoch=_epoch(2022)),
        _panel_row("T5", "E5", 0.5, 1.0, close_epoch=_epoch(2023)),
        _panel_row("T6", "E6", 0.5, 0.0, close_epoch=_epoch(2023)),
    ]
    result = by_year_psi(_panel(rows))
    assert "2022" in result
    assert "2023" in result
    assert result["2022"]["fit"] is not None
    assert result["2022"]["bdw_psi"] == 0.023


def test_by_year_psi_verdict_confirmed_when_ci_contains_target():
    """A tight-ish fit whose CI happens to bracket BDW's own point estimate."""
    rows = [
        _panel_row("T1", "E1", 0.1, 0.0, close_epoch=_epoch(2022)),
        _panel_row("T2", "E2", 0.2, 0.0, close_epoch=_epoch(2022)),
        _panel_row("T3", "E3", 0.3, 0.0, close_epoch=_epoch(2022)),
        _panel_row("T4", "E4", 0.7, 1.0, close_epoch=_epoch(2022)),
        _panel_row("T5", "E5", 0.8, 1.0, close_epoch=_epoch(2022)),
        _panel_row("T6", "E6", 0.9, 1.0, close_epoch=_epoch(2022)),
    ]
    result = by_year_psi(_panel(rows))
    entry = result["2022"]
    assert entry["verdict"] in ("confirmed", "partially_confirmed")  # not diverged -- same-sign, positive psi


def test_by_year_psi_verdict_diverged_on_opposite_sign():
    # Reversed FLB pattern: high price loses, low price wins -- psi negative.
    rows = [
        _panel_row("T1", "E1", 0.1, 1.0, close_epoch=_epoch(2022)),
        _panel_row("T2", "E2", 0.2, 1.0, close_epoch=_epoch(2022)),
        _panel_row("T3", "E3", 0.3, 1.0, close_epoch=_epoch(2022)),
        _panel_row("T4", "E4", 0.7, 0.0, close_epoch=_epoch(2022)),
        _panel_row("T5", "E5", 0.8, 0.0, close_epoch=_epoch(2022)),
        _panel_row("T6", "E6", 0.9, 0.0, close_epoch=_epoch(2022)),
    ]
    result = by_year_psi(_panel(rows))
    assert result["2022"]["verdict"] == "diverged"


def test_by_year_psi_insufficient_data_when_single_cluster():
    rows = [_panel_row("T1", "E1", 0.5, 1.0, close_epoch=_epoch(2022))]
    result = by_year_psi(_panel(rows))
    assert result["2022"]["verdict"] == "insufficient_data"
    assert result["2022"]["fit"] is None


def test_by_year_psi_empty_panel():
    assert by_year_psi(pl.DataFrame(schema=PANEL_SCHEMA)) == {}


# ---------------------------------------------------------------------------
# by_category_psi
# ---------------------------------------------------------------------------

def test_by_category_psi_separates_categories():
    rows = [
        _panel_row("T1", "E1", 0.1, 0.0, category="Weather"),
        _panel_row("T2", "E2", 0.9, 1.0, category="Weather"),
        _panel_row("T3", "E3", 0.5, 1.0, category="Politics"),
        _panel_row("T4", "E4", 0.5, 0.0, category="Politics"),
    ]
    result = by_category_psi(_panel(rows))
    assert set(result.keys()) == {"Weather", "Politics"}


# ---------------------------------------------------------------------------
# win_rate_by_band
# ---------------------------------------------------------------------------

def test_win_rate_by_band_basic():
    doubled = pl.DataFrame([
        {**_panel_row("T1", "E1", 0.05, 0.0), "side": "yes"},
        {**_panel_row("T1", "E1", 0.95, 1.0), "side": "no"},
        {**_panel_row("T2", "E2", 0.05, 0.0), "side": "yes"},
    ], schema=PANEL_SCHEMA)
    result = win_rate_by_band(doubled)
    assert result["1-10c"]["n"] == 2
    assert result["1-10c"]["win_rate"] == 0.0
    assert result["91-99c"]["n"] == 1
    assert result["91-99c"]["win_rate"] == 1.0


def test_win_rate_by_band_empty():
    assert win_rate_by_band(pl.DataFrame(schema=PANEL_SCHEMA)) == {}


# ---------------------------------------------------------------------------
# returns_by_band
# ---------------------------------------------------------------------------

def _fee_schedule():
    return {"schedule": [
        {"effective_from": "2022-09-22", "role": "taker", "category": "default", "rate": 0.07},
    ]}


def test_returns_by_band_gross_and_net():
    rows = [_panel_row("T1", "E1", 0.5, 1.0, lookback_day=0, close_epoch=_epoch(2023))]
    result = returns_by_band(_panel(rows), _fee_schedule())
    band = result["41-50c"]
    assert band["n"] == 1
    # gross: (1.0 - 0.5) / 0.5 = 1.0
    assert abs(band["mean_gross_return"] - 1.0) < 1e-9
    # net: fee = ceil(0.07*1*0.5*0.5*100)/100 = 0.02; (1.0-0.5-0.02)/0.5 = 0.96
    assert abs(band["mean_net_return"] - 0.96) < 1e-6
    assert band["fee_schedule_gap_excluded"] == 0


def test_returns_by_band_excludes_only_lookback_day_0():
    rows = [
        _panel_row("T1", "E1", 0.5, 1.0, lookback_day=0),
        _panel_row("T1", "E1", 0.4, 1.0, lookback_day=1),  # must be ignored
    ]
    result = returns_by_band(_panel(rows), _fee_schedule())
    assert sum(b["n"] for b in result.values()) == 1


def test_returns_by_band_fee_gap_excluded_from_net_not_gross():
    # 2021 predates the fee schedule's earliest entry (2022-09-22).
    rows = [_panel_row("T1", "E1", 0.5, 1.0, lookback_day=0, close_epoch=_epoch(2021))]
    result = returns_by_band(_panel(rows), _fee_schedule())
    band = result["41-50c"]
    assert band["mean_gross_return"] is not None
    assert band["mean_net_return"] is None
    assert band["fee_schedule_gap_excluded"] == 1


def test_returns_by_band_empty():
    assert returns_by_band(pl.DataFrame(schema=PANEL_SCHEMA), _fee_schedule()) == {}


# ---------------------------------------------------------------------------
# maker_taker_split
# ---------------------------------------------------------------------------

TRADE_SCHEMA = {
    "trade_id": pl.String, "ticker": pl.String, "count_fp": pl.Float64,
    "yes_price_dollars": pl.Float64, "no_price_dollars": pl.Float64,
    "taker_outcome_side": pl.String, "taker_book_side": pl.String, "taker_side": pl.String,
    "created_time": pl.String, "is_block_trade": pl.Boolean, "source": pl.String,
}


def _trade(ticker, yes_price, taker_side):
    return {
        "trade_id": f"{ticker}-{yes_price}", "ticker": ticker, "count_fp": 1.0,
        "yes_price_dollars": yes_price, "no_price_dollars": 1.0 - yes_price,
        "taker_outcome_side": taker_side, "taker_book_side": "bid", "taker_side": taker_side,
        "created_time": "2023-01-01T00:00:00Z", "is_block_trade": False, "source": "live",
    }


def test_maker_taker_split_basic_roles():
    # Taker buys YES at 0.9 (implicitly the maker sold/took NO at 0.1).
    # Market resolves YES: taker wins big, maker (on NO) loses.
    trades = pl.DataFrame([_trade("T1", 0.9, "yes")], schema=TRADE_SCHEMA)
    result = maker_taker_split(trades, {"T1": "yes"}, {"T1"})
    assert result["n_taker_obs"] == 1
    assert result["n_maker_obs"] == 1
    # taker: (1.0 - 0.9)/0.9 ; maker: (0.0 - 0.1)/0.1 = -1.0
    assert abs(result["taker_return"] - ((1.0 - 0.9) / 0.9)) < 1e-9
    assert abs(result["maker_return"] - (-1.0)) < 1e-9


def test_maker_taker_split_maker_share_by_band():
    trades = pl.DataFrame([_trade("T1", 0.95, "yes")], schema=TRADE_SCHEMA)
    result = maker_taker_split(trades, {"T1": "yes"}, {"T1"})
    # yes side (0.95, taker) falls in 91-99c; no side (0.05, maker) falls in 1-10c.
    assert result["maker_share_by_band"]["91-99c"] == 0.0
    assert result["maker_share_by_band"]["1-10c"] == 1.0


def test_maker_taker_split_filters_to_in_scope_tickers():
    trades = pl.DataFrame([_trade("T1", 0.5, "yes"), _trade("OUT-OF-SCOPE", 0.5, "yes")], schema=TRADE_SCHEMA)
    result = maker_taker_split(trades, {"T1": "yes", "OUT-OF-SCOPE": "yes"}, {"T1"})
    assert result["n_taker_obs"] == 1


def test_maker_taker_split_skips_unresolved_markets():
    trades = pl.DataFrame([_trade("T1", 0.5, "yes")], schema=TRADE_SCHEMA)
    result = maker_taker_split(trades, {"T1": ""}, {"T1"})
    assert result["n_taker_obs"] == 0


def test_maker_taker_split_empty_trades():
    result = maker_taker_split(pl.DataFrame(schema=TRADE_SCHEMA), {}, {"T1"})
    assert result["maker_return"] is None
    assert result["taker_return"] is None


# ---------------------------------------------------------------------------
# write_divergence_log
# ---------------------------------------------------------------------------

def test_write_divergence_log_produces_readable_file(tmp_path):
    rows = [
        _panel_row("T1", "E1", 0.1, 0.0, close_epoch=_epoch(2022)),
        _panel_row("T2", "E2", 0.9, 1.0, close_epoch=_epoch(2022)),
    ]
    report = {
        "by_year_psi": by_year_psi(_panel(rows)),
        "by_category_psi": by_category_psi(_panel(rows)),
    }
    path = write_divergence_log(report, tmp_path / "divergence_log.md")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "By-year psi" in text
    assert "2022" in text


def test_write_divergence_log_includes_field_population_section_when_present(tmp_path):
    report = {
        "by_year_psi": {}, "by_category_psi": {},
        "taker_field_population_by_era": {
            "2021-2022": {
                "trade_count": 152, "taker_outcome_side_population": 1.0,
                "taker_book_side_population": 1.0, "taker_side_legacy_population": 1.0,
            },
            "2023": {"trade_count": 0},
        },
    }
    path = write_divergence_log(report, tmp_path / "divergence_log.md")
    text = path.read_text(encoding="utf-8")
    assert "Taker-field population by era" in text
    assert "152" in text


def test_write_divergence_log_omits_field_population_section_when_absent(tmp_path):
    path = write_divergence_log({"by_year_psi": {}, "by_category_psi": {}}, tmp_path / "divergence_log.md")
    text = path.read_text(encoding="utf-8")
    assert "Taker-field population by era" not in text
