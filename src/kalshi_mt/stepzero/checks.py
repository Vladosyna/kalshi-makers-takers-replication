"""The five Step Zero checks (spec S3, hard gate).

Design notes from live API research (docs.kalshi.com, 2026-07):

- `/historical/markets` cannot be filtered by close/settled timestamp range
  (only `tickers`/`event_ticker`/`series_ticker`) -- unlike the live
  `/markets` endpoint, which accepts those range filters even for dates well
  before the historical cutoff. Discovery of "which tickers are old" always
  goes through the live endpoint's range filters first; the historical
  endpoint is only used to fetch full records for tickers already known.
- `GET /historical/cutoff` reports the exact boundary at which data moves
  from the live family to the historical family -- check 2 records it as
  evidence directly, rather than leaving the boundary to be inferred solely
  from which calls happened to come back empty. Discovery/fetch logic still
  tries live-then-historical defensively rather than branching purely on
  the cutoff value, since a market can be metadata-only in one family and
  data-bearing in the other for reasons the cutoff alone doesn't capture.
- `series_ticker` is not a field of Market; resolving it (needed for the
  live candlesticks endpoint's path) requires `GET /events/{event_ticker}`.
  The historical candlesticks endpoint needs no series_ticker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from kalshi_mt.api.kalshi import KalshiClient, KalshiMarket

log = logging.getLogger(__name__)

CheckStatus = Literal["PASS", "PARTIAL", "FAIL", "AUTH_REQUIRED"]


@dataclass
class CheckResult:
    id: int
    name: str
    status: CheckStatus
    evidence: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


def _epoch(y: int, m: int, d: int = 1) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def _parse_iso_epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(v).timestamp())
    except (ValueError, TypeError):
        return None


def _is_auth_error(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403)


def _http_status_evidence(exc: httpx.HTTPStatusError) -> dict[str, Any]:
    return {
        "status_code": exc.response.status_code,
        "url": str(exc.request.url) if exc.request is not None else None,
        "body": exc.response.text[:500],
    }


# ---------------------------------------------------------------------------
# Check 1 -- unauthenticated access
# ---------------------------------------------------------------------------


async def check_unauthenticated_access(client: KalshiClient) -> CheckResult:
    try:
        markets, _ = await client.list_markets(limit=5)
    except httpx.HTTPStatusError as exc:
        if _is_auth_error(exc):
            return CheckResult(
                1, "Unauthenticated access", "AUTH_REQUIRED",
                {"endpoint": "/markets", **_http_status_evidence(exc)},
                "GET /markets requires authentication -- hard gate tripped.",
            )
        return CheckResult(
            1, "Unauthenticated access", "FAIL",
            {"endpoint": "/markets", **_http_status_evidence(exc)},
            "GET /markets returned an unexpected error status.",
        )
    except Exception as exc:  # network/parse failure, not an auth decision
        return CheckResult(
            1, "Unauthenticated access", "FAIL", {"endpoint": "/markets", "error": str(exc)},
            "GET /markets failed for a reason other than auth.",
        )

    try:
        trades, _ = await client.get_trades(limit=5)
    except httpx.HTTPStatusError as exc:
        if _is_auth_error(exc):
            return CheckResult(
                1, "Unauthenticated access", "AUTH_REQUIRED",
                {"endpoint": "/trades", **_http_status_evidence(exc)},
                "GET /trades requires authentication -- hard gate tripped.",
            )
        return CheckResult(
            1, "Unauthenticated access", "FAIL",
            {"endpoint": "/trades", **_http_status_evidence(exc)},
            "GET /trades returned an unexpected error status.",
        )
    except Exception as exc:
        return CheckResult(
            1, "Unauthenticated access", "FAIL", {"endpoint": "/trades", "error": str(exc)},
            "GET /trades failed for a reason other than auth.",
        )

    return CheckResult(
        1, "Unauthenticated access", "PASS",
        {"markets_returned": len(markets), "trades_returned": len(trades)},
        "Both /markets and /trades respond without any auth header.",
    )


# ---------------------------------------------------------------------------
# Check 2 -- 2021-2022 positive-check
# ---------------------------------------------------------------------------


_FALLBACK_CATEGORIES = ("Climate and Weather", "Economics")
_MAX_SERIES_TRIED = 10  # per category
_MAX_PAGES_PER_SERIES = 20  # empirically, a long-running daily series needs ~9 pages of 1000 to reach 2021
# Confirmed by direct manual probing (2026-07): KXHIGHNY's historical markets page
# back to 2021-08-07 (~9 pages of 1000). /series has no stable ordering and no
# "how long has this run" signal, so most categories' first few series returned
# turn out to be much newer products with far shallower history -- this one
# known-deep anchor guarantees the discovery loop has at least one series that
# will actually reach the 2021-2022 window, with the category scan below adding
# genuine diversity on top rather than being the sole source.
_KNOWN_ANCHOR_SERIES = ("KXHIGHNY",)


async def discover_candidate_early_tickers(
    client: KalshiClient, n: int = 5
) -> tuple[list[KalshiMarket], dict[str, Any]]:
    """Find up to n settled markets closing in 2021-2022, spread across
    distinct series where possible.

    Two facts, both confirmed empirically against the live API rather than
    assumed from docs, drive this design:

    1. Live /markets has its own (long) metadata retention -- confirmed back
       to 2023 -- but genuinely returns nothing for 2021-2022 date ranges;
       that data exists only via /historical/markets.
    2. /historical/markets has no close-time range filter (only
       tickers/event_ticker/series_ticker/cursor/limit) and returns each
       series' settled markets in reverse-chronological pages -- reaching
       2021 for a long-running daily series (e.g. weather) took ~9 pages of
       1000 in testing. A single-page-per-series breadth scan across
       Kalshi's ~12k series (tried first, then abandoned) never reaches far
       enough back for any given series and is also far too slow. The fix is
       depth, not breadth: page a handful of series that are likely to have
       run since 2021 (weather, economics -- long-standing Kalshi verticals)
       until each is exhausted or clearly past the window, not scan broadly
       across series that mostly didn't exist yet in 2021.

    This category bias is a Step Zero diagnostic convenience only -- it has
    no bearing on Phase 3's actual R1 sample construction, which follows the
    spec's own deterministic, non-editorial filters.
    """
    start_ts, end_ts = _epoch(2021, 1, 1), _epoch(2023, 1, 1)
    evidence: dict[str, Any] = {}

    cutoff = await client.get_historical_cutoff()
    if cutoff is not None:
        evidence["historical_cutoff"] = {
            "market_settled_ts": cutoff.market_settled_ts,
            "trades_created_ts": cutoff.trades_created_ts,
        }

    markets, _ = await client.list_markets(
        status="settled", min_close_ts=start_ts, max_close_ts=end_ts, limit=100
    )
    evidence["live_endpoint_candidate_count"] = len(markets)
    fallback_used = False

    if len(markets) < n:
        fallback_used = True
        found: list[KalshiMarket] = []
        series_pages: dict[str, int] = {}
        series_tried: list[str] = []

        async def _page_series(ticker: str) -> None:
            cursor: str | None = None
            for page in range(_MAX_PAGES_PER_SERIES):
                hist_markets, next_cursor = await client.list_historical_markets(
                    series_ticker=ticker, cursor=cursor, limit=1000
                )
                series_pages[ticker] = page + 1
                if not hist_markets:
                    break
                close_epochs = [
                    e for e in (_parse_iso_epoch(m.close_time) for m in hist_markets) if e is not None
                ]
                for m in hist_markets:
                    ct = _parse_iso_epoch(m.close_time)
                    # NOTE: verified against real API responses -- a resolved
                    # Market object's own `status` field is "finalized" (or,
                    # transiently, "determined"/"disputed"/"amended"), never
                    # the literal string "settled" -- that value only exists
                    # in the /markets QUERY PARAMETER's enum, not the
                    # returned object. `result` populated with "yes"/"no" is
                    # the correct, robust signal a market actually resolved.
                    if ct is not None and start_ts <= ct < end_ts and m.result in ("yes", "no"):
                        found.append(m)
                # Pages are reverse-chronological: once a page's newest row
                # already predates our window, every later page for this
                # series is even older -- stop paging it.
                if close_epochs and max(close_epochs) < start_ts:
                    break
                if not next_cursor:
                    break
                cursor = next_cursor

        for ticker in _KNOWN_ANCHOR_SERIES:
            series_tried.append(ticker)
            await _page_series(ticker)

        for category in _FALLBACK_CATEGORIES:
            if len(markets) + len(found) >= n * 3:
                break
            series_list = await client.list_series(category=category, limit=_MAX_SERIES_TRIED)
            for series in series_list:
                if series.ticker in _KNOWN_ANCHOR_SERIES:
                    continue  # already tried above
                series_tried.append(series.ticker)
                if len(markets) + len(found) >= n * 3:
                    break
                await _page_series(series.ticker)

        markets = markets + found
        evidence["series_tried"] = series_tried
        evidence["series_pages_fetched"] = series_pages
        evidence["historical_fallback_candidate_count"] = len(found)

    evidence["fallback_used"] = fallback_used

    # A market with volume_fp == 0 can never have trades by definition (e.g.
    # an untraded strike in a multi-strike range series) -- picking one would
    # test nothing about the API's ability to serve real history. Prefer
    # markets with recorded volume; only fall back to zero-volume ones if
    # genuinely nothing else is available, so the check still reports
    # something rather than silently under-filling the candidate list.
    seen: set[str] = set()
    with_volume: list[KalshiMarket] = []
    without_volume: list[KalshiMarket] = []
    for m in markets:
        if m.ticker in seen:
            continue
        seen.add(m.ticker)
        (with_volume if (m.volume_fp or 0) > 0 else without_volume).append(m)

    diverse = (with_volume + without_volume)[:n]
    evidence["candidate_tickers"] = [m.ticker for m in diverse]
    return diverse, evidence


def _volume_roughly_consistent(volume_fp: float | None, trade_count: int) -> bool:
    """Sanity check only -- a market with meaningful volume_fp should have
    at least one trade; we don't try to reconcile exact counts (a single
    trade can fill many contracts)."""
    if volume_fp is None:
        return trade_count > 0
    if volume_fp <= 0:
        return True
    return trade_count > 0


async def check_2021_2022_positive(client: KalshiClient) -> CheckResult:
    candidates, discovery_evidence = await discover_candidate_early_tickers(client, n=5)
    if not candidates:
        return CheckResult(
            2, "2021-2022 positive-check", "FAIL", {"discovery": discovery_evidence},
            "No candidate 2021-2022 settled markets could be discovered at all.",
        )

    per_market: list[dict[str, Any]] = []
    any_historical_fallback = False
    all_ok = True

    for m in candidates:
        entry: dict[str, Any] = {
            "ticker": m.ticker, "close_time": m.close_time, "volume_fp": m.volume_fp,
        }

        detail = await client.get_market(m.ticker)
        used_hist_meta = False
        if detail is None or not detail.close_time:
            hist, _ = await client.list_historical_markets(tickers=m.ticker, limit=1)
            detail = hist[0] if hist else detail
            used_hist_meta = True
        entry["metadata_source"] = "historical" if used_hist_meta else "live"
        entry["metadata_close_time_in_window"] = bool(
            detail and detail.close_time and _epoch(2021, 1, 1) <= (_parse_iso_epoch(detail.close_time) or 0) < _epoch(2023, 1, 1)
        )

        trades, _ = await client.get_trades(ticker=m.ticker, limit=1000)
        used_hist_trades = False
        if not trades:
            trades, _ = await client.get_historical_trades(ticker=m.ticker, limit=1000)
            used_hist_trades = True
            if trades:
                any_historical_fallback = True
        entry["trade_count"] = len(trades)
        entry["trades_source"] = "historical" if used_hist_trades else "live"
        entry["volume_consistent"] = _volume_roughly_consistent(m.volume_fp, len(trades))

        market_ok = bool(trades) and detail is not None and entry["metadata_close_time_in_window"]
        entry["ok"] = market_ok
        if not market_ok:
            all_ok = False
        per_market.append(entry)

    if all_ok:
        status: CheckStatus = "PARTIAL" if any_historical_fallback else "PASS"
        notes = (
            "All 5 sample markets yielded real 2021-2022 metadata and trade history"
            + (", via the /historical/* fallback for at least one." if any_historical_fallback else " from the live endpoints directly.")
        )
    else:
        status = "FAIL"
        notes = "At least one 2021-2022 sample market yielded zero trades or unverifiable metadata from both live and historical endpoints."

    return CheckResult(
        2, "2021-2022 positive-check", status,
        {"discovery": discovery_evidence, "markets": per_market}, notes,
    )


# ---------------------------------------------------------------------------
# Check 3 -- taker_outcome_side / taker_book_side population by era
# ---------------------------------------------------------------------------


async def _sample_trades_for_era(
    client: KalshiClient,
    known_ticker: str | None,
    min_close_ts: int | None,
    max_close_ts: int | None,
) -> tuple[str | None, list, str]:
    """Return (ticker_used, trades, source) for one representative market in an era."""
    if known_ticker is not None:
        candidates = [known_ticker]
    else:
        # NOTE: verified against real API responses -- for older markets the
        # live /markets endpoint's own `result` field frequently comes back
        # empty even though `status` shows "closed" (a live-endpoint sample
        # of 59 real 2023-era markets had zero with a populated result), and
        # a `status="settled"` query filter returns nothing at all for that
        # far back (its live-endpoint meaning is narrower than "resolved a
        # while ago"). Neither the query filter nor the object's own fields
        # are trustworthy signals of "did this actually resolve and trade"
        # for old markets -- fetching trades directly is. So: no status
        # filter, no result requirement, just a wide close-time-windowed
        # candidate pool, verified by whether trades actually come back.
        found, _ = await client.list_markets(
            min_close_ts=min_close_ts, max_close_ts=max_close_ts, limit=50
        )
        if not found:
            return None, [], "none"
        candidates = [m.ticker for m in found]

    # A listed market can still have zero fills (e.g. an illiquid strike
    # nobody traded) -- try several candidates rather than accepting
    # whichever one happened to sort first.
    for ticker in candidates:
        trades, _ = await client.get_trades(ticker=ticker, limit=1000)
        source = "live"
        if not trades:
            trades, _ = await client.get_historical_trades(ticker=ticker, limit=1000)
            source = "historical"
        if trades:
            return ticker, trades, source
    return candidates[0], [], "none"


async def check_taker_field_population(
    client: KalshiClient, era_2021_2022_tickers: list[str]
) -> CheckResult:
    era_defs: list[tuple[str, str | None, int | None, int | None]] = [
        ("2021-2022", era_2021_2022_tickers[0] if era_2021_2022_tickers else None,
         _epoch(2021, 1, 1), _epoch(2023, 1, 1)),
        ("2023", None, _epoch(2023, 1, 1), _epoch(2024, 1, 1)),
        ("2024", None, _epoch(2024, 1, 1), _epoch(2025, 1, 1)),
        ("2025-jan-apr", None, _epoch(2025, 1, 1), _epoch(2025, 5, 1)),
    ]

    per_era: dict[str, Any] = {}
    worst_rate = 1.0
    any_data = False

    for label, known_ticker, min_ts, max_ts in era_defs:
        ticker, trades, source = await _sample_trades_for_era(client, known_ticker, min_ts, max_ts)
        if not trades:
            per_era[label] = {"ticker": ticker, "trade_count": 0, "source": source}
            continue
        any_data = True
        n = len(trades)
        outcome_pop = sum(1 for t in trades if t.taker_outcome_side is not None) / n
        book_pop = sum(1 for t in trades if t.taker_book_side is not None) / n
        legacy_pop = sum(1 for t in trades if t.taker_side is not None) / n
        per_era[label] = {
            "ticker": ticker, "trade_count": n, "source": source,
            "taker_outcome_side_population": round(outcome_pop, 4),
            "taker_book_side_population": round(book_pop, 4),
            "taker_side_legacy_population": round(legacy_pop, 4),
        }
        worst_rate = min(worst_rate, outcome_pop, book_pop)

    if not any_data:
        status: CheckStatus = "FAIL"
    elif worst_rate >= 0.95:
        status = "PASS"
    else:
        # Purpose note: thin new-field coverage is a documented degradation
        # for Phase 6 to know about, never a blocker -- this check cannot
        # trip the STOP gate. taker_side (legacy) remains a guaranteed
        # fallback per Kalshi's changelog ("will not be removed before
        # 2026-05-28", still in the future).
        status = "PARTIAL"

    notes = (
        "taker_outcome_side/taker_book_side population by era, with legacy taker_side "
        "tracked as a comparison point. Low population in an era is a documented "
        "degradation for Phase 6's fee/return work, not a failure -- taker_side remains "
        "live and populated per Kalshi's own deprecation notice."
    )
    return CheckResult(3, "Taker-field population", status, {"eras": per_era}, notes)


# ---------------------------------------------------------------------------
# Check 4 -- /trades timestamp bracketing
# ---------------------------------------------------------------------------


async def check_timestamp_bracketing(client: KalshiClient, ticker: str) -> CheckResult:
    full, _ = await client.get_trades(ticker=ticker, limit=1000)
    use_historical = False
    if not full:
        full, _ = await client.get_historical_trades(ticker=ticker, limit=1000)
        use_historical = True
    if not full:
        return CheckResult(
            4, "/trades timestamp bracketing", "FAIL", {"ticker": ticker},
            "No trades available for the bracketing probe ticker.",
        )

    timestamps = sorted(t for t in (_parse_iso_epoch(tr.created_time) for tr in full) if t is not None)
    if len(timestamps) < 3:
        return CheckResult(
            4, "/trades timestamp bracketing", "FAIL",
            {"ticker": ticker, "trade_count": len(full)},
            "Too few timestamped trades to probe bracketing.",
        )

    lo, hi = timestamps[0], timestamps[-1]
    mid_lo = lo + (hi - lo) // 3
    mid_hi = lo + 2 * (hi - lo) // 3

    # Bracket against the SAME endpoint family the full set came from -- a
    # ticker whose trades live only in /historical/trades will always come
    # back empty from the live endpoint regardless of whether min_ts/max_ts
    # bracketing itself works, which would misreport this check as FAIL.
    if use_historical:
        bracketed, _ = await client.get_historical_trades(ticker=ticker, min_ts=mid_lo, max_ts=mid_hi, limit=1000)
    else:
        bracketed, _ = await client.get_trades(ticker=ticker, min_ts=mid_lo, max_ts=mid_hi, limit=1000)

    strictly_bounded = 0 < len(bracketed) < len(full) or (
        len(bracketed) > 0
        and all(mid_lo <= (_parse_iso_epoch(t.created_time) or 0) <= mid_hi for t in bracketed)
        and len(bracketed) != len(full)
    )
    evidence = {
        "ticker": ticker, "full_count": len(full), "bracketed_count": len(bracketed),
        "range": [lo, hi], "bracket": [mid_lo, mid_hi],
    }
    if strictly_bounded:
        return CheckResult(4, "/trades timestamp bracketing", "PASS", evidence,
                            "min_ts/max_ts returns a strictly bounded subset.")
    return CheckResult(4, "/trades timestamp bracketing", "FAIL", evidence,
                        "Bracketed query returned the same set as unbounded -- params may be ignored.")


# ---------------------------------------------------------------------------
# Check 5 -- candlestick/quote availability for the spread filter
# ---------------------------------------------------------------------------


async def check_quote_availability(
    client: KalshiClient, era_tickers: dict[str, str | None]
) -> CheckResult:
    per_era: dict[str, Any] = {}
    any_quote = False
    every_era_has_quote = True

    for label, ticker in era_tickers.items():
        if ticker is None:
            per_era[label] = {"ticker": None, "has_quote": False, "reason": "no market discovered"}
            every_era_has_quote = False
            continue

        market = await client.get_market(ticker)
        if market is None or not market.close_time:
            # Live /markets/{ticker} 404s for markets old enough to have
            # rolled into the historical archive (same live/historical split
            # as everywhere else) -- fall back exactly like check 2 does.
            hist, _ = await client.list_historical_markets(tickers=ticker, limit=1)
            market = hist[0] if hist else market
        close_ts = _parse_iso_epoch(market.close_time) if market else None
        if close_ts is None:
            per_era[label] = {"ticker": ticker, "has_quote": False, "reason": "no close_time"}
            every_era_has_quote = False
            continue

        start_ts, end_ts = close_ts - 86_400, close_ts
        candles: list = []
        source = "live"
        event = await client.get_event(market.event_ticker) if market and market.event_ticker else None
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

        has_quote = any(c.has_quote for c in candles)
        per_era[label] = {
            "ticker": ticker, "candle_count": len(candles), "source": source, "has_quote": has_quote,
        }
        if has_quote:
            any_quote = True
        else:
            every_era_has_quote = False

    if every_era_has_quote and any_quote:
        status: CheckStatus = "PASS"
    elif any_quote:
        status = "PARTIAL"
    else:
        status = "FAIL"

    notes = (
        "Non-null yes_bid/yes_ask near each market's close_time, checked live then "
        "/historical/*. PARTIAL/FAIL eras document exactly where the spread<=20c filter "
        "(Phase 3) will need a substitute or a coverage caveat."
    )
    return CheckResult(5, "Quote availability for spread filter", status, {"eras": per_era}, notes)
