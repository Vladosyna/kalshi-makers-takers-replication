"""Pass 1: whole-universe metadata discovery + boundary-tick price panel +
closing quotes for the spread filter (spec S3).

Two independently resumable discovery sub-phases, since they need
fundamentally different Kalshi endpoint families (Phase 1's own empirical
findings -- see api/kalshi.py's module docstring):

  - Live sweep (2023-01-01..R2 end): cheap, cursor-paginated
    min_close_ts/max_close_ts range queries against /markets. No `status`
    filter -- proven unreliable this far back (a real live probe found
    status="settled" returns nothing at all for a 2023 window, while the
    same window with no status filter returns real markets). Every market
    in range is upserted regardless of its live status/result fields, which
    are frequently stale for older markets; Phase 3's construction re-derives
    "did this actually resolve" from trade/settlement evidence, not from
    trusting these fields.
  - Historical series scan (2021-01-01..2023-01-01): live /markets returns
    NOTHING for this era (confirmed live). The only path is paging every
    series' /historical/markets by cursor until reaching the window or
    exhausting that series' history, checkpointed per series in
    series_scan_state so a multi-hour scan (spec's own "1-3 day polite
    fetch" estimate, run across the full ~12k-series universe) survives a
    restart and can be driven forward a bounded amount per invocation.

Then, for every discovered market: resolve series_ticker (GET
/events/{event_ticker} -- not a Market field) and category, fetch the ~11
boundary-tick price-panel rows, and fetch one closing candlestick for the
spread filter.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from kalshi_mt.api.kalshi import KalshiClient, KalshiMarket, KalshiTrade
from kalshi_mt.store import db
from kalshi_mt.util import (
    epoch_to_et,
    et_day_start,
    et_to_epoch,
    iso_to_epoch,
    shift_et_calendar_days,
)

log = logging.getLogger(__name__)

R1_START = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp())
R1_END = int(datetime(2025, 4, 30, 23, 59, 59, tzinfo=timezone.utc).timestamp())
R2_START = int(datetime(2025, 5, 1, tzinfo=timezone.utc).timestamp())
R2_END = int(datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc).timestamp())
# Empirically confirmed during Phase 1: live /markets returns real metadata
# for close_time ranges from roughly 2023 onward, but genuinely nothing for
# 2021-2022 -- a different (longer) retention window than /historical/cutoff's
# own ~60-day live/historical trades-and-candlesticks boundary.
LIVE_METADATA_FLOOR = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())

PANEL_LOOKBACK_DAYS = 10


def _window_flags(close_time_epoch: int | None) -> tuple[int, int]:
    if close_time_epoch is None:
        return 0, 0
    in_r1 = int(R1_START <= close_time_epoch <= R1_END)
    in_r2 = int(R2_START <= close_time_epoch <= R2_END)
    return in_r1, in_r2


def _market_to_row(m: KalshiMarket, source: str) -> dict[str, Any]:
    close_epoch = iso_to_epoch(m.close_time)
    open_epoch = iso_to_epoch(m.open_time)
    in_r1, in_r2 = _window_flags(close_epoch)
    return {
        "ticker": m.ticker, "event_ticker": m.event_ticker, "status": m.status,
        "result": m.result, "open_time": m.open_time, "open_time_epoch": open_epoch,
        "close_time": m.close_time, "close_time_epoch": close_epoch,
        "settlement_ts": m.settlement_ts, "volume_fp": m.volume_fp, "metadata_source": source,
        "in_r1_window": in_r1, "in_r2_window": in_r2,
    }


# -- Sub-phase A: live sweep (2023-01-01 .. R2_END) --------------------------

async def discover_live_window(
    client: KalshiClient, conn, start_ts: int = LIVE_METADATA_FLOOR, end_ts: int = R2_END,
    page_limit: int = 1000, max_pages: int | None = None,
) -> dict[str, int]:
    log_id = db.log_fetch(conn, "pass1_discovery_live", f"{start_ts}-{end_ts}", "in_progress")
    cursor: str | None = None
    fetched = 0
    pages = 0
    while True:
        markets, next_cursor = await client.list_markets(
            min_close_ts=start_ts, max_close_ts=end_ts, cursor=cursor, limit=page_limit
        )
        for m in markets:
            db.upsert_market(conn, _market_to_row(m, "live"))
        fetched += len(markets)
        conn.commit()
        pages += 1
        if not next_cursor or not markets:
            break
        cursor = next_cursor
        if max_pages is not None and pages >= max_pages:
            break
    db.finish_fetch_log(conn, log_id, "done", fetched_count=fetched, notes=f"{pages} pages")
    conn.commit()
    return {"fetched": fetched, "pages": pages}


# -- Sub-phase B: historical series scan (2021-01-01 .. 2023-01-01) ---------

async def discover_historical_series(
    client: KalshiClient, conn, start_ts: int = R1_START, end_ts: int = LIVE_METADATA_FLOOR,
    max_series_this_run: int | None = None, max_pages_per_series: int = 20,
) -> dict[str, int]:
    all_series = await client.list_series(limit=100_000)  # /series has no real limit param; client-truncates
    for s in all_series:
        if db.get_series_scan_state(conn, s.ticker) is None:
            db.upsert_series_scan_state(conn, {
                "series_ticker": s.ticker, "status": "pending", "pages_fetched": 0,
                "markets_found_in_window": 0, "reached_before_window": 0, "last_cursor": None,
            })
    conn.commit()

    pending = conn.execute(
        "SELECT series_ticker, last_cursor, pages_fetched FROM series_scan_state "
        "WHERE status IN ('pending', 'in_progress') ORDER BY series_ticker"
    ).fetchall()
    if max_series_this_run is not None:
        pending = pending[:max_series_this_run]

    series_processed = 0
    markets_found_total = 0
    for row in pending:
        ticker, cursor, pages_so_far = row["series_ticker"], row["last_cursor"], row["pages_fetched"]
        found_in_series = 0
        reached_before = False
        for _ in range(max_pages_per_series):
            markets, next_cursor = await client.list_historical_markets(
                series_ticker=ticker, cursor=cursor, limit=1000
            )
            pages_so_far += 1
            if not markets:
                break
            close_epochs: list[int] = []
            for m in markets:
                row_dict = _market_to_row(m, "historical")
                ce = row_dict["close_time_epoch"]
                if ce is not None:
                    close_epochs.append(ce)
                if ce is not None and start_ts <= ce < end_ts:
                    db.upsert_market(conn, row_dict)
                    found_in_series += 1
            # Pages are reverse-chronological (empirically confirmed, Phase 1):
            # once a page's newest row already predates the window, every
            # later page for this series is even older.
            if close_epochs and max(close_epochs) < start_ts:
                reached_before = True
                break
            if not next_cursor:
                break
            cursor = next_cursor
        db.upsert_series_scan_state(conn, {
            "series_ticker": ticker, "status": "done", "pages_fetched": pages_so_far,
            "markets_found_in_window": found_in_series,
            "reached_before_window": int(reached_before), "last_cursor": cursor,
        })
        conn.commit()
        series_processed += 1
        markets_found_total += found_in_series

    remaining = conn.execute(
        "SELECT COUNT(*) FROM series_scan_state WHERE status IN ('pending', 'in_progress')"
    ).fetchone()[0]
    return {
        "series_processed_this_run": series_processed,
        "markets_found_this_run": markets_found_total,
        "series_remaining": remaining,
    }


# -- Sub-phase C: series_ticker + category resolution ------------------------

async def resolve_series_and_category(
    client: KalshiClient, conn, batch_size: int | None = 500,
) -> dict[str, int]:
    all_series = await client.list_series(limit=100_000)
    category_by_series = {s.ticker: s.category for s in all_series}

    rows = conn.execute(
        "SELECT ticker, event_ticker FROM markets "
        "WHERE series_ticker IS NULL AND event_ticker IS NOT NULL"
    ).fetchall()
    if batch_size is not None:
        rows = rows[:batch_size]

    event_cache: dict[str, str | None] = {}
    resolved = 0
    for row in rows:
        ticker, event_ticker = row["ticker"], row["event_ticker"]
        if event_ticker not in event_cache:
            event = await client.get_event(event_ticker)
            event_cache[event_ticker] = event.series_ticker if event else None
        series_ticker = event_cache[event_ticker]
        if series_ticker is None:
            continue
        conn.execute(
            "UPDATE markets SET series_ticker = ?, category = ? WHERE ticker = ?",
            (series_ticker, category_by_series.get(series_ticker), ticker),
        )
        resolved += 1
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE series_ticker IS NULL AND event_ticker IS NOT NULL"
    ).fetchone()[0]
    return {"resolved_this_run": resolved, "remaining": remaining}


# -- Sub-phase D: boundary-tick price panel -----------------------------------

async def _last_trade_before(
    client: KalshiClient, ticker: str, max_ts: int,
) -> tuple[KalshiTrade, str] | tuple[None, None]:
    """The single most recent trade at/before max_ts, live then historical
    fallback. Assumes (confirmed empirically, Phase 1) both /markets/trades
    and /historical/trades return results newest-first by default, so
    limit=1 with max_ts set gives exactly this trade directly -- no need to
    fetch a whole day's tape just to find its last row."""
    trades, _ = await client.get_trades(ticker=ticker, max_ts=max_ts, limit=1)
    if trades:
        return trades[0], "live"
    trades, _ = await client.get_historical_trades(ticker=ticker, max_ts=max_ts, limit=1)
    if trades:
        return trades[0], "historical"
    return None, None


