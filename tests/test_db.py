from __future__ import annotations

from kalshi_mt.store import db


def test_connect_creates_schema(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    assert db.get_meta(conn, "schema_version") == db.SCHEMA_VERSION
    conn.close()


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    conn1 = db.connect(path)
    conn1.close()
    conn2 = db.connect(path)  # second connect on the same file must not error
    assert db.get_meta(conn2, "schema_version") == db.SCHEMA_VERSION
    conn2.close()


def test_connect_migrates_markets_table_missing_new_columns(tmp_path):
    """The `markets` table already existed with real data before
    settlement_value_dollars/last_price_dollars were added (2026-07-21
    audit) -- CREATE TABLE IF NOT EXISTS is a no-op against it, so connect()
    must explicitly ALTER TABLE the missing columns in, and do so safely on
    every subsequent connect() (not just the first)."""
    import sqlite3
    path = tmp_path / "old_schema.db"
    # Simulate a pre-migration database: the real CREATE TABLE, minus the
    # two new columns -- must keep every column the SCHEMA script's own
    # CREATE INDEX IF NOT EXISTS statements reference (close_time_epoch,
    # series_ticker, in_r1_window, in_r2_window), since those indexes run
    # against this table unconditionally on every connect().
    raw = sqlite3.connect(path)
    raw.execute("""
        CREATE TABLE markets (
          ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT, category TEXT,
          status TEXT, result TEXT, open_time TEXT, open_time_epoch INTEGER,
          close_time TEXT, close_time_epoch INTEGER, settlement_ts TEXT, volume_fp REAL,
          metadata_source TEXT, in_r1_window INTEGER DEFAULT 0, in_r2_window INTEGER DEFAULT 0,
          first_seen_ts TEXT, last_synced_ts TEXT
        )
    """)
    raw.execute("INSERT INTO markets (ticker, volume_fp) VALUES ('PRE-1', 42.0)")
    raw.commit()
    raw.close()

    conn = db.connect(path)  # must not raise -- ALTER TABLE ADD COLUMN, not CREATE
    cols = {row[1] for row in conn.execute("PRAGMA table_info(markets)")}
    assert "settlement_value_dollars" in cols
    assert "last_price_dollars" in cols
    row = conn.execute("SELECT ticker, volume_fp FROM markets WHERE ticker='PRE-1'").fetchone()
    assert row[0] == "PRE-1" and row[1] == 42.0  # pre-existing data untouched
    conn.close()

    conn2 = db.connect(path)  # re-running the migration on an already-migrated table must not error
    cols2 = {row[1] for row in conn2.execute("PRAGMA table_info(markets)")}
    assert "settlement_value_dollars" in cols2
    conn2.close()


def test_upsert_market_persists_settlement_and_last_price(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.upsert_market(conn, {
        "ticker": "ABC-1", "settlement_value_dollars": 1.0, "last_price_dollars": 0.97,
    })
    conn.commit()
    row = conn.execute(
        "SELECT settlement_value_dollars, last_price_dollars FROM markets WHERE ticker='ABC-1'"
    ).fetchone()
    assert row[0] == 1.0
    assert row[1] == 0.97


def test_upsert_market_preserves_first_seen_ts(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.upsert_market(conn, {"ticker": "ABC-1", "close_time_epoch": 100, "in_r1_window": 1})
    conn.commit()
    first_seen = conn.execute(
        "SELECT first_seen_ts FROM markets WHERE ticker = 'ABC-1'"
    ).fetchone()[0]

    db.upsert_market(conn, {"ticker": "ABC-1", "close_time_epoch": 100, "volume_fp": 5.0, "in_r1_window": 1})
    conn.commit()
    row = conn.execute(
        "SELECT first_seen_ts, volume_fp FROM markets WHERE ticker = 'ABC-1'"
    ).fetchone()
    assert row[0] == first_seen
    assert row[1] == 5.0


def test_upsert_market_in_window_flags_are_sticky_max(tmp_path):
    """A market discovered once via the R1 sweep and once via the R2 sweep
    must end up flagged in BOTH windows, not have the second upsert clobber
    the first's flag."""
    conn = db.connect(tmp_path / "test.db")
    db.upsert_market(conn, {"ticker": "ABC-1", "in_r1_window": 1, "in_r2_window": 0})
    db.upsert_market(conn, {"ticker": "ABC-1", "in_r1_window": 0, "in_r2_window": 1})
    conn.commit()
    row = conn.execute(
        "SELECT in_r1_window, in_r2_window FROM markets WHERE ticker = 'ABC-1'"
    ).fetchone()
    assert row[0] == 1
    assert row[1] == 1


def test_upsert_quote(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.upsert_market(conn, {"ticker": "ABC-1"})
    db.upsert_quote(conn, {
        "ticker": "ABC-1", "end_period_ts": 100, "yes_bid_close": 0.45,
        "yes_ask_close": 0.47, "spread": 0.02, "source": "live",
    })
    conn.commit()
    row = conn.execute("SELECT spread FROM quotes WHERE ticker = 'ABC-1'").fetchone()
    assert row[0] == 0.02


def test_upsert_quote_attempted_with_no_data_found(tmp_path):
    """The "attempted, nothing found" case: caller passes spread=None (and
    the other quote fields None) -- a row still exists, distinguishing it
    from a ticker whose quote fetch was never attempted at all (no row)."""
    conn = db.connect(tmp_path / "test.db")
    db.upsert_market(conn, {"ticker": "ABC-1"})
    db.upsert_quote(conn, {
        "ticker": "ABC-1", "end_period_ts": None, "yes_bid_close": None,
        "yes_ask_close": None, "spread": None, "source": "historical",
    })
    conn.commit()
    row = conn.execute("SELECT spread FROM quotes WHERE ticker = 'ABC-1'").fetchone()
    assert row is not None
    assert row[0] is None


def test_upsert_price_panel_row_dedups_on_ticker_and_lookback_day(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.upsert_market(conn, {"ticker": "ABC-1"})
    db.upsert_price_panel_row(conn, {
        "ticker": "ABC-1", "lookback_day": 0, "trade_id": "t1",
        "yes_price_dollars": 0.5, "created_time": "2022-01-01T00:00:00Z", "source": "live",
    })
    db.upsert_price_panel_row(conn, {
        "ticker": "ABC-1", "lookback_day": 0, "trade_id": "t2",
        "yes_price_dollars": 0.6, "created_time": "2022-01-01T00:00:01Z", "source": "live",
    })
    conn.commit()
    rows = conn.execute("SELECT trade_id FROM price_panel WHERE ticker = 'ABC-1'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "t2"


def test_fetch_log_lifecycle(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    log_id = db.log_fetch(conn, "pass1_discovery_live", "2023-2026", "in_progress")
    conn.commit()
    db.finish_fetch_log(conn, log_id, "done", recorded_count=10, fetched_count=10)
    conn.commit()
    row = conn.execute("SELECT status, recorded_count, fetched_count FROM fetch_log WHERE id = ?", (log_id,)).fetchone()
    assert row[0] == "done"
    assert row[1] == 10
    assert row[2] == 10


def test_series_scan_state_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    assert db.get_series_scan_state(conn, "KXHIGHNY") is None
    db.upsert_series_scan_state(conn, {
        "series_ticker": "KXHIGHNY", "status": "in_progress", "pages_fetched": 3,
        "markets_found_in_window": 12, "reached_before_window": 0, "last_cursor": "abc",
    })
    conn.commit()
    row = db.get_series_scan_state(conn, "KXHIGHNY")
    assert row["status"] == "in_progress"
    assert row["pages_fetched"] == 3


def test_live_window_scan_state_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    assert db.get_live_window_scan_state(conn, 1000, 2000) is None
    db.upsert_live_window_scan_state(conn, {
        "window_start": 1000, "window_end": 2000, "status": "in_progress",
        "cursor": "abc", "fetched_count": 40, "pages_fetched": 4,
    })
    conn.commit()
    row = db.get_live_window_scan_state(conn, 1000, 2000)
    assert row["status"] == "in_progress"
    assert row["cursor"] == "abc"
    assert row["fetched_count"] == 40
    # A different (window_start, window_end) key is a distinct checkpoint.
    assert db.get_live_window_scan_state(conn, 2000, 3000) is None


def test_pass2_progress_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.upsert_market(conn, {"ticker": "ABC-1"})
    assert db.get_pass2_progress(conn, "ABC-1") is None
    db.upsert_pass2_progress(conn, {
        "ticker": "ABC-1", "status": "in_progress", "cursor": "xyz",
        "source": "historical", "trade_count": 50,
    })
    conn.commit()
    row = db.get_pass2_progress(conn, "ABC-1")
    assert row["status"] == "in_progress"
    assert row["trade_count"] == 50
