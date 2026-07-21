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
    """Keyed by (min_close_ts, max_close_ts) so each concurrent sub-window
    (discover_live_window splits its range into n_concurrent_windows
    independent cursor walks) gets its own page sequence, matching how a
    real API scopes pagination to the query's own filter bounds -- a
    single shared call-index counter would incorrectly interleave
    concurrent lanes."""

    def __init__(self, pages_by_window):
        self.pages_by_window = pages_by_window  # {(min_ts, max_ts): [(markets, cursor), ...]}
        self.calls_per_window: dict[tuple, list] = {}

    async def list_markets(self, status=None, min_close_ts=None, max_close_ts=None,
                            min_settled_ts=None, max_settled_ts=None, series_ticker=None,
                            event_ticker=None, cursor=None, limit=100):
        key = (min_close_ts, max_close_ts)
        calls = self.calls_per_window.setdefault(key, [])
        calls.append(cursor)
        idx = len(calls) - 1
        pages = self.pages_by_window.get(key, [])
        if idx >= len(pages):
            return [], None
        return pages[idx]


def test_discover_live_window_paginates_and_flags_windows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    r1_epoch = pass1.R1_START + 86400  # inside R1
    r2_epoch = pass1.R2_START + 86400  # inside R2
    window_key = (pass1.LIVE_METADATA_FLOOR, pass1.R2_END)
    client = _FakeLiveDiscoveryClient({
        window_key: [
            ([_market("A-1", r1_epoch)], "cursor2"),
            ([_market("B-1", r2_epoch)], None),
        ],
    })
    stats = asyncio.run(pass1.discover_live_window(client, conn, page_limit=1000, n_concurrent_windows=1))
    assert stats["fetched"] == 2
    assert stats["pages"] == 2
    a = conn.execute("SELECT in_r1_window, in_r2_window FROM markets WHERE ticker='A-1'").fetchone()
    b = conn.execute("SELECT in_r1_window, in_r2_window FROM markets WHERE ticker='B-1'").fetchone()
    assert (a[0], a[1]) == (1, 0)
    assert (b[0], b[1]) == (0, 1)


