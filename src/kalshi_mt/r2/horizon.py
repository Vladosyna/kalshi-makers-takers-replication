"""R2 horizon robustness (docs/analysis_plan.md S2.6): re-estimates the
composition-weighted delta_bar_fee / delta_bar_pub headline test statistic
(the same weighted-average-of-CategoryR2Result.fee/publication.delta
construction as r2/decomposition.py's own within component, collapsed to
just that term since these robustness checks always use the frozen 2024
mix as weights -- there is no separate R2-window weight set here, so
between is not meaningful and is not computed) under two specs that strip
out horizon-to-close composition as a possible confound:

1. Horizon-stratified: refit every category's boundary-interacted MZ
   regression (r2/regression.py's fit_category_r2, via fit_all_categories)
   separately WITHIN each lookback_day bucket (0..10), rather than pooling
   all ~11 observations per contract together -- if delta_bar were a
   horizon-composition artifact (e.g. the fee boundary happens to coincide
   with a shift in which lookback days get sampled), it should vary
   sharply across buckets; a genuine price-level effect should be broadly
   stable across horizons.
2. One-observation-per-contract: refit on the lookback_day == 0 subset
   only (the close-day trade -- already exactly one row per ticker by
   r1/panel.py's construction), which by construction cannot carry any
   horizon-composition confound since there is no within-contract horizon
   variation left to compose. Mechanically this is bucket 0 of the
   stratified sweep above; run_horizon_robustness extracts it from
   `by_bucket` rather than recomputing.

Per spec: "A delta_bar that survives both is treated as evidence against
pure horizon-composition drift; one that doesn't is reported as such, not
discarded" -- this module computes and reports both re-estimates. It does
not itself render a verdict: r2/verdicts.py's determine_verdict remains
the sole verdict-issuing function, applied by report code to whichever
delta_bar estimate is under discussion.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from kalshi_mt.r2.regression import CategoryR2Result, fit_all_categories

CLOSE_DAY_LOOKBACK = 0


@dataclass
class HorizonBucketResult:
    lookback_day: int
    n: int
    delta_bar_fee: float | None
    delta_bar_pub: float | None
    n_categories_fit: int


@dataclass
class HorizonRobustnessResult:
    by_bucket: list[HorizonBucketResult]
    close_only: HorizonBucketResult | None


def composition_weighted_delta_bar(
    category_fits: dict[str, CategoryR2Result],
    frozen_2024_mix: dict[str, float],
    boundary: str = "fee",
) -> float | None:
    """Sum_c w_bar_c * delta_{b,c} over categories present in BOTH
    category_fits and the frozen mix -- spec S2.1's primary test statistic
    construction, and algebraically identical to r2/decomposition.py's
    `within` component when r2_category_weights equals frozen_2024_mix
    (between collapses to zero in that case, so this module never needs
    to build a separate r2-window weight set). Returns None when no
    category in category_fits carries a nonzero frozen weight -- there is
    nothing to weight."""
    if not category_fits:
        return None
    total = 0.0
    any_weight = False
    for category, fit in category_fits.items():
        w_bar = frozen_2024_mix.get(category, 0.0)
        if w_bar == 0.0:
            continue
        any_weight = True
        boundary_coef = fit.fee if boundary == "fee" else fit.publication
        total += w_bar * boundary_coef.delta
    return total if any_weight else None


def _fit_and_weight(
    df: pl.DataFrame, frozen_2024_mix: dict[str, float],
    n_wild_bootstrap: int, seed: int,
) -> tuple[dict[str, CategoryR2Result], float | None, float | None]:
    fits = fit_all_categories(df, n_wild_bootstrap=n_wild_bootstrap, seed=seed)
    delta_bar_fee = composition_weighted_delta_bar(fits, frozen_2024_mix, "fee")
    delta_bar_pub = composition_weighted_delta_bar(fits, frozen_2024_mix, "publication")
    return fits, delta_bar_fee, delta_bar_pub


def horizon_stratified(
    yes_only: pl.DataFrame, frozen_2024_mix: dict[str, float],
    n_wild_bootstrap: int = 999, seed: int = 0,
) -> list[HorizonBucketResult]:
    """Re-estimates delta_bar_fee/delta_bar_pub separately within each
    lookback_day bucket present in the panel. A bucket with too little
    data to fit any category (fit_all_categories returns {}) still
    appears in the output with delta_bar fields None and
    n_categories_fit=0 -- a documented gap, not a silently-dropped
    bucket."""
    if yes_only.is_empty():
        return []
    results: list[HorizonBucketResult] = []
    for lookback_day in sorted(yes_only["lookback_day"].unique().to_list()):
        bucket = yes_only.filter(pl.col("lookback_day") == lookback_day)
        fits, delta_bar_fee, delta_bar_pub = _fit_and_weight(
            bucket, frozen_2024_mix, n_wild_bootstrap, seed,
        )
        results.append(HorizonBucketResult(
            lookback_day=lookback_day, n=len(bucket),
            delta_bar_fee=delta_bar_fee, delta_bar_pub=delta_bar_pub,
            n_categories_fit=len(fits),
        ))
    return results


def close_only_spec(
    yes_only: pl.DataFrame, frozen_2024_mix: dict[str, float],
    n_wild_bootstrap: int = 999, seed: int = 0,
) -> HorizonBucketResult | None:
    """The one-observation-per-contract spec on its own, without running
    the full horizon-stratified sweep -- useful for callers that only
    want this cheaper check. Returns None on an empty panel or a panel
    with no lookback_day==0 rows."""
    if yes_only.is_empty():
        return None
    close_only = yes_only.filter(pl.col("lookback_day") == CLOSE_DAY_LOOKBACK)
    if close_only.is_empty():
        return None
    fits, delta_bar_fee, delta_bar_pub = _fit_and_weight(
        close_only, frozen_2024_mix, n_wild_bootstrap, seed,
    )
    return HorizonBucketResult(
        lookback_day=CLOSE_DAY_LOOKBACK, n=len(close_only),
        delta_bar_fee=delta_bar_fee, delta_bar_pub=delta_bar_pub,
        n_categories_fit=len(fits),
    )


def run_horizon_robustness(
    yes_only: pl.DataFrame, frozen_2024_mix: dict[str, float],
    n_wild_bootstrap: int = 999, seed: int = 0,
) -> HorizonRobustnessResult:
    """Both spec S2.6 re-estimates in one call. close_only is extracted
    from by_bucket's lookback_day==0 entry rather than recomputed via
    close_only_spec -- same fit, no reason to pay for it twice."""
    by_bucket = horizon_stratified(yes_only, frozen_2024_mix, n_wild_bootstrap=n_wild_bootstrap, seed=seed)
    close_only = next((b for b in by_bucket if b.lookback_day == CLOSE_DAY_LOOKBACK), None)
    return HorizonRobustnessResult(by_bucket=by_bucket, close_only=close_only)
