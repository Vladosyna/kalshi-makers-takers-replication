# Step Zero findings -- 2026-07-16T10:33:22+00:00

- Base URL: `https://external-api.kalshi.com/trade-api/v2`
- Verdict: **GO**

## Check 1: Unauthenticated access -- PASS

Both /markets and /trades respond without any auth header.

```json
{
  "markets_returned": 5,
  "trades_returned": 5
}
```

## Check 2: 2021-2022 positive-check -- PARTIAL

All 5 sample markets yielded real 2021-2022 metadata and trade history, via the /historical/* fallback for at least one.

```json
{
  "discovery": {
    "historical_cutoff": {
      "market_settled_ts": "2026-05-17T00:00:00Z",
      "trades_created_ts": "2026-05-17T00:00:00Z"
    },
    "live_endpoint_candidate_count": 0,
    "series_tried": [
      "KXHIGHNY"
    ],
    "series_pages_fetched": {
      "KXHIGHNY": 9
    },
    "historical_fallback_candidate_count": 1447,
    "fallback_used": true,
    "candidate_tickers": [
      "HIGHNY-22DEC30-T57",
      "HIGHNY-22DEC30-B56.5",
      "HIGHNY-22DEC30-B54.5",
      "HIGHNY-22DEC30-B52.5",
      "HIGHNY-22DEC30-B50.5"
    ]
  },
  "markets": [
    {
      "ticker": "HIGHNY-22DEC30-T57",
      "close_time": "2022-12-31T04:59:00Z",
      "volume_fp": 3847.0,
      "metadata_source": "historical",
      "metadata_close_time_in_window": true,
      "trade_count": 152,
      "trades_source": "historical",
      "volume_consistent": true,
      "ok": true
    },
    {
      "ticker": "HIGHNY-22DEC30-B56.5",
      "close_time": "2022-12-31T04:59:00Z",
      "volume_fp": 2628.0,
      "metadata_source": "historical",
      "metadata_close_time_in_window": true,
      "trade_count": 107,
      "trades_source": "historical",
      "volume_consistent": true,
      "ok": true
    },
    {
      "ticker": "HIGHNY-22DEC30-B54.5",
      "close_time": "2022-12-31T04:59:00Z",
      "volume_fp": 3162.0,
      "metadata_source": "historical",
      "metadata_close_time_in_window": true,
      "trade_count": 79,
      "trades_source": "historical",
      "volume_consistent": true,
      "ok": true
    },
    {
      "ticker": "HIGHNY-22DEC30-B52.5",
      "close_time": "2022-12-31T04:59:00Z",
      "volume_fp": 2393.0,
      "metadata_source": "historical",
      "metadata_close_time_in_window": true,
      "trade_count": 53,
      "trades_source": "historical",
      "volume_consistent": true,
      "ok": true
    },
    {
      "ticker": "HIGHNY-22DEC30-B50.5",
      "close_time": "2022-12-31T04:59:00Z",
      "volume_fp": 1152.0,
      "metadata_source": "historical",
      "metadata_close_time_in_window": true,
      "trade_count": 11,
      "trades_source": "historical",
      "volume_consistent": true,
      "ok": true
    }
  ]
}
```

## Check 3: Taker-field population -- PASS

taker_outcome_side/taker_book_side population by era, with legacy taker_side tracked as a comparison point. Low population in an era is a documented degradation for Phase 6's fee/return work, not a failure -- taker_side remains live and populated per Kalshi's own deprecation notice.

```json
{
  "eras": {
    "2021-2022": {
      "ticker": "HIGHNY-22DEC30-T57",
      "trade_count": 152,
      "source": "historical",
      "taker_outcome_side_population": 1.0,
      "taker_book_side_population": 1.0,
      "taker_side_legacy_population": 1.0
    },
    "2023": {
      "ticker": "USDJPY-23DEC2510-T143.749",
      "trade_count": 0,
      "source": "none"
    },
    "2024": {
      "ticker": "KXWTIW-24DEC27-T74.99",
      "trade_count": 0,
      "source": "none"
    },
    "2025-jan-apr": {
      "ticker": "KXWTIW-25APR25-T70.99",
      "trade_count": 0,
      "source": "none"
    }
  }
}
```

## Check 4: /trades timestamp bracketing -- PASS

min_ts/max_ts returns a strictly bounded subset.

```json
{
  "ticker": "HIGHNY-22DEC30-T57",
  "full_count": 152,
  "bracketed_count": 77,
  "range": [
    1672358682,
    1672451480
  ],
  "bracket": [
    1672389614,
    1672420547
  ]
}
```

## Check 5: Quote availability for spread filter -- PARTIAL

Non-null yes_bid/yes_ask near each market's close_time, checked live then /historical/*. PARTIAL/FAIL eras document exactly where the spread<=20c filter (Phase 3) will need a substitute or a coverage caveat.

```json
{
  "eras": {
    "2021-2022": {
      "ticker": "HIGHNY-22DEC30-T57",
      "candle_count": 9,
      "source": "historical",
      "has_quote": true
    },
    "2023": {
      "ticker": "USDJPY-23DEC2510-T143.749",
      "candle_count": 0,
      "source": "historical",
      "has_quote": false
    },
    "2024": {
      "ticker": "KXWTIW-24DEC27-T74.99",
      "candle_count": 0,
      "source": "historical",
      "has_quote": false
    },
    "2025-jan-apr": {
      "ticker": "KXWTIW-25APR25-T70.99",
      "candle_count": 0,
      "source": "historical",
      "has_quote": false
    }
  }
}
```

## Placeholder inventory resolved

| Spec `[VERIFY]` item | Check | Status |
|---|---|---|
| Unauthenticated access to required Kalshi endpoints | Check 1 | PASS |
| 2021-2022 historical trade-tape depth (positive check, not inferred from a 200 response) | Check 2 | PARTIAL |
| taker_outcome_side/taker_book_side population vs legacy taker_side | Check 3 | PASS |
| /trades min_ts/max_ts timestamp bracketing support | Check 4 | PASS |
| Historical bid/ask (yes_bid/yes_ask) availability for the spread<=20c filter | Check 5 | PARTIAL |