async def fetch_price_panel(
    client: KalshiClient, conn, ticker: str, close_time_epoch: int,
) -> dict[str, int]:
    """spec S1: "last trade on closing day plus last trade before the same
    time on each of up to 10 prior days." lookback_day=0's own trade
    timestamp is the reference clock time for every subsequent day (not
    close_time itself) -- construction pin from the plan: skip a lookback
    day with no trade STRICTLY within that ET calendar day, never backfill
    from an earlier day just because a bracketed query happened to return
    one."""
    trade0, source0 = await _last_trade_before(client, ticker, close_time_epoch)
    if trade0 is None:
        return {"rows_written": 0}
    t0_epoch = iso_to_epoch(trade0.created_time)
    if t0_epoch is None:
        return {"rows_written": 0}
    db.upsert_price_panel_row(conn, {
        "ticker": ticker, "lookback_day": 0, "trade_id": trade0.trade_id,
        "yes_price_dollars": trade0.yes_price_dollars, "created_time": trade0.created_time,
        "source": source0,
    })
    written = 1
    t0_et = epoch_to_et(t0_epoch)

    for day in range(1, PANEL_LOOKBACK_DAYS + 1):
        ref_et = shift_et_calendar_days(t0_et, day)
        ref_epoch = et_to_epoch(ref_et)
        day_start_epoch = et_to_epoch(et_day_start(ref_et))
        trade, source = await _last_trade_before(client, ticker, ref_epoch)
        if trade is None:
            continue
        trade_epoch = iso_to_epoch(trade.created_time)
        if trade_epoch is None or trade_epoch < day_start_epoch:
            continue  # no qualifying trade strictly within this ET calendar day -- skip, no backfill
        db.upsert_price_panel_row(conn, {
            "ticker": ticker, "lookback_day": day, "trade_id": trade.trade_id,
            "yes_price_dollars": trade.yes_price_dollars, "created_time": trade.created_time,
            "source": source,
        })
        written += 1
    conn.commit()
    return {"rows_written": written}


