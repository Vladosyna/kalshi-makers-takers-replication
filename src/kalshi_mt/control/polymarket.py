"""Polymarket control-venue overlay (docs/analysis_plan.md S2.4): a
secular-trend check, NOT a difference-in-differences. Computes Kalshi's own
Mincer-Zarnowitz spec (r1/regression.py's fit_mz_regression -- reused
directly, unmodified, since spec S2.4 calls for "same MZ spec") on
Polymarket's monthly psi path over the ONLY window a control venue exists
for: 2025-05-01 through 2025-12-31 (CONTROL_END is the SII-WANGZJ archive's
own last date, not a choice made here).

Data source: the HuggingFace dataset `SII-WANGZJ/Polymarket_data`
(Claude.md S3), specifically two of its five files --
`quant.parquet` (cleaned trades unified to the YES token) and
`markets.parquet` (market metadata + resolution outcomes, joins to
quant.parquet on market id). Both are large (quant.parquet is 21-27GB) and
must stay lazy end to end (`polars.scan_parquet` + `.collect(streaming=True)`
only at the very end, on the already-filtered/joined result) -- never
`pl.read_parquet` on either file whole.

[VERIFY] Column-role detection: the exact column names in quant.parquet and
markets.parquet are not documented anywhere this repo has access to without
actually downloading and inspecting the real files (a multi-GB operation
deliberately not performed as part of writing this module). Rather than
hardcode guessed names as certainties, `_detect_column` matches against
candidate-name lists sourced from the file descriptions above and Polymarket
API conventions this codebase's own sibling project documents (condition_id,
p_yes-style fields). On first live run against the real files, verify the
detected columns look sane (spot-check a few rows) and extend the candidate
lists here if detection comes up empty -- do not silently proceed on a
None-valued detected column.

Three caveats spec S2.4 requires verbatim in any write-up that cites this
module's output (CAVEATS below), plus the coverage-gap statement: R2's
final ~6 months (2026-01 through 2026-06) have NO control venue -- the
archive ends 2025-12-31, and even a longer archive would be contaminated by
Polymarket's own 2026 fee reform (crypto Jan, sports Feb, other categories
Mar, per the sibling forecast lab's sourced data/fee_schedule.yaml). This
module refuses (raises) rather than silently truncating or extrapolating
when asked for a window outside [CONTROL_START, CONTROL_END].
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl
import yaml
from huggingface_hub import hf_hub_download

from kalshi_mt.r1.panel import PANEL_SCHEMA
from kalshi_mt.r1.regression import MZResult, fit_mz_regression

HF_REPO_ID = "SII-WANGZJ/Polymarket_data"

CONTROL_START = int(datetime(2025, 5, 1, tzinfo=timezone.utc).timestamp())
CONTROL_END = int(datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())

CAVEATS = [
    "Polymarket's tail bias is REVERSED relative to Kalshi's (Qin & Yang 2026) -- "
    "this control informs market-wide secular efficiency drift, not level or sign; "
    "'parallel trends' language must not be used.",
    "Archive provenance: a different repository, a different use (the market's own "
    "calibration path, not a model-skill claim) -- the sibling forecast lab's own "
    "archive-usage guardrail does not apply here.",
    "Spillover: BDW's publication could in principle move Polymarket too via "
    "cross-venue arbitrageurs. If the control shows a comparable delta at the same "
    "dates, that strengthens the secular-trend explanation -- it is not a defect "
    "in this design.",
]

COVERAGE_GAP_STATEMENT = (
    "The control overlay covers only 2025-05 through 2025-12 (the SII-WANGZJ "
    "archive's own end date). R2's final ~6 months (2026-01 through 2026-06) have "
    "NO control venue: extending the archive is not merely unavailable -- "
    "Polymarket's own 2026 fee reform begins January 2026, so even a hypothetically "
    "longer archive would contaminate the control with Polymarket's own treatment "
    "inside that window. The Kalshi-vs-Polymarket differential is reported only "
    "over the covered sub-window; the later period is uncontrolled, not silently "
    "extrapolated."
)

# [VERIFY] best-effort candidate names -- see module docstring.
MARKET_ID_CANDIDATES = ["condition_id", "conditionid", "market_id", "marketid", "id", "market"]
PRICE_CANDIDATES = ["price", "yes_price", "p_yes", "close_price", "p", "trade_price"]
TRADE_TS_CANDIDATES = ["timestamp", "ts", "trade_ts", "time", "created_at", "trade_time"]
OUTCOME_CANDIDATES = ["outcome", "resolved_outcome", "payout_yes", "result", "winner", "y"]
RESOLVED_TS_CANDIDATES = ["resolved_ts", "end_date_iso", "end_date", "close_time", "resolution_date", "resolved_time"]
CATEGORY_CANDIDATES = ["category", "tag", "market_category", "event_category"]


def download_bootstrap_files(local_dir: str = "data/bootstrap") -> dict[str, str]:
    """Downloads quant.parquet + markets.parquet only -- Claude.md S3
    explicitly excludes orderfilled.parquet/trades.parquet/users.parquet
    (85GB combined, raw/user-level, not needed here). Not exercised by
    unit tests (real network + tens of GB); tests call
    build_polymarket_panel directly against small local fixture files."""
    quant_path = hf_hub_download(
        repo_id=HF_REPO_ID, filename="quant.parquet", repo_type="dataset", local_dir=local_dir,
    )
    markets_path = hf_hub_download(
        repo_id=HF_REPO_ID, filename="markets.parquet", repo_type="dataset", local_dir=local_dir,
    )
    return {"quant_path": quant_path, "markets_path": markets_path}


def _detect_column(available: list[str], candidates: list[str]) -> str | None:
    lowered = {c.lower(): c for c in available}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def load_category_map(path: str | Path = "data/category_map_polymarket_kalshi.yaml") -> dict[str, str]:
    """Polymarket category/tag -> Kalshi category string. Best-effort
    (see the yaml file's own header) -- rows whose Polymarket category
    isn't in this map still flow through build_polymarket_panel with
    category=None rather than being dropped; S2.4's headline monthly psi
    path is market-wide, not category-interacted, so an incomplete map
    degrades a descriptive breakdown, never the headline itself."""
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {str(k): str(v) for k, v in (raw.get("map") or {}).items()}


def _outcome_to_float(value: Any) -> float | None:
    """Normalizes whatever the detected outcome column contains (numeric
    0/1, or 'yes'/'no'-style strings) to a resolved-binary float. Returns
    None for anything that isn't unambiguously binary (e.g. a still-open
    market or a multi-outcome market that leaked past the binary filter) --
    callers drop None rows rather than guessing."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return 1.0 if float(value) >= 0.5 else 0.0
    text = str(value).strip().lower()
    if text in ("yes", "y", "true", "1"):
        return 1.0
    if text in ("no", "n", "false", "0"):
        return 0.0
    return None


