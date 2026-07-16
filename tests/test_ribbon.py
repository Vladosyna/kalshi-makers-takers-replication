from __future__ import annotations

from kalshi_mt.fees.ribbon import compute_ribbon, default_fee_grid


def test_default_fee_grid_starts_at_zero():
    grid = default_fee_grid(0.07, plausible_band=0.5)
    assert grid[0] == 0.0
    assert min(grid) == 0.0


def test_default_fee_grid_spans_plausible_band():
    grid = default_fee_grid(0.07, plausible_band=0.5)
    assert abs(max(grid) - 0.07 * 1.5) < 1e-6
    non_zero = [r for r in grid if r > 0]
    assert abs(min(non_zero) - 0.07 * 0.5) < 1e-6


def test_default_fee_grid_zero_sourced_rate_degenerate():
    grid = default_fee_grid(0.0, plausible_band=0.5)
    assert grid == [0.0]


def test_compute_ribbon_finds_exact_break_even_on_linear_margin():
    # margin(rate) = 10 - 100*rate -- crosses zero at rate=0.10 exactly.
    margin_fn = lambda r: 10.0 - 100.0 * r
    rates = [0.0, 0.05, 0.08, 0.09, 0.10, 0.11, 0.15, 0.20]
    result = compute_ribbon(margin_fn, rates)
    assert result.break_even_rate is not None
    assert abs(result.break_even_rate - 0.10) < 1e-9
    assert result.sign_flips is True
    assert result.fragile is True


def test_compute_ribbon_no_sign_flip_not_fragile():
    # margin always positive across the whole grid.
    margin_fn = lambda r: 5.0 - r  # never crosses zero for r in [0, 1)
    rates = [0.0, 0.1, 0.2, 0.3]
    result = compute_ribbon(margin_fn, rates)
    assert result.sign_flips is False
    assert result.fragile is False
    assert result.break_even_rate is None


def test_compute_ribbon_always_negative_not_fragile():
    margin_fn = lambda r: -1.0 - r
    rates = [0.0, 0.1, 0.2]
    result = compute_ribbon(margin_fn, rates)
    assert result.fragile is False


def test_compute_ribbon_records_margins_in_order():
    margin_fn = lambda r: r * 2
    rates = [0.0, 0.1, 0.2]
    result = compute_ribbon(margin_fn, rates)
    assert result.margins == [0.0, 0.2, 0.4]


def test_compute_ribbon_empty_rates():
    result = compute_ribbon(lambda r: r, [])
    assert result.rates == []
    assert result.break_even_rate is None
    assert result.fragile is False


def test_compute_ribbon_realistic_maker_margin_scenario():
    """A plausible R2 scenario: the maker>=50c margin is positive at the
    sourced rate (0.0175) but flips negative at the upper end of the
    plausible band -- exactly the case the fragile rule exists for."""
    sourced_rate = 0.0175
    # Margin shrinks linearly with rate, crossing zero within the band.
    margin_fn = lambda r: 0.03 - r  # positive at 0.0175 (0.0125), negative above 0.03
    grid = default_fee_grid(sourced_rate, plausible_band=1.0)  # band up to 0.035
    result = compute_ribbon(margin_fn, grid)
    assert result.fragile is True
    assert result.break_even_rate is not None
    assert abs(result.break_even_rate - 0.03) < 1e-6
