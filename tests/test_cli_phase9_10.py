from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from kalshi_mt.store import db
from kalshi_mt.store.parquet import TradeStore
from tests.test_cli_r2 import _epoch, _seed_full_fixture, _write_frozen_mix, cli_env  # noqa: F401


def _seed_trades(trade_store: TradeStore, ticker: str, created_time: str) -> None:
    """One maker-side and one taker-side observation at yes_price=0.6 --
    both sides of the >=0.5 band get at least one real, distinct
    (maker, taker) pair so compute_maker_margin_ge_50c's layer_a/b/c are
    real floats, not None."""
    rows = [
        {
            "trade_id": f"{ticker}-trade-yes", "ticker": ticker, "count_fp": 10.0,
            "yes_price_dollars": 0.6, "no_price_dollars": 0.4,
            "taker_outcome_side": "yes", "taker_book_side": "yes", "taker_side": "yes",
            "created_time": created_time, "is_block_trade": False, "source": "live",
        },
        {
            "trade_id": f"{ticker}-trade-no", "ticker": ticker, "count_fp": 10.0,
            "yes_price_dollars": 0.6, "no_price_dollars": 0.4,
            "taker_outcome_side": "no", "taker_book_side": "no", "taker_side": "no",
            "created_time": created_time, "is_block_trade": False, "source": "live",
        },
    ]
    trade_store.append(rows)


def _seed_trades_for_all_r2_markets(tmp_path):
    trade_store = TradeStore(tmp_path / "parquet")
    for i in range(5):
        _seed_trades(trade_store, f"R2-preP-{i}", "2025-06-01T00:00:00Z")
    for i in range(5):
        _seed_trades(trade_store, f"R2-postP-{i}", "2025-10-01T00:00:00Z")


def _prepare_locked_r2(cli_env):
    from kalshi_mt import cli

    conn, tmp_path = cli_env
    _seed_full_fixture(conn)
    conn.close()
    _write_frozen_mix(tmp_path / "data" / "frozen_2024_mix.json")
    _seed_trades_for_all_r2_markets(tmp_path)

    runner = CliRunner()
    r2_result = runner.invoke(cli.app, ["r2"])
    assert r2_result.exit_code == 0, r2_result.output
    return tmp_path, runner


# ---------------------------------------------------------------------------
# kmt r3-check
# ---------------------------------------------------------------------------

def test_r3_check_blocked_without_r2_lock(cli_env):
    from kalshi_mt import cli

    conn, tmp_path = cli_env
    conn.close()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["r3-check"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["r2_locked"] is False
    assert payload["firewall_clear"] is False
    assert "locked_error" in payload


def test_r3_check_clear_after_r2_locked(cli_env):
    from kalshi_mt import cli

    tmp_path, runner = _prepare_locked_r2(cli_env)
    result = runner.invoke(cli.app, ["r3-check"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["r2_locked"] is True
    assert payload["import_violations"] == []
    assert payload["firewall_clear"] is True
    assert payload["r2_verdict"] is not None


# ---------------------------------------------------------------------------
# kmt escalate
# ---------------------------------------------------------------------------

def test_escalate_refuses_without_r2_lock(cli_env):
    from kalshi_mt import cli

    conn, tmp_path = cli_env
    conn.close()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["escalate"])
    assert result.exit_code == 1
    assert "is missing" in result.output


def test_escalate_end_to_end(cli_env):
    from kalshi_mt import cli

    tmp_path, runner = _prepare_locked_r2(cli_env)
    result = runner.invoke(cli.app, ["escalate"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert "maker_margin" in payload
    assert set(payload["maker_margin"].keys()) >= {"layer_a", "layer_b", "layer_c"}
    assert payload["maker_margin"]["layer_a"] is not None  # real trades were seeded

    assert "escalation" in payload
    assert isinstance(payload["escalation"]["escalate"], bool)
    assert isinstance(payload["escalation"]["triggers"], list)
    assert "delta_bar_fee" in payload["escalation"]["detail"]


# ---------------------------------------------------------------------------
# kmt report
# ---------------------------------------------------------------------------

def test_report_refuses_without_r2_lock(cli_env):
    from kalshi_mt import cli

    conn, tmp_path = cli_env
    conn.close()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["report"])
    assert result.exit_code == 1
    assert "is missing" in result.output


def test_report_end_to_end_writes_draft(cli_env):
    from kalshi_mt import cli

    tmp_path, runner = _prepare_locked_r2(cli_env)
    result = runner.invoke(cli.app, ["report"])
    assert result.exit_code == 0, result.output

    draft_path = tmp_path / "reports" / "final" / "draft.md"
    assert draft_path.exists()
    content = draft_path.read_text(encoding="utf-8")
    assert "Section 6" in content
    assert "Escalate:" in content
    assert "R3" in content

    # stdout has a human-readable line THEN a JSON blob -- just confirm the
    # JSON blob parses and names the same path.
    json_start = result.output.index("{")
    payload = json.loads(result.output[json_start:])
    assert payload["path"] == str(draft_path)
    assert isinstance(payload["escalate"], bool)


def test_report_venue_matches_escalation_result(cli_env):
    from kalshi_mt import cli

    tmp_path, runner = _prepare_locked_r2(cli_env)
    escalate_result = runner.invoke(cli.app, ["escalate"])
    assert escalate_result.exit_code == 0, escalate_result.output
    escalate_payload = json.loads(escalate_result.output)

    report_result = runner.invoke(cli.app, ["report"])
    assert report_result.exit_code == 0, report_result.output
    json_start = report_result.output.index("{")
    report_payload = json.loads(report_result.output[json_start:])

    assert report_payload["escalate"] == escalate_payload["escalation"]["escalate"]