def build_polymarket_panel(
    quant_path: str | Path, markets_path: str | Path,
    category_map: dict[str, str] | None = None,
    start: int = CONTROL_START, end: int = CONTROL_END,
) -> pl.DataFrame:
    """Lazy-scans both parquet files, joins on the detected market-id
    column, keeps resolved binary markets whose resolution timestamp falls
    in [start, end], and takes each market's LAST trade at or before its
    own resolution as the closing price P -- the same "last observation
    before resolution" idea R1/R2's own boundary-tick panel uses for
    Kalshi, at the coarser grain this archive actually supports (no
    per-lookback-day history here, hence lookback_day is always 0 in the
    output). Output is PANEL_SCHEMA-shaped (r1/panel.py) so it can be
    passed directly into fit_mz_regression unmodified: event_ticker is set
    equal to the market id (no natural finer grouping exists in this
    archive) -- the same one-cluster-per-market degradation
    r1/regression.py's own `_cluster_ids` already falls back to when no
    event mapping is available, not a new special case introduced here.

    Raises ValueError if [start, end] extends outside
    [CONTROL_START, CONTROL_END] -- the archive has no data past
    2025-12-31, and even where it did, S2.4's coverage-gap rule forbids
    treating a longer window as controlled (see module docstring)."""
    if start < CONTROL_START or end > CONTROL_END:
        raise ValueError(
            f"requested window [{start}, {end}] extends outside the archive's "
            f"controlled coverage [{CONTROL_START}, {CONTROL_END}] -- see "
            "COVERAGE_GAP_STATEMENT; this module refuses to extrapolate."
        )

    category_map = category_map or {}

    markets_lf = pl.scan_parquet(markets_path)
    markets_columns = markets_lf.collect_schema().names()
    market_id_col = _detect_column(markets_columns, MARKET_ID_CANDIDATES)
    outcome_col = _detect_column(markets_columns, OUTCOME_CANDIDATES)
    resolved_ts_col = _detect_column(markets_columns, RESOLVED_TS_CANDIDATES)
    category_col = _detect_column(markets_columns, CATEGORY_CANDIDATES)
    if market_id_col is None or outcome_col is None or resolved_ts_col is None:
        raise ValueError(
            "could not detect required markets.parquet columns "
            f"(market_id={market_id_col}, outcome={outcome_col}, resolved_ts={resolved_ts_col}); "
            "extend the candidate-name lists in this module -- see the [VERIFY] docstring note."
        )

    quant_lf = pl.scan_parquet(quant_path)
    quant_columns = quant_lf.collect_schema().names()
    quant_market_id_col = _detect_column(quant_columns, MARKET_ID_CANDIDATES)
    price_col = _detect_column(quant_columns, PRICE_CANDIDATES)
    trade_ts_col = _detect_column(quant_columns, TRADE_TS_CANDIDATES)
    if quant_market_id_col is None or price_col is None or trade_ts_col is None:
        raise ValueError(
            "could not detect required quant.parquet columns "
            f"(market_id={quant_market_id_col}, price={price_col}, trade_ts={trade_ts_col}); "
            "extend the candidate-name lists in this module -- see the [VERIFY] docstring note."
        )

    markets_select = [
        pl.col(market_id_col).cast(pl.String).alias("market_id"),
        pl.col(outcome_col).alias("_outcome_raw"),
        pl.col(resolved_ts_col).alias("_resolved_ts_raw"),
    ]
    if category_col is not None:
        markets_select.append(pl.col(category_col).cast(pl.String).alias("_category_raw"))
    markets_filtered = markets_lf.select(markets_select)

    quant_filtered = quant_lf.select([
        pl.col(quant_market_id_col).cast(pl.String).alias("market_id"),
        pl.col(price_col).cast(pl.Float64).alias("_price_raw"),
        pl.col(trade_ts_col).alias("_trade_ts_raw"),
    ])

    joined = markets_filtered.join(quant_filtered, on="market_id", how="inner")
    collected = joined.collect(engine="streaming")

    records: list[dict[str, Any]] = []
    for row in collected.iter_rows(named=True):
        resolved_epoch = _to_epoch(row["_resolved_ts_raw"])
        if resolved_epoch is None or not (start <= resolved_epoch <= end):
            continue
        outcome = _outcome_to_float(row["_outcome_raw"])
        if outcome is None:
            continue
        trade_epoch = _to_epoch(row["_trade_ts_raw"])
        if trade_epoch is None or trade_epoch > resolved_epoch:
            continue
        price = row["_price_raw"]
        if price is None or not (0.0 < price < 1.0):
            continue
        raw_category = row.get("_category_raw")
        records.append({
            "ticker": row["market_id"], "event_ticker": row["market_id"],
            "lookback_day": 0, "category": category_map.get(raw_category) if raw_category else None,
            "close_time_epoch": resolved_epoch, "side": "yes",
            "y": outcome, "p": price, "source": "polymarket_archive",
            "_trade_epoch": trade_epoch,
        })

    if not records:
        return pl.DataFrame(schema=PANEL_SCHEMA)

    # Last trade at-or-before resolution, per market -- if quant.parquet
    # carries multiple trades per market (expected; it's a trade tape, not
    # a one-row-per-market snapshot), keep only the latest.
    df = pl.DataFrame(records)
    df = df.sort("_trade_epoch").group_by("ticker", maintain_order=False).last()
    return df.select(list(PANEL_SCHEMA.keys())).cast(PANEL_SCHEMA)