def test_discover_live_window_respects_max_pages(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    window_key = (pass1.LIVE_METADATA_FLOOR, pass1.R2_END)
    client = _FakeLiveDiscoveryClient({
        window_key: [
            ([_market("A-1", pass1.R1_START)], "c2"),
            ([_market("B-1", pass1.R1_START)], "c3"),
            ([_market("C-1", pass1.R1_START)], None),
        ],
    })
    stats = asyncio.run(pass1.discover_live_window(client, conn, max_pages=1, n_concurrent_windows=1))
    assert stats["pages"] == 1


def test_discover_live_window_splits_into_concurrent_subwindows(tmp_path):
    """Each of N sub-windows gets its OWN independent cursor walk -- total
    fetched is the sum across all lanes, not just the first one."""
    conn = db.connect(tmp_path / "t.db")
    start_ts, end_ts = 0, 1000
    n_windows = 4
    step = (end_ts - start_ts) // n_windows
    boundaries = [start_ts + i * step for i in range(n_windows)] + [end_ts]
    pages_by_window = {}
    for i in range(n_windows):
        key = (boundaries[i], boundaries[i + 1])
        pages_by_window[key] = [([_market(f"M{i}", boundaries[i])], None)]

    client = _FakeLiveDiscoveryClient(pages_by_window)
    stats = asyncio.run(pass1.discover_live_window(
        client, conn, start_ts=start_ts, end_ts=end_ts, n_concurrent_windows=n_windows
    ))
    assert stats["fetched"] == n_windows  # one market discovered per sub-window
    tickers = {r[0] for r in conn.execute("SELECT ticker FROM markets").fetchall()}
    assert tickers == {f"M{i}" for i in range(n_windows)}


def test_discover_live_window_resumes_from_saved_cursor(tmp_path):
    """A partial call (bounded by max_pages, modeling either a verification
    run or a process that gets restarted before finishing) must checkpoint
    its cursor -- a later call resumes from it instead of re-fetching page 1,
    the exact gap that let a real multi-hour run make zero net progress
    across several restarts on its densest sub-window."""
    conn = db.connect(tmp_path / "t.db")
    window_key = (pass1.LIVE_METADATA_FLOOR, pass1.R2_END)
    client = _FakeLiveDiscoveryClient({
        window_key: [
            ([_market("A-1", pass1.R1_START)], "cursor-after-page-1"),
            ([_market("B-1", pass1.R1_START)], None),
        ],
    })
    stats1 = asyncio.run(pass1.discover_live_window(client, conn, max_pages=1, n_concurrent_windows=1))
    assert stats1["fetched"] == 1
    assert stats1["pages"] == 1
    checkpoint = db.get_live_window_scan_state(conn, *window_key)
    assert checkpoint["status"] == "in_progress"
    assert checkpoint["cursor"] == "cursor-after-page-1"

    stats2 = asyncio.run(pass1.discover_live_window(client, conn, n_concurrent_windows=1))
    assert stats2["fetched"] == 1  # only the NEW page fetched this call, not re-counting page 1
    assert stats2["pages"] == 1
    calls = client.calls_per_window[window_key]
    assert calls[1] == "cursor-after-page-1"  # the resumed call's first request used the saved cursor

    final_checkpoint = db.get_live_window_scan_state(conn, *window_key)
    assert final_checkpoint["status"] == "done"
    assert final_checkpoint["fetched_count"] == 2
    tickers = {r[0] for r in conn.execute("SELECT ticker FROM markets").fetchall()}
    assert tickers == {"A-1", "B-1"}


def test_discover_live_window_skips_subwindow_already_done(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    window_key = (pass1.LIVE_METADATA_FLOOR, pass1.R2_END)
    db.upsert_live_window_scan_state(conn, {
        "window_start": window_key[0], "window_end": window_key[1],
        "status": "done", "cursor": None, "fetched_count": 5, "pages_fetched": 3,
    })
    conn.commit()
    client = _FakeLiveDiscoveryClient({
        window_key: [([_market("SHOULD-NOT-APPEAR", pass1.R1_START)], None)],
    })
    stats = asyncio.run(pass1.discover_live_window(client, conn, n_concurrent_windows=1))
    assert stats["fetched"] == 0
    assert stats["pages"] == 0
    assert window_key not in client.calls_per_window  # no HTTP call made at all
    assert conn.execute(
        "SELECT COUNT(*) FROM markets WHERE ticker='SHOULD-NOT-APPEAR'"
    ).fetchone()[0] == 0


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


class _FakeFlakySeriesClient:
    """FAILS (a stand-in for an exhausted-retry 429/504, both real and
    observed live) raises on FLAKY's 2nd page, after its 1st page's market
    was already found -- STABLE completes normally in the same batch."""

    def __init__(self):
        self.series = [KalshiSeries.model_validate({"ticker": "FLAKY", "category": "Weather"}),
                       KalshiSeries.model_validate({"ticker": "STABLE", "category": "Economics"})]

    async def list_series(self, category=None, limit=200):
        return self.series

    async def list_historical_markets(self, tickers=None, event_ticker=None,
                                       series_ticker=None, cursor=None, limit=100):
        window_epoch = pass1.R1_START + 3600
        if series_ticker == "FLAKY":
            if cursor is None:
                return [_market("FLAKY-p1", window_epoch)], "p2"
            raise RuntimeError("simulated exhausted-retry failure (429/504)")
        if series_ticker == "STABLE":
            return [_market("STABLE-window", window_epoch)], None
        return [], None


def test_discover_historical_series_one_failure_does_not_lose_others_progress(tmp_path):
    """The real bug this guards: without return_exceptions=True, FLAKY's
    exception used to cancel STABLE's still-in-flight coroutine too, even
    though STABLE would have completed cleanly on its own."""
    conn = db.connect(tmp_path / "t.db")
    client = _FakeFlakySeriesClient()
    stats = asyncio.run(pass1.discover_historical_series(client, conn))
    assert stats["series_failed_this_run"] == 1

    # STABLE completed despite FLAKY's sibling failure in the same gather().
    stable_state = db.get_series_scan_state(conn, "STABLE")
    assert stable_state["status"] == "done"
    assert conn.execute("SELECT COUNT(*) FROM markets WHERE ticker='STABLE-window'").fetchone()[0] == 1

    # FLAKY's page-1 progress is checkpointed, not lost, despite the page-2 failure.
    flaky_state = db.get_series_scan_state(conn, "FLAKY")
    assert flaky_state["status"] == "in_progress"
    assert flaky_state["pages_fetched"] == 1
    assert flaky_state["last_cursor"] == "p2"
    assert conn.execute("SELECT COUNT(*) FROM markets WHERE ticker='FLAKY-p1'").fetchone()[0] == 1

    assert stats["series_remaining"] == 1  # FLAKY still pending for a later call


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


def test_resolve_series_and_category_min_volume_filters_thin_markets(tmp_path):
    """A live sweep across R1+R2 can discover hundreds of thousands of thin
    markets that will never clear Phase 3's $1k volume filter -- confirmed
    live, 2026-07. min_volume_fp keeps resolve_series_and_category (one
    GET /events call per unresolved market) from spending API budget on
    markets no downstream phase will ever use."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "THICK", "event_ticker": "EVT-1", "volume_fp": 5000.0})
    db.upsert_market(conn, {"ticker": "THIN", "event_ticker": "EVT-1", "volume_fp": 10.0})
    conn.commit()

    client = _FakeResolveClient()
    stats = asyncio.run(pass1.resolve_series_and_category(client, conn, min_volume_fp=1000.0))
    assert stats["resolved_this_run"] == 1

    thick = conn.execute("SELECT series_ticker FROM markets WHERE ticker='THICK'").fetchone()
    thin = conn.execute("SELECT series_ticker FROM markets WHERE ticker='THIN'").fetchone()
    assert thick[0] == "KXHIGHNY"
    assert thin[0] is None


def test_resolve_series_and_category_min_open_duration_filters_short_markets(tmp_path):
    """The 24h open-duration filter must scope resolution too, not just the
    panel/quote loop -- category is consumed only for in-scope markets, all
    of which must clear >=24h, so resolving hourly-reset sub-markets is pure
    wasted GET /events budget (2026-07-21 pipeline audit)."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "LONG", "event_ticker": "EVT-1", "open_time_epoch": 0, "close_time_epoch": 100_000})
    db.upsert_market(conn, {"ticker": "SHORT", "event_ticker": "EVT-1", "open_time_epoch": 97_000, "close_time_epoch": 100_000})
    conn.commit()

    client = _FakeResolveClient()
    stats = asyncio.run(pass1.resolve_series_and_category(client, conn, min_open_duration_s=86_400.0))
    assert stats["resolved_this_run"] == 1

    long_row = conn.execute("SELECT series_ticker FROM markets WHERE ticker='LONG'").fetchone()
    short_row = conn.execute("SELECT series_ticker FROM markets WHERE ticker='SHORT'").fetchone()
    assert long_row[0] == "KXHIGHNY"
    assert short_row[0] is None


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


def test_fetch_price_panel_skips_whole_panel_when_no_trade_on_closing_et_day(tmp_path):
    """'closing day' = calendar ET date of close_time_epoch (construction
    pin, spec S3). If the only trade at/before close_time_epoch actually
    falls on an EARLIER ET calendar day (no trade during the closing day
    itself), day 0 must be skipped -- not silently mislabeled as the closing
    price -- exactly the skip-no-backfill discipline days 1-10 already use.
    Since every lookback day is walked back from t0, an invalid day-0
    anchor would re-anchor the whole panel onto the wrong calendar, so the
    whole market's panel is skipped (2026-07-21 audit)."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    close_epoch = et_to_epoch(datetime(2023, 6, 15, 20, 0, 0, tzinfo=ET))
    # Only trade is from the PRIOR ET calendar day -- no trade on close's own day.
    stale_trade_epoch = et_to_epoch(datetime(2023, 6, 14, 23, 0, 0, tzinfo=ET))
    client = _FakePanelClient([(stale_trade_epoch, "t_stale", 0.5)])

    result = asyncio.run(pass1.fetch_price_panel(client, conn, "A-1", close_epoch))
    assert result["rows_written"] == 0
    rows = conn.execute("SELECT COUNT(*) FROM price_panel WHERE ticker='A-1'").fetchone()[0]
    assert rows == 0


class _FakeHistoricalOnlyPanelClient:
    """Serves every trade ONLY via the historical family; live /markets/trades
    always returns empty (the pre-cutoff-market case). Records each family's
    call count so a test can prove fetch_price_panel probes live at most once
    (on the closing-day trade) rather than on every lookback day."""

    def __init__(self, trades):
        self.trades = sorted(trades, key=lambda t: -t[0])
        self.live_calls = 0
        self.historical_calls = 0

    async def get_trades(self, ticker=None, min_ts=None, max_ts=None, cursor=None, limit=100):
        self.live_calls += 1
        return [], None

    async def get_historical_trades(self, ticker=None, min_ts=None, max_ts=None, cursor=None, limit=100):
        self.historical_calls += 1
        for epoch, tid, price in self.trades:
            if epoch <= max_ts:
                return [KalshiTrade.model_validate({
                    "trade_id": tid, "ticker": ticker, "yes_price_dollars": str(price),
                    "created_time": _iso(epoch),
                })], None
        return [], None


def test_fetch_price_panel_probes_live_family_once_for_historical_market(tmp_path):
    """A pre-cutoff market's trades live only in the historical family. The
    closing-day probe determines that once; the 10 lookback days must then
    prefer historical and NOT pay a wasted empty live round-trip each
    (2026-07-21 audit -- ~2x trade calls otherwise). So live is probed
    exactly once (day 0) while historical answers all 11 days."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    close_epoch = et_to_epoch(datetime(2023, 6, 15, 20, 0, 0, tzinfo=ET))
    t0_et = epoch_to_et(close_epoch)
    trades = [(et_to_epoch(t0_et - timedelta(days=day)), f"t{day}", 0.5) for day in range(11)]

    client = _FakeHistoricalOnlyPanelClient(trades)
    result = asyncio.run(pass1.fetch_price_panel(client, conn, "A-1", close_epoch))
    assert result["rows_written"] == 11
    assert client.live_calls == 1        # only the day-0 family probe, not one per lookback day
    assert client.historical_calls == 11  # day 0 + 10 lookback days all answered here


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
        self.event_calls = []

    async def get_event(self, event_ticker):
        self.event_calls.append(event_ticker)
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
    row = conn.execute(
        "SELECT source, spread FROM quotes WHERE ticker='A-1'"
    ).fetchone()
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


def test_fetch_closing_quote_reuses_stored_series_ticker_skips_get_event(tmp_path):
    """resolve_series_and_category already persists markets.series_ticker
    before the panel/quote loop runs; passing it in must skip the redundant
    GET /events round-trip (2026-07-21 audit) -- get_event is a fallback for
    the not-yet-resolved remainder only."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    client = _FakeQuoteClient(live_candles=[_candle(1000, 0.45, 0.47)])
    result = asyncio.run(
        pass1.fetch_closing_quote(client, conn, "A-1", "EVT-1", 2000, series_ticker="KXHIGHNY")
    )
    assert result["has_quote"] is True
    assert client.event_calls == []  # never called -- series_ticker was already known
    row = conn.execute("SELECT source FROM quotes WHERE ticker='A-1'").fetchone()
    assert row[0] == "live"


def test_fetch_closing_quote_falls_back_to_get_event_when_series_ticker_unresolved(tmp_path):
    """series_ticker=None (not yet resolved) must still fall back to
    get_event -- the reuse optimization must never break the unresolved case."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    client = _FakeQuoteClient(live_candles=[_candle(1000, 0.45, 0.47)])
    result = asyncio.run(
        pass1.fetch_closing_quote(client, conn, "A-1", "EVT-1", 2000, series_ticker=None)
    )
    assert result["has_quote"] is True
    assert client.event_calls == ["EVT-1"]


def test_fetch_closing_quote_no_quote_anywhere_still_writes_a_row(tmp_path):
    """Neither live nor historical had a quote -- must still write a
    `quotes` row with spread=NULL, distinct from no row at all, so
    r1/filters.py can tell "attempted, not found" (structural) apart
    from "not yet attempted" (operational)."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "A-1"})
    client = _FakeQuoteClient(live_candles=[], historical_candles=[])
    result = asyncio.run(pass1.fetch_closing_quote(client, conn, "A-1", "EVT-1", 2000))
    assert result["has_quote"] is False
    row = conn.execute("SELECT spread FROM quotes WHERE ticker='A-1'").fetchone()
    assert row is not None
    assert row[0] is None


# ---------------------------------------------------------------------------
# run_pass1 -- min_volume_fp restricts the expensive per-market phases
# ---------------------------------------------------------------------------

class _NoOpDiscoveryClient:
    """Empty responses for every discovery call -- no NEW markets appear --
    so a run_pass1 test can focus purely on how it treats PRE-SEEDED
    markets during the panel/quote phase, tracking which tickers actually
    get a get_trades call (the expensive part min_volume_fp guards)."""

    def __init__(self):
        self.trade_fetch_calls: list[str] = []
        self.event_calls: list[str] = []

    async def list_markets(self, **kwargs):
        return [], None

    async def list_series(self, category=None, limit=200):
        return []

    async def list_historical_markets(self, **kwargs):
        return [], None

    async def get_event(self, event_ticker):
        self.event_calls.append(event_ticker)
        return None

    async def get_trades(self, ticker=None, min_ts=None, max_ts=None, cursor=None, limit=100):
        self.trade_fetch_calls.append(ticker)
        return [], None

    async def get_historical_trades(self, ticker=None, min_ts=None, max_ts=None, cursor=None, limit=100):
        return [], None

    async def get_candlesticks(self, series_ticker, ticker, start_ts, end_ts, period_interval=1440):
        return []

    async def get_historical_candlesticks(self, ticker, start_ts, end_ts, period_interval=1440):
        return []


# Both markets below open 100_000s (>24h) before close so they clear the
# default open-duration filter -- these two tests isolate the VOLUME gate,
# so their fixtures must not also trip the duration gate.
def test_run_pass1_default_min_volume_skips_thin_markets_panel_quote_fetch(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "THICK", "open_time_epoch": 0, "close_time_epoch": 100_000, "volume_fp": 5000.0})
    db.upsert_market(conn, {"ticker": "THIN", "open_time_epoch": 0, "close_time_epoch": 100_000, "volume_fp": 10.0})
    conn.commit()

    client = _NoOpDiscoveryClient()
    stats = asyncio.run(pass1.run_pass1(client, conn, max_series_this_run=0))

    assert "THICK" in client.trade_fetch_calls
    assert "THIN" not in client.trade_fetch_calls
    assert stats["markets_processed"] == 1


def test_run_pass1_min_volume_none_processes_every_market(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "THICK", "open_time_epoch": 0, "close_time_epoch": 100_000, "volume_fp": 5000.0})
    db.upsert_market(conn, {"ticker": "THIN", "open_time_epoch": 0, "close_time_epoch": 100_000, "volume_fp": 10.0})
    conn.commit()

    client = _NoOpDiscoveryClient()
    stats = asyncio.run(pass1.run_pass1(client, conn, max_series_this_run=0, min_volume_fp=None))

    assert "THICK" in client.trade_fetch_calls
    assert "THIN" in client.trade_fetch_calls
    assert stats["markets_processed"] == 2


def test_run_pass1_default_open_duration_skips_hourly_reset_markets(tmp_path):
    # LONG opens 100_000s (>24h) before close; SHORT opens 3_000s (<24h)
    # before close -- the hourly-reset crypto/index shape BDW's "open >= 24h"
    # filter exists to drop. Both clear the volume gate, so this isolates
    # the duration gate.
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "LONG", "open_time_epoch": 0, "close_time_epoch": 100_000, "volume_fp": 5000.0})
    db.upsert_market(conn, {"ticker": "SHORT", "open_time_epoch": 97_000, "close_time_epoch": 100_000, "volume_fp": 5000.0})
    conn.commit()

    client = _NoOpDiscoveryClient()
    stats = asyncio.run(pass1.run_pass1(client, conn, max_series_this_run=0))

    assert "LONG" in client.trade_fetch_calls
    assert "SHORT" not in client.trade_fetch_calls
    assert stats["markets_processed"] == 1


def test_run_pass1_default_open_duration_skips_markets_missing_open_time(tmp_path):
    # A NULL open_time_epoch fails the first guard and is skipped, matching
    # pass2: the 24h check is unverifiable without an open time.
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "NOOPEN", "close_time_epoch": 100_000, "volume_fp": 5000.0})
    conn.commit()

    client = _NoOpDiscoveryClient()
    stats = asyncio.run(pass1.run_pass1(client, conn, max_series_this_run=0))

    assert "NOOPEN" not in client.trade_fetch_calls
    assert stats["markets_processed"] == 0


def test_run_pass1_resumes_orphaned_market_missing_only_its_quote(tmp_path):
    """A market crashed between fetch_price_panel's commit and
    fetch_closing_quote's commit is left with price_panel rows but no
    quotes row. The OLD resume key (`NOT IN price_panel`) permanently
    excluded such orphans -- confirmed live, 97 existed by 2026-07-21 -- so
    their closing quote was never fetched again. The resume key must be
    `NOT IN quotes` (the row fetch_closing_quote always writes last) so the
    orphan is re-selected and its quote gets fetched; the panel replay
    (idempotent upsert) is harmless."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {
        "ticker": "ORPHAN", "open_time_epoch": 0, "close_time_epoch": 100_000, "volume_fp": 5000.0,
    })
    # Simulate the crash: a price_panel row exists, but no quotes row.
    db.upsert_price_panel_row(conn, {
        "ticker": "ORPHAN", "lookback_day": 0, "trade_id": "t0",
        "yes_price_dollars": 0.5, "created_time": "2023-01-01T00:00:00Z", "source": "historical",
    })
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM quotes WHERE ticker='ORPHAN'").fetchone()[0] == 0

    client = _NoOpDiscoveryClient()
    stats = asyncio.run(pass1.run_pass1(client, conn, max_series_this_run=0))

    assert "ORPHAN" in client.trade_fetch_calls  # re-processed, not silently skipped forever
    assert stats["markets_processed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM quotes WHERE ticker='ORPHAN'").fetchone()[0] == 1


def test_run_pass1_min_open_duration_none_processes_short_markets(tmp_path):
    # The escape hatch: min_open_duration_s=None fetches even a sub-24h
    # market (for a targeted verification run).
    conn = db.connect(tmp_path / "t.db")
    db.upsert_market(conn, {"ticker": "SHORT", "open_time_epoch": 97_000, "close_time_epoch": 100_000, "volume_fp": 5000.0})
    conn.commit()

    client = _NoOpDiscoveryClient()
    stats = asyncio.run(
        pass1.run_pass1(client, conn, max_series_this_run=0, min_open_duration_s=None)
    )

    assert "SHORT" in client.trade_fetch_calls
    assert stats["markets_processed"] == 1