# -- Sub-phase E: closing quote for the spread filter -------------------------

async def fetch_closing_quote(
    client: KalshiClient, conn, ticker: str, event_ticker: str | None, close_time_epoch: int,
) -> dict[str, bool]:
    """One closing candlestick per market -- the input to spec S1's final
    spread<=20c filter (Phase 3). Live then historical fallback."""
    start_ts, end_ts = close_time_epoch - 86_400, close_time_epoch
    candles = []
    source = "live"
    if event_ticker:
        event = await client.get_event(event_ticker)
        if event and event.series_ticker:
            try:
                candles = await client.get_candlesticks(
                    event.series_ticker, ticker, start_ts, end_ts, period_interval=60
                )
            except Exception:
                candles = []
    if not candles:
        source = "historical"
        try:
            candles = await client.get_historical_candlesticks(
                ticker, start_ts, end_ts, period_interval=60
            )
        except Exception:
            candles = []

    quoted = [c for c in candles if c.has_quote]
    if not quoted:
        return {"has_quote": False}
    last = max(quoted, key=lambda c: c.end_period_ts or 0)
    spread = None
    if last.yes_ask.close_dollars is not None and last.yes_bid.close_dollars is not None:
        spread = round(last.yes_ask.close_dollars - last.yes_bid.close_dollars, 4)
    db.upsert_quote(conn, {
        "ticker": ticker, "end_period_ts": last.end_period_ts,
        "yes_bid_close": last.yes_bid.close_dollars, "yes_ask_close": last.yes_ask.close_dollars,
        "spread": spread, "source": source,
    })
    conn.commit()
    return {"has_quote": True}


