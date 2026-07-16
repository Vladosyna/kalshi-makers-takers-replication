"""Kalshi public market-data client (read-only, no account/auth).

Every endpoint used here shows `security: []` in Kalshi's OpenAPI spec
(confirmed live against docs.kalshi.com during Phase 1 implementation).
Only the fields this repo consumes are modeled -- `extra = "ignore"`
everywhere so upstream schema additions don't break parsing.

Field-naming note (confirmed live, not the older cents-integer API shape):
price/fee fields carry a `_dollars` suffix and arrive as strings (e.g.
"0.0460"); count fields carry a `_fp` suffix (fixed-point counts, also
string-encoded).

Two parallel endpoint families exist and both matter to this replication:
  - live:       /markets, /trades, /series/{s}/markets/{t}/candlesticks
  - historical: /historical/markets, /historical/trades,
                /historical/markets/{t}/candlesticks
`GET /historical/cutoff` reports the exact timestamps at which data moves
from the live family to the historical one -- Step Zero queries it directly
rather than inferring the boundary by trial and error.

Important asymmetry discovered while building this client: unlike the live
`/markets` endpoint, `/historical/markets` does NOT accept close/settled
timestamp range filters (`min_close_ts` etc.) -- only `tickers`,
`event_ticker`, `series_ticker`, `cursor`, `limit`. Any code that wants to
*discover* old tickers by date range must do it against the live endpoint
(which still accepts the range filters even for pre-cutoff dates) or by
listing series and paging a specific series_ticker's historical markets,
never by asking /historical/markets for a raw date-range scan.

`series_ticker` is not a field on the Market object itself -- it is only
`event_ticker`. Resolving a market's series (needed for the live
candlesticks endpoint's path) requires a separate `GET /events/{event_ticker}`
call. The historical candlesticks endpoint sidesteps this: its path is
`/historical/markets/{ticker}/candlesticks`, no series_ticker needed.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, field_validator

from kalshi_mt.api.http import BaseClient, TokenBucket

log = logging.getLogger(__name__)

KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class KalshiMarket(BaseModel):
    """Subset of a Kalshi Market object (GetMarkets / GetHistoricalMarkets)."""

    ticker: str
    event_ticker: str | None = None
    market_type: str | None = None
    status: str | None = None
    result: str | None = None
    open_time: str | None = None
    close_time: str | None = None
    latest_expiration_time: str | None = None
    settlement_ts: str | None = None
    settlement_value_dollars: float | None = None
    yes_bid_dollars: float | None = None
    yes_ask_dollars: float | None = None
    no_bid_dollars: float | None = None
    no_ask_dollars: float | None = None
    last_price_dollars: float | None = None
    volume_fp: float | None = None
    volume_24h_fp: float | None = None
    open_interest_fp: float | None = None
    rules_primary: str | None = None

    model_config = {"extra": "ignore"}

    _norm = field_validator(
        "settlement_value_dollars",
        "yes_bid_dollars",
        "yes_ask_dollars",
        "no_bid_dollars",
        "no_ask_dollars",
        "last_price_dollars",
        "volume_fp",
        "volume_24h_fp",
        "open_interest_fp",
        mode="before",
    )(_coerce_float)


class KalshiEvent(BaseModel):
    """Subset of a Kalshi Event object (GetEvent) -- the only source of
    series_ticker for a given market's event_ticker."""

    event_ticker: str
    series_ticker: str | None = None
    title: str | None = None
    sub_title: str | None = None

    model_config = {"extra": "ignore"}


class KalshiTrade(BaseModel):
    """A Kalshi Trade object (GetTrades / GetHistoricalTrades -- identical shape)."""

    trade_id: str
    ticker: str
    count_fp: float | None = None
    yes_price_dollars: float | None = None
    no_price_dollars: float | None = None
    taker_outcome_side: str | None = None  # "yes" | "no" -- canonical field
    taker_book_side: str | None = None  # "bid" | "ask" -- canonical field
    taker_side: str | None = None  # deprecated legacy field, still populated
    created_time: str | None = None
    is_block_trade: bool | None = None

    model_config = {"extra": "ignore"}

    _norm = field_validator("count_fp", "yes_price_dollars", "no_price_dollars", mode="before")(
        _coerce_float
    )


