"""SQLite schema and writers (single file data/kmt.db, WAL mode).

Idempotent, restart-safe by construction: every writer is an upsert keyed on
a natural key (ticker / trade_id), and the resumable-scan tables
(series_scan_state, pass2_progress) let a multi-hour fetch be stopped and
picked back up without re-doing finished work or double-counting trades.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from kalshi_mt.util import PROJECT_ROOT, now_utc_iso

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

-- One row per market discovered in the union of the R1 (2021-01-01..
-- 2025-04-30) and R2 (2025-05-01..2026-06-30) windows. series_ticker is
-- resolved via GET /events/{event_ticker} at insert time -- it is not a
-- field on the live Market object itself (api/kalshi.py docstring).
CREATE TABLE IF NOT EXISTS markets (
  ticker TEXT PRIMARY KEY,
  event_ticker TEXT,
  series_ticker TEXT,
  category TEXT,
  status TEXT,
  result TEXT,
  open_time TEXT,
  open_time_epoch INTEGER,
  close_time TEXT,
  close_time_epoch INTEGER,
  settlement_ts TEXT,
  volume_fp REAL,
  metadata_source TEXT,        -- 'live' | 'historical' -- which endpoint family answered
  in_r1_window INTEGER DEFAULT 0,
  in_r2_window INTEGER DEFAULT 0,
  first_seen_ts TEXT,
  last_synced_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_markets_close_time ON markets(close_time_epoch);
CREATE INDEX IF NOT EXISTS idx_markets_series ON markets(series_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_r1 ON markets(in_r1_window);
CREATE INDEX IF NOT EXISTS idx_markets_r2 ON markets(in_r2_window);

-- The closing candlestick per market, used for the spec's final-spread<=20c
-- filter (Phase 3). One row per market -- the filter only needs the quote
-- nearest close_time, not a full quote history.
CREATE TABLE IF NOT EXISTS quotes (
  ticker TEXT PRIMARY KEY REFERENCES markets(ticker),
  end_period_ts INTEGER,
  yes_bid_close REAL,
  yes_ask_close REAL,
  spread REAL,
  source TEXT,                 -- 'live' | 'historical'
  fetched_ts TEXT
);

-- Pass 1's boundary ticks: the last trade before a reference time, for the
-- closing day (lookback_day=0) and each of up to 10 prior ET-calendar days
-- (lookback_day=1..10). This is the raw material for R1's daily-lookback
-- price panel (spec S1: "last trade on closing day plus last trade before
-- the same time on each of up to 10 prior days"). A market can have fewer
-- than 11 rows -- skip-no-backfill on days with no qualifying trade.
CREATE TABLE IF NOT EXISTS price_panel (
  ticker TEXT NOT NULL REFERENCES markets(ticker),
  lookback_day INTEGER NOT NULL,
  trade_id TEXT,
  yes_price_dollars REAL,
  created_time TEXT,
  source TEXT,
  PRIMARY KEY (ticker, lookback_day)
);

-- Pass 2's full trade tape lives in Parquet (store/parquet.py), month-
-- partitioned per spec S3 -- it can run to many millions of rows across the
-- full R1+R2 universe, columnar storage is the right fit, and the spec's
-- own data plan is explicit: "raw JSON -> Parquet partitioned by month +
-- SQLite index." SQLite holds only the resumability bookkeeping
-- (pass2_progress below), never the trade rows themselves.

-- Freeze-timestamp + count-reconciliation ledger, one row per bounded unit
-- of fetch work (a series scan, a live-window discovery sweep, a Pass 2
-- market). spec S3: "freeze timestamp recorded per pass + per-market
-- trade-count reconciliation (recorded count vs fetched count) as the
-- completeness contract."
CREATE TABLE IF NOT EXISTS fetch_log (
  id INTEGER PRIMARY KEY,
  pass TEXT NOT NULL,          -- 'pass1_discovery_live' | 'pass1_discovery_historical'
                                -- | 'pass1_panel' | 'pass1_quotes' | 'pass2'
  scope TEXT,                  -- series ticker, market ticker, or a window label
  status TEXT,                 -- 'in_progress' | 'done' | 'error'
  freeze_ts TEXT,
  recorded_count INTEGER,
  fetched_count INTEGER,
  started_ts TEXT,
  finished_ts TEXT,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_pass_scope ON fetch_log(pass, scope);

-- Resumable checkpoint for the expensive 2021-2022 series-by-series
-- /historical/markets cursor scan (Kalshi's own live /markets returns
-- nothing at all for that era -- confirmed live during Phase 1; see
-- fetch/pass1.py). Without this, a multi-hour scan across ~12k series
-- could not survive a restart without redoing already-exhausted series.
CREATE TABLE IF NOT EXISTS series_scan_state (
  series_ticker TEXT PRIMARY KEY,
  status TEXT NOT NULL,        -- 'pending' | 'in_progress' | 'done' | 'error'
  pages_fetched INTEGER DEFAULT 0,
  markets_found_in_window INTEGER DEFAULT 0,
  reached_before_window INTEGER DEFAULT 0,  -- 1 once a page's rows are all older than the scan window
  last_cursor TEXT,
  updated_ts TEXT
);

-- Resumable per-market checkpoint for Pass 2's full trade-tape fetch.
CREATE TABLE IF NOT EXISTS pass2_progress (
  ticker TEXT PRIMARY KEY REFERENCES markets(ticker),
  status TEXT NOT NULL,        -- 'pending' | 'in_progress' | 'done' | 'error'
  cursor TEXT,
  source TEXT,                 -- 'live' | 'historical' -- which family is being paged
  trade_count INTEGER DEFAULT 0,
  updated_ts TEXT
);

-- Every market considered and excluded from the R1/R2 analysis universe,
-- with a reason code -- answers "why isn't ticker X in the reproduction"
-- and defends against selection-bias claims in review, same rationale as
-- the sibling lab's own universe_log. Append-only in practice (re-running
-- filters on the same ticker just adds another dated row; the log is a
-- history of exclusion decisions, not a single current-state table).
CREATE TABLE IF NOT EXISTS universe_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  ticker TEXT NOT NULL,
  window TEXT NOT NULL,        -- 'r1' | 'r2'
  reason_code TEXT NOT NULL    -- 'volume_below_1000' | 'spread_above_20c' | 'no_quote_available'
                                -- | 'missing_open_or_close_time' | 'open_below_24h'
                                -- | 'settlement_last_trade_mismatch'
);
CREATE INDEX IF NOT EXISTS idx_universe_log_ticker ON universe_log(ticker);
CREATE INDEX IF NOT EXISTS idx_universe_log_reason ON universe_log(reason_code);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the database with schema applied."""
    path = Path(db_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (now_utc_iso(),)
    )
    conn.commit()
    return conn


