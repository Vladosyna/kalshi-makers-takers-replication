"""R2 composition decomposition (docs/analysis_plan.md S2.3):

Delta_psi_agg = Sum_c w_bar_c * Delta_psi_c        (within)
              + Sum_c Delta_w_c * psi_bar_c         (between)

where w_bar_c is the FROZEN calendar-2024 category weight (Phase 3's own
data/frozen_2024_mix.json artifact), Delta_psi_c is category c's own
fitted boundary slope-shift (delta_{b,c} from r2/regression.py's
CategoryR2Result), Delta_w_c is category c's R2-window weight minus its
frozen 2024 weight, and psi_bar_c is category c's own baseline
(pre-boundary) slope within the R2 regression (psi_c from the same fit).

Delta_psi_agg is DEFINED as within + between -- a constructive
decomposition, not an independently-measured aggregate separately checked
against two components that happen to sum to it. The within/between split
IS the aggregate summary this analysis reports (spec's own "decompose
Delta_psi_agg = ..." framing). Any narrative sentence about "the bias"
changing refers to the within component only; between is reported
alongside, never folded into that claim.

Sports (zero weight in the frozen 2024 mix, since it did not exist yet)
naturally falls out of the WITHIN term (w_bar=0 there) but still
contributes to BETWEEN via its nonzero R2-window weight -- exactly
capturing "a new category appeared and shifted the composition" without
needing special-case code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from kalshi_mt.r2.regression import CategoryR2Result
from kalshi_mt.r2.verdicts import DeltaBarEstimate


@dataclass
class DecompositionResult:
    within: float
    between: float
    aggregate: float  # within + between, by definition
    per_category: dict[str, dict[str, float]] = field(default_factory=dict)


def category_weights_from_panel(yes_only_r2: pl.DataFrame) -> dict[str, float]:
    """R2-window category shares by CONTRACT count -- one row per ticker,
    matching Phase 3's own frozen-2024-mix convention (r1/reconcile.py's
    compute_calendar_2024_mix dedups the same way, for the same reason:
    the Yes-only panel has up to 11 price rows per contract)."""
    if yes_only_r2.is_empty():
        return {}
    contracts = yes_only_r2.unique(subset=["ticker"])
    counts = contracts.group_by("category").len()
    total = counts["len"].sum()
    if total == 0:
        return {}
    return {(row["category"] or "unknown"): row["len"] / total for row in counts.iter_rows(named=True)}


def decompose(
    category_fits: dict[str, CategoryR2Result],
    frozen_2024_mix: dict[str, float],
    r2_category_weights: dict[str, float],
    boundary: str = "fee",
) -> DecompositionResult:
    """boundary: 'fee' or 'publication' -- selects which BoundaryCoefficients
    field on each CategoryR2Result supplies Delta_psi_c. Only categories
    with a real R2 fit contribute (a category with insufficient R2 data to
    fit at all has no delta_psi_c to decompose -- a documented limitation,
    not silently forced to zero)."""
    if not category_fits:
        return DecompositionResult(within=0.0, between=0.0, aggregate=0.0)

    within_total = 0.0
    between_total = 0.0
    per_category: dict[str, dict[str, float]] = {}

    for category, fit in category_fits.items():
        w_bar = frozen_2024_mix.get(category, 0.0)
        w_r2 = r2_category_weights.get(category, 0.0)
        delta_w = w_r2 - w_bar
        boundary_coef = fit.fee if boundary == "fee" else fit.publication
        delta_psi_c = boundary_coef.delta
        psi_bar_c = fit.psi_c

        within_c = w_bar * delta_psi_c
        between_c = delta_w * psi_bar_c
        within_total += within_c
        between_total += between_c
        per_category[category] = {
            "w_bar": w_bar, "w_r2": w_r2, "delta_w": delta_w,
            "delta_psi_c": delta_psi_c, "psi_bar_c": psi_bar_c,
            "within_contribution": within_c, "between_contribution": between_c,
        }

    return DecompositionResult(
        within=within_total, between=between_total,
        aggregate=within_total + between_total, per_category=per_category,
    )


def delta_bar_with_ci(
    category_fits: dict[str, CategoryR2Result],
    frozen_2024_mix: dict[str, float],
    boundary: str = "fee",
) -> DeltaBarEstimate | None:
    """The primary composition-weighted test statistic (spec S2.1) WITH a
    95% confidence interval -- the input r2/verdicts.py's determine_verdict
    needs, and the piece this module didn't originally produce (`decompose`
    above returns point estimates only).

    delta_bar = Sum_c w_bar_c * delta_{b,c} -- the same construction as
    `within` above when the r2-window weight set equals frozen_2024_mix
    (between collapses to zero in that case), and the same point estimate
    r2/horizon.py's composition_weighted_delta_bar computes for the
    horizon-robustness checks, which don't carry a formal CI because spec
    S2.6 treats them as a descriptive pass/fail on the point estimate, not
    a second verdict-bearing test.

    The CI treats each category's own delta_{b,c} as STATISTICALLY
    INDEPENDENT of every other category's -- not an approximation of
    convenience, but a direct consequence of the existing design:
    categories are non-overlapping partitions of the panel (a market
    belongs to exactly one category), and r2/regression.py's
    fit_category_r2 already fits each one in its own separate regression.
    Under independence, the variance of a weighted SUM of independent
    estimators is the weighted sum of their variances:

        Var(delta_bar) = Sum_c w_bar_c^2 * Var(delta_{b,c})

    using each category's own delta_se (whichever fit_category_r2 already
    computed for it -- asymptotic cluster-robust or wild-bootstrap). The
    reported interval is the standard 95% normal-approximation interval
    (+-1.96 SE), the same convention fit_category_r2 itself uses on its
    asymptotic path.

    Returns None under the same "nothing to weight" condition as
    composition_weighted_delta_bar: no category in category_fits carries a
    nonzero frozen weight."""
    if not category_fits:
        return None
    delta_bar = 0.0
    variance = 0.0
    any_weight = False
    for category, fit in category_fits.items():
        w_bar = frozen_2024_mix.get(category, 0.0)
        if w_bar == 0.0:
            continue
        any_weight = True
        boundary_coef = fit.fee if boundary == "fee" else fit.publication
        delta_bar += w_bar * boundary_coef.delta
        variance += (w_bar ** 2) * (boundary_coef.delta_se ** 2)
    if not any_weight:
        return None
    se_bar = variance ** 0.5
    return DeltaBarEstimate(
        delta_bar=delta_bar, ci_lo=delta_bar - 1.96 * se_bar, ci_hi=delta_bar + 1.96 * se_bar,
    )
