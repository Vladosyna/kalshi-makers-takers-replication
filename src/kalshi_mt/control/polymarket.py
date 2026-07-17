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
the join is done by DuckDB directly against the parquet files on disk
(build_polymarket_panel's own docstring explains why, not polars) --
never `pl.read_parquet` on either file whole.

Column-role detection -- VERIFIED LIVE 2026-07-17 against the real
downloaded archive (both files, ~26GB total). `_detect_column`'s
candidate-name lists were guesses when this module was first written;
the real schemas turned out to match most of them directly
(condition_id, price, timestamp, end_date), with one exception:
markets.parquet has NO scalar outcome/result/winner column at all --
only `outcome_prices`, a stringified 2-element list (see
OUTCOME_LIST_CANDIDATES and _parse_outcome_prices_first for the fix), and
NO category/tag/market_category/event_category column either (there is
no fallback for this one -- see below). Confirmed end-to-end: a real run
of build_polymarket_panel against the full archive returns 163,097 panel
rows across the full 2025-05..2025-12 window in ~6 seconds, and
monthly_psi_path produces a result for all 8 months (n ranging
4,719 to 46,020 per month).

Category mapping is a KNOWN GAP, not a bug: markets.parquet carries no
category-like column at all (only `question`/`event_title`/`event_slug`
free text), so `category_col` detection always returns None against the
real archive and every panel row's `category` comes back NULL. This
degrades only the descriptive by-category breakdown S2.4 mentions as
secondary -- the headline monthly psi path is market-wide, not
category-interacted, so it is unaffected. Recovering a category signal
would require text-classifying `event_title`/`question` (e.g. keyword
matching against data/category_map_polymarket_kalshi.yaml's Kalshi-side
vocabulary), which is future work, not a blocker for the control-venue
overlay's primary estimand.

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

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
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

# Column names verified 2026-07-17 against the real downloaded archive
# (data/bootstrap/{quant,markets}.parquet, ~26GB) -- the [VERIFY] guess
# below was WRONG for the outcome column specifically; see
# OUTCOME_LIST_CANDIDATES and _parse_outcome_prices_first.
MARKET_ID_CANDIDATES = ["condition_id", "conditionid", "market_id", "marketid", "id", "market"]
PRICE_CANDIDATES = ["price", "yes_price", "p_yes", "close_price", "p", "trade_price"]
TRADE_TS_CANDIDATES = ["timestamp", "ts", "trade_ts", "time", "created_at", "trade_time"]
OUTCOME_CANDIDATES = ["outcome", "resolved_outcome", "payout_yes", "result", "winner", "y"]
# The real markets.parquet has none of the above -- it has `outcome_prices`
# instead: a STRINGIFIED 2-element list (e.g. "['0', '1']" or "['1', '0']"),
# one entry per outcome slot (answer1/token1, answer2/token2). Verified
# empirically against 15 real resolved markets spanning several category
# framings (Up/Down, Yes/No, Over/Under, named entities): whenever index 0
# is "1", quant.parquet's own `price` column for that market's last trade
# sits at ~0.99-0.999; whenever index 0 is "0", the last trade sits at
# ~0.001-0.009. This confirms both (a) index 0 is the resolved value for
# the token1/answer1 side, and (b) quant.parquet's price really is already
# normalized to that same side, matching this module's own docstring claim
# ("cleaned trades unified to the YES token") -- so no per-trade side
# conversion is needed, only the outcome extraction below.
OUTCOME_LIST_CANDIDATES = ["outcome_prices"]
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


def _parse_outcome_prices_first(value: Any) -> float | None:
    """Parses the real archive's `outcome_prices` column -- a STRINGIFIED
    2-element list, e.g. "['0', '1']" -- and returns index 0 (the
    token1/answer1 side, empirically confirmed to be the same side
    quant.parquet's own `price` column is normalized against; see
    OUTCOME_LIST_CANDIDATES). `ast.literal_eval` only parses Python
    literals (lists/strings/numbers), never executes arbitrary code, so
    this is safe against untrusted string content. Returns None for
    anything that doesn't parse to a non-empty list (an unresolved market,
    a malformed row, or a differently-shaped multi-outcome market)."""
    if value is None:
        return None
    try:
        parsed = ast.literal_eval(value) if isinstance(value, str) else value
    except (ValueError, SyntaxError):
        return None
    if not isinstance(parsed, (list, tuple)) or not parsed:
        return None
    try:
        return float(parsed[0])
    except (TypeError, ValueError):
        return None


def _quote_ident(name: str) -> str:
    """Double-quotes a SQL identifier for DuckDB, escaping embedded quotes.
    Identifiers here always come from `_detect_column`'s fixed candidate
    lists (matched against the archive's own real column names), never
    from unvalidated external input, but quoting is cheap insurance."""
    return '"' + name.replace('"', '""') + '"'


def _epoch_sql(dtype: pl.DataType, column_sql: str) -> str:
    """Builds a DuckDB SQL expression that normalizes `column_sql` (a
    quoted, possibly table-qualified identifier) to epoch seconds,
    dispatching on the column's polars dtype the same way the module's
    date-window pre-filter does: native temporal columns use DuckDB's
    epoch(), numeric columns (assumed already epoch seconds -- true for
    every schema this module has actually seen) pass through unchanged,
    and anything else (e.g. an ISO-8601 string column) is cast to
    TIMESTAMP first."""
    if dtype.is_temporal():
        return f"epoch({column_sql})"
    if dtype.is_numeric():
        return column_sql
    return f"epoch(TRY_CAST({column_sql} AS TIMESTAMP))"


def build_polymarket_panel(
    quant_path: str | Path, markets_path: str | Path,
    category_map: dict[str, str] | None = None,
    start: int = CONTROL_START, end: int = CONTROL_END,
) -> pl.DataFrame:
    """Joins markets.parquet and quant.parquet on the detected market-id
    column via DuckDB (not polars), keeps resolved binary markets whose
    resolution timestamp falls in [start, end], and takes each market's
    LAST trade at or before its own resolution as the closing price P --
    the same "last observation before resolution" idea R1/R2's own
    boundary-tick panel uses for Kalshi, at the coarser grain this archive
    actually supports (no per-lookback-day history here, hence
    lookback_day is always 0 in the output).

    DuckDB, not polars' own streaming engine, does the join: verified live
    (2026-07-17) against the real ~26GB quant.parquet that polars 1.42.1's
    `collect(engine="streaming")` on this exact join shape (two lazy-scanned
    parquets, filtered, joined on a cast string key) is NOT memory-bounded
    in practice -- process RSS climbed past 11GB and was still climbing
    when killed, even with the [start, end] window already pushed down
    onto the join's build side. The equivalent DuckDB query, restricting
    the join to the already-small in-window market set and computing the
    last-trade-per-market ranking natively via `row_number()`, completed in
    ~13s. Both parquet files are read directly by DuckDB's own
    `read_parquet()` (no full materialization into polars first); only the
    already-deduplicated (one row per market) result crosses into Python.

    Output is PANEL_SCHEMA-shaped (r1/panel.py) so it can be passed
    directly into fit_mz_regression unmodified: event_ticker is set equal
    to the market id (no natural finer grouping exists in this archive) --
    the same one-cluster-per-market degradation r1/regression.py's own
    `_cluster_ids` already falls back to when no event mapping is
    available, not a new special case introduced here.

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

    markets_schema = pl.scan_parquet(markets_path).collect_schema()
    markets_columns = markets_schema.names()
    market_id_col = _detect_column(markets_columns, MARKET_ID_CANDIDATES)
    outcome_col = _detect_column(markets_columns, OUTCOME_CANDIDATES)
    outcome_list_col = _detect_column(markets_columns, OUTCOME_LIST_CANDIDATES) if outcome_col is None else None
    resolved_ts_col = _detect_column(markets_columns, RESOLVED_TS_CANDIDATES)
    category_col = _detect_column(markets_columns, CATEGORY_CANDIDATES)
    if market_id_col is None or (outcome_col is None and outcome_list_col is None) or resolved_ts_col is None:
        raise ValueError(
            "could not detect required markets.parquet columns "
            f"(market_id={market_id_col}, outcome={outcome_col or outcome_list_col}, "
            f"resolved_ts={resolved_ts_col}); extend the candidate-name lists in this "
            "module -- see the [VERIFY] docstring note."
        )

    quant_schema = pl.scan_parquet(quant_path).collect_schema()
    quant_columns = quant_schema.names()
    quant_market_id_col = _detect_column(quant_columns, MARKET_ID_CANDIDATES)
    price_col = _detect_column(quant_columns, PRICE_CANDIDATES)
    trade_ts_col = _detect_column(quant_columns, TRADE_TS_CANDIDATES)
    if quant_market_id_col is None or price_col is None or trade_ts_col is None:
        raise ValueError(
            "could not detect required quant.parquet columns "
            f"(market_id={quant_market_id_col}, price={price_col}, trade_ts={trade_ts_col}); "
            "extend the candidate-name lists in this module -- see the [VERIFY] docstring note."
        )

    outcome_raw_col = outcome_col if outcome_col is not None else outcome_list_col
    market_id_ident = _quote_ident(market_id_col)
    outcome_ident = _quote_ident(outcome_raw_col)
    resolved_ident = _quote_ident(resolved_ts_col)
    quant_market_id_ident = _quote_ident(quant_market_id_col)
    price_ident = _quote_ident(price_col)
    trade_ts_ident = _quote_ident(trade_ts_col)

    resolved_epoch_sql = _epoch_sql(markets_schema[resolved_ts_col], resolved_ident)
    trade_epoch_sql = _epoch_sql(quant_schema[trade_ts_col], f"t.{trade_ts_ident}")
    category_select = f", {_quote_ident(category_col)}::VARCHAR AS _category_raw" if category_col is not None else ""
    category_out = ", m._category_raw" if category_col is not None else ""

    query = f"""
    WITH in_window_markets AS (
        SELECT {market_id_ident}::VARCHAR AS market_id,
               {outcome_ident} AS _outcome_raw,
               {resolved_epoch_sql} AS _resolved_epoch
               {category_select}
        FROM read_parquet(?)
        WHERE {resolved_epoch_sql} BETWEEN ? AND ?
    ),
    last_trade AS (
        SELECT t.{quant_market_id_ident}::VARCHAR AS market_id,
               t.{price_ident}::DOUBLE AS _price_raw,
               {trade_epoch_sql} AS _trade_epoch,
               row_number() OVER (
                   PARTITION BY t.{quant_market_id_ident} ORDER BY {trade_epoch_sql} DESC
               ) AS rn
        FROM read_parquet(?) AS t
        JOIN in_window_markets m ON m.market_id = t.{quant_market_id_ident}::VARCHAR
        WHERE {trade_epoch_sql} <= m._resolved_epoch
    )
    SELECT m.market_id, m._outcome_raw, m._resolved_epoch{category_out},
           lt._price_raw, lt._trade_epoch
    FROM in_window_markets m
    JOIN last_trade lt ON lt.market_id = m.market_id AND lt.rn = 1
    """
    con = duckdb.connect()
    joined = con.execute(query, [str(markets_path), start, end, str(quant_path)]).pl()

    if joined.is_empty():
        return pl.DataFrame(schema=PANEL_SCHEMA)

    records: list[dict[str, Any]] = []
    for row in joined.iter_rows(named=True):
        raw_outcome = row["_outcome_raw"]
        if outcome_col is None:
            raw_outcome = _parse_outcome_prices_first(raw_outcome)
        outcome = _outcome_to_float(raw_outcome)
        if outcome is None:
            continue
        price = row["_price_raw"]
        if price is None or not (0.0 < price < 1.0):
            continue
        raw_category = row.get("_category_raw")
        records.append({
            "ticker": row["market_id"], "event_ticker": row["market_id"],
            "lookback_day": 0, "category": category_map.get(raw_category) if raw_category else None,
            "close_time_epoch": int(row["_resolved_epoch"]), "side": "yes",
            "y": outcome, "p": price, "source": "polymarket_archive",
        })

    if not records:
        return pl.DataFrame(schema=PANEL_SCHEMA)

    df = pl.DataFrame(records)
    return df.select(list(PANEL_SCHEMA.keys())).cast(PANEL_SCHEMA)


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