def upsert_market(conn: sqlite3.Connection, row: dict) -> None:
    """Idempotent market upsert; preserves first_seen_ts across re-syncs."""
    conn.execute(
        """
        INSERT INTO markets (ticker, event_ticker, series_ticker, category, status, result,
                             open_time, open_time_epoch, close_time, close_time_epoch,
                             settlement_ts, volume_fp, metadata_source, in_r1_window, in_r2_window,
                             first_seen_ts, last_synced_ts)
        VALUES (:ticker, :event_ticker, :series_ticker, :category, :status, :result,
                :open_time, :open_time_epoch, :close_time, :close_time_epoch, :settlement_ts,
                :volume_fp, :metadata_source, :in_r1_window, :in_r2_window, :now, :now)
        ON CONFLICT(ticker) DO UPDATE SET
            event_ticker=excluded.event_ticker, series_ticker=excluded.series_ticker,
            category=excluded.category, status=excluded.status, result=excluded.result,
            open_time=excluded.open_time, open_time_epoch=excluded.open_time_epoch,
            close_time=excluded.close_time, close_time_epoch=excluded.close_time_epoch,
            settlement_ts=excluded.settlement_ts,
            volume_fp=excluded.volume_fp, metadata_source=excluded.metadata_source,
            in_r1_window=MAX(markets.in_r1_window, excluded.in_r1_window),
            in_r2_window=MAX(markets.in_r2_window, excluded.in_r2_window),
            last_synced_ts=excluded.last_synced_ts
        """,
        {
            "event_ticker": None, "series_ticker": None, "category": None, "status": None,
            "result": None, "open_time": None, "open_time_epoch": None, "close_time": None,
            "close_time_epoch": None, "settlement_ts": None, "volume_fp": None,
            "metadata_source": None, "in_r1_window": 0, "in_r2_window": 0,
            **row, "now": now_utc_iso(),
        },
    )


