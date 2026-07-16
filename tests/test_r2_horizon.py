from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import polars as pl

from kalshi_mt.r1.panel import PANEL_SCHEMA
from kalshi_mt.r2.horizon import (
    CLOSE_DAY_LOOKBACK,
    close_only_spec,
    composition_weighted_delta_bar,
    horizon_stratified,
    run_horizon_robustness,
)
from kalshi_mt.r2.regression import BoundaryCoefficients, CategoryR2Result


def _epoch(y, m=1, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


BEFORE_FEE = _epoch(2025, 1)
AFTER_FEE = _epoch(2025, 6)


def _row(ticker, event, p, y, close_epoch, lookback_day=0, category="Weather"):
    return {
        "ticker": ticker, "event_ticker": event, "lookback_day": lookback_day, "category": category,
        "close_time_epoch": close_epoch, "side": "yes", "y": y, "p": p, "source": "live",
    }


def _panel(rows):
    return pl.DataFrame(rows, schema=PANEL_SCHEMA)


def _well_calibrated_rows(n, close_epoch, prefix, lookback_day=0, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        p = 0.1 + 0.8 * (i / max(n - 1, 1))
        y = 1.0 if rng.random() < p else 0.0
        rows.append(_row(f"{prefix}-{i}", f"{prefix}-E{i}", p, y, close_epoch, lookback_day=lookback_day))
    return rows


def _coef(delta):
    return BoundaryCoefficients(alpha=0.0, delta=delta, delta_se=0.1, delta_ci_lo=delta - 0.2,
                                 delta_ci_hi=delta + 0.2, used_wild_bootstrap=False)


def _fit(category, psi_c, fee_delta, pub_delta=0.0):
    return CategoryR2Result(
        category=category, n=100, n_clusters=100, alpha_c=0.0, psi_c=psi_c,
        fee=_coef(fee_delta), publication=_coef(pub_delta),
    )


# ---------------------------------------------------------------------------
# composition_weighted_delta_bar
# ---------------------------------------------------------------------------

def test_composition_weighted_delta_bar_hand_computation():
    fits = {
        "Weather": _fit("Weather", psi_c=2.0, fee_delta=-1.0),
        "Politics": _fit("Politics", psi_c=1.0, fee_delta=-3.0),
    }
    frozen = {"Weather": 0.6, "Politics": 0.4}
    result = composition_weighted_delta_bar(fits, frozen, boundary="fee")
    assert abs(result - (0.6 * -1.0 + 0.4 * -3.0)) < 1e-9


def test_composition_weighted_delta_bar_matches_decompose_within_when_weights_equal():
    """Algebraic claim in the module docstring: this equals
    decomposition.py's `within` term whenever r2_category_weights equals
    frozen_2024_mix (between collapses to zero)."""
    from kalshi_mt.r2.decomposition import decompose

    fits = {
        "Weather": _fit("Weather", psi_c=2.0, fee_delta=-1.0),
        "Politics": _fit("Politics", psi_c=1.0, fee_delta=-3.0),
    }
    frozen = {"Weather": 0.6, "Politics": 0.4}
    decomp = decompose(fits, frozen, frozen, boundary="fee")  # same weights both sides
    delta_bar = composition_weighted_delta_bar(fits, frozen, boundary="fee")
    assert decomp.between == 0.0
    assert abs(decomp.within - delta_bar) < 1e-9


def test_composition_weighted_delta_bar_publication_boundary():
    fits = {"Weather": _fit("Weather", psi_c=1.0, fee_delta=-1.0, pub_delta=-9.0)}
    frozen = {"Weather": 1.0}
    assert composition_weighted_delta_bar(fits, frozen, boundary="fee") == -1.0
    assert composition_weighted_delta_bar(fits, frozen, boundary="publication") == -9.0


def test_composition_weighted_delta_bar_zero_frozen_weight_category_excluded():
    fits = {"Sports": _fit("Sports", psi_c=1.5, fee_delta=-4.0)}
    frozen = {}  # Sports has no frozen weight -- new category
    assert composition_weighted_delta_bar(fits, frozen) is None


def test_composition_weighted_delta_bar_empty_fits():
    assert composition_weighted_delta_bar({}, {"Weather": 1.0}) is None


# ---------------------------------------------------------------------------
# horizon_stratified
# ---------------------------------------------------------------------------

def test_horizon_stratified_empty_panel():
    assert horizon_stratified(_panel([]), {"Weather": 1.0}) == []


def test_horizon_stratified_one_bucket_per_lookback_day():
    rows = []
    for lookback_day in (0, 1, 2):
        rows += _well_calibrated_rows(30, BEFORE_FEE, f"D{lookback_day}A", lookback_day=lookback_day, seed=lookback_day)
        rows += _well_calibrated_rows(30, AFTER_FEE, f"D{lookback_day}B", lookback_day=lookback_day, seed=lookback_day + 10)
    result = horizon_stratified(_panel(rows), {"Weather": 1.0}, n_wild_bootstrap=20)
    assert [b.lookback_day for b in result] == [0, 1, 2]
    for bucket in result:
        assert bucket.n == 60
        assert bucket.n_categories_fit == 1
        assert bucket.delta_bar_fee is not None


def test_horizon_stratified_thin_bucket_reports_no_fit_rather_than_dropping():
    """A lookback_day bucket with too little data to fit still appears in
    the output, with delta_bar fields None and n_categories_fit=0."""
    plenty = _well_calibrated_rows(30, BEFORE_FEE, "A", lookback_day=0, seed=1) + \
        _well_calibrated_rows(30, AFTER_FEE, "B", lookback_day=0, seed=2)
    thin = [_row("T-thin", "E-thin", 0.2, 0.0, BEFORE_FEE, lookback_day=5)]
    result = horizon_stratified(_panel(plenty + thin), {"Weather": 1.0}, n_wild_bootstrap=20)
    by_day = {b.lookback_day: b for b in result}
    assert by_day[0].n_categories_fit == 1
    assert by_day[5].n_categories_fit == 0
    assert by_day[5].delta_bar_fee is None
    assert by_day[5].n == 1


def test_horizon_stratified_captures_a_baked_in_shift_at_every_horizon():
    """A fee-boundary shift baked into every lookback_day bucket should
    show up as a comparably negative delta_bar_fee in each bucket -- the
    behavior expected when the effect is a real price-level shift, not a
    horizon-composition artifact."""
    rows = []
    for lookback_day in (0, 1):
        before = _well_calibrated_rows(30, BEFORE_FEE, f"A{lookback_day}", lookback_day=lookback_day, seed=lookback_day)
        after = [
            _row(f"B{lookback_day}-{i}", f"B{lookback_day}-E{i}", 0.1 + 0.8 * (i / 29), 0.0, AFTER_FEE, lookback_day=lookback_day)
            for i in range(30)
        ]
        rows += before + after
    result = horizon_stratified(_panel(rows), {"Weather": 1.0}, n_wild_bootstrap=30)
    for bucket in result:
        assert bucket.delta_bar_fee is not None
        assert bucket.delta_bar_fee < -0.3  # comparably negative at every horizon


# ---------------------------------------------------------------------------
# close_only_spec
# ---------------------------------------------------------------------------

def test_close_only_spec_filters_to_lookback_zero():
    day0 = _well_calibrated_rows(30, BEFORE_FEE, "A", lookback_day=0, seed=1) + \
        _well_calibrated_rows(30, AFTER_FEE, "B", lookback_day=0, seed=2)
    day3 = _well_calibrated_rows(30, BEFORE_FEE, "C", lookback_day=3, seed=3)
    result = close_only_spec(_panel(day0 + day3), {"Weather": 1.0}, n_wild_bootstrap=20)
    assert result is not None
    assert result.lookback_day == CLOSE_DAY_LOOKBACK
    assert result.n == 60  # only the lookback_day==0 rows


def test_close_only_spec_empty_panel_returns_none():
    assert close_only_spec(_panel([]), {"Weather": 1.0}) is None


def test_close_only_spec_no_close_day_rows_returns_none():
    rows = _well_calibrated_rows(30, BEFORE_FEE, "A", lookback_day=4, seed=1)
    assert close_only_spec(_panel(rows), {"Weather": 1.0}) is None


# ---------------------------------------------------------------------------
# run_horizon_robustness -- orchestration
# ---------------------------------------------------------------------------

def test_run_horizon_robustness_close_only_matches_bucket_zero():
    rows = []
    for lookback_day in (0, 1):
        rows += _well_calibrated_rows(30, BEFORE_FEE, f"A{lookback_day}", lookback_day=lookback_day, seed=lookback_day)
        rows += _well_calibrated_rows(30, AFTER_FEE, f"B{lookback_day}", lookback_day=lookback_day, seed=lookback_day + 5)
    result = run_horizon_robustness(_panel(rows), {"Weather": 1.0}, n_wild_bootstrap=20, seed=42)
    bucket_zero = next(b for b in result.by_bucket if b.lookback_day == 0)
    assert result.close_only is not None
    assert result.close_only.n == bucket_zero.n
    assert result.close_only.delta_bar_fee == bucket_zero.delta_bar_fee


def test_run_horizon_robustness_empty_panel():
    result = run_horizon_robustness(_panel([]), {"Weather": 1.0})
    assert result.by_bucket == []
    assert result.close_only is None
