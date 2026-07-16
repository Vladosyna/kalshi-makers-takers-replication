from __future__ import annotations

import polars as pl

from kalshi_mt.fees.returns import counterfactual_return, gross_return, three_layer_return
from kalshi_mt.fees.schedule import load_fee_schedule
from kalshi_mt.r2.maker_margin import MakerMarginResult, compute_maker_margin_ge_50c
from kalshi_mt.store.parquet import TRADE_SCHEMA


def _trade(
    trade_id: str,
    ticker: str,
    yes_price: float,
    taker_outcome_side: str,
    created_time: str,
    count_fp: float = 100.0,
) -> dict:
    return {
        "trade_id": trade_id,
        "ticker": ticker,
        "count_fp": count_fp,
        "yes_price_dollars": yes_price,
        "no_price_dollars": 1.0 - yes_price,
        "taker_outcome_side": taker_outcome_side,
        "taker_book_side": taker_outcome_side,
        "taker_side": taker_outcome_side,
        "created_time": created_time,
        "is_block_trade": False,
        "source": "historical",
    }


def _df(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=TRADE_SCHEMA)


def _real_schedule():
    return load_fee_schedule()


def _minimal_schedule():
    return {
        "schedule": [
            {"effective_from": "2022-09-22", "role": "taker", "category": "default", "rate": 0.07},
            {"effective_from": "2022-09-22", "role": "maker", "category": "default", "rate": 0.0},
            {"effective_from": "2025-05-01", "role": "maker", "category": "default", "rate": 0.0175},
        ],
    }


# ---------------------------------------------------------------------------
# (a) layer-a == layer-c for the MAKER side alone, as a locked-in regression
# check of the fact this module's whole design rests on.
# ---------------------------------------------------------------------------

def test_maker_layer_a_equals_layer_c_invariant():
    s = _minimal_schedule()
    layers_pre = three_layer_return(s, "maker", None, 100.0, 1.0, 0.6, "2023-06-01T00:00:00Z")
    assert layers_pre.gross == layers_pre.counterfactual

    layers_post = three_layer_return(s, "maker", None, 100.0, 1.0, 0.6, "2026-01-01T00:00:00Z")
    assert layers_post.gross == layers_post.counterfactual

    cf = counterfactual_return(s, "maker", None, 100.0, 1.0, 0.6)
    assert cf == gross_return(1.0, 0.6)


# ---------------------------------------------------------------------------
# (b) hand-computed gross margin at a fixed >=50c price.
# ---------------------------------------------------------------------------

def test_margin_layer_a_hand_computation():
    # yes_price == 0.50 is the one case where BOTH sides of a single trade
    # clear the >=50c threshold (each side is exactly 0.50), giving exactly
    # one maker observation and one taker observation from a single trade.
    s = _minimal_schedule()
    resolutions = {"T1": "yes"}
    categories = {"T1": "default"}
    trades = _df([
        _trade("t1", "T1", 0.50, "yes", "2023-01-01T00:00:00Z"),
    ])
    result = compute_maker_margin_ge_50c(trades, resolutions, categories, s, {"T1"})

    taker_return = (1.0 - 0.50) / 0.50  # yes side is taker, resolves yes
    maker_return = (0.0 - 0.50) / 0.50  # no side is maker, resolves loss
    expected_margin_a = maker_return - taker_return

    assert result.layer_a is not None
    assert abs(result.layer_a - expected_margin_a) < 1e-9
    assert result.n_maker_a == 1
    assert result.n_taker_a == 1


# ---------------------------------------------------------------------------
# (c) a side price < 0.50 is excluded from that side's contribution. Two
# trades are used because within a single trade (yes_price != 0.50) only
# one of its two complementary sides can clear the >=50c threshold; wrongly
# including the below-50c side of either trade would change the margin.
# ---------------------------------------------------------------------------

def test_side_below_50c_excluded():
    s = _minimal_schedule()
    resolutions = {"T1": "yes"}
    categories = {"T1": "default"}

    # Trade A: yes side (0.65) is maker and clears the band; no side (0.35,
    # taker) does not. Trade B: yes side (0.75) is taker and clears the
    # band; no side (0.25, maker) does not.
    trades = _df([
        _trade("a", "T1", 0.65, "no", "2023-01-01T00:00:00Z"),
        _trade("b", "T1", 0.75, "yes", "2023-01-01T00:00:00Z"),
    ])
    result = compute_maker_margin_ge_50c(trades, resolutions, categories, s, {"T1"})

    assert result.n_maker_a == 1
    assert result.n_taker_a == 1

    expected_maker_return = (1.0 - 0.65) / 0.65
    expected_taker_return = (1.0 - 0.75) / 0.75
    expected_margin = expected_maker_return - expected_taker_return
    assert result.layer_a is not None
    assert abs(result.layer_a - expected_margin) < 1e-9

    # If the below-50c complementary sides (no@0.35 as taker, no@0.25 as
    # maker) were wrongly included, the margin would differ from the above.
    wrongly_included_maker_return = (expected_maker_return + (0.0 - 0.25) / 0.25) / 2
    wrongly_included_taker_return = (expected_taker_return + (0.0 - 0.35) / 0.35) / 2
    wrong_margin = wrongly_included_maker_return - wrongly_included_taker_return
    assert abs(result.layer_a - wrong_margin) > 1e-6


