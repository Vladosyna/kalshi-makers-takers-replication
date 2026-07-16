"""R2 verdict binding (docs/analysis_plan.md S2.2): persisted / attenuated /
vanished / reversed / indeterminate, bound to formal cluster-robust tests on
the composition-weighted delta_bar -- never inferred by comparing
significance stars across separately-estimated per-window regressions (the
informal trap BDW's own "diminishing bias" language falls into, and the
trap this repo's own methodological review flagged explicitly).

Verdict definitions, verbatim from the analysis plan:

  persisted:     fail to reject delta_bar=0 AND reject delta_bar=-psi_bar_R1
                 (full disappearance ruled out)
  attenuated:    reject delta_bar=0, with -psi_bar_R1 < delta_bar < 0
  vanished:      fail to reject delta_bar=-psi_bar_R1 AND reject delta_bar=0
  reversed:      reject delta_bar=0, with psi_bar_R1+delta_bar significantly < 0
  indeterminate: none of the above cleanly hold -- reported as such, never
                 forced into the nearest label

Implementation note: the four named verdicts partition the CI's
relationship to two reference points, {0, -psi_bar_R1}, into four
mutually-exclusive cases (reject-neither -> indeterminate; reject-zero-only
-> persisted; reject-full-disappearance-only -> vanished; reject-both ->
split further by where delta_bar itself landed, and by a second
significance test on the COMBINED slope psi_bar_R1+delta_bar for
reversed). This resolves the spec's four prose definitions into a single,
deterministic decision tree rather than leaving overlapping conditions to
be checked in an ad hoc order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Verdict = Literal["persisted", "attenuated", "vanished", "reversed", "indeterminate"]


@dataclass
class DeltaBarEstimate:
    """A composition-weighted delta_bar and its 95% CI -- the input to
    determine_verdict. Callers compute this via the composition-weighted
    average of per-category delta_{b,c} estimates (r2/decomposition.py's
    within component is exactly this weighted sum, before dividing by
    total weight)."""
    delta_bar: float
    ci_lo: float
    ci_hi: float


def _excludes_zero(ci_lo: float, ci_hi: float) -> bool:
    return ci_lo > 0 or ci_hi < 0


def _contains(value: float, ci_lo: float, ci_hi: float) -> bool:
    return ci_lo <= value <= ci_hi


def _strictly_between(x: float, a: float, b: float) -> bool:
    lo, hi = min(a, b), max(a, b)
    return lo < x < hi


def determine_verdict(estimate: DeltaBarEstimate, psi_bar_r1: float) -> Verdict:
    """psi_bar_r1: R1's reproduced full-sample psi (r1/regression.py's
    MZResult.psi from the R1 fit) -- the reference "full bias" level that
    "full disappearance" (delta_bar = -psi_bar_r1) and "reversal"
    (psi_bar_r1 + delta_bar significantly < 0) are measured against."""
    full_disappearance = -psi_bar_r1
    reject_zero = _excludes_zero(estimate.ci_lo, estimate.ci_hi)
    reject_full_disappearance = not _contains(full_disappearance, estimate.ci_lo, estimate.ci_hi)

    if not reject_zero and reject_full_disappearance:
        return "persisted"
    if reject_zero and not reject_full_disappearance:
        return "vanished"
    if not reject_zero and not reject_full_disappearance:
        # The CI contains both 0 and full disappearance -- this estimate
        # cannot distinguish "no change" from "the bias fully vanished."
        return "indeterminate"

    # reject_zero AND reject_full_disappearance: delta_bar is confidently
    # away from both reference points. Which side it landed on (and, if
    # past full disappearance, whether the COMBINED slope is itself
    # significantly negative) separates attenuated from reversed.
    if _strictly_between(estimate.delta_bar, full_disappearance, 0.0):
        return "attenuated"

    combined_lo = psi_bar_r1 + estimate.ci_lo
    combined_hi = psi_bar_r1 + estimate.ci_hi
    combined_point = psi_bar_r1 + estimate.delta_bar
    if combined_point < 0 and _excludes_zero(combined_lo, combined_hi):
        return "reversed"

    return "indeterminate"
