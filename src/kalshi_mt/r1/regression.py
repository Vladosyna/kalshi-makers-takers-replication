"""R1 Mincer-Zarnowitz regression (spec S1): (Y-P) = alpha + psi*P + epsilon
in cents, Yes-only, event-clustered standard errors.

Clustering note (docs/analysis_plan.md S1, spec S4): with contracts nested
in events (each Kalshi ticker belongs to exactly one event_ticker),
Cameron-Gelbach-Miller two-way (event, contract) variance reduces
algebraically to one-way event clustering -- V_2way = V_event + V_contract -
V_intersection, and nesting makes the intersection grouping identical to
grouping by contract alone, so V_intersection = V_contract and the two
contract terms cancel exactly. This module implements the reduced, correct
form directly (one-way clustering on event_ticker) rather than running a
two-way routine that would just reproduce the same numbers with extra
code and extra room for a library quirk to go unnoticed.
`verify_two_way_equals_one_way_clustering` is the one-time numerical proof
spec S4 asks for ("verify it numerically once"), not something every fit
re-runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl
import statsmodels.api as sm


@dataclass
class MZResult:
    n: int
    n_clusters: int
    alpha: float
    alpha_se: float
    psi: float
    psi_se: float
    f_stat: float
    f_pvalue: float


def _cluster_ids(df: pl.DataFrame) -> np.ndarray:
    """event_ticker where known, falling back to the row's own ticker -- a
    market Pass 1 hasn't resolved a series/event for yet still clusters on
    itself rather than being silently dropped from the fit."""
    event = df["event_ticker"].to_list()
    ticker = df["ticker"].to_list()
    return np.array([e if e else t for e, t in zip(event, ticker)])


def fit_mz_regression(yes_only: pl.DataFrame) -> MZResult | None:
    """Yes-only panel in, one fitted MZ regression out. Returns None (never
    raises) whenever the subset can't support a well-defined 2-parameter
    clustered fit: empty, fewer than 2 event clusters, n<=2 (a saturated
    2-parameter model has zero residual degrees of freedom -- statsmodels'
    small-sample cluster correction divides by nobs-k_params and would
    ZeroDivisionError), or zero variance in P (a degenerate, non-full-rank
    design matrix -- statsmodels' add_constant would silently collapse to a
    single column and shift every coefficient index, not raise). These are
    real conditions thin by-year/by-category subsets can hit in practice,
    not merely theoretical."""
    if yes_only.is_empty() or len(yes_only) <= 2:
        return None

    y = yes_only["y"].to_numpy()
    p = yes_only["p"].to_numpy()
    y_minus_p_cents = (y - p) * 100.0
    p_cents = p * 100.0

    if np.ptp(p_cents) == 0:  # zero variance -- every price identical
        return None

    clusters = _cluster_ids(yes_only)
    n_clusters = len(np.unique(clusters))
    if n_clusters < 2:
        return None

    x = sm.add_constant(p_cents, has_constant="add")
    fit = sm.OLS(y_minus_p_cents, x).fit(cov_type="cluster", cov_kwds={"groups": clusters})

    return MZResult(
        n=len(yes_only), n_clusters=n_clusters,
        alpha=float(fit.params[0]), alpha_se=float(fit.bse[0]),
        psi=float(fit.params[1]), psi_se=float(fit.bse[1]),
        f_stat=float(fit.fvalue), f_pvalue=float(fit.f_pvalue),
    )


def verify_two_way_equals_one_way_clustering(yes_only: pl.DataFrame) -> dict[str, Any]:
    """Numerical proof of the module docstring's algebraic claim. Fits the
    same regression with one-way event clustering and with two-way
    (event, contract) clustering and asserts the SEs match to floating-
    point tolerance."""
    if yes_only.is_empty():
        return {"verified": False, "reason": "empty panel"}

    y = yes_only["y"].to_numpy()
    p = yes_only["p"].to_numpy()
    y_minus_p_cents = (y - p) * 100.0
    p_cents = p * 100.0
    x = sm.add_constant(p_cents)

    event_clusters = _cluster_ids(yes_only)
    contract_clusters = yes_only["ticker"].to_numpy()

    if len(np.unique(event_clusters)) < 2:
        return {"verified": False, "reason": "fewer than 2 event clusters"}

    # statsmodels' two-way clustering (cov_cluster_2groups) views the groups
    # array as a structured record to combine cluster ids, which fails on
    # modern numpy for string/object dtype arrays ("Cannot change data-type
    # for array of references"). Integer-coding the labels first sidesteps
    # this library/numpy-version interaction entirely -- the codes carry
    # the same clustering information as the original strings.
    event_codes = np.unique(event_clusters, return_inverse=True)[1]
    contract_codes = np.unique(contract_clusters, return_inverse=True)[1]

    one_way = sm.OLS(y_minus_p_cents, x).fit(cov_type="cluster", cov_kwds={"groups": event_codes})
    two_way = sm.OLS(y_minus_p_cents, x).fit(
        cov_type="cluster",
        cov_kwds={"groups": np.column_stack([event_codes, contract_codes])},
    )

    alpha_match = bool(np.isclose(one_way.bse[0], two_way.bse[0], rtol=1e-6))
    psi_match = bool(np.isclose(one_way.bse[1], two_way.bse[1], rtol=1e-6))
    return {
        "verified": alpha_match and psi_match,
        "one_way_alpha_se": float(one_way.bse[0]), "two_way_alpha_se": float(two_way.bse[0]),
        "one_way_psi_se": float(one_way.bse[1]), "two_way_psi_se": float(two_way.bse[1]),
    }