# ---------------------------------------------------------------------------
# (d) empty trades / empty in_scope_tickers.
# ---------------------------------------------------------------------------

def test_empty_trades_returns_all_none():
    s = _minimal_schedule()
    empty_trades = _df([])
    result = compute_maker_margin_ge_50c(empty_trades, {}, {}, s, {"T1"})
    assert isinstance(result, MakerMarginResult)
    assert result.layer_a is None
    assert result.layer_b is None
    assert result.layer_c is None
    assert result.n_maker_a == 0
    assert result.n_taker_a == 0


def test_empty_in_scope_tickers_returns_all_none():
    s = _minimal_schedule()
    trades = _df([
        _trade("t1", "T1", 0.60, "yes", "2023-01-01T00:00:00Z"),
    ])
    result = compute_maker_margin_ge_50c(trades, {"T1": "yes"}, {"T1": "default"}, s, set())
    assert result.layer_a is None
    assert result.layer_b is None
    assert result.layer_c is None


# ---------------------------------------------------------------------------
# (e) fee-schedule-gap trade excluded from layer b only, not layer a or c.
# net_return (layer b) looks up the fee schedule at the trade's own
# created_time, which predates the minimal schedule's earliest entry here,
# so both roles gap out of layer b. counterfactual_return (layer c) always
# looks up the schedule at the FIXED COUNTERFACTUAL_AS_OF (2025-04-30),
# independent of the trade's own date, so it does not gap here.
# ---------------------------------------------------------------------------

def test_fee_schedule_gap_excludes_only_b():
    s = _minimal_schedule()
    resolutions = {"T1": "yes"}
    categories = {"T1": "default"}
    trades = _df([
        _trade("t1", "T1", 0.50, "yes", "2021-01-01T00:00:00Z"),
    ])
    result = compute_maker_margin_ge_50c(trades, resolutions, categories, s, {"T1"})

    assert result.n_maker_a == 1
    assert result.n_taker_a == 1
    assert result.layer_a is not None

    assert result.n_maker_b == 0
    assert result.n_taker_b == 0
    assert result.layer_b is None
    assert result.gap_excluded_b == 2

    assert result.n_maker_c == 1
    assert result.n_taker_c == 1
    assert result.layer_c is not None
    assert result.gap_excluded_c == 0


# ---------------------------------------------------------------------------
# Additional coverage: real fee schedule, missing resolution/category, and
# a post-2025-05 maker trade where layer c mechanically diverges from
# layer b (since layer b now charges the maker a nonzero fee).
# ---------------------------------------------------------------------------

def test_real_fee_schedule_loads_and_runs():
    s = _real_schedule()
    resolutions = {"T1": "yes"}
    categories = {"T1": "default"}
    trades = _df([
        _trade("t1", "T1", 0.50, "yes", "2023-06-01T00:00:00Z"),
    ])
    result = compute_maker_margin_ge_50c(trades, resolutions, categories, s, {"T1"})
    assert result.layer_a is not None
    assert result.layer_b is not None
    assert result.layer_c is not None
    # Pre-boundary trade (2023-06, before the 2025-05-01 maker-fee
    # introduction): the maker rate is 0 both at the trade's own date (b)
    # and at the fixed counterfactual date (c), and the taker rate (0.07)
    # is constant across all eras -- so b and c coincide exactly for this
    # trade. This is incidental to this trade's date, not a general
    # identity: see the module docstring for why only the MAKER's own
    # return (not the margin as a whole) is invariant between a and c.
    assert result.layer_b == result.layer_c
    assert result.layer_a != result.layer_b


def test_missing_resolution_or_category_skips_without_crash():
    s = _minimal_schedule()
    trades = _df([
        _trade("t1", "UNKNOWN", 0.60, "yes", "2023-01-01T00:00:00Z"),
    ])
    result = compute_maker_margin_ge_50c(trades, {}, {}, s, {"UNKNOWN"})
    assert result.layer_a is None
    assert result.n_maker_a == 0
    assert result.n_taker_a == 0


def test_invalid_count_fp_skipped():
    s = _minimal_schedule()
    resolutions = {"T1": "yes"}
    categories = {"T1": "default"}
    trades = _df([
        _trade("t1", "T1", 0.60, "yes", "2023-01-01T00:00:00Z", count_fp=0.0),
    ])
    result = compute_maker_margin_ge_50c(trades, resolutions, categories, s, {"T1"})
    assert result.layer_a is None
    assert result.n_maker_a == 0
    assert result.n_taker_a == 0


def test_post_boundary_maker_fee_makes_layer_b_diverge_from_layer_c():
    # Post-boundary trade (2026): layer c looks up the maker rate at the
    # fixed pre-boundary date (0.0) while layer b looks up the maker rate
    # at the trade's own real date (0.0175, since 2025-05-01) -- the
    # taker rate is 0.07 in both (constant across all eras), so the
    # maker-fee difference alone must push margin_b below margin_c.
    s = _minimal_schedule()
    resolutions = {"T1": "yes"}
    categories = {"T1": "default"}
    trades = _df([
        _trade("t1", "T1", 0.50, "yes", "2026-01-01T00:00:00Z"),
    ])
    result = compute_maker_margin_ge_50c(trades, resolutions, categories, s, {"T1"})

    assert result.layer_a is not None
    assert result.layer_b is not None
    assert result.layer_c is not None
    assert result.layer_b < result.layer_c
