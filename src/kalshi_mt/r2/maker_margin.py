"""Maker/taker return gap and the maker >=50c margin (docs/analysis_plan.md
S2.5, S3.2), in the three fee layers, and the escalation-relevant number
S5's third trigger condition needs (S5: "the maker >=50c margin changes
sign between layers (a) and (c) AND survives the entire fee-sensitivity
ribbon").

WHY "margin" means the maker-vs-taker SPREAD, not the maker's own return:
this repo's fee history (data/fees.yaml) has the taker rate constant at
0.07 across every era, and the maker rate at 0.0 through 2025-04-30, then
0.0175 from 2025-05-01. Layer (a) is gross/zero-fee for any trade; layer
(c) is the pre-2025-05 schedule held constant and applied to post-2025
trades (fees/returns.py's COUNTERFACTUAL_AS_OF). For a MAKER's own trade,
layer (c) looks up the maker rate as of 2025-04-30 -- 0.0 -- which is
IDENTICAL to layer (a)'s zero fee. So a maker's own average return can
never differ between layers (a) and (c): "the maker's own return" cannot
change sign between them, which would make S5's trigger vacuous. The only
reading under which the trigger is meaningful is margin = mean(maker
return) - mean(taker return), restricted to the >=50c side-price band
(mirroring r1/reproduction.py's maker_taker_split, but layer-aware and
threshold-restricted instead of gross-only/10c-banded). Under layer (c)
the taker side now pays a nonzero fee while the maker side still pays
zero, which mechanically pushes margin_c more maker-favorable than
margin_a -- and CAN cross zero if the underlying gross gap was already
small or negative for makers at the >=50c band.

Per-trade role/payout logic (which side is maker vs taker, what the
payout is) mirrors r1/reproduction.py's maker_taker_split exactly; the
>=50c band check here is a simple `side_price >= 0.5` per side, not the
10c-bucketing price_band() helper used elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl

from kalshi_mt.fees.returns import counterfactual_return, gross_return, net_return


@dataclass
class MakerMarginResult:
    """margin_X = mean(maker_return_X) - mean(taker_return_X) for the
    >=50c side-price band, layer X in {a: gross, b: net-of-own-era-fees,
    c: pre-2025-05-schedule-held-constant counterfactual}. None if either
    side had zero valid observations for that layer (an empty mean is
    undefined, not zero).

    n_maker_a / n_taker_a are always the full >=50c-band observation
    counts (layer a is defined for every in-band side). n_maker_b /
    n_taker_b and n_maker_c / n_taker_c are the counts actually
    contributing to that layer's mean, i.e. excluding fee-schedule-gap
    observations for that layer specifically -- gap_excluded_b /
    gap_excluded_c report how many in-band observations were dropped for
    that reason (per side, summed across maker+taker).
    """

    layer_a: float | None
    layer_b: float | None
    layer_c: float | None
    n_maker_a: int
    n_taker_a: int
    n_maker_b: int
    n_taker_b: int
    n_maker_c: int
    n_taker_c: int
    gap_excluded_b: int
    gap_excluded_c: int


def _margin(maker: list[float], taker: list[float]) -> float | None:
    if not maker or not taker:
        return None
    return float(np.mean(maker)) - float(np.mean(taker))


def compute_maker_margin_ge_50c(
    trades: pl.DataFrame,
    resolutions: dict[str, str],
    categories: dict[str, str | None],
    fee_schedule: dict[str, Any],
    in_scope_tickers: set[str],
) -> MakerMarginResult:
    """Main entry point. Iterates every trade in `trades` restricted to
    `in_scope_tickers`; for each of its two sides (yes-side and the
    complementary no-side, same doubled-basis shape as
    r1/reproduction.py's maker_taker_split), includes that side only if
    its own price is >= 0.50, determines maker/taker role from
    taker_outcome_side, and accumulates gross/net/counterfactual returns
    per role. `resolutions` is {ticker: 'yes'|'no'}, `categories` is
    {ticker: category-or-None} (looked up by the caller, e.g. from the
    markets table), `fee_schedule` is fees/schedule.py's loaded dict.
    """
    empty = MakerMarginResult(
        layer_a=None, layer_b=None, layer_c=None,
        n_maker_a=0, n_taker_a=0, n_maker_b=0, n_taker_b=0, n_maker_c=0, n_taker_c=0,
        gap_excluded_b=0, gap_excluded_c=0,
    )
    if trades.is_empty() or not in_scope_tickers:
        return empty

    trades = trades.filter(pl.col("ticker").is_in(list(in_scope_tickers)))
    maker_a: list[float] = []
    taker_a: list[float] = []
    maker_b: list[float] = []
    taker_b: list[float] = []
    maker_c: list[float] = []
    taker_c: list[float] = []
    gap_excluded_b = 0
    gap_excluded_c = 0

    for row in trades.iter_rows(named=True):
        ticker = row["ticker"]
        result = resolutions.get(ticker)
        taker_side = row["taker_outcome_side"]
        yes_price = row["yes_price_dollars"]
        count_fp = row["count_fp"]
        created_time = row["created_time"]
        if result not in ("yes", "no") or taker_side not in ("yes", "no") or yes_price is None:
            continue
        if not (0.0 < yes_price < 1.0):
            continue
        if count_fp is None or count_fp <= 0:
            continue
        if created_time is None:
            continue
        category = categories.get(ticker)

        payout_yes = 1.0 if result == "yes" else 0.0
        yes_role = "taker" if taker_side == "yes" else "maker"
        no_role = "taker" if taker_side == "no" else "maker"

        for side_price, side_role, side_payout in (
            (yes_price, yes_role, payout_yes),
            (1.0 - yes_price, no_role, 1.0 - payout_yes),
        ):
            if side_price < 0.5:
                continue

            gross = gross_return(side_payout, side_price)
            net = net_return(fee_schedule, side_role, category, count_fp, side_payout, side_price, created_time)
            cf = counterfactual_return(fee_schedule, side_role, category, count_fp, side_payout, side_price)

            if side_role == "maker":
                maker_a.append(gross)
                if net is not None:
                    maker_b.append(net)
                else:
                    gap_excluded_b += 1
                if cf is not None:
                    maker_c.append(cf)
                else:
                    gap_excluded_c += 1
            else:
                taker_a.append(gross)
                if net is not None:
                    taker_b.append(net)
                else:
                    gap_excluded_b += 1
                if cf is not None:
                    taker_c.append(cf)
                else:
                    gap_excluded_c += 1

    return MakerMarginResult(
        layer_a=_margin(maker_a, taker_a),
        layer_b=_margin(maker_b, taker_b),
        layer_c=_margin(maker_c, taker_c),
        n_maker_a=len(maker_a),
        n_taker_a=len(taker_a),
        n_maker_b=len(maker_b),
        n_taker_b=len(taker_b),
        n_maker_c=len(maker_c),
        n_taker_c=len(taker_c),
        gap_excluded_b=gap_excluded_b,
        gap_excluded_c=gap_excluded_c,
    )
