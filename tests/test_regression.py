from __future__ import annotations

import numpy as np
import polars as pl

from kalshi_mt.r1.panel import PANEL_SCHEMA
from kalshi_mt.r1.regression import fit_mz_regression, verify_two_way_equals_one_way_clustering


def _panel(rows):
    """rows: list of (ticker, event_ticker, p, y) -- lookback_day/category/
    close_time_epoch/side/source filled with placeholder values, irrelevant
    to the regression itself."""
    return pl.DataFrame(
        [
            {
                "ticker": t, "event_ticker": e, "lookback_day": 0, "category": "Weather",
                "close_time_epoch": 1000, "side": "yes", "y": y, "p": p, "source": "live",
            }
            for t, e, p, y in rows
        ],
        schema=PANEL_SCHEMA,
    )


def test_fit_mz_regression_matches_independent_ols_oracle():
    rows = [
        ("T1", "E1", 0.10, 0.0), ("T2", "E2", 0.30, 0.0), ("T3", "E3", 0.50, 1.0),
        ("T4", "E4", 0.70, 1.0), ("T5", "E5", 0.90, 1.0),
    ]
    df = _panel(rows)
    result = fit_mz_regression(df)
    assert result is not None

    p_cents = np.array([r[2] * 100.0 for r in rows])
    y_minus_p_cents = np.array([(r[3] - r[2]) * 100.0 for r in rows])
    expected_psi, expected_alpha = np.polyfit(p_cents, y_minus_p_cents, 1)

    assert abs(result.psi - expected_psi) < 1e-6
    assert abs(result.alpha - expected_alpha) < 1e-6
    assert result.n == 5
    assert result.n_clusters == 5


def test_fit_mz_regression_empty_panel_returns_none():
    df = pl.DataFrame(schema=PANEL_SCHEMA)
    assert fit_mz_regression(df) is None


def test_fit_mz_regression_single_cluster_returns_none():
    rows = [("T1", "E1", 0.1, 0.0), ("T2", "E1", 0.5, 1.0), ("T3", "E1", 0.9, 1.0)]
    df = _panel(rows)
    assert fit_mz_regression(df) is None


def test_fit_mz_regression_saturated_two_point_fit_returns_none():
    """n=2 with 2 parameters (alpha, psi) has zero residual degrees of
    freedom -- statsmodels' small-sample cluster correction divides by
    (nobs - k_params) and would ZeroDivisionError if not guarded."""
    rows = [("T1", "E1", 0.2, 0.0), ("T2", "E2", 0.8, 1.0)]
    df = _panel(rows)
    assert fit_mz_regression(df) is None


def test_fit_mz_regression_zero_price_variance_returns_none():
    """Every row sharing the same P collapses the design matrix to rank 1 --
    statsmodels' add_constant would otherwise silently drop a column and
    shift every coefficient index (an IndexError, not a clean failure)."""
    rows = [("T1", "E1", 0.5, 0.0), ("T2", "E2", 0.5, 1.0), ("T3", "E3", 0.5, 1.0)]
    df = _panel(rows)
    assert fit_mz_regression(df) is None


def test_fit_mz_regression_missing_event_ticker_clusters_on_own_ticker():
    """A market with no resolved event_ticker still contributes to the fit,
    clustered on itself rather than being silently dropped."""
    rows = [
        ("T1", None, 0.1, 0.0), ("T2", None, 0.3, 0.0), ("T3", None, 0.5, 1.0),
        ("T4", None, 0.7, 1.0), ("T5", None, 0.9, 1.0),
    ]
    df = _panel(rows)
    result = fit_mz_regression(df)
    assert result is not None
    assert result.n_clusters == 5  # 5 distinct tickers, each its own fallback cluster


def test_fit_mz_regression_well_calibrated_fixture_has_positive_slope():
    """A well-calibrated market (low prices lose, high prices win) has
    y-p rising with p: at p=10c,y=0 -> y-p=-10c; at p=90c,y=1 -> y-p=+10c.
    The fitted psi must be positive on this fixture."""
    rows = [
        ("T1", "E1", 0.10, 0.0), ("T2", "E2", 0.20, 0.0), ("T3", "E3", 0.30, 0.0),
        ("T4", "E4", 0.70, 1.0), ("T5", "E5", 0.80, 1.0), ("T6", "E6", 0.90, 1.0),
    ]
    df = _panel(rows)
    result = fit_mz_regression(df)
    assert result is not None
    assert result.psi > 0


# ---------------------------------------------------------------------------
# two-way == one-way clustering equivalence (spec S4's numerical proof)
# ---------------------------------------------------------------------------

def test_two_way_clustering_equals_one_way_when_nested():
    """Multiple contracts (tickers) per event -- exactly the nested
    structure the module docstring's algebraic claim depends on."""
    rows = []
    rng_y = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0]
    rng_p = [0.1, 0.9, 0.2, 0.8, 0.3, 0.7, 0.4, 0.6]
    for i, (y, p) in enumerate(zip(rng_y, rng_p)):
        event = f"E{i // 2}"  # two contracts share each event
        rows.append((f"T{i}", event, p, y))
    df = _panel(rows)

    result = verify_two_way_equals_one_way_clustering(df)
    assert result["verified"] is True
    assert abs(result["one_way_alpha_se"] - result["two_way_alpha_se"]) < 1e-6
    assert abs(result["one_way_psi_se"] - result["two_way_psi_se"]) < 1e-6


def test_two_way_clustering_verification_empty_panel():
    df = pl.DataFrame(schema=PANEL_SCHEMA)
    result = verify_two_way_equals_one_way_clustering(df)
    assert result["verified"] is False
    assert "empty" in result["reason"]


def test_two_way_clustering_verification_single_cluster():
    rows = [("T1", "E1", 0.1, 0.0), ("T2", "E1", 0.9, 1.0)]
    df = _panel(rows)
    result = verify_two_way_equals_one_way_clustering(df)
    assert result["verified"] is False
