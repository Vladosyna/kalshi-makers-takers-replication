"""Kalshi fee schedule lookup (spec S3/S4) -- right-continuous step function
keyed on trade FILL timestamp, never seeded from a "current" row backwards
(data/fees.yaml's own header comment explains the sourcing and its one
known gap -- pre-2022-09-22 data, see FeeScheduleGapError below).

Fee formula (both roles): fee_usd = ceil_to_cent(rate * C * P * (1-P)), the
ceiling applied to the ORDER TOTAL for C contracts, not a per-contract
rounding then multiplied out -- BDW's own construction ("total rounded up
to the next cent"). Return convention is pinned separately in
docs/analysis_plan.md S3.1 (r = (payout - P - f) / P) -- this module only
computes the fee itself.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

import yaml

Role = Literal["maker", "taker"]


class FeeScheduleGapError(RuntimeError):
    """Raised when a lookup timestamp predates every known fee entry for a
    role -- a right-continuous step function has nothing to return here.
    Silently defaulting (to 0.0, or to the earliest known rate) would
    corrupt every downstream net-of-cost number without anyone noticing;
    the approved implementation plan's own Phase 4 design is explicit that
    this must raise, not fail-soft."""


def load_fee_schedule(path: Path | None = None) -> dict[str, Any]:
    """Defaults to PROJECT_ROOT / "data" / "fees.yaml", resolved via a
    local import (not a module-level constant) so that test monkeypatching
    of util.PROJECT_ROOT actually takes effect -- a constant derived once
    at import time would freeze to whatever PROJECT_ROOT was at first
    import and silently ignore any later monkeypatch.setattr."""
    if path is None:
        from kalshi_mt.util import PROJECT_ROOT

        path = PROJECT_ROOT / "data" / "fees.yaml"
    p = path
    if not p.exists():
        return {"version": 0, "schedule": []}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("schedule", [])
    return data


def rate_for(schedule: dict[str, Any], role: Role, category: str | None, as_of_ts: str) -> float:
    """Latest entry with effective_from <= as_of_ts for (role, category),
    falling back to (role, 'default'). Raises FeeScheduleGapError if
    as_of_ts predates every known entry for this role."""
    role_entries = [e for e in schedule.get("schedule", []) if e.get("role") == role]
    if not role_entries:
        raise FeeScheduleGapError(f"no fee schedule entries at all for role={role!r}")

    earliest = min(e["effective_from"] for e in role_entries)
    if as_of_ts < earliest:
        raise FeeScheduleGapError(
            f"as_of_ts={as_of_ts!r} predates the earliest known {role!r} fee entry "
            f"({earliest!r}) -- a genuine gap in data/fees.yaml, not a bug to paper "
            "over with a default rate."
        )

    applicable = [e for e in role_entries if e.get("effective_from", "") <= as_of_ts]
    for cat in (category, "default"):
        matching = [e for e in applicable if e.get("category") == cat]
        if matching:
            return float(max(matching, key=lambda e: e["effective_from"])["rate"])
    # applicable is non-empty (checked above) but nothing matched category
    # or 'default' -- fall back to the latest applicable row regardless of
    # its category rather than raising on a category-taxonomy mismatch.
    return float(max(applicable, key=lambda e: e["effective_from"])["rate"])


def fee_usd_for(
    schedule: dict[str, Any], role: Role, category: str | None,
    contract_count: float, price_dollars: float, as_of_ts: str,
) -> float:
    """fee = ceil_to_cent(rate * C * P * (1-P)) on the ORDER TOTAL for
    `contract_count` contracts -- matches BDW's own construction exactly,
    not a per-contract-then-multiplied rounding."""
    rate = rate_for(schedule, role, category, as_of_ts)
    raw_cents = rate * contract_count * price_dollars * (1.0 - price_dollars) * 100.0
    # Tiny epsilon guards an exact-cent float (e.g. 1.7500000000000002 from
    # binary floating point) from being spuriously rounded up an extra cent.
    return math.ceil(raw_cents - 1e-9) / 100.0


def fee_usd_bdw_illustration(
    schedule: dict[str, Any], role: Role, category: str | None,
    price_dollars: float, as_of_ts: str,
) -> float:
    """BDW's own illustrative figure: the fee on a FIXED 100-contract order
    (their own "~1.77% at 50c" line, spec S1). Reported ALONGSIDE the
    actual-order-size fee_usd_for figure, never as a substitute for it."""
    return fee_usd_for(schedule, role, category, 100.0, price_dollars, as_of_ts)
