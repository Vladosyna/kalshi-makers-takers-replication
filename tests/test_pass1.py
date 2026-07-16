"""Pass 1: discovery, series/category resolution, price panel, closing quotes.

Async code driven via asyncio.run() from plain `def test_*` functions --
pytest-asyncio is not a project dependency (matches tests/test_stepzero.py).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from kalshi_mt.api.kalshi import KalshiCandlestick, KalshiEvent, KalshiMarket, KalshiSeries, KalshiTrade
from kalshi_mt.fetch import pass1
from kalshi_mt.store import db
from kalshi_mt.util import ET, epoch_to_et, et_to_epoch


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _market(ticker: str, close_epoch: int, event_ticker: str = "EVT-1", status: str = "closed", result: str = "") -> KalshiMarket:
    return KalshiMarket.model_validate({
        "ticker": ticker, "event_ticker": event_ticker, "status": status, "result": result,
        "close_time": _iso(close_epoch), "volume_fp": "10.00",
    })


# ---------------------------------------------------------------------------
# discover_live_window
# ---------------------------------------------------------------------------

class _FakeLiveDiscoveryClient:
    """Two pages then an empty cursor."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def list_markets(self, status=None, min_close_ts=None, max_close_ts=None,
                            min_settled_ts=None, max_settled_ts=None, series_ticker=None,
                            event_ticker=None, cursor=None, limit=100):
        self.calls.append(cursor)
        idx = len(self.calls) - 1
        if idx >= len(self.pages):
            return [], None
        return self.pages[idx]


def test_discover_live_window_paginates_and_flags_windows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    r1_epoch = pass1.R1_START + 86400  # inside R1
    r2_epoch = pass1.R2_START + 86400  # inside R2
    client = _FakeLiveDiscoveryClient([
        ([_market("A-1", r1_epoch)], "cursor2"),
        ([_market("B-1", r2_epoch)], None),
    ])
    stats = asyncio.run(pass1.discover_live_window(client, conn, page_limit=1000))
    assert stats["fetched"] == 2
    assert stats["pages"] == 2
    a = conn.execute("SELECT in_r1_window, in_r2_window FROM markets WHERE ticker='A-1'").fetchone()
    b = conn.execute("SELECT in_r1_window, in_r2_window FROM markets WHERE ticker='B-1'").fetchone()
    assert (a[0], a[1]) == (1, 0)
    assert (b[0], b[1]) == (0, 1)


