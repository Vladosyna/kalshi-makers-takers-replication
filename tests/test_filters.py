from __future__ import annotations

from kalshi_mt.r1.filters import MIN_OPEN_SECONDS, apply_and_log, apply_r1_filters, summarize
from kalshi_mt.store import db


def _seed(conn, ticker, *, volume_fp=2000.0, spread=0.05, open_epoch=0,
          close_epoch=2 * 86400, result="yes", day0_price=0.9, in_r1=1, with_quote=True):
    db.upsert_market(conn, {
        "ticker": ticker, "volume_fp": volume_fp, "open_time_epoch": open_epoch,
        "close_time_epoch": close_epoch, "result": result, "in_r1_window": in_r1,
    })
    if with_quote:
        db.upsert_quote(conn, {
            "ticker": ticker, "end_period_ts": close_epoch, "yes_bid_close": 0.45,
            "yes_ask_close": 0.45 + spread, "spread": spread, "source": "live",
        })
    if day0_price is not None:
        db.upsert_price_panel_row(conn, {
            "ticker": ticker, "lookback_day": 0, "trade_id": "t0",
            "yes_price_dollars": day0_price, "created_time": "2022-01-01T00:00:00Z", "source": "live",
        })
    conn.commit()


def test_market_passing_every_filter(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "OK-1")
    results = apply_r1_filters(conn)
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].reason_codes == []


def test_low_volume_fails(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "LOW-VOL", volume_fp=500.0)
    r = apply_r1_filters(conn)[0]
    assert r.passed is False
    assert "volume_below_1000" in r.reason_codes


def test_wide_spread_fails(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "WIDE", spread=0.25)
    r = apply_r1_filters(conn)[0]
    assert "spread_above_20c" in r.reason_codes


def test_no_quote_row_at_all_fails_as_not_yet_fetched(tmp_path):
    """No `quotes` row means Pass 1 hasn't attempted this ticker's quote
    fetch yet -- an operational gap, not a structural one."""
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "NOQUOTE", with_quote=False)
    r = apply_r1_filters(conn)[0]
    assert "spread_filter_not_yet_fetched" in r.reason_codes
    assert "spread_filter_not_computable" not in r.reason_codes


def test_attempted_quote_with_no_data_fails_as_not_computable(tmp_path):
    """A `quotes` row DOES exist (Pass 1 tried live+historical) but
    spread is null -- Kalshi genuinely has no bid/ask history here (Step
    Zero Check 5's own finding). Structural, not operational -- must not
    collapse into the same reason code as an unattempted ticker."""
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "NODATA", with_quote=False)
    db.upsert_quote(conn, {
        "ticker": "NODATA", "end_period_ts": None, "yes_bid_close": None,
        "yes_ask_close": None, "spread": None, "source": "historical",
    })
    conn.commit()
    r = apply_r1_filters(conn)[0]
    assert "spread_filter_not_computable" in r.reason_codes
    assert "spread_filter_not_yet_fetched" not in r.reason_codes


def test_short_open_duration_fails(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "SHORT", open_epoch=0, close_epoch=MIN_OPEN_SECONDS - 1)
    r = apply_r1_filters(conn)[0]
    assert "open_below_24h" in r.reason_codes


def test_exactly_24h_passes_the_duration_check(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "EXACT", open_epoch=0, close_epoch=MIN_OPEN_SECONDS)
    r = apply_r1_filters(conn)[0]
    assert "open_below_24h" not in r.reason_codes


def test_settlement_mismatch_detected(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    # last trade implied "yes" (price 0.9) but the market actually settled "no"
    _seed(conn, "MISMATCH", result="no", day0_price=0.9)
    r = apply_r1_filters(conn)[0]
    assert "settlement_last_trade_mismatch" in r.reason_codes


def test_settlement_agreement_not_flagged(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "AGREE", result="yes", day0_price=0.95)
    r = apply_r1_filters(conn)[0]
    assert "settlement_last_trade_mismatch" not in r.reason_codes


def test_missing_result_flagged_as_visible_exclusion(tmp_path):
    """A stale/unsynced `result` (Pass 1's live sweep can leave it NULL for
    older markets -- fetch/pass1.py's own docstring) must fail visibly here
    with 'result_missing_or_invalid', not silently pass this gate only to
    be invisibly dropped later by r1/panel.py's `WHERE result IN
    ('yes','no')` -- an unattributed shortfall against BDW's 156,986
    (2026-07-21 audit)."""
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "STALE", result=None)
    r = apply_r1_filters(conn)[0]
    assert r.passed is False
    assert "result_missing_or_invalid" in r.reason_codes
    assert "settlement_last_trade_mismatch" not in r.reason_codes


