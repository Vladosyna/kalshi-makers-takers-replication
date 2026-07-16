"""R2 pooled category-interacted Mincer-Zarnowitz regression
(docs/analysis_plan.md S2.1):

(Y - P) = alpha_c + psi_c*P + Sum_b [alpha_{b,c}*D_b + delta_{b,c}*(D_b*P)] + epsilon

Implementation note (a documented simplification, not a deviation from the
spec's intent): since category enters purely as interactions -- every term
is category-specific, with no coefficient pooled/shared across categories
-- running ONE giant regression with category x term interaction columns is
mathematically equivalent, for the POINT ESTIMATES, to running one SEPARATE
regression per category with just the two boundary dummies and their price
interactions. This module takes the per-category approach: simpler to
implement, test, and reason about, and each category's event-clustered SEs
come from that category's own event clusters rather than a shared
residual-variance pool a single mega-regression would otherwise impose.

Wild cluster bootstrap (spec S2.1) replaces the standard asymptotic
cluster-robust SE whenever a category has fewer than 50 event clusters --
thin categories are exactly where that asymptotic approximation is least
trustworthy. The bootstrap here follows the Cameron-Gelbach-Miller (2008)
/ Webb (2014) spirit -- Rademacher cluster weights on the NULL-restricted
model's residuals -- re-centered on the actual fitted coefficient for the
reported interval (a standard, defensible practical construction; not a
full formal WCB p-value grid inversion).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import polars as pl
import statsmodels.api as sm

WILD_BOOTSTRAP_CLUSTER_THRESHOLD = 50

FEE_BOUNDARY_EPOCH = int(datetime(2025, 5, 1, tzinfo=timezone.utc).timestamp())
PUBLICATION_BOUNDARY_EPOCH = int(datetime(2025, 9, 8, tzinfo=timezone.utc).timestamp())


@dataclass
class BoundaryCoefficients:
    alpha: float            # alpha_{b,c}: level shift at the boundary
    delta: float            # delta_{b,c}: slope shift at the boundary -- the estimand of interest
    delta_se: float
    delta_ci_lo: float
    delta_ci_hi: float
    used_wild_bootstrap: bool


@dataclass
class CategoryR2Result:
    category: str
    n: int
    n_clusters: int
    alpha_c: float
    psi_c: float
    fee: BoundaryCoefficients
    publication: BoundaryCoefficients


def _cluster_ids(df: pl.DataFrame) -> np.ndarray:
    event = df["event_ticker"].to_list()
    ticker = df["ticker"].to_list()
    return np.array([e if e else t for e, t in zip(event, ticker)])


def _wild_cluster_bootstrap_ci(
    y: np.ndarray, x: np.ndarray, clusters: np.ndarray, param_idx: int,
    n_reps: int = 999, seed: int = 0,
) -> tuple[float, float, float]:
    """Rademacher-weighted wild cluster bootstrap imposing H0: param=0 on
    the restricted model. Returns (bootstrap_se, ci_lo, ci_hi) for the
    coefficient at param_idx: the null-imposed residuals generate the
    bootstrap distribution's SPREAD, which is then re-centered on the
    actual (unrestricted) fitted coefficient for the reported interval."""
    restricted_x = np.delete(x, param_idx, axis=1)
    restricted_fit = sm.OLS(y, restricted_x).fit()
    restricted_resid = restricted_fit.resid
    restricted_fitted = restricted_fit.fittedvalues

    unique_clusters = np.unique(clusters)
    cluster_index = {c: i for i, c in enumerate(unique_clusters)}
    cluster_idx_vec = np.array([cluster_index[c] for c in clusters])
    rng = np.random.default_rng(seed)

    boot_deltas = np.empty(n_reps)
    for b in range(n_reps):
        cluster_weights = rng.choice([-1.0, 1.0], size=len(unique_clusters))
        weight_vec = cluster_weights[cluster_idx_vec]
        y_star = restricted_fitted + restricted_resid * weight_vec
        boot_fit = sm.OLS(y_star, x).fit()
        boot_deltas[b] = boot_fit.params[param_idx]

    boot_se = float(np.std(boot_deltas, ddof=1))
    original_fit = sm.OLS(y, x).fit()
    delta_hat = float(original_fit.params[param_idx])
    lo_pct, hi_pct = np.percentile(boot_deltas, [2.5, 97.5])
    half_width = (hi_pct - lo_pct) / 2.0
    return boot_se, delta_hat - half_width, delta_hat + half_width


def fit_category_r2(
    df: pl.DataFrame, category: str,
    fee_boundary_epoch: int = FEE_BOUNDARY_EPOCH,
    publication_boundary_epoch: int = PUBLICATION_BOUNDARY_EPOCH,
    n_wild_bootstrap: int = 999, seed: int = 0,
) -> CategoryR2Result | None:
    """One category's pooled fee+publication-interacted MZ regression.
    `df` should already be filtered to this category (Yes-only panel shape,
    r1/panel.py's PANEL_SCHEMA). Returns None if there are too few
    observations for the 6-parameter design, fewer than 2 event clusters,
    or zero price variance (same degenerate-fit guards as r1/regression.py)."""
    if df.is_empty() or len(df) <= 6:
        return None

    y = df["y"].to_numpy()
    p = df["p"].to_numpy()
    close_epoch = df["close_time_epoch"].to_numpy()
    y_minus_p_cents = (y - p) * 100.0
    p_cents = p * 100.0

    if np.ptp(p_cents) == 0:
        return None

    d_fee = (close_epoch >= fee_boundary_epoch).astype(float)
    d_pub = (close_epoch >= publication_boundary_epoch).astype(float)

    x = np.column_stack([
        np.ones_like(p_cents), p_cents,
        d_fee, d_fee * p_cents,
        d_pub, d_pub * p_cents,
    ])

    clusters = _cluster_ids(df)
    n_clusters = len(np.unique(clusters))
    if n_clusters < 2:
        return None

    fit = sm.OLS(y_minus_p_cents, x).fit(cov_type="cluster", cov_kwds={"groups": clusters})
    use_wild = n_clusters < WILD_BOOTSTRAP_CLUSTER_THRESHOLD

    def _boundary(alpha_idx: int, delta_idx: int) -> BoundaryCoefficients:
        alpha = float(fit.params[alpha_idx])
        delta = float(fit.params[delta_idx])
        if use_wild:
            delta_se, ci_lo, ci_hi = _wild_cluster_bootstrap_ci(
                y_minus_p_cents, x, clusters, delta_idx, n_reps=n_wild_bootstrap, seed=seed,
            )
        else:
            delta_se = float(fit.bse[delta_idx])
            ci_lo, ci_hi = delta - 1.96 * delta_se, delta + 1.96 * delta_se
        return BoundaryCoefficients(
            alpha=alpha, delta=delta, delta_se=delta_se,
            delta_ci_lo=ci_lo, delta_ci_hi=ci_hi, used_wild_bootstrap=use_wild,
        )

    return CategoryR2Result(
        category=category, n=len(df), n_clusters=n_clusters,
        alpha_c=float(fit.params[0]), psi_c=float(fit.params[1]),
        fee=_boundary(2, 3), publication=_boundary(4, 5),
    )


def fit_all_categories(
    yes_only_r2: pl.DataFrame, n_wild_bootstrap: int = 999, seed: int = 0,
) -> dict[str, CategoryR2Result]:
    """Fits every category present in the R2-window Yes-only panel.
    Categories that return None (too thin / degenerate) are simply
    omitted from the result -- callers needing to know about them should
    check panel category counts separately, not infer absence from a
    missing key here being an error."""
    if yes_only_r2.is_empty():
        return {}
    results: dict[str, CategoryR2Result] = {}
    for category in sorted(c for c in yes_only_r2["category"].unique().to_list() if c):
        fit = fit_category_r2(
            yes_only_r2.filter(pl.col("category") == category), category,
            n_wild_bootstrap=n_wild_bootstrap, seed=seed,
        )
        if fit is not None:
            results[category] = fit
    return results
