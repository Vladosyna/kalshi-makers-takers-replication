from __future__ import annotations

import pytest

from kalshi_mt.fees.schedule import (
    FeeScheduleGapError,
    fee_usd_bdw_illustration,
    fee_usd_for,
    load_fee_schedule,
    rate_for,
)


def _schedule():
    return {
        "version": 1,
        "schedule": [
            {"effective_from": "2022-09-22", "role": "taker", "category": "default", "rate": 0.07},
            {"effective_from": "2022-09-22", "role": "maker", "category": "default", "rate": 0.0},
            {"effective_from": "2025-05-01", "role": "maker", "category": "default", "rate": 0.0175},
        ],
    }


def test_rate_for_taker_before_and_after_maker_fee_introduction():
    s = _schedule()
    assert rate_for(s, "taker", None, "2023-01-01T00:00:00Z") == 0.07
    assert rate_for(s, "taker", None, "2026-01-01T00:00:00Z") == 0.07


def test_rate_for_maker_switches_at_effective_date():
    s = _schedule()
    assert rate_for(s, "maker", None, "2025-04-30T23:59:59Z") == 0.0
    assert rate_for(s, "maker", None, "2025-05-01T00:00:00Z") == 0.0175
    assert rate_for(s, "maker", None, "2026-01-01T00:00:00Z") == 0.0175


def test_rate_for_falls_back_to_default_category():
    s = {"schedule": [{"effective_from": "2022-01-01", "role": "taker", "category": "default", "rate": 0.07}]}
    assert rate_for(s, "taker", "sports", "2023-01-01") == 0.07


def test_rate_for_prefers_specific_category_over_default():
    s = {"schedule": [
        {"effective_from": "2022-01-01", "role": "taker", "category": "default", "rate": 0.07},
        {"effective_from": "2022-01-01", "role": "taker", "category": "sports", "rate": 0.03},
    ]}
    assert rate_for(s, "taker", "sports", "2023-01-01") == 0.03
    assert rate_for(s, "taker", "weather", "2023-01-01") == 0.07


def test_rate_for_raises_on_date_before_earliest_entry():
    s = _schedule()
    with pytest.raises(FeeScheduleGapError):
        rate_for(s, "taker", None, "2021-01-01T00:00:00Z")


def test_rate_for_raises_on_unknown_role():
    s = _schedule()
    with pytest.raises(FeeScheduleGapError):
        rate_for(s, "unknown_role", None, "2023-01-01")  # type: ignore[arg-type]


def test_rate_for_boundary_date_uses_new_rate_not_old():
    """The maker-fee boundary is the exact date the whole R1/R2 split hinges
    on -- get the inclusive/exclusive edge right."""
    s = _schedule()
    assert rate_for(s, "maker", None, "2025-05-01") == 0.0175


def test_fee_usd_for_matches_hand_computed_formula():
    s = _schedule()
    # taker, P=0.50, C=100, rate=0.07: 0.07*100*0.5*0.5 = 1.75
    fee = fee_usd_for(s, "taker", None, 100.0, 0.50, "2023-01-01")
    assert fee == 1.75


def test_fee_usd_for_ceils_to_cent_on_order_total():
    s = _schedule()
    # rate=0.07, C=1, P=0.30: 0.07*1*0.3*0.7 = 0.0147 -> ceil to 0.02
    fee = fee_usd_for(s, "taker", None, 1.0, 0.30, "2023-01-01")
    assert fee == 0.02


def test_fee_usd_for_zero_at_price_extremes():
    s = _schedule()
    assert fee_usd_for(s, "taker", None, 100.0, 0.0, "2023-01-01") == 0.0
    fee_near_one = fee_usd_for(s, "taker", None, 100.0, 0.999, "2023-01-01")
    assert fee_near_one <= 0.01  # tiny but not necessarily exactly zero


def test_fee_usd_for_maker_zero_before_fee_introduction():
    s = _schedule()
    fee = fee_usd_for(s, "maker", None, 100.0, 0.50, "2024-01-01")
    assert fee == 0.0


def test_fee_usd_bdw_illustration_uses_fixed_100_contracts():
    s = _schedule()
    illustration = fee_usd_bdw_illustration(s, "taker", None, 0.50, "2023-01-01")
    actual_size = fee_usd_for(s, "taker", None, 100.0, 0.50, "2023-01-01")
    assert illustration == actual_size == 1.75


def test_fee_usd_bdw_illustration_independent_of_real_order_size():
    """The illustration always uses C=100 regardless of the real fill size
    passed nowhere near it -- confirms it's not accidentally wired to a
    caller's actual contract count."""
    s = _schedule()
    small_order_fee = fee_usd_for(s, "taker", None, 3.0, 0.50, "2023-01-01")
    illustration = fee_usd_bdw_illustration(s, "taker", None, 0.50, "2023-01-01")
    assert small_order_fee != illustration


def test_load_fee_schedule_missing_file_returns_empty(tmp_path):
    data = load_fee_schedule(tmp_path / "does_not_exist.yaml")
    assert data == {"version": 0, "schedule": []}


def test_real_committed_fees_yaml_loads_and_is_nonempty():
    """Smoke test: the actual committed data/fees.yaml parses and has real
    entries -- catches a YAML typo or accidental truncation."""
    data = load_fee_schedule()
    assert data["schedule"]
    assert rate_for(data, "taker", None, "2023-01-01") == 0.07
    assert rate_for(data, "maker", None, "2024-01-01") == 0.0
    assert rate_for(data, "maker", None, "2025-06-01") == 0.0175
