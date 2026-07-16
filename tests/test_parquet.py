from __future__ import annotations

from kalshi_mt.store.parquet import TradeStore, month_str


def _trade(trade_id, ticker="ABC-1", created_time="2022-12-30T17:15:45Z", **kw):
    return {
        "trade_id": trade_id, "ticker": ticker, "count_fp": 1.0,
        "yes_price_dollars": 0.49, "no_price_dollars": 0.51,
        "taker_outcome_side": "no", "taker_book_side": "ask", "taker_side": "no",
        "created_time": created_time, "is_block_trade": False, "source": "historical",
        **kw,
    }


def test_month_str():
    assert month_str("2022-12-30T17:15:45Z") == "2022-12"
    assert month_str("2026-01-05T00:00:00Z") == "2026-01"


def test_append_and_read(tmp_path):
    store = TradeStore(tmp_path / "parquet")
    written = store.append([_trade("t1"), _trade("t2")])
    assert written == 2
    df = store.read_for_ticker("ABC-1")
    assert len(df) == 2
    assert set(df["trade_id"]) == {"t1", "t2"}


def test_append_dedups_on_trade_id(tmp_path):
    store = TradeStore(tmp_path / "parquet")
    store.append([_trade("t1")])
    written_again = store.append([_trade("t1"), _trade("t2")])
    assert written_again == 1  # t1 already present, only t2 is new
    df = store.read_for_ticker("ABC-1")
    assert len(df) == 2


def test_append_partitions_by_month(tmp_path):
    store = TradeStore(tmp_path / "parquet")
    store.append([
        _trade("t1", created_time="2022-12-30T17:15:45Z"),
        _trade("t2", created_time="2023-01-02T00:00:00Z"),
    ])
    assert set(store.months_on_disk()) == {"2022-12", "2023-01"}


def test_read_for_ticker_filters_correctly(tmp_path):
    store = TradeStore(tmp_path / "parquet")
    store.append([_trade("t1", ticker="ABC-1"), _trade("t2", ticker="XYZ-1")])
    df = store.read_for_ticker("ABC-1")
    assert len(df) == 1
    assert df["ticker"][0] == "ABC-1"


def test_append_empty_list_is_noop(tmp_path):
    store = TradeStore(tmp_path / "parquet")
    assert store.append([]) == 0
    assert store.months_on_disk() == []


def test_read_range_missing_month_is_empty(tmp_path):
    store = TradeStore(tmp_path / "parquet")
    df = store.read_range(["2099-01"])
    assert df.is_empty()
