from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from kalshi_mt.store import db


def _epoch(y, m=1, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def _seed_r1_market(conn, ticker, category, close_epoch, price, result):
    db.upsert_market(conn, {
        "ticker": ticker, "event_ticker": f"{ticker}-EVT", "category": category,
        "result": result, "close_time_epoch": close_epoch, "in_r1_window": 1,
    })
    db.upsert_price_panel_row(conn, {
        "ticker": ticker, "lookback_day": 0, "trade_id": f"{ticker}-t0",
        "yes_price_dollars": price, "created_time": "2024-01-01T00:00:00Z", "source": "live",
    })
    conn.commit()


def _seed_r2_market(conn, ticker, category, close_epoch, price, result):
    """Passes apply_r1_filters(window='r2'): volume>=1000, spread<=0.2,
    open>=24h, and day0_price/result must agree (no settlement mismatch)."""
    db.upsert_market(conn, {
        "ticker": ticker, "event_ticker": f"{ticker}-EVT", "category": category,
        "result": result, "volume_fp": 5000.0,
        "open_time_epoch": close_epoch - 2 * 86400, "close_time_epoch": close_epoch,
        "in_r2_window": 1,
    })
    db.upsert_quote(conn, {
        "ticker": ticker, "end_period_ts": close_epoch, "yes_bid_close": price - 0.02,
        "yes_ask_close": price + 0.02, "spread": 0.04, "source": "live",
    })
    db.upsert_price_panel_row(conn, {
        "ticker": ticker, "lookback_day": 0, "trade_id": f"{ticker}-t0",
        "yes_price_dollars": price, "created_time": "2025-06-01T00:00:00Z", "source": "live",
    })
    conn.commit()


def _seed_full_fixture(conn):
    # R1: 10 Weather markets, well-spread prices, mixed outcomes -- enough
    # n and price variance for fit_mz_regression to produce a real psi.
    for i in range(10):
        p = 0.1 + 0.08 * i  # 0.10 .. 0.82
        result = "yes" if p >= 0.5 else "no"
        _seed_r1_market(conn, f"R1-{i}", "Weather", _epoch(2024, 6, 1) + i, p, result)

    # R2: 10 Weather markets, closing after the fee boundary (2025-05-01),
    # straddling the publication boundary (2025-09-08) -- 5 before, 5 after.
    for i in range(5):
        p = 0.55 + 0.08 * i  # 0.55 .. 0.87 -- result "yes"
        _seed_r2_market(conn, f"R2-preP-{i}", "Weather", _epoch(2025, 6, 1) + i, p, "yes")
    for i in range(5):
        p = 0.10 + 0.08 * i  # 0.10 .. 0.42 -- result "no"
        _seed_r2_market(conn, f"R2-postP-{i}", "Weather", _epoch(2025, 10, 1) + i, p, "no")


def _write_frozen_mix(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "computed_ts": "2026-01-01T00:00:00Z", "basis": "yes_only_contract_count",
        "source_window": "calendar_2024", "weights": {"Weather": 1.0},
    }), encoding="utf-8")


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from kalshi_mt import cli, util

    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)

    def _fake_load_config():
        return {
            "storage": {
                "db_path": str(db_path), "parquet_dir": str(tmp_path / "parquet"),
                "logs_dir": str(tmp_path / "logs"),
            },
            "logging": {"level": "INFO"},
        }

    monkeypatch.setattr(cli, "load_config", _fake_load_config)
    monkeypatch.setattr(util, "PROJECT_ROOT", tmp_path)
    return conn, tmp_path


def test_r2_cli_refuses_without_frozen_mix(cli_env):
    from kalshi_mt import cli

    conn, tmp_path = cli_env
    conn.close()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["r2"])
    assert result.exit_code == 1
    assert "frozen_2024_mix" in result.output or "is missing" in result.output


def test_r2_cli_end_to_end_produces_verdict(cli_env):
    from kalshi_mt import cli

    conn, tmp_path = cli_env
    _seed_full_fixture(conn)
    conn.close()
    _write_frozen_mix(tmp_path / "data" / "frozen_2024_mix.json")

    runner = CliRunner()
    result = runner.invoke(cli.app, ["r2"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["psi_bar_r1"] is not None
    assert "Weather" in payload["categories_fit"]
    assert payload["pooled_panel_n"] == payload["r1_panel_n"] + payload["r2_panel_n"]

    for boundary in ("fee", "publication"):
        assert payload["delta_bar"][boundary] is not None
        assert set(payload["delta_bar"][boundary].keys()) == {"delta_bar", "ci_lo", "ci_hi"}
        assert payload["verdict"][boundary] in (
            "persisted", "attenuated", "vanished", "reversed", "indeterminate",
        )
        assert "within" in payload["decomposition"][boundary]
        assert "between" in payload["decomposition"][boundary]

    assert "by_bucket" in payload["horizon_robustness"]
    assert "close_only" in payload["horizon_robustness"]
    assert payload["r2_filters"]["passed"] == 10  # all 10 seeded R2 markets pass the proxy filters

    # The locked verdict artifact -- r3/firewall.py's own dependency --
    # must actually land on disk, not just print to stdout.
    lock_path = tmp_path / "reports" / "r2" / "verdict_lock.json"
    assert lock_path.exists()
    assert payload["locked_artifact"] == str(lock_path)
    assert "locked_ts" in payload
    from kalshi_mt.r2.report import load_r2_report
    assert load_r2_report(lock_path)["verdict"] == payload["verdict"]


def test_r2_cli_r2_filters_exclude_a_bad_market(cli_env):
    """A market failing the R2 proxy filter (thin volume) must not reach
    the regression -- confirms apply_and_log(window='r2') is actually
    wired into the CLI, not just present in filters.py."""
    from kalshi_mt import cli

    conn, tmp_path = cli_env
    _seed_full_fixture(conn)
    db.upsert_market(conn, {
        "ticker": "R2-thin", "event_ticker": "R2-thin-EVT", "category": "Weather",
        "result": "yes", "volume_fp": 10.0,  # below MIN_VOLUME_FP
        "open_time_epoch": _epoch(2025, 6, 20) - 2 * 86400, "close_time_epoch": _epoch(2025, 6, 20),
        "in_r2_window": 1,
    })
    db.upsert_quote(conn, {
        "ticker": "R2-thin", "end_period_ts": _epoch(2025, 6, 20), "yes_bid_close": 0.58,
        "yes_ask_close": 0.62, "spread": 0.04, "source": "live",
    })
    conn.commit()
    conn.close()
    _write_frozen_mix(tmp_path / "data" / "frozen_2024_mix.json")

    runner = CliRunner()
    result = runner.invoke(cli.app, ["r2"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["r2_filters"]["failed"] == 1
    assert payload["r2_panel_n"] == 10  # R2-thin excluded, only the 10 good markets remain
