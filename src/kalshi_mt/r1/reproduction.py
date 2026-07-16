"""R1 reproduction report (spec S1/S2): by-year psi vs BDW Table 9,
by-category psi vs Table 8, win-rate-vs-price curve vs Fig 3, returns-by-
band vs Fig 5, maker/taker split vs Fig 6/Table 10.

Verdict vocabulary (confirmed/partially confirmed/diverged,
docs/analysis_plan.md S1) is applied only where BDW give an exact numeric
target: by-year psi (Table 9). Everywhere else BDW describe a PATTERN, not
a point target (e.g. Table 8: "smaller/insignificant for politics and
entertainment"), so this module reports those comparisons descriptively
rather than forcing a verdict label onto a target the source paper never
pinned to a number.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from kalshi_mt.fees.schedule import FeeScheduleGapError, fee_usd_for
from kalshi_mt.r1.panel import price_band
from kalshi_mt.r1.regression import MZResult, fit_mz_regression
from kalshi_mt.util import now_utc_iso

# BDW Table 9 (spec S1): the one exact-number by-year reproduction target.
# 2025 covers Jan-Apr only (R1's own window boundary), not the full year.
BDW_PSI_BY_YEAR: dict[str, float] = {
    "2021": 0.041, "2022": 0.023, "2023": 0.036, "2024": 0.048, "2025": 0.021,
}


def _year_label(close_time_epoch: int | None) -> str | None:
    if close_time_epoch is None:
        return None
    return str(datetime.fromtimestamp(close_time_epoch, tz=timezone.utc).year)


def _verdict(fit: MZResult, bdw_psi: float) -> str:
    """confirmed: same sign as BDW AND our 95% CI contains BDW's point
    estimate. partially_confirmed: same sign, CI excludes BDW's point (a
    materially different magnitude). diverged: opposite sign."""
    ci_lo = fit.psi - 1.96 * fit.psi_se
    ci_hi = fit.psi + 1.96 * fit.psi_se
    if (fit.psi > 0) != (bdw_psi > 0):
        return "diverged"
    if ci_lo <= bdw_psi <= ci_hi:
        return "confirmed"
    return "partially_confirmed"


def by_year_psi(yes_only: pl.DataFrame) -> dict[str, dict[str, Any]]:
    if yes_only.is_empty():
        return {}
    df = yes_only.with_columns(
        pl.col("close_time_epoch").map_elements(_year_label, return_dtype=pl.String).alias("year")
    ).filter(pl.col("year").is_not_null())

    results: dict[str, dict[str, Any]] = {}
    for year in sorted(df["year"].unique().to_list()):
        fit = fit_mz_regression(df.filter(pl.col("year") == year))
        target = BDW_PSI_BY_YEAR.get(year)
        results[year] = {
            "fit": fit, "bdw_psi": target,
            "verdict": _verdict(fit, target) if (fit is not None and target is not None) else "insufficient_data",
        }
    return results


def by_category_psi(yes_only: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """No exact BDW target per category (Table 8 gives a pattern only) --
    reported descriptively, verdict left to the reader/paper prose."""
    if yes_only.is_empty():
        return {}
    results: dict[str, dict[str, Any]] = {}
    for category in sorted(c for c in yes_only["category"].unique().to_list() if c):
        results[category] = {"fit": fit_mz_regression(yes_only.filter(pl.col("category") == category))}
    return results


def win_rate_by_band(doubled: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """Fig 3's win-rate-vs-price curve, doubled Yes+No basis."""
    if doubled.is_empty():
        return {}
    df = doubled.with_columns(pl.col("p").map_elements(price_band, return_dtype=pl.String).alias("band"))
    results: dict[str, dict[str, Any]] = {}
    for (band,), group in df.group_by("band"):
        results[band] = {"n": len(group), "win_rate": float(group["y"].mean()), "mean_price": float(group["p"].mean())}
    return results