def _to_epoch(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


@dataclass
class MonthlyPsiResult:
    month: str  # "YYYY-MM"
    result: MZResult | None


def _month_bounds(year: int, month: int) -> tuple[int, int]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = datetime(end_year, end_month, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp()) - 1


def monthly_psi_path(
    panel: pl.DataFrame, start: int = CONTROL_START, end: int = CONTROL_END,
) -> list[MonthlyPsiResult]:
    """Kalshi's own MZ spec (fit_mz_regression, reused unmodified), fit
    separately per calendar month in [start, end] -- spec S2.4's "monthly
    psi path". A month with too little data to fit (fit_mz_regression's
    own degenerate-case guards) still appears in the output with
    result=None, not silently dropped -- same convention as
    r2/horizon.py's thin-bucket handling."""
    if start < CONTROL_START or end > CONTROL_END:
        raise ValueError(
            f"requested window [{start}, {end}] extends outside the archive's "
            f"controlled coverage [{CONTROL_START}, {CONTROL_END}]."
        )
    results: list[MonthlyPsiResult] = []
    start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
    year, month = start_dt.year, start_dt.month
    while True:
        month_start, month_end = _month_bounds(year, month)
        if month_start > end:
            break
        bucket = panel.filter(
            (pl.col("close_time_epoch") >= max(month_start, start))
            & (pl.col("close_time_epoch") <= min(month_end, end))
        )
        results.append(MonthlyPsiResult(month=f"{year:04d}-{month:02d}", result=fit_mz_regression(bucket)))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return results
