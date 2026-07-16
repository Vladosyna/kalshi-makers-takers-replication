from __future__ import annotations

import polars as pl

from kalshi_mt.r1.panel import PANEL_SCHEMA
from kalshi_mt.r2.decomposition import category_weights_from_panel, decompose, delta_bar_with_ci
from kalshi_mt.r2.regression import BoundaryCoefficients, CategoryR2Result


def _coef(delta, delta_se=0.1):
    return BoundaryCoefficients(alpha=0.0, delta=delta, delta_se=delta_se, delta_ci_lo=delta - 0.2,
                                 delta_ci_hi=delta + 0.2, used_wild_bootstrap=False)


def _fit(category, psi_c, fee_delta, pub_delta=0.0, fee_se=0.1, pub_se=0.1):
    return CategoryR2Result(
        category=category, n=100, n_clusters=100, alpha_c=0.0, psi_c=psi_c,
        fee=_coef(fee_delta, fee_se), publication=_coef(pub_delta, pub_se),
    )


# ---------------------------------------------------------------------------
# decompose -- hand-computed identity
# ---------------------------------------------------------------------------

def test_decompose_two_category_hand_computation():
    # Weather: w_bar=0.6, psi_bar=2.0, fee_delta=-1.0
    # Politics: w_bar=0.4, psi_bar=1.0, fee_delta=-3.0
    fits = {
        "Weather": _fit("Weather", psi_c=2.0, fee_delta=-1.0),
        "Politics": _fit("Politics", psi_c=1.0, fee_delta=-3.0),
    }
    frozen = {"Weather": 0.6, "Politics": 0.4}
    r2_weights = {"Weather": 0.5, "Politics": 0.5}  # composition shifted a bit in R2

    result = decompose(fits, frozen, r2_weights, boundary="fee")

    # within = 0.6*(-1.0) + 0.4*(-3.0) = -0.6 - 1.2 = -1.8
    expected_within = 0.6 * -1.0 + 0.4 * -3.0
    # between = (0.5-0.6)*2.0 + (0.5-0.4)*1.0 = -0.2 + 0.1 = -0.1
    expected_between = (0.5 - 0.6) * 2.0 + (0.5 - 0.4) * 1.0

    assert abs(result.within - expected_within) < 1e-9
    assert abs(result.between - expected_between) < 1e-9
    assert abs(result.aggregate - (expected_within + expected_between)) < 1e-9


def test_decompose_aggregate_is_always_within_plus_between():
    """The aggregate is DEFINED as within+between -- verify this holds on
    an arbitrary fixture, not just the hand-computed one above."""
    fits = {
        "A": _fit("A", psi_c=0.5, fee_delta=2.0),
        "B": _fit("B", psi_c=-1.0, fee_delta=-0.5),
        "C": _fit("C", psi_c=3.0, fee_delta=0.1),
    }
    frozen = {"A": 0.2, "B": 0.5, "C": 0.3}
    r2_weights = {"A": 0.4, "B": 0.3, "C": 0.3}
    result = decompose(fits, frozen, r2_weights)
    assert result.aggregate == result.within + result.between


def test_decompose_new_category_zero_frozen_weight_only_affects_between():
    """A category (e.g. Sports) with zero weight in the frozen 2024 mix
    (it didn't exist yet) contributes nothing to WITHIN but still shows up
    in BETWEEN via its nonzero R2-window weight."""
    fits = {"Sports": _fit("Sports", psi_c=1.5, fee_delta=-4.0)}
    frozen = {}  # Sports absent entirely -- .get() falls back to 0.0
    r2_weights = {"Sports": 0.3}

    result = decompose(fits, frozen, r2_weights)
    assert result.within == 0.0  # w_bar=0 -> within contribution is exactly zero
    assert result.between == (0.3 - 0.0) * 1.5
    assert result.per_category["Sports"]["w_bar"] == 0.0


def test_decompose_category_in_frozen_mix_but_no_r2_fit_is_excluded():
    """A category with insufficient R2 data to fit at all contributes
    NOTHING (no delta_psi_c exists to decompose) -- a documented
    limitation, not silently zeroed or forced."""
    fits = {"Weather": _fit("Weather", psi_c=1.0, fee_delta=-1.0)}
    frozen = {"Weather": 0.5, "Entertainment": 0.5}  # Entertainment has no R2 fit
    r2_weights = {"Weather": 0.8}
    result = decompose(fits, frozen, r2_weights)
    assert set(result.per_category.keys()) == {"Weather"}


def test_decompose_publication_boundary_uses_publication_delta():
    fits = {"Weather": _fit("Weather", psi_c=1.0, fee_delta=-1.0, pub_delta=-9.0)}
    frozen = {"Weather": 1.0}
    r2_weights = {"Weather": 1.0}
    result_fee = decompose(fits, frozen, r2_weights, boundary="fee")
    result_pub = decompose(fits, frozen, r2_weights, boundary="publication")
    assert result_fee.within == 1.0 * -1.0
    assert result_pub.within == 1.0 * -9.0


