"""Return convention and three-layer fee decomposition (docs/analysis_plan.md
S3): r = (payout - P - f) / P, pinned once.

Never subtract a per-notional fee from a per-capital return -- the
resulting 1/P bias is ~20x at 5c and ~2x at 50c, concentrated exactly on
the tail bins and the >=50c threshold the R2 headline depends on (see this
module's own tests for the worked comparison). `fee_usd_for` returns the
fee for the WHOLE order (contract_count contracts); dividing by
contract_count before subtracting from a per-contract price is what keeps
the units consistent -- that division is the entire fix.

Three fee layers for every maker/taker quantity (analysis_plan.md S3.2):
  (a) gross / zero-fee    -- no fee subtracted
  (b) net of own-era fees -- the fee schedule in force on the trade's own fill date
  (c) fee-held-constant counterfactual -- the schedule as it stood immediately
      BEFORE the 2025-05-01 fee-change boundary, applied regardless of the
      trade's own actual date -- isolates the schedule change from any real
      behavioral change that happened alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kalshi_mt.fees.schedule import FeeScheduleGapError, Role, fee_usd_for

# The last instant before the 2025-05-01 fee-change boundary (spec S1/S2) --
# layer (c) always looks the rate up as of this fixed date, never the
# trade's own date, by design.
COUNTERFACTUAL_AS_OF = "2025-04-30T23:59:59Z"


@dataclass
class ReturnLayers:
    gross: float
    net: float | None            # None if the fee schedule has a coverage gap at as_of_ts
    counterfactual: float | None  # None if the fee schedule has a coverage gap at COUNTERFACTUAL_AS_OF


def gross_return(payout: float, price: float) -> float:
    """Layer (a): r = (payout - P) / P, f = 0."""
    return (payout - price) / price


def net_return(
    schedule: dict[str, Any], role: Role, category: str | None,
    contract_count: float, payout: float, price: float, as_of_ts: str,
) -> float | None:
    """Layer (b): r = (payout - P - f) / P, f from the schedule in force at
    as_of_ts, converted to a PER-CONTRACT fee before subtracting from the
    per-contract price -- never divide a whole-order fee by 1 and subtract
    it from a single-contract return. Returns None (not an exception) on a
    fee-schedule coverage gap -- callers report this as an explicit
    exclusion count, matching r1/reproduction.py's existing convention for
    the same gap."""
    try:
        fee_usd = fee_usd_for(schedule, role, category, contract_count, price, as_of_ts)
    except FeeScheduleGapError:
        return None
    fee_per_contract = fee_usd / contract_count
    return (payout - price - fee_per_contract) / price


def counterfactual_return(
    schedule: dict[str, Any], role: Role, category: str | None,
    contract_count: float, payout: float, price: float,
) -> float | None:
    """Layer (c): this trade's return under the schedule as it stood
    immediately before the 2025-05-01 boundary, regardless of the trade's
    own actual date."""
    return net_return(schedule, role, category, contract_count, payout, price, COUNTERFACTUAL_AS_OF)


def three_layer_return(
    schedule: dict[str, Any], role: Role, category: str | None,
    contract_count: float, payout: float, price: float, as_of_ts: str,
) -> ReturnLayers:
    return ReturnLayers(
        gross=gross_return(payout, price),
        net=net_return(schedule, role, category, contract_count, payout, price, as_of_ts),
        counterfactual=counterfactual_return(schedule, role, category, contract_count, payout, price),
    )