class _OhlcDollars(BaseModel):
    # NOTE: verified against real API responses -- GetMarketCandlesticks
    # (live) and GetHistoricalMarketCandlesticks use DIFFERENT field names
    # for the identical concept: live sends "close_dollars"/"open_dollars"/
    # etc, historical sends bare "close"/"open"/etc. AliasChoices accepts
    # either so one model correctly parses both endpoint families.
    open_dollars: float | None = Field(default=None, validation_alias=AliasChoices("open_dollars", "open"))
    low_dollars: float | None = Field(default=None, validation_alias=AliasChoices("low_dollars", "low"))
    high_dollars: float | None = Field(default=None, validation_alias=AliasChoices("high_dollars", "high"))
    close_dollars: float | None = Field(default=None, validation_alias=AliasChoices("close_dollars", "close"))

    model_config = {"extra": "ignore", "populate_by_name": True}

    _norm = field_validator(
        "open_dollars", "low_dollars", "high_dollars", "close_dollars", mode="before"
    )(_coerce_float)


class _PriceDollars(_OhlcDollars):
    mean_dollars: float | None = Field(default=None, validation_alias=AliasChoices("mean_dollars", "mean"))
    previous_dollars: float | None = Field(
        default=None, validation_alias=AliasChoices("previous_dollars", "previous")
    )
    min_dollars: float | None = Field(default=None, validation_alias=AliasChoices("min_dollars", "min"))
    max_dollars: float | None = Field(default=None, validation_alias=AliasChoices("max_dollars", "max"))

    _norm2 = field_validator(
        "mean_dollars", "previous_dollars", "min_dollars", "max_dollars", mode="before"
    )(_coerce_float)


class KalshiCandlestick(BaseModel):
    """A Kalshi Candlestick object (GetMarketCandlesticks /
    GetHistoricalMarketCandlesticks -- see _OhlcDollars docstring: field
    names differ between the two endpoint families, handled via aliases)."""

    end_period_ts: int | None = None
    yes_bid: _OhlcDollars | None = None
    yes_ask: _OhlcDollars | None = None
    price: _PriceDollars | None = None
    volume_fp: float | None = Field(default=None, validation_alias=AliasChoices("volume_fp", "volume"))
    open_interest_fp: float | None = Field(
        default=None, validation_alias=AliasChoices("open_interest_fp", "open_interest")
    )

    model_config = {"extra": "ignore", "populate_by_name": True}

    _norm = field_validator("volume_fp", "open_interest_fp", mode="before")(_coerce_float)

    @property
    def has_quote(self) -> bool:
        """True if this candle carries a genuine two-sided bid/ask quote."""
        return bool(
            self.yes_bid
            and self.yes_ask
            and self.yes_bid.close_dollars is not None
            and self.yes_ask.close_dollars is not None
        )


class KalshiSeries(BaseModel):
    ticker: str
    title: str | None = None
    category: str | None = None
    frequency: str | None = None
    fee_type: str | None = None
    volume_fp: float | None = None

    model_config = {"extra": "ignore"}

    _norm = field_validator("volume_fp", mode="before")(_coerce_float)


class HistoricalCutoff(BaseModel):
    """GET /historical/cutoff -- the exact boundary at which data moves from
    the live endpoint family to the /historical/* family."""

    market_settled_ts: str | None = None
    trades_created_ts: str | None = None
    orders_updated_ts: str | None = None

    model_config = {"extra": "ignore"}