# -- Orchestration -------------------------------------------------------------

async def run_pass1(
    client: KalshiClient, conn,
    max_series_this_run: int | None = None,
    market_processing_limit: int | None = None,
    live_max_pages: int | None = None,
    series_resolution_batch_size: int | None = 500,
) -> dict[str, Any]:
    """Discovery (live sweep + historical series scan) -> series/category
    resolution -> price-panel + closing-quote fetch for every discovered
    market missing them. Every sub-phase is independently resumable; pass
    small `max_series_this_run`/`market_processing_limit`/`live_max_pages`
    values to bound a single invocation (verification, a scheduled
    incremental run) rather than the full multi-hour sweep. The live sweep
    alone can touch tens of thousands of markets across the full R1+R2
    window (spec's own ~50-80k-market estimate) -- `live_max_pages` is what
    keeps a verification run from silently turning into a near-production
    one; omit it only when you actually intend the full sweep."""
    live_stats = await discover_live_window(client, conn, max_pages=live_max_pages)
    hist_stats = await discover_historical_series(
        client, conn, max_series_this_run=max_series_this_run
    )
    resolve_stats = await resolve_series_and_category(
        client, conn, batch_size=series_resolution_batch_size
    )

    rows = conn.execute(
        "SELECT ticker, event_ticker, close_time_epoch FROM markets "
        "WHERE close_time_epoch IS NOT NULL "
        "AND ticker NOT IN (SELECT ticker FROM price_panel)"
    ).fetchall()
    if market_processing_limit is not None:
        rows = rows[:market_processing_limit]

    panel_written = 0
    quotes_written = 0
    for row in rows:
        panel_result = await fetch_price_panel(client, conn, row["ticker"], row["close_time_epoch"])
        panel_written += panel_result["rows_written"]
        quote_result = await fetch_closing_quote(
            client, conn, row["ticker"], row["event_ticker"], row["close_time_epoch"]
        )
        quotes_written += int(quote_result["has_quote"])

    return {
        "live_discovery": live_stats,
        "historical_discovery": hist_stats,
        "series_resolution": resolve_stats,
        "markets_processed": len(rows),
        "panel_rows_written": panel_written,
        "quotes_written": quotes_written,
    }
