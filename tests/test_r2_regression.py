from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import polars as pl

from kalshi_mt.r1.panel import PANEL_SCHEMA
from kalshi_mt.r2.regression import (
    FEE_BOUNDARY_EPOCH,
    PUBLICATION_BOUNDARY_EPOCH,
    fit_all_categories,
    fit_category_r2,
)


def _epoch(y, m=1, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


BEFORE_FEE = _epoch(2025, 1)   # before 2025-05-01
AFTER_FEE = _epoch(2025, 6)    # after fee, before publication (2025-09-08)
AFTER_PUB = _epoch(2025, 10)   # after publication


def _row(ticker, event, p, y, close_epoch, category="Weather"):
    return {
        "ticker": ticker, "event_ticker": event, "lookback_day": 0, "category": category,
        "close_time_epoch": close_epoch, "side": "yes", "y": y, "p": p, "source": "live",
    }


def _panel(rows):
    return pl.DataFrame(rows, schema=PANEL_SCHEMA)


def _well_calibrated_rows(n, close_epoch, prefix, seed=0):
    """n contracts, well-calibrated (win rate matches price), distinct
    events, spread across the price range -- enough variance in P and
    enough clusters for a well-defined fit."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        p = 0.1 + 0.8 * (i / max(n - 1, 1))
        y = 1.0 if rng.random() < p else 0.0
        rows.append(_row(f"{prefix}-{i}", f"{prefix}-E{i}", p, y, close_epoch))
    return rows


# ---------------------------------------------------------------------------
# fit_category_r2 -- degenerate cases
# ---------------------------------------------------------------------------

def test_fit_category_r2_too_few_rows_returns_none():
    rows = _well_calibrated_rows(5, BEFORE_FEE, "T")
    assert fit_category_r2(_panel(rows), "Weather") is None


def test_fit_category_r2_single_cluster_returns_none():
    rows = [_row("T1", "E1", 0.2, 0.0, BEFORE_FEE), _row("T2", "E1", 0.8, 1.0, AFTER_FEE)] * 5
    assert fit_category_r2(_panel(rows), "Weather") is None


def test_fit_category_r2_zero_price_variance_returns_none():
    rows = [_row(f"T{i}", f"E{i}", 0.5, float(i % 2), BEFORE_FEE) for i in range(10)]
    assert fit_category_r2(_panel(rows), "Weather") is None


def test_fit_category_r2_empty_panel_returns_none():
    assert fit_category_r2(_panel([]), "Weather") is None


# ---------------------------------------------------------------------------
# fit_category_r2 -- real fits, asymptotic path (>=50 clusters)
# ---------------------------------------------------------------------------

def test_fit_category_r2_many_clusters_uses_asymptotic_not_wild():
    rows = _well_calibrated_rows(60, BEFORE_FEE, "A", seed=1) + _well_calibrated_rows(60, AFTER_PUB, "B", seed=2)
    result = fit_category_r2(_panel(rows), "Weather", n_wild_bootstrap=20)
    assert result is not None
    assert result.n_clusters >= 50
    assert result.fee.used_wild_bootstrap is False
    assert result.publication.used_wild_bootstrap is False


def test_fit_category_r2_detects_a_baked_in_fee_boundary_shift():
    """Before the fee boundary: well-calibrated (E[Y-P] = 0 at every P, so
    psi ~ 0). After: every outcome is exactly the WORST case (Y=0 always),
    so Y-P = -P exactly -- a slope of -1 (cents/cents) on that side alone.
    The fitted delta (the SLOPE shift, not a level) should land close to
    that theoretical -1, not some much larger number -- get the arithmetic
    right rather than asserting an arbitrary "big" threshold."""
    before = _well_calibrated_rows(40, BEFORE_FEE, "A", seed=3)
    after = [
        _row(f"B-{i}", f"B-E{i}", 0.1 + 0.8 * (i / 39), 0.0, AFTER_FEE)  # AFTER_FEE, before publication
        for i in range(40)
    ]
    result = fit_category_r2(_panel(before + after), "Weather", n_wild_bootstrap=50)
    assert result is not None
    assert -1.5 < result.fee.delta < -0.5  # close to the theoretical -1 slope shift
    assert result.fee.delta_ci_hi < 0  # unambiguously negative, CI excludes zero


def test_fit_category_r2_zero_effect_when_before_after_identical_pattern():
    """Same well-calibrated pattern on both sides of the boundary -- delta
    should be small (no artificial shift baked in)."""
    before = _well_calibrated_rows(60, BEFORE_FEE, "A", seed=5)
    after = _well_calibrated_rows(60, AFTER_FEE, "B", seed=5)  # same seed -- same underlying pattern
    result = fit_category_r2(_panel(before + after), "Weather", n_wild_bootstrap=20)
    assert result is not None
    # Not a formal hypothesis test here -- just confirms the CI is a sane,
    # bounded interval around a small delta, not a blown-up or nonsensical one.
    assert abs(result.fee.delta) < 20.0
    assert result.fee.delta_ci_lo < result.fee.delta_ci_hi


# ---------------------------------------------------------------------------
# fit_category_r2 -- wild bootstrap path (<50 clusters)
# ---------------------------------------------------------------------------

def test_fit_category_r2_few_clusters_uses_wild_bootstrap():
    rows = _well_calibrated_rows(10, BEFORE_FEE, "A", seed=7) + _well_calibrated_rows(10, AFTER_PUB, "B", seed=8)
    result = fit_category_r2(_panel(rows), "Weather", n_wild_bootstrap=50)
    assert result is not None
    assert result.n_clusters < 50
    assert result.fee.used_wild_bootstrap is True
    assert result.fee.delta_se > 0
    assert result.fee.delta_ci_lo < result.fee.delta_ci_hi


def test_fit_category_r2_wild_bootstrap_deterministic_given_seed():
    rows = _well_calibrated_rows(10, BEFORE_FEE, "A", seed=7) + _well_calibrated_rows(10, AFTER_PUB, "B", seed=8)
    r1 = fit_category_r2(_panel(rows), "Weather", n_wild_bootstrap=30, seed=42)
    r2 = fit_category_r2(_panel(rows), "Weather", n_wild_bootstrap=30, seed=42)
    assert r1.fee.delta_se == r2.fee.delta_se
    assert r1.fee.delta_ci_lo == r2.fee.delta_ci_lo


# ---------------------------------------------------------------------------
# publication boundary (independent of the fee boundary)
# ---------------------------------------------------------------------------

def test_fit_category_r2_publication_boundary_independent_of_fee():
    """A shift ONLY at publication (Sept 2025), not at the fee boundary --
    fee.delta should stay small while publication.delta reflects the same
    ~-1 theoretical slope shift as the fee-boundary fixture above (same
    "Y=0 always after this point" construction, just at the later
    boundary)."""
    before_fee = _well_calibrated_rows(30, BEFORE_FEE, "A", seed=9)
    after_fee_before_pub = _well_calibrated_rows(30, AFTER_FEE, "B", seed=9)  # same pattern as before -- no fee effect
    after_pub = [
        _row(f"C-{i}", f"C-E{i}", 0.1 + 0.8 * (i / 29), 0.0, AFTER_PUB)
        for i in range(30)
    ]
    result = fit_category_r2(
        _panel(before_fee + after_fee_before_pub + after_pub), "Weather", n_wild_bootstrap=30
    )
    assert result is not None
    assert -1.5 < result.publication.delta < -0.3
    assert abs(result.fee.delta) < abs(result.publication.delta)


# ---------------------------------------------------------------------------
# fit_all_categories
# ---------------------------------------------------------------------------

def test_fit_all_categories_separates_by_category():
    weather_rows = _well_calibrated_rows(15, BEFORE_FEE, "W", seed=1)
    politics_rows = _well_calibrated_rows(15, BEFORE_FEE, "P", seed=2)
    for r in politics_rows:
        r["category"] = "Politics"
    result = fit_all_categories(_panel(weather_rows + politics_rows), n_wild_bootstrap=20)
    assert set(result.keys()) == {"Weather", "Politics"}


def test_fit_all_categories_omits_categories_that_fail_to_fit():
    thin_rows = [_row("T1", "E1", 0.2, 0.0, BEFORE_FEE, category="TooThin")]
    result = fit_all_categories(_panel(thin_rows), n_wild_bootstrap=20)
    assert "TooThin" not in result


def test_fit_all_categories_empty_panel():
    assert fit_all_categories(_panel([])) == {}