def test_discover_live_window_respects_max_pages(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    client = _FakeLiveDiscoveryClient([
        ([_market("A-1", pass1.R1_START)], "c2"),
        ([_market("B-1", pass1.R1_START)], "c3"),
        ([_market("C-1", pass1.R1_START)], None),
    ])
    stats = asyncio.run(pass1.discover_live_window(client, conn, max_pages=1))
    assert stats["pages"] == 1
    assert stats["fetched"] == 1


# ---------------------------------------------------------------------------
# discover_historical_series
# ---------------------------------------------------------------------------

class _FakeSeriesScanClient:
    """One series (LONGRUN) needs 3 pages to reach the window; another
    (SHORT) is exhausted after 1 empty-ish page inside the window already."""

    def __init__(self):
        self.series = [KalshiSeries.model_validate({"ticker": "LONGRUN", "category": "Weather"}),
                       KalshiSeries.model_validate({"ticker": "SHORT", "category": "Economics"})]
        self.hist_calls = []

    async def list_series(self, category=None, limit=200):
        return self.series

    async def list_historical_markets(self, tickers=None, event_ticker=None,
                                       series_ticker=None, cursor=None, limit=100):
        self.hist_calls.append((series_ticker, cursor))
        window_epoch = pass1.R1_START + 3600  # inside [R1_START, LIVE_METADATA_FLOOR)
        recent_epoch = pass1.LIVE_METADATA_FLOOR + 3600  # too recent, outside the scan window
        old_epoch = pass1.R1_START - 3600  # before the scan window entirely

        if series_ticker == "LONGRUN":
            if cursor is None:
                return [_market("LR-recent", recent_epoch)], "p2"
            if cursor == "p2":
                return [_market("LR-window", window_epoch)], "p3"
            if cursor == "p3":
                return [_market("LR-old", old_epoch)], None  # page's rows all predate the window -> stop
        if series_ticker == "SHORT":
            return [_market("SH-window", window_epoch)], None
        return [], None


def test_discover_historical_series_pages_until_window_and_checkpoints(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    client = _FakeSeriesScanClient()
    stats = asyncio.run(pass1.discover_historical_series(client, conn))
    assert stats["series_processed_this_run"] == 2
    assert stats["series_remaining"] == 0

    tickers = {r[0] for r in conn.execute("SELECT ticker FROM markets").fetchall()}
    assert "LR-window" in tickers
    assert "SH-window" in tickers
    assert "LR-recent" not in tickers  # outside [R1_START, LIVE_METADATA_FLOOR)
    assert "LR-old" not in tickers

    longrun_state = db.get_series_scan_state(conn, "LONGRUN")
    assert longrun_state["status"] == "done"
    assert longrun_state["pages_fetched"] == 3
    assert longrun_state["reached_before_window"] == 1


def test_discover_historical_series_resumable_via_max_series_this_run(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    client = _FakeSeriesScanClient()
    stats1 = asyncio.run(pass1.discover_historical_series(client, conn, max_series_this_run=1))
    assert stats1["series_processed_this_run"] == 1
    assert stats1["series_remaining"] == 1

    stats2 = asyncio.run(pass1.discover_historical_series(client, conn, max_series_this_run=1))
    assert stats2["series_processed_this_run"] == 1
    assert stats2["series_remaining"] == 0


# ---------------------------------------------------------------------------
# resolve_series_and_category
# ---------------------------------------------------------------------------

class _FakeResolveClient:
    def __init__(self):
        self.event_calls = []

    async def list_series(self, category=None, limit=200):
        return [KalshiSeries.model_validate({"ticker": "KXHIGHNY", "category": "Climate and Weather"})]

    async def get_event(self, event_ticker):
        self.event_calls.append(event_ticker)
        if event_ticker == "EVT-1":
            return KalshiEvent.model_validate({"event_ticker": "EVT-1", "series_ticker": "KXHIGHNY"})
        return None


def test_resolve_series_and_category_backfills_and_caches_events(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1", "event_ticker": "EVT-1"})
    db.upsert_market(conn, {"ticker": "A-2", "event_ticker": "EVT-1"})  # same event -- must hit cache, not re-call
    conn.commit()

    client = _FakeResolveClient()
    stats = asyncio.run(pass1.resolve_series_and_category(client, conn))
    assert stats["resolved_this_run"] == 2
    assert stats["remaining"] == 0
    assert client.event_calls == ["EVT-1"]  # cached, not called twice

    row = conn.execute("SELECT series_ticker, category FROM markets WHERE ticker='A-1'").fetchone()
    assert row[0] == "KXHIGHNY"
    assert row[1] == "Climate and Weather"


# ---------------------------------------------------------------------------
# fetch_price_panel
# ---------------------------------------------------------------------------

class _FakePanelClient:
    """trades: list of (epoch, trade_id, price). skip_days: lookback days
    that should have NO qualifying trade (the fake returns a stale trade
    from before that day's own start, simulating the skip-no-backfill case)."""

    def __init__(self, trades, skip_days=frozenset()):
        self.trades = sorted(trades, key=lambda t: -t[0])
        self.skip_days = skip_days
        self.max_ts_calls = []

    async def get_trades(self, ticker=None, min_ts=None, max_ts=None, cursor=None, limit=100):
        self.max_ts_calls.append(max_ts)
        for epoch, tid, price in self.trades:
            if epoch <= max_ts:
                return [KalshiTrade.model_validate({
                    "trade_id": tid, "ticker": ticker, "yes_price_dollars": str(price),
                    "created_time": _iso(epoch),
                })], None
        return [], None

    async def get_historical_trades(self, ticker=None, min_ts=None, max_ts=None, cursor=None, limit=100):
        return [], None  # everything resolved live in this fixture


def test_fetch_price_panel_full_11_days(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    close_epoch = et_to_epoch(datetime(2023, 6, 15, 20, 0, 0, tzinfo=ET))  # a summer weekday, no DST edge

    # One trade at exactly the reference clock time on each of days 0..10.
    trades = []
    t0_et = epoch_to_et(close_epoch)
    for day in range(11):
        day_et = t0_et - timedelta(days=day)
        trades.append((et_to_epoch(day_et), f"t{day}", 0.5 + day * 0.01))

    result = asyncio.run(pass1.fetch_price_panel(_FakePanelClient(trades), conn, "A-1", close_epoch))
    assert result["rows_written"] == 11
    rows = conn.execute(
        "SELECT lookback_day FROM price_panel WHERE ticker='A-1' ORDER BY lookback_day"
    ).fetchall()
    assert [r[0] for r in rows] == list(range(11))


def test_fetch_price_panel_skips_day_with_no_qualifying_trade_no_backfill(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    close_epoch = et_to_epoch(datetime(2023, 6, 15, 20, 0, 0, tzinfo=ET))
    t0_et = epoch_to_et(close_epoch)

    # Trades on day 0 and day 2 only -- day 1 has a trade that's actually
    # from day 0 (i.e. no real day-1 trade), so it must be SKIPPED, not
    # silently backfilled from day 0's trade.
    trades = [
        (et_to_epoch(t0_et), "t0", 0.50),
        (et_to_epoch(t0_et - timedelta(days=2)), "t2", 0.52),
    ]
    result = asyncio.run(pass1.fetch_price_panel(_FakePanelClient(trades), conn, "A-1", close_epoch))
    assert result["rows_written"] == 2  # day 0 and day 2 only
    rows = {r[0] for r in conn.execute("SELECT lookback_day FROM price_panel WHERE ticker='A-1'").fetchall()}
    assert rows == {0, 2}


def test_fetch_price_panel_no_trades_at_all_writes_nothing(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    result = asyncio.run(pass1.fetch_price_panel(_FakePanelClient([]), conn, "A-1", 1700000000))
    assert result["rows_written"] == 0


# ---------------------------------------------------------------------------
# fetch_closing_quote
# ---------------------------------------------------------------------------

def _candle(end_ts, bid, ask):
    return KalshiCandlestick.model_validate({
        "end_period_ts": end_ts,
        "yes_bid": {"close_dollars": str(bid)}, "yes_ask": {"close_dollars": str(ask)},
    })


class _FakeQuoteClient:
    def __init__(self, live_candles=None, historical_candles=None):
        self.live_candles = live_candles or []
        self.historical_candles = historical_candles or []

    async def get_event(self, event_ticker):
        return KalshiEvent.model_validate({"event_ticker": event_ticker, "series_ticker": "SER-1"})

    async def get_candlesticks(self, series_ticker, ticker, start_ts, end_ts, period_interval=1440):
        return self.live_candles

    async def get_historical_candlesticks(self, ticker, start_ts, end_ts, period_interval=1440):
        return self.historical_candles


def test_fetch_closing_quote_prefers_live(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    client = _FakeQuoteClient(live_candles=[_candle(1000, 0.45, 0.47)])
    result = asyncio.run(pass1.fetch_closing_quote(client, conn, "A-1", "EVT-1", 2000))
    assert result["has_quote"] is True
    row = conn.execute("SELECT source, spread FROM quotes WHERE ticker='A-1'").fetchone()
    assert row[0] == "live"
    assert abs(row[1] - 0.02) < 1e-9


def test_fetch_closing_quote_falls_back_to_historical(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    client = _FakeQuoteClient(live_candles=[], historical_candles=[_candle(1000, 0.10, 0.15)])
    result = asyncio.run(pass1.fetch_closing_quote(client, conn, "A-1", "EVT-1", 2000))
    assert result["has_quote"] is True
    row = conn.execute("SELECT source FROM quotes WHERE ticker='A-1'").fetchone()
    assert row[0] == "historical"


def test_fetch_closing_quote_no_quote_anywhere(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    client = _FakeQuoteClient(live_candles=[], historical_candles=[])
    result = asyncio.run(pass1.fetch_closing_quote(client, conn, "A-1", "EVT-1", 2000))
    assert result["has_quote"] is False
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
