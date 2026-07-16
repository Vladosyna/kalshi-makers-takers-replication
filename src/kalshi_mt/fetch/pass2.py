"""Pass 2: full trade tape for in-scope contracts only (spec S3).

"In-scope" here is a cheap, spec-consistent PROXY of R1's real filters
(volume>=$1k, spread<=20c, open>=24h) computed directly from what Pass 1
already fetched -- not Phase 3's own, more careful construction-time
filtering (the 63-mismatch settlement check in particular needs a
settlement-price comparison Pass 2 doesn't need to wait for). The point is
sizing: the spec's own words -- "The >=$1k volume filter selects exactly the
heavy-tape markets, so pass 2 is the real budget" -- so this proxy exists to
avoid fetching full tapes for markets that clearly won't survive Phase 3's
filters, not to BE those filters.

Resumable per-market via pass2_progress (store/db.py): a market's cursor and
running trade_count survive a restart, so a multi-hour Pass 2 run (the
in-scope set can still run into the thousands of markets, each needing its
own full paginated tape) can be driven forward a bounded amount per
invocation rather than needing to complete in one sitting.
"""

from __future__ import annotations

import logging
from typing import Any

from kalshi_mt.api.kalshi import KalshiClient, KalshiTrade
from kalshi_mt.store import db
from kalshi_mt.store.parquet import TradeStore
from kalshi_mt.util import now_utc_iso

log = logging.getLogger(__name__)

MIN_VOLUME_FP = 1000.0
MAX_SPREAD = 0.20
MIN_OPEN_SECONDS = 24 * 3600


def select_in_scope_tickers(conn) -> list[str]:
    """Markets meeting the volume/spread/open-duration proxy filter, that
    Pass 2 hasn't already finished. Requires an open_time and close_time
    (both needed for the open>=24h check) and a quote row (needed for the
    spread check) -- a market missing either simply isn't in scope yet, not
    an error; Pass 1's coverage determines what's checkable here."""
    rows = conn.execute(
        """
        SELECT m.ticker
        FROM markets m
        JOIN quotes q ON q.ticker = m.ticker
        LEFT JOIN pass2_progress p ON p.ticker = m.ticker
        WHERE (m.in_r1_window = 1 OR m.in_r2_window = 1)
          AND m.volume_fp >= ?
          AND q.spread IS NOT NULL AND q.spread <= ?
          AND m.open_time_epoch IS NOT NULL AND m.close_time_epoch IS NOT NULL
          AND (m.close_time_epoch - m.open_time_epoch) >= ?
          AND (p.status IS NULL OR p.status != 'done')
        """,
        (MIN_VOLUME_FP, MAX_SPREAD, MIN_OPEN_SECONDS),
    ).fetchall()
    return [r[0] for r in rows]


async def fetch_full_tape_for_market(
    client: KalshiClient, conn, trade_store: TradeStore, ticker: str,
    page_limit: int = 1000, max_pages: int | None = None,
) -> dict[str, Any]:
    """Resumable full-tape fetch for one market. Determines live vs
    historical once (whichever family answers first) and sticks with it for
    the whole ticker -- a market's trades don't move between families
    mid-fetch, so there's no need to re-probe every page."""
    progress = db.get_pass2_progress(conn, ticker)
    cursor = progress["cursor"] if progress else None
    source = progress["source"] if progress else None
    trade_count = progress["trade_count"] if progress else 0
    started_fresh = source is None

    if source is None:
        # Determine the family once, on page 1 -- a market's trades don't
        # move between live/historical mid-fetch, so no need to re-probe
        # every subsequent page. This first page counts toward max_pages
        # just like every other page (a prior version of this function
        # didn't count it, which silently let a "one page per call" resume
        # budget actually fetch two pages on its very first invocation).
        trades, next_cursor = await client.get_trades(ticker=ticker, limit=page_limit)
        if trades:
            source = "live"
        else:
            trades, next_cursor = await client.get_historical_trades(ticker=ticker, limit=page_limit)
            source = "historical"
        if trades:
            trade_count += trade_store.append([_trade_row(t, ticker, source) for t in trades])
        cursor = next_cursor
        db.upsert_pass2_progress(conn, {
            "ticker": ticker, "status": "in_progress" if cursor else "done",
            "cursor": cursor, "source": source, "trade_count": trade_count,
        })
        conn.commit()

    fetch = client.get_trades if source == "live" else client.get_historical_trades
    pages = 1 if started_fresh else 0
    while cursor and (max_pages is None or pages < max_pages):
        trades, next_cursor = await fetch(ticker=ticker, cursor=cursor, limit=page_limit)
        if trades:
            trade_count += trade_store.append([_trade_row(t, ticker, source) for t in trades])
        pages += 1
        cursor = next_cursor
        db.upsert_pass2_progress(conn, {
            "ticker": ticker, "status": "in_progress" if cursor else "done",
            "cursor": cursor, "source": source, "trade_count": trade_count,
        })
        conn.commit()

    status = "done" if not cursor else "in_progress"
    return {"ticker": ticker, "status": status, "trade_count": trade_count, "source": source}


def _trade_row(t: KalshiTrade, ticker: str, source: str) -> dict[str, Any]:
    return {
        "trade_id": t.trade_id, "ticker": ticker, "count_fp": t.count_fp,
        "yes_price_dollars": t.yes_price_dollars, "no_price_dollars": t.no_price_dollars,
        "taker_outcome_side": t.taker_outcome_side, "taker_book_side": t.taker_book_side,
        "taker_side": t.taker_side, "created_time": t.created_time,
        "is_block_trade": t.is_block_trade, "source": source,
    }


async def run_pass2(
    client: KalshiClient, conn, trade_store: TradeStore,
    ticker_limit: int | None = None, max_pages_per_market: int | None = None,
) -> dict[str, Any]:
    """Fetch full tapes for in-scope markets not yet done. `ticker_limit`
    bounds how many markets this invocation processes (each to completion,
    modulo `max_pages_per_market`) -- pass small values for verification
    runs rather than the full in-scope set."""
    tickers = select_in_scope_tickers(conn)
    if ticker_limit is not None:
        tickers = tickers[:ticker_limit]

    log_id = db.log_fetch(conn, "pass2", f"{len(tickers)} tickers", "in_progress")
    results = []
    total_trades = 0
    done_count = 0
    for ticker in tickers:
        result = await fetch_full_tape_for_market(
            client, conn, trade_store, ticker, max_pages=max_pages_per_market
        )
        results.append(result)
        total_trades += result["trade_count"]
        if result["status"] == "done":
            done_count += 1

    db.finish_fetch_log(
        conn, log_id, "done", fetched_count=total_trades,
        notes=f"{done_count}/{len(tickers)} markets fully done this run",
    )
    conn.commit()
    return {
        "tickers_attempted": len(tickers), "markets_done": done_count,
        "total_trades_written": total_trades, "results": results,
    }
