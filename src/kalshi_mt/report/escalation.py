"""Escalation rule (docs/analysis_plan.md S5), verbatim:

  ## 5. Escalation rule (bound to S2.2's tests, no informal language)

  Escalate from a replication note to a standalone short paper iff:

  ( delta_bar_fee rejects zero at 5%, under the primary composition-weighted test )
                                OR
  ( delta_bar_pub rejects zero at 5%, under the primary composition-weighted test )
                                OR
  ( the maker >=50c margin changes sign between layers (a) and (c)
    AND survives the entire fee-sensitivity ribbon -- i.e. is NOT labeled 'fragile' per S3.3 )

  No other trigger. 'Materially regime-shifted' is not used as a standalone
  justification anywhere in this repo's write-up -- every escalation claim
  traces to one of the three conditions above.

This module implements that three-condition OR as a single pure function.
It does no I/O and performs no fetching -- every value it needs (the two
composition-weighted delta_bar estimates with their CIs, the two maker
margins, and the fee-sensitivity ribbon) is computed elsewhere and passed
in directly.

"Rejects zero at 5%" reuses this repo's own established convention
(r2/verdicts.py's _excludes_zero, used for S2.2's verdict tests): a 95% CI
"rejects zero" iff it strictly excludes zero (ci_lo > 0 or ci_hi < 0). A CI
edge that lands exactly on zero does NOT count as excluding it -- the
comparisons below use the same strict inequalities, not <=/>=.

None-handling: delta_bar_fee, delta_bar_pub, either maker margin, or the
ribbon can each independently be None (an estimate that could not be
computed from insufficient data -- an expected, documented possibility,
not an error condition). A None value simply means the condition(s) that
depend on it cannot fire; it never raises, and it never affects the other
conditions' evaluation. In particular, a None ribbon means the sign-flip
condition's fragility check has nothing to check, so that condition is
treated as NOT surviving the ribbon (you cannot claim a result "survives
the entire fee-sensitivity ribbon" when the ribbon was never run). If all
five inputs are None, determine_escalation returns escalate=False with an
empty triggers list -- not an error.

Sign-flip edge case: "changes sign between layers (a) and (c)" is
evaluated as sign(margin_a) != sign(margin_c), where sign is strictly
positive vs strictly negative. A margin of exactly 0.0 is deliberately
treated as having NEITHER sign -- it hasn't "flipped" to or from anything,
it's the boundary itself. This matters because the superficially
equivalent check `margin_a * margin_c < 0` happens to give the same
answer for "opposite strict signs" but for the wrong reason (it's a
product-sign trick, not a statement about what a sign flip means), and it
does not make the exactly-zero case explicit or documented -- a margin of
0.0 on either side yields product 0, which correctly fails the `< 0` test,
but only incidentally. _sign_of below makes the zero case a first-class,
explicit branch (sign 0, neither positive nor negative) so the "did it
flip" logic reads as what it means rather than relying on that
coincidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kalshi_mt.fees.ribbon import RibbonResult
from kalshi_mt.r2.verdicts import DeltaBarEstimate, _excludes_zero


@dataclass
class EscalationResult:
    escalate: bool
    triggers: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


def _sign_of(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def _sign_flip(margin_a: float, margin_c: float) -> bool:
    sign_a = _sign_of(margin_a)
    sign_c = _sign_of(margin_c)
    if sign_a == 0 or sign_c == 0:
        return False
    return sign_a != sign_c


def _delta_bar_detail(estimate: DeltaBarEstimate | None) -> dict[str, Any]:
    if estimate is None:
        return {"available": False, "delta_bar": None, "ci_lo": None, "ci_hi": None, "rejects_zero": False}
    rejects_zero = _excludes_zero(estimate.ci_lo, estimate.ci_hi)
    return {
        "available": True,
        "delta_bar": estimate.delta_bar,
        "ci_lo": estimate.ci_lo,
        "ci_hi": estimate.ci_hi,
        "rejects_zero": rejects_zero,
    }


def _maker_margin_detail(
    maker_margin_layer_a: float | None,
    maker_margin_layer_c: float | None,
    ribbon: RibbonResult | None,
) -> dict[str, Any]:
    margins_available = maker_margin_layer_a is not None and maker_margin_layer_c is not None
    sign_flip = _sign_flip(maker_margin_layer_a, maker_margin_layer_c) if margins_available else False
    ribbon_available = ribbon is not None
    ribbon_fragile = ribbon.fragile if ribbon_available else None
    survives_ribbon = ribbon_available and not ribbon.fragile
    condition_met = margins_available and sign_flip and survives_ribbon
    return {
        "margins_available": margins_available,
        "layer_a": maker_margin_layer_a,
        "layer_c": maker_margin_layer_c,
        "sign_flip": sign_flip,
        "ribbon_available": ribbon_available,
        "ribbon_fragile": ribbon_fragile,
        "survives_ribbon": survives_ribbon,
        "condition_met": condition_met,
    }


def determine_escalation(
    delta_bar_fee: DeltaBarEstimate | None,
    delta_bar_pub: DeltaBarEstimate | None,
    maker_margin_layer_a: float | None,
    maker_margin_layer_c: float | None,
    ribbon: RibbonResult | None,
) -> EscalationResult:
    """The pure S5 escalation OR. Every one of the three named conditions
    is evaluated independently -- no short-circuiting -- so `triggers` can
    report all conditions that fired, not just the first one checked."""
    detail: dict[str, Any] = {}
    triggers: list[str] = []

    fee_detail = _delta_bar_detail(delta_bar_fee)
    detail["delta_bar_fee"] = fee_detail
    if fee_detail["rejects_zero"]:
        triggers.append("delta_bar_fee_significant")

    pub_detail = _delta_bar_detail(delta_bar_pub)
    detail["delta_bar_pub"] = pub_detail
    if pub_detail["rejects_zero"]:
        triggers.append("delta_bar_pub_significant")

    margin_detail = _maker_margin_detail(maker_margin_layer_a, maker_margin_layer_c, ribbon)
    detail["maker_margin_sign_flip"] = margin_detail
    if margin_detail["condition_met"]:
        triggers.append("maker_margin_sign_flip_survives_ribbon")

    return EscalationResult(escalate=bool(triggers), triggers=triggers, detail=detail)
