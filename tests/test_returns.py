from __future__ import annotations

from kalshi_mt.fees.returns import (
    COUNTERFACTUAL_AS_OF,
    counterfactual_return,
    gross_return,
    net_return,
    three_layer_return,
)


def _schedule():
    return {
        "schedule": [
            {"effective_from": "2022-09-22", "role": "taker", "category": "default", "rate": 0.07},
            {"effective_from": "2022-09-22", "role": "maker", "category": "default", "rate": 0.0},
            {"effective_from": "2025-05-01", "role": "maker", "category": "default", "rate": 0.0175},
        ],
    }


# ---------------------------------------------------------------------------
# gross_return
# ---------------------------------------------------------------------------

def test_gross_return_win_and_loss():
    assert gross_return(1.0, 0.5) == 1.0    # bought at 50c, won -> +100%
    assert gross_return(0.0, 0.5) == -1.0   # bought at 50c, lost -> -100%


# ---------------------------------------------------------------------------
# net_return
# ---------------------------------------------------------------------------

def test_net_return_matches_hand_computation():
    s = _schedule()
    # taker, C=100, P=0.5, payout=1.0: fee_usd = ceil(0.07*100*0.5*0.5*100)/100 = 1.75
    # fee_per_contract = 0.0175; r = (1.0 - 0.5 - 0.0175) / 0.5 = 0.965
    r = net_return(s, "taker", None, 100.0, 1.0, 0.5, "2023-01-01")
    assert abs(r - 0.965) < 1e-9


def test_net_return_none_on_fee_schedule_gap():
    s = _schedule()
    r = net_return(s, "taker", None, 100.0, 1.0, 0.5, "2021-01-01")
    assert r is None


def test_net_return_always_worse_than_gross_for_a_positive_fee():
    s = _schedule()
    gross = gross_return(1.0, 0.5)
    net = net_return(s, "taker", None, 100.0, 1.0, 0.5, "2023-01-01")
    assert net < gross


# ---------------------------------------------------------------------------
# counterfactual_return
# ---------------------------------------------------------------------------

def test_counterfactual_uses_pre_boundary_schedule_for_a_post_boundary_trade():
    s = _schedule()
    # A trade actually filled in 2026 (post maker-fee introduction), as a
    # MAKER -- its real net return would use the 0.0175 maker rate, but the
    # counterfactual must use the PRE-2025-05 rate (0.0 for makers).
    real_net = net_return(s, "maker", None, 100.0, 1.0, 0.5, "2026-01-01")
    counterfactual = counterfactual_return(s, "maker", None, 100.0, 1.0, 0.5)
    assert real_net < counterfactual  # real return is worse (fee-bearing); counterfactual is fee-free
    assert counterfactual == gross_return(1.0, 0.5)  # maker fee was 0 pre-boundary -> counterfactual == gross


def test_counterfactual_uses_fixed_date_not_trade_date():
    s = _schedule()
    c1 = counterfactual_return(s, "maker", None, 100.0, 1.0, 0.5)
    # Same call again -- COUNTERFACTUAL_AS_OF is a module constant, not a
    # parameter, so this is trivially stable; the real assertion is that it
    # matches a direct net_return call pinned to that exact date.
    c2 = net_return(s, "maker", None, 100.0, 1.0, 0.5, COUNTERFACTUAL_AS_OF)
    assert c1 == c2


# ---------------------------------------------------------------------------
# three_layer_return
# ---------------------------------------------------------------------------

def test_three_layer_return_bundles_all_three():
    s = _schedule()
    layers = three_layer_return(s, "taker", None, 100.0, 1.0, 0.5, "2023-01-01")
    assert layers.gross == gross_return(1.0, 0.5)
    assert layers.net == net_return(s, "taker", None, 100.0, 1.0, 0.5, "2023-01-01")
    assert layers.counterfactual == counterfactual_return(s, "taker", None, 100.0, 1.0, 0.5)


def test_three_layer_return_net_none_propagates_cleanly():
    s = _schedule()
    layers = three_layer_return(s, "taker", None, 100.0, 1.0, 0.5, "2021-01-01")
    assert layers.gross is not None
    assert layers.net is None


# ---------------------------------------------------------------------------
# The ~20x-at-5c / ~2x-at-50c bias claim (docs/analysis_plan.md S3.1),
# reproduced numerically: a "buggy" version that subtracts the per-contract
# fee directly from the return (missing the second division by P) versus
# the correct r = (payout - P - f)/P.
# ---------------------------------------------------------------------------

def _buggy_return(payout: float, price: float, fee_per_contract: float) -> float:
    """The exact bug the module docstring warns against: fee subtracted as
    if it were already a per-capital rate, missing the /P normalization."""
    return gross_return(payout, price) - fee_per_contract


def test_fee_impact_understated_by_roughly_20x_at_5c():
    s = _schedule()
    price = 0.05
    fee_usd = 0.07 * 100 * price * (1 - price)
    fee_usd = (int(fee_usd * 100) + (1 if fee_usd * 100 % 1 > 1e-9 else 0)) / 100  # ceil to cent
    fee_per_contract = fee_usd / 100.0

    correct = net_return(s, "taker", None, 100.0, 0.0, price, "2023-01-01")
    buggy = _buggy_return(0.0, price, fee_per_contract)
    correct_fee_impact = gross_return(0.0, price) - correct
    buggy_fee_impact = gross_return(0.0, price) - buggy

    ratio = correct_fee_impact / buggy_fee_impact
    assert 15 < ratio < 25  # spec's own "~20x at 5c"


def test_fee_impact_understated_by_roughly_2x_at_50c():
    s = _schedule()
    price = 0.50
    correct = net_return(s, "taker", None, 100.0, 0.0, price, "2023-01-01")
    fee_per_contract = 0.0175
    buggy = _buggy_return(0.0, price, fee_per_contract)
    correct_fee_impact = gross_return(0.0, price) - correct
    buggy_fee_impact = gross_return(0.0, price) - buggy

    ratio = correct_fee_impact / buggy_fee_impact
    assert 1.5 < ratio < 2.5  # spec's own "~2x at 50c"