def returns_by_band(yes_only: pl.DataFrame, fee_schedule: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Fig 5's returns-by-10c-band: buy at the closing-day entry price
    (lookback_day=0), hold to resolution. Gross (f=0) and net-of-taker-fee
    (BDW's own "takers only" fee model in their window), per
    docs/analysis_plan.md S3.1: r = (payout - P - f) / P. A market whose
    close_time predates the fee schedule's earliest entry (data/fees.yaml's
    documented gap) contributes to the gross figure but is excluded from
    net -- not silently zero-feed."""
    day0 = yes_only.filter(pl.col("lookback_day") == 0)
    if day0.is_empty():
        return {}
    df = day0.with_columns(pl.col("p").map_elements(price_band, return_dtype=pl.String).alias("band"))

    results: dict[str, dict[str, Any]] = {}
    for (band,), group in df.group_by("band"):
        gross, net = [], []
        gap_excluded = 0
        for row in group.iter_rows(named=True):
            p, payout = row["p"], row["y"]
            if p <= 0:
                continue
            gross.append((payout - p) / p)
            as_of = datetime.fromtimestamp(row["close_time_epoch"], tz=timezone.utc).isoformat()
            try:
                fee = fee_usd_for(fee_schedule, "taker", row["category"], 1.0, p, as_of)
                net.append((payout - p - fee) / p)
            except FeeScheduleGapError:
                gap_excluded += 1
        results[band] = {
            "n": len(group),
            "mean_gross_return": float(np.mean(gross)) if gross else None,
            "mean_net_return": float(np.mean(net)) if net else None,
            "fee_schedule_gap_excluded": gap_excluded,
        }
    return results


def maker_taker_split(
    trades: pl.DataFrame, resolutions: dict[str, str], in_scope_tickers: set[str],
) -> dict[str, Any]:
    """Fig 6/Table 10: average return to makers vs takers, and maker share
    by price band. Every trade has exactly one taker (taker_outcome_side)
    and one implicit maker on the opposite outcome side at the
    complementary price (1 - yes_price) -- doubled basis by construction,
    matching how Fig 6's maker-share curve is itself defined.

    `trades` is Pass 2's raw Parquet trade tape (store/parquet.py's
    TradeStore.read_all() or read_for_ticker, already filtered to
    in_scope_tickers by the caller if desired -- filtering here too for
    safety). `resolutions` is {ticker: 'yes'|'no'}.
    """
    if trades.is_empty() or not in_scope_tickers:
        return {"maker_return": None, "taker_return": None, "maker_share_by_band": {}}

    trades = trades.filter(pl.col("ticker").is_in(list(in_scope_tickers)))
    maker_returns: list[float] = []
    taker_returns: list[float] = []
    band_counts: dict[str, dict[str, int]] = {}

    for row in trades.iter_rows(named=True):
        result = resolutions.get(row["ticker"])
        taker_side = row["taker_outcome_side"]
        yes_price = row["yes_price_dollars"]
        if result not in ("yes", "no") or taker_side not in ("yes", "no") or yes_price is None:
            continue
        if not (0.0 < yes_price < 1.0):
            continue
        payout_yes = 1.0 if result == "yes" else 0.0

        yes_role = "taker" if taker_side == "yes" else "maker"
        no_role = "taker" if taker_side == "no" else "maker"

        for side_price, side_role, side_payout in (
            (yes_price, yes_role, payout_yes),
            (1.0 - yes_price, no_role, 1.0 - payout_yes),
        ):
            band = price_band(side_price)
            counts = band_counts.setdefault(band, {"maker": 0, "total": 0})
            counts["total"] += 1
            side_return = (side_payout - side_price) / side_price
            if side_role == "maker":
                counts["maker"] += 1
                maker_returns.append(side_return)
            else:
                taker_returns.append(side_return)

    maker_share_by_band = {
        band: (counts["maker"] / counts["total"] if counts["total"] else None)
        for band, counts in band_counts.items()
    }
    return {
        "maker_return": float(np.mean(maker_returns)) if maker_returns else None,
        "taker_return": float(np.mean(taker_returns)) if taker_returns else None,
        "n_maker_obs": len(maker_returns),
        "n_taker_obs": len(taker_returns),
        "maker_share_by_band": maker_share_by_band,
    }


def write_divergence_log(report: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# R1 divergence log -- {now_utc_iso()}", ""]

    lines += ["## By-year psi vs BDW Table 9", "", "| Year | Our psi | BDW psi | Verdict |", "|---|---|---|---|"]
    for year, entry in sorted(report.get("by_year_psi", {}).items()):
        fit = entry["fit"]
        our_psi = f"{fit.psi:.4f}" if fit else "n/a"
        bdw = entry["bdw_psi"]
        lines.append(f"| {year} | {our_psi} | {bdw if bdw is not None else 'n/a'} | {entry['verdict']} |")
    lines.append("")

    lines += ["## By-category psi (descriptive, no BDW point target)", "", "| Category | psi | n |", "|---|---|---|"]
    for category, entry in sorted(report.get("by_category_psi", {}).items()):
        fit = entry["fit"]
        if fit:
            lines.append(f"| {category} | {fit.psi:.4f} | {fit.n} |")
        else:
            lines.append(f"| {category} | insufficient data | - |")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