def test_decompose_empty_category_fits():
    result = decompose({}, {"Weather": 1.0}, {"Weather": 1.0})
    assert result.within == 0.0
    assert result.between == 0.0
    assert result.aggregate == 0.0
    assert result.per_category == {}


# ---------------------------------------------------------------------------
# category_weights_from_panel
# ---------------------------------------------------------------------------

def _panel_row(ticker, category, lookback_day=0):
    return {
        "ticker": ticker, "event_ticker": f"E-{ticker}", "lookback_day": lookback_day,
        "category": category, "close_time_epoch": 1000, "side": "yes", "y": 1.0, "p": 0.5,
        "source": "live",
    }


def test_category_weights_from_panel_dedups_by_ticker():
    rows = [
        _panel_row("T1", "Weather", lookback_day=0),
        _panel_row("T1", "Weather", lookback_day=1),  # same contract, must NOT double-count
        _panel_row("T2", "Politics", lookback_day=0),
    ]
    df = pl.DataFrame(rows, schema=PANEL_SCHEMA)
    weights = category_weights_from_panel(df)
    assert weights == {"Weather": 0.5, "Politics": 0.5}


def test_category_weights_from_panel_empty():
    assert category_weights_from_panel(pl.DataFrame(schema=PANEL_SCHEMA)) == {}


# ---------------------------------------------------------------------------
# delta_bar_with_ci
# ---------------------------------------------------------------------------

def test_delta_bar_with_ci_point_estimate_matches_within():
    fits = {
        "Weather": _fit("Weather", psi_c=2.0, fee_delta=-1.0, fee_se=0.2),
        "Politics": _fit("Politics", psi_c=1.0, fee_delta=-3.0, fee_se=0.5),
    }
    frozen = {"Weather": 0.6, "Politics": 0.4}
    decomp = decompose(fits, frozen, frozen, boundary="fee")  # between collapses to 0
    estimate = delta_bar_with_ci(fits, frozen, boundary="fee")
    assert abs(estimate.delta_bar - decomp.within) < 1e-9


def test_delta_bar_with_ci_hand_computed_variance():
    # Var(delta_bar) = w1^2*se1^2 + w2^2*se2^2 under independence.
    fits = {
        "Weather": _fit("Weather", psi_c=0.0, fee_delta=-1.0, fee_se=0.2),
        "Politics": _fit("Politics", psi_c=0.0, fee_delta=-3.0, fee_se=0.5),
    }
    frozen = {"Weather": 0.6, "Politics": 0.4}
    estimate = delta_bar_with_ci(fits, frozen, boundary="fee")

    expected_delta_bar = 0.6 * -1.0 + 0.4 * -3.0
    expected_variance = (0.6 ** 2) * (0.2 ** 2) + (0.4 ** 2) * (0.5 ** 2)
    expected_se = expected_variance ** 0.5

    assert abs(estimate.delta_bar - expected_delta_bar) < 1e-9
    assert abs(estimate.ci_lo - (expected_delta_bar - 1.96 * expected_se)) < 1e-9
    assert abs(estimate.ci_hi - (expected_delta_bar + 1.96 * expected_se)) < 1e-9


def test_delta_bar_with_ci_wider_se_gives_wider_interval():
    tight = {"Weather": _fit("Weather", psi_c=0.0, fee_delta=-1.0, fee_se=0.1)}
    wide = {"Weather": _fit("Weather", psi_c=0.0, fee_delta=-1.0, fee_se=1.0)}
    frozen = {"Weather": 1.0}
    tight_est = delta_bar_with_ci(tight, frozen, boundary="fee")
    wide_est = delta_bar_with_ci(wide, frozen, boundary="fee")
    assert (wide_est.ci_hi - wide_est.ci_lo) > (tight_est.ci_hi - tight_est.ci_lo)


def test_delta_bar_with_ci_publication_boundary_uses_publication_se():
    fits = {"Weather": _fit("Weather", psi_c=1.0, fee_delta=-1.0, pub_delta=-9.0, fee_se=0.1, pub_se=2.0)}
    frozen = {"Weather": 1.0}
    fee_est = delta_bar_with_ci(fits, frozen, boundary="fee")
    pub_est = delta_bar_with_ci(fits, frozen, boundary="publication")
    assert fee_est.delta_bar == -1.0
    assert pub_est.delta_bar == -9.0
    assert (pub_est.ci_hi - pub_est.ci_lo) > (fee_est.ci_hi - fee_est.ci_lo)


def test_delta_bar_with_ci_zero_frozen_weight_category_excluded():
    fits = {"Sports": _fit("Sports", psi_c=1.5, fee_delta=-4.0)}
    assert delta_bar_with_ci(fits, {}, boundary="fee") is None


def test_delta_bar_with_ci_empty_fits():
    assert delta_bar_with_ci({}, {"Weather": 1.0}) is None
