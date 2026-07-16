"""Parquet trade-tape store, partitioned data/parquet/month=YYYY-MM/*.parquet.

Dedup key is trade_id -- appends drop rows whose key already exists in the
partition, restart-safe by construction (same anti-join pattern as the
sibling lab's store/snapshots.py, month granularity here per spec S3 rather
than that lab's daily partitions -- the two repos partition at different
grain by design, not by accident). This holds Pass 2's full trade tape,
which can run to many millions of rows across the R1+R2 universe -- SQLite
(store/db.py) holds only the small bookkeeping tables, never the trades
themselves.
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from kalshi_mt.util import PROJECT_ROOT

log = logging.getLogger(__name__)

TRADE_SCHEMA = {
    "trade_id": pl.String,
    "ticker": pl.String,
    "count_fp": pl.Float64,
    "yes_price_dollars": pl.Float64,
    "no_price_dollars": pl.Float64,
    "taker_outcome_side": pl.String,
    "taker_book_side": pl.String,
    "taker_side": pl.String,
    "created_time": pl.String,
    "is_block_trade": pl.Boolean,
    "source": pl.String,  # 'live' | 'historical'
}


def month_str(created_time: str) -> str:
    """YYYY-MM from an ISO-8601 timestamp string ('2022-12-30T17:15:45Z' -> '2022-12')."""
    return created_time[:7]


class TradeStore:
    def __init__(self, parquet_dir: str | Path) -> None:
        base = Path(parquet_dir)
        self.base = base if base.is_absolute() else PROJECT_ROOT / base
        self.base.mkdir(parents=True, exist_ok=True)

    def _partition(self, month: str) -> Path:
        return self.base / f"month={month}" / "trades.parquet"

    def append(self, rows: list[dict]) -> int:
        """Append trade rows, deduplicating on trade_id within each month
        partition. Returns the number of genuinely new rows written."""
        if not rows:
            return 0
        clean = [{col: r.get(col) for col in TRADE_SCHEMA} for r in rows]
        df = pl.DataFrame(clean, schema=TRADE_SCHEMA)
        df = df.filter(pl.col("created_time").is_not_null() & pl.col("trade_id").is_not_null())
        if df.is_empty():
            return 0
        written = 0
        df = df.with_columns(pl.col("created_time").str.slice(0, 7).alias("_month"))
        for (month,), part in df.partition_by("_month", as_dict=True).items():
            part = part.drop("_month")
            path = self._partition(month)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                existing = pl.read_parquet(path)
                keys = existing.select("trade_id")
                part = part.join(keys, on="trade_id", how="anti")
                if part.is_empty():
                    continue
                merged = pl.concat([existing, part], how="diagonal")
            else:
                merged = part
            merged.write_parquet(path)
            written += len(part)
        return written

    def months_on_disk(self) -> list[str]:
        return sorted(p.name.split("=", 1)[1] for p in self.base.glob("month=*") if p.is_dir())

    def read_range(self, months: list[str]) -> pl.DataFrame:
        frames = [
            pl.read_parquet(self._partition(m)) for m in months if self._partition(m).exists()
        ]
        if not frames:
            return pl.DataFrame(schema=TRADE_SCHEMA)
        return pl.concat(frames, how="diagonal")

    def read_all(self) -> pl.DataFrame:
        return self.read_range(self.months_on_disk())

    def read_for_ticker(self, ticker: str, months: list[str] | None = None) -> pl.DataFrame:
        """All trades for one ticker. Scans the given months (or every
        partition on disk if not given) -- fine for R1/R2 analysis reads;
        Pass 2's own progress/count tracking lives in SQLite's
        pass2_progress table, not derived by re-scanning Parquet."""
        df = self.read_range(months if months is not None else self.months_on_disk())
        if df.is_empty():
            return df
        return df.filter(pl.col("ticker") == ticker)