class KalshiClient(BaseClient):
    def __init__(self, bucket: TokenBucket, base_url: str = KALSHI_BASE_URL) -> None:
        super().__init__(base_url, bucket)

    # -- live: markets ----------------------------------------------------

    async def list_markets(
        self,
        status: str | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        min_settled_ts: int | None = None,
        max_settled_ts: int | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[KalshiMarket], str | None]:
        params: dict[str, Any] = {"limit": limit}
        for key, val in (
            ("status", status),
            ("min_close_ts", min_close_ts),
            ("max_close_ts", max_close_ts),
            ("min_settled_ts", min_settled_ts),
            ("max_settled_ts", max_settled_ts),
            ("series_ticker", series_ticker),
            ("event_ticker", event_ticker),
            ("cursor", cursor),
        ):
            if val is not None:
                params[key] = val
        raw = await self.get_json("/markets", params=params)
        markets = _parse_list(raw, "markets", KalshiMarket)
        return markets, (raw.get("cursor") if isinstance(raw, dict) else None) or None

    async def get_market(self, ticker: str) -> KalshiMarket | None:
        try:
            raw = await self.get_json(f"/markets/{ticker}")
        except Exception:
            log.warning("kalshi: get_market failed", extra={"ctx": {"ticker": ticker}})
            return None
        item = raw.get("market") if isinstance(raw, dict) else None
        return _parse_one(item, KalshiMarket)

    async def get_event(self, event_ticker: str) -> KalshiEvent | None:
        try:
            raw = await self.get_json(f"/events/{event_ticker}")
        except Exception:
            log.warning("kalshi: get_event failed", extra={"ctx": {"event_ticker": event_ticker}})
            return None
        item = raw.get("event") if isinstance(raw, dict) else None
        return _parse_one(item, KalshiEvent)

    # -- historical: markets ------------------------------------------------
    # NOTE: no close/settled timestamp filters here -- /historical/markets
    # only accepts tickers/event_ticker/series_ticker/cursor/limit. Discover
    # candidate old tickers via the live /markets range filters or by
    # paging a known series_ticker, then use this for the canonical record.

    async def list_historical_markets(
        self,
        tickers: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[KalshiMarket], str | None]:
        params: dict[str, Any] = {"limit": limit}
        for key, val in (
            ("tickers", tickers),
            ("event_ticker", event_ticker),
            ("series_ticker", series_ticker),
            ("cursor", cursor),
        ):
            if val is not None:
                params[key] = val
        raw = await self.get_json("/historical/markets", params=params)
        markets = _parse_list(raw, "markets", KalshiMarket)
        return markets, (raw.get("cursor") if isinstance(raw, dict) else None) or None

    async def get_historical_cutoff(self) -> HistoricalCutoff | None:
        try:
            raw = await self.get_json("/historical/cutoff")
        except Exception:
            log.warning("kalshi: get_historical_cutoff failed")
            return None
        return _parse_one(raw, HistoricalCutoff)

    # -- live + historical: trades -----------------------------------------

    async def get_trades(
        self,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[KalshiTrade], str | None]:
        # NOTE: verified against the raw OpenAPI yaml embedded in Kalshi's own
        # docs (docs.kalshi.com/api-reference/market/get-trades.md) -- the
        # real path is /markets/trades, not /trades. An earlier AI-summarized
        # fetch of the same page reported "/trades" and was wrong; this cost
        # a live Step Zero run five checks' worth of 404s before the raw
        # OpenAPI source was consulted directly. Trust the schema, not the
        # summary.
        return await self._get_trades("/markets/trades", ticker, min_ts, max_ts, cursor, limit)

    async def get_historical_trades(
        self,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[KalshiTrade], str | None]:
        return await self._get_trades(
            "/historical/trades", ticker, min_ts, max_ts, cursor, limit
        )

    async def _get_trades(
        self,
        path: str,
        ticker: str | None,
        min_ts: int | None,
        max_ts: int | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[KalshiTrade], str | None]:
        params: dict[str, Any] = {"limit": limit}
        for key, val in (
            ("ticker", ticker),
            ("min_ts", min_ts),
            ("max_ts", max_ts),
            ("cursor", cursor),
        ):
            if val is not None:
                params[key] = val
        raw = await self.get_json(path, params=params)
        trades = _parse_list(raw, "trades", KalshiTrade)
        return trades, (raw.get("cursor") if isinstance(raw, dict) else None) or None

    # -- live + historical: candlesticks -------------------------------------

    async def get_candlesticks(
        self,
        series_ticker: str,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1440,
    ) -> list[KalshiCandlestick]:
        raw = await self.get_json(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        return _parse_list(raw, "candlesticks", KalshiCandlestick)

    async def get_historical_candlesticks(
        self,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1440,
    ) -> list[KalshiCandlestick]:
        raw = await self.get_json(
            f"/historical/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        return _parse_list(raw, "candlesticks", KalshiCandlestick)

    # -- live: series ---------------------------------------------------------

    async def list_series(
        self, category: str | None = None, limit: int = 200
    ) -> list[KalshiSeries]:
        # NOTE: verified against the raw OpenAPI yaml -- GET /series takes NO
        # limit param at all (only category/tags/include_product_metadata/
        # include_volume/min_updated_ts) and always returns its full,
        # unbounded result set (~12k series in production). `limit` here is
        # therefore a CLIENT-SIDE truncation of the response, not a request
        # param -- sending a fabricated "limit" query param would just be
        # silently ignored by the server, which is worse than not sending it.
        params: dict[str, Any] = {}
        if category is not None:
            params["category"] = category
        raw = await self.get_json("/series", params=params or None)
        series = _parse_list(raw, "series", KalshiSeries)
        return series[:limit]


def _parse_list(raw: Any, key: str, model: type[BaseModel]) -> list[Any]:
    items = raw.get(key, []) if isinstance(raw, dict) else []
    parsed = []
    for item in items:
        try:
            parsed.append(model.model_validate(item))
        except Exception:
            log.warning("kalshi: unparseable %s item", key, extra={"ctx": {"item": item}})
            continue
    return parsed


def _parse_one(item: Any, model: type[BaseModel]) -> Any:
    if not item:
        return None
    try:
        return model.model_validate(item)
    except Exception:
        log.warning("kalshi: unparseable item for %s", model.__name__)
        return None