def upsert_quote(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO quotes (ticker, end_period_ts, yes_bid_close, yes_ask_close, spread,
                            source, fetched_ts)
        VALUES (:ticker, :end_period_ts, :yes_bid_close, :yes_ask_close, :spread, :source, :now)
        ON CONFLICT(ticker) DO UPDATE SET
            end_period_ts=excluded.end_period_ts, yes_bid_close=excluded.yes_bid_close,
            yes_ask_close=excluded.yes_ask_close, spread=excluded.spread,
            source=excluded.source, fetched_ts=excluded.fetched_ts
        """,
        {**row, "now": now_utc_iso()},
    )


def upsert_price_panel_row(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO price_panel (ticker, lookback_day, trade_id, yes_price_dollars,
                                 created_time, source)
        VALUES (:ticker, :lookback_day, :trade_id, :yes_price_dollars, :created_time, :source)
        ON CONFLICT(ticker, lookback_day) DO UPDATE SET
            trade_id=excluded.trade_id, yes_price_dollars=excluded.yes_price_dollars,
            created_time=excluded.created_time, source=excluded.source
        """,
        row,
    )


def log_fetch(
    conn: sqlite3.Connection, pass_name: str, scope: str, status: str,
    recorded_count: int | None = None, fetched_count: int | None = None,
    notes: str | None = None, freeze_ts: str | None = None,
) -> int:
    """Append a fetch_log row. Returns the row id (pass in via `notes` to
    later mark a specific row 'done' -- callers track their own id)."""
    now = now_utc_iso()
    cur = conn.execute(
        """
        INSERT INTO fetch_log (pass, scope, status, freeze_ts, recorded_count, fetched_count,
                               started_ts, finished_ts, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pass_name, scope, status, freeze_ts or now, recorded_count, fetched_count,
         now, now if status in ("done", "error") else None, notes),
    )
    return cur.lastrowid


def finish_fetch_log(
    conn: sqlite3.Connection, log_id: int, status: str,
    recorded_count: int | None = None, fetched_count: int | None = None,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE fetch_log SET status = ?, finished_ts = ?,
            recorded_count = COALESCE(?, recorded_count),
            fetched_count = COALESCE(?, fetched_count),
            notes = COALESCE(?, notes)
        WHERE id = ?
        """,
        (status, now_utc_iso(), recorded_count, fetched_count, notes, log_id),
    )


def get_series_scan_state(conn: sqlite3.Connection, series_ticker: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM series_scan_state WHERE series_ticker = ?", (series_ticker,)
    ).fetchone()


def upsert_series_scan_state(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO series_scan_state (series_ticker, status, pages_fetched,
                                       markets_found_in_window, reached_before_window,
                                       last_cursor, updated_ts)
        VALUES (:series_ticker, :status, :pages_fetched, :markets_found_in_window,
                :reached_before_window, :last_cursor, :now)
        ON CONFLICT(series_ticker) DO UPDATE SET
            status=excluded.status, pages_fetched=excluded.pages_fetched,
            markets_found_in_window=excluded.markets_found_in_window,
            reached_before_window=excluded.reached_before_window,
            last_cursor=excluded.last_cursor, updated_ts=excluded.updated_ts
        """,
        {**row, "now": now_utc_iso()},
    )


def get_pass2_progress(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pass2_progress WHERE ticker = ?", (ticker,)
    ).fetchone()


def upsert_pass2_progress(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO pass2_progress (ticker, status, cursor, source, trade_count, updated_ts)
        VALUES (:ticker, :status, :cursor, :source, :trade_count, :now)
        ON CONFLICT(ticker) DO UPDATE SET
            status=excluded.status, cursor=excluded.cursor, source=excluded.source,
            trade_count=excluded.trade_count, updated_ts=excluded.updated_ts
        """,
        {**row, "now": now_utc_iso()},
    )


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def log_universe_exclusions(
    conn: sqlite3.Connection, window: str, exclusions: list[tuple[str, str]],
) -> int:
    """Batch-append (ticker, reason_code) exclusion rows. Returns rows written."""
    if not exclusions:
        return 0
    now = now_utc_iso()
    conn.executemany(
        "INSERT INTO universe_log (ts, ticker, window, reason_code) VALUES (?, ?, ?, ?)",
        [(now, ticker, window, reason_code) for ticker, reason_code in exclusions],
    )
    return len(exclusions)
