"""Kalshi client model validation and pagination behavior.

Async code driven via asyncio.run() from plain `def test_*` functions --
pytest-asyncio is not a project dependency.
"""

from __future__ import annotations

import asyncio

import httpx

from kalshi_mt.api.http import TokenBucket
from kalshi_mt.api.kalshi import KalshiCandlestick, KalshiClient, KalshiMarket, KalshiTrade


def test_kalshi_market_parses_dollar_and_fp_strings():
    m = KalshiMarket.model_validate(
        {
            "ticker": "KXTEST-26JAN01-T1",
            "event_ticker": "KXTEST-26JAN01",
            "status": "settled",
            "result": "yes",
            "close_time": "2026-01-01T00:00:00Z",
            "settlement_ts": "2026-01-01T01:00:00Z",
            "yes_bid_dollars": "0.4500",
            "yes_ask_dollars": "0.4700",
            "last_price_dollars": "0.4600",
            "volume_fp": "8615.00",
            "open_interest_fp": "120.00",
        }
    )
    assert m.ticker == "KXTEST-26JAN01-T1"
    assert m.yes_bid_dollars == 0.45
    assert m.yes_ask_dollars == 0.47
    assert m.last_price_dollars == 0.46
    assert m.volume_fp == 8615.0
    assert m.open_interest_fp == 120.0
    assert m.result == "yes"


def test_kalshi_market_missing_fields_default_to_none():
    m = KalshiMarket.model_validate({"ticker": "KXTEST-26JAN01-T1"})
    assert m.ticker == "KXTEST-26JAN01-T1"
    assert m.close_time is None
    assert m.yes_bid_dollars is None
    assert m.volume_fp is None


def test_kalshi_market_unknown_fields_ignored():
    m = KalshiMarket.model_validate(
        {"ticker": "KXTEST-26JAN01-T1", "some_future_field_not_yet_modeled": 42}
    )
    assert m.ticker == "KXTEST-26JAN01-T1"


def test_kalshi_trade_parses_taker_fields():
    t = KalshiTrade.model_validate(
        {
            "trade_id": "abc123",
            "ticker": "KXTEST-26JAN01-T1",
            "count_fp": "5.00",
            "yes_price_dollars": "0.4600",
            "no_price_dollars": "0.5400",
            "taker_outcome_side": "yes",
            "taker_book_side": "bid",
            "taker_side": "yes",
            "created_time": "2026-01-01T00:30:00Z",
            "is_block_trade": False,
        }
    )
    assert t.trade_id == "abc123"
    assert t.count_fp == 5.0
    assert t.yes_price_dollars == 0.46
    assert t.taker_outcome_side == "yes"
    assert t.taker_book_side == "bid"
    assert t.taker_side == "yes"
    assert t.is_block_trade is False


def test_kalshi_trade_missing_new_fields_still_has_legacy():
    """Simulates a pre-migration trade row: only the deprecated field populated."""
    t = KalshiTrade.model_validate(
        {
            "trade_id": "old1",
            "ticker": "KXTEST-21JAN01-T1",
            "taker_side": "no",
            "created_time": "2021-01-01T00:00:00Z",
        }
    )
    assert t.taker_outcome_side is None
    assert t.taker_book_side is None
    assert t.taker_side == "no"


def test_candlestick_has_quote_property():
    with_quote = KalshiCandlestick.model_validate(
        {
            "end_period_ts": 1700000000,
            "yes_bid": {"close_dollars": "0.45"},
            "yes_ask": {"close_dollars": "0.47"},
        }
    )
    assert with_quote.has_quote is True

    without_quote = KalshiCandlestick.model_validate({"end_period_ts": 1700000000})
    assert without_quote.has_quote is False

    one_sided = KalshiCandlestick.model_validate(
        {"end_period_ts": 1700000000, "yes_bid": {"close_dollars": "0.45"}}
    )
    assert one_sided.has_quote is False


def test_candlestick_accepts_historical_bare_field_names():
    """GetHistoricalMarketCandlesticks sends bare "close"/"open"/etc, not the
    live endpoint's "close_dollars"/"open_dollars" -- verified against a real
    response. Both must parse into the same model."""
    historical_shape = KalshiCandlestick.model_validate(
        {
            "end_period_ts": 1672329600,
            "yes_bid": {"close": "0.0000", "high": "0.0200", "low": "0.0000", "open": "0.0100"},
            "yes_ask": {"close": "1.0000", "high": "1.0000", "low": "0.0400", "open": "1.0000"},
            "volume": "0.00",
            "open_interest": "0.00",
        }
    )
    assert historical_shape.has_quote is True
    assert historical_shape.yes_bid.close_dollars == 0.0
    assert historical_shape.yes_ask.close_dollars == 1.0
    assert historical_shape.volume_fp == 0.0
    assert historical_shape.open_interest_fp == 0.0


def test_list_markets_cursor_pagination_two_pages():
    seen_params: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen_params.append(params)
        if "cursor" not in params:
            return httpx.Response(
                200,
                json={
                    "markets": [{"ticker": "KXTEST-A"}, {"ticker": "KXTEST-B"}],
                    "cursor": "page2token",
                },
            )
        assert params["cursor"] == "page2token"
        return httpx.Response(200, json={"markets": [{"ticker": "KXTEST-C"}], "cursor": ""})

    async def _run():
        bucket = TokenBucket(rate=1000.0, burst=1000)
        client = KalshiClient(bucket, base_url="https://example.test")
        client._client = httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        )

        page1, cursor1 = await client.list_markets(limit=100)
        assert [m.ticker for m in page1] == ["KXTEST-A", "KXTEST-B"]
        assert cursor1 == "page2token"

        page2, cursor2 = await client.list_markets(limit=100, cursor=cursor1)
        assert [m.ticker for m in page2] == ["KXTEST-C"]
        assert cursor2 is None  # empty-string cursor normalizes to None (exhausted)

        await client.aclose()

    asyncio.run(_run())
    assert "cursor" not in seen_params[0]
    assert seen_params[1]["cursor"] == "page2token"


def test_get_trades_unparseable_item_is_skipped_not_fatal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "trades": [
                    {"trade_id": "ok1", "ticker": "KXTEST-A"},
                    {"ticker": "missing-trade-id-should-be-skipped"},
                ],
                "cursor": "",
            },
        )

    async def _run():
        bucket = TokenBucket(rate=1000.0, burst=1000)
        client = KalshiClient(bucket, base_url="https://example.test")
        client._client = httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        )
        trades, cursor = await client.get_trades(ticker="KXTEST-A")
        assert [t.trade_id for t in trades] == ["ok1"]
        assert cursor is None
        await client.aclose()

    asyncio.run(_run())
