from __future__ import annotations

from kalshi_mt.r1.panel import basis_counts, build_doubled_panel, build_yes_only_panel, price_band
from kalshi_mt.store import db


def _seed_market_with_panel(conn, ticker, *, result="yes", category="Weather",
                             lookback_prices=None):
    lookback_prices = lookback_prices if lookback_prices is not None else {0: 0.9}
    db.upsert_market(conn, {
        "ticker": ticker, "result": result, "category": category,
        "close_time_epoch": 1000, "in_r1_window": 1,
    })
    for day, price in lookback_prices.items():
        db.upsert_price_panel_row(conn, {
            "ticker": ticker, "lookback_day": day, "trade_id": f"t{day}",
            "yes_price_dollars": price, "created_time": "2022-01-01T00:00:00Z", "source": "live",
        })
    conn.commit()


def test_build_yes_only_panel_basic(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market_with_panel(conn, "A-1", result="yes", lookback_prices={0: 0.9, 1: 0.85})
    df = build_yes_only_panel(conn, {"A-1"})
    assert len(df) == 2
    assert set(df["side"].to_list()) == {"yes"}
    assert set(df["y"].to_list()) == {1.0}


def test_build_yes_only_panel_no_outcome(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market_with_panel(conn, "A-1", result="no", lookback_prices={0: 0.1})
    df = build_yes_only_panel(conn, {"A-1"})
    row = df.row(0, named=True)
    assert row["y"] == 0.0
    assert row["p"] == 0.1


def test_build_yes_only_panel_excludes_unresolved_markets(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market_with_panel(conn, "PENDING", result="")
    df = build_yes_only_panel(conn, {"PENDING"})
    assert df.is_empty()


def test_build_yes_only_panel_excludes_out_of_scope_tickers(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market_with_panel(conn, "IN-SCOPE")
    _seed_market_with_panel(conn, "OUT-OF-SCOPE")
    df = build_yes_only_panel(conn, {"IN-SCOPE"})
    assert len(df) == 1
    assert df["ticker"][0] == "IN-SCOPE"


def test_build_yes_only_panel_empty_scope(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market_with_panel(conn, "A-1")
    df = build_yes_only_panel(conn, set())
    assert df.is_empty()


def test_build_doubled_panel_complements_price_and_outcome(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market_with_panel(conn, "A-1", result="yes", lookback_prices={0: 0.9})
    yes_only = build_yes_only_panel(conn, {"A-1"})
    doubled = build_doubled_panel(yes_only)
    assert len(doubled) == 2
    sides = {row["side"]: row for row in doubled.iter_rows(named=True)}
    assert sides["yes"]["p"] == 0.9
    assert sides["yes"]["y"] == 1.0
    assert abs(sides["no"]["p"] - 0.1) < 1e-9
    assert sides["no"]["y"] == 0.0


def test_build_doubled_panel_empty_input(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    df = build_yes_only_panel(conn, set())
    doubled = build_doubled_panel(df)
    assert doubled.is_empty()


def test_basis_counts_invariant_holds(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed_market_with_panel(conn, "A-1", lookback_prices={0: 0.9, 1: 0.5, 2: 0.4})
    yes_only = build_yes_only_panel(conn, {"A-1"})
    doubled = build_doubled_panel(yes_only)
    counts = basis_counts(yes_only, doubled)
    assert counts["yes_only_n"] == 3
    assert counts["doubled_n"] == 6
    assert counts["doubled_equals_2x_yes_only"] is True


def test_price_band_boundaries():
    assert price_band(0.01) == "1-10c"
    assert price_band(0.10) == "1-10c"
    assert price_band(0.11) == "11-20c"
    assert price_band(0.50) == "41-50c"
    assert price_band(0.51) == "51-60c"
    assert price_band(0.99) == "91-99c"
    assert price_band(0.91) == "91-99c"