def test_no_price_panel_row_does_not_trigger_mismatch(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "NOPANEL", result="yes", day0_price=None)
    r = apply_r1_filters(conn)[0]
    assert "settlement_last_trade_mismatch" not in r.reason_codes


# ---------------------------------------------------------------------------
# dollar_volume_by_ticker mode -- the TRUE $1k notional gate (2026-07-21
# audit), replacing the contract-count volume_fp proxy when threaded in.
# ---------------------------------------------------------------------------

def test_dollar_volume_mode_passes_market_clearing_true_notional(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "REAL-VOL")
    db.upsert_pass2_progress(conn, {
        "ticker": "REAL-VOL", "status": "done", "cursor": None, "source": "historical", "trade_count": 1,
    })
    conn.commit()
    r = apply_r1_filters(conn, dollar_volume_by_ticker={"REAL-VOL": 5000.0})[0]
    assert r.passed is True
    assert "volume_below_1000" not in r.reason_codes


def test_dollar_volume_mode_fails_market_below_true_notional_despite_high_contract_count(tmp_path):
    """The whole point of the fix: a market with plenty of CONTRACTS but
    thin real DOLLAR notional (cheap price) must fail here, even though
    the old volume_fp proxy would have passed it."""
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "CHEAP-CONTRACTS", volume_fp=5000.0)  # would pass the OLD contract-count proxy
    db.upsert_pass2_progress(conn, {
        "ticker": "CHEAP-CONTRACTS", "status": "done", "cursor": None, "source": "historical", "trade_count": 1,
    })
    conn.commit()
    r = apply_r1_filters(conn, dollar_volume_by_ticker={"CHEAP-CONTRACTS": 100.0})[0]  # real notional: $100
    assert r.passed is False
    assert "volume_below_1000" in r.reason_codes


def test_dollar_volume_mode_pass2_not_done_fails_as_not_yet_fetched(tmp_path):
    """A market Pass 2 hasn't finished must fail 'dollar_volume_not_yet_fetched'
    (operational), never silently 'volume_below_1000' just because it's
    absent from the aggregate dict."""
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "NOT-DONE")
    conn.commit()  # no pass2_progress row at all
    r = apply_r1_filters(conn, dollar_volume_by_ticker={})[0]
    assert r.passed is False
    assert "dollar_volume_not_yet_fetched" in r.reason_codes
    assert "volume_below_1000" not in r.reason_codes


def test_dollar_volume_mode_none_falls_back_to_contract_count_proxy(tmp_path):
    """dollar_volume_by_ticker=None (the default) must behave exactly like
    the pre-2026-07-21 contract-count proxy -- a lightweight/preview mode
    for callers without Pass 2 data, not the production R1/R2 gate."""
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "LOW-VOL", volume_fp=500.0)
    r = apply_r1_filters(conn, dollar_volume_by_ticker=None)[0]
    assert r.passed is False
    assert "volume_below_1000" in r.reason_codes


def test_r2_window_markets_excluded_from_r1_filters(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "R2ONLY", in_r1=0)
    assert apply_r1_filters(conn) == []


def test_summarize_counts_reasons(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "OK-1")
    _seed(conn, "LOW-VOL", volume_fp=100.0)
    _seed(conn, "WIDE", spread=0.5)
    summary = summarize(apply_r1_filters(conn))
    assert summary["total"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 2
    assert summary["reason_counts"]["volume_below_1000"] == 1
    assert summary["reason_counts"]["spread_above_20c"] == 1


def test_apply_and_log_writes_universe_log(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "OK-1")
    _seed(conn, "LOW-VOL", volume_fp=100.0)
    summary = apply_and_log(conn)
    assert summary["passed"] == 1
    rows = conn.execute("SELECT ticker, reason_code FROM universe_log").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "LOW-VOL"
    assert rows[0][1] == "volume_below_1000"


def test_apply_and_log_multiple_reasons_write_multiple_rows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, "BAD", volume_fp=100.0, spread=0.9)
    apply_and_log(conn)
    rows = conn.execute("SELECT reason_code FROM universe_log WHERE ticker='BAD'").fetchall()
    assert {r[0] for r in rows} == {"volume_below_1000", "spread_above_20c"}
