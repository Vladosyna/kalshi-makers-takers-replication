"""Pass 2: in-scope selection + resumable full trade-tape fetch."""

from __future__ import annotations

import asyncio

from kalshi_mt.api.kalshi import KalshiTrade
from kalshi_mt.fetch import pass2
from kalshi_mt.store import db
from kalshi_mt.store.parquet import TradeStore


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _seed_market(conn, ticker, *, volume_fp=2000.0, spread=0.05, open_epoch=0,
                  close_epoch=10 * 86400, in_r1=1):
    db.upsert_market(conn, {
        "ticker": ticker, "volume_fp": volume_fp,
        "open_time_epoch": open_epoch, "close_time_epoch": close_epoch,
        "in_r1_window": in_r1,
    })
    db.upsert_quote(conn, {
        "ticker": ticker, "end_period_ts": close_epoch, "yes_bid_close": 0.45,
        "yes_ask_close": 0.45 + spread, "spread": spread, "source": "live",
    })
    conn.commit()


# ---------------------------------------------------------------------------
# select_in_scope_tickers
# ---------------------------------------------------------------------------

def test_select_in_scope_requires_volume_spread_and_duration(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market(conn, "OK-1", volume_fp=2000.0, spread=0.05, open_epoch=0, close_epoch=2 * 86400)
    _seed_market(conn, "LOW-VOLUME", volume_fp=500.0, spread=0.05, open_epoch=0, close_epoch=2 * 86400)
    _seed_market(conn, "WIDE-SPREAD", volume_fp=2000.0, spread=0.30, open_epoch=0, close_epoch=2 * 86400)
    _seed_market(conn, "TOO-SHORT", volume_fp=2000.0, spread=0.05, open_epoch=0, close_epoch=3600)

    tickers = set(pass2.select_in_scope_tickers(conn))
    assert tickers == {"OK-1"}


def test_select_in_scope_excludes_markets_outside_both_windows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market(conn, "NEITHER", in_r1=0)
    assert pass2.select_in_scope_tickers(conn) == []


def test_select_in_scope_excludes_already_done(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market(conn, "OK-1")
    db.upsert_pass2_progress(conn, {
        "ticker": "OK-1", "status": "done", "cursor": None, "source": "live", "trade_count": 5,
    })
    conn.commit()
    assert pass2.select_in_scope_tickers(conn) == []


def test_select_in_scope_includes_in_progress(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market(conn, "OK-1")
    db.upsert_pass2_progress(conn, {
        "ticker": "OK-1", "status": "in_progress", "cursor": "c1", "source": "live", "trade_count": 5,
    })
    conn.commit()
    assert pass2.select_in_scope_tickers(conn) == ["OK-1"]


# ---------------------------------------------------------------------------
# fetch_full_tape_for_market
# ---------------------------------------------------------------------------

class _FakeTapeClient:
    """live_pages / historical_pages: list of (trades, next_cursor)."""

    def __init__(self, live_pages=None, historical_pages=None):
        self.live_pages = live_pages or []
        self.historical_pages = historical_pages or []
        self.live_calls = 0
        self.hist_calls = 0

    async def get_trades(self, ticker=None, min_ts=None, max_ts=None, cursor=None, limit=100):
        idx = self.live_calls
        self.live_calls += 1
        if idx >= len(self.live_pages):
            return [], None
        return self.live_pages[idx]

    async def get_historical_trades(self, ticker=None, min_ts=None, max_ts=None, cursor=None, limit=100):
        idx = self.hist_calls
        self.hist_calls += 1
        if idx >= len(self.historical_pages):
            return [], None
        return self.historical_pages[idx]


def _trade(tid, ticker="A-1", epoch=1000):
    return KalshiTrade.model_validate({
        "trade_id": tid, "ticker": ticker, "count_fp": "1.00",
        "yes_price_dollars": "0.5000", "created_time": _iso(epoch),
    })


def test_fetch_full_tape_single_page_live(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    store = TradeStore(tmp_path / "parquet")
    client = _FakeTapeClient(live_pages=[([_trade("t1"), _trade("t2")], None)])

    result = asyncio.run(pass2.fetch_full_tape_for_market(client, conn, store, "A-1"))
    assert result["status"] == "done"
    assert result["trade_count"] == 2
    assert result["source"] == "live"
    assert len(store.read_for_ticker("A-1")) == 2

    progress = db.get_pass2_progress(conn, "A-1")
    assert progress["status"] == "done"
    assert progress["trade_count"] == 2


def test_fetch_full_tape_falls_back_to_historical_when_live_empty(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    store = TradeStore(tmp_path / "parquet")
    client = _FakeTapeClient(live_pages=[([], None)], historical_pages=[([_trade("t1")], None)])

    result = asyncio.run(pass2.fetch_full_tape_for_market(client, conn, store, "A-1"))
    assert result["source"] == "historical"
    assert result["trade_count"] == 1


def test_fetch_full_tape_paginates_across_multiple_pages(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    store = TradeStore(tmp_path / "parquet")
    client = _FakeTapeClient(live_pages=[
        ([_trade("t1")], "c2"),
        ([_trade("t2")], "c3"),
        ([_trade("t3")], None),
    ])
    result = asyncio.run(pass2.fetch_full_tape_for_market(client, conn, store, "A-1"))
    assert result["status"] == "done"
    assert result["trade_count"] == 3


def test_fetch_full_tape_resumable_across_two_invocations(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    store = TradeStore(tmp_path / "parquet")
    client = _FakeTapeClient(live_pages=[
        ([_trade("t1")], "c2"),
        ([_trade("t2")], "c3"),
        ([_trade("t3")], None),
    ])

    result1 = asyncio.run(
        pass2.fetch_full_tape_for_market(client, conn, store, "A-1", max_pages=1)
    )
    assert result1["status"] == "in_progress"
    assert result1["trade_count"] == 1

    # Resume: a fresh call must pick up the persisted cursor/source, not restart.
    result2 = asyncio.run(pass2.fetch_full_tape_for_market(client, conn, store, "A-1"))
    assert result2["status"] == "done"
    assert result2["trade_count"] == 3
    assert len(store.read_for_ticker("A-1")) == 3


def test_fetch_full_tape_no_trades_anywhere(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    store = TradeStore(tmp_path / "parquet")
    client = _FakeTapeClient()
    result = asyncio.run(pass2.fetch_full_tape_for_market(client, conn, store, "A-1"))
    assert result["status"] == "done"
    assert result["trade_count"] == 0


# ---------------------------------------------------------------------------
# run_pass2 orchestration
# ---------------------------------------------------------------------------

def test_run_pass2_processes_in_scope_tickers_and_logs(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market(conn, "OK-1")
    _seed_market(conn, "OK-2")
    store = TradeStore(tmp_path / "parquet")
    client = _FakeTapeClient(live_pages=[([_trade("t1", ticker="OK-1")], None)])
    # Second market reuses the same fake but its own live_calls index --
    # simplest to just accept it may get zero trades since the fake is shared;
    # what's under test is orchestration/bookkeeping, not per-market fidelity.

    stats = asyncio.run(pass2.run_pass2(client, conn, store))
    assert stats["tickers_attempted"] == 2
    log_row = conn.execute(
        "SELECT status FROM fetch_log WHERE pass = 'pass2' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert log_row[0] == "done"


def test_run_pass2_respects_ticker_limit(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market(conn, "OK-1")
    _seed_market(conn, "OK-2")
    store = TradeStore(tmp_path / "parquet")
    client = _FakeTapeClient()
    stats = asyncio.run(pass2.run_pass2(client, conn, store, ticker_limit=1))
    assert stats["tickers_attempted"] == 1
