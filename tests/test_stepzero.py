"""Step Zero: FakeKalshiClient drives all 5 checks through PASS/PARTIAL/FAIL/
AUTH_REQUIRED, plus a CliRunner check on the STOP/GO exit codes.

Async code driven via asyncio.run() from plain `def test_*` functions --
pytest-asyncio is not a project dependency.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from typer.testing import CliRunner

from kalshi_mt.api.kalshi import KalshiCandlestick, KalshiEvent, KalshiMarket, KalshiTrade
from kalshi_mt.stepzero.checks import _epoch
from kalshi_mt.stepzero.report import render_findings_markdown, run_step_zero
from kalshi_mt.util import load_config

BOUND_2021_2022 = (_epoch(2021, 1, 1), _epoch(2023, 1, 1))
BOUND_2023 = (_epoch(2023, 1, 1), _epoch(2024, 1, 1))
BOUND_2024 = (_epoch(2024, 1, 1), _epoch(2025, 1, 1))
BOUND_2025 = (_epoch(2025, 1, 1), _epoch(2025, 5, 1))

CANDIDATE_2021_2022 = ["KX2021A", "KX2021B", "KX2021C", "KX2021D", "KX2021E"]
ERA_TICKERS = {"2023": "KX2023A", "2024": "KX2024A", "2025-jan-apr": "KX2025A"}


def _auth_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/x")
    response = httpx.Response(status, request=request, json={"error": "auth"})
    return httpx.HTTPStatusError("auth required", request=request, response=response)


def _market(ticker: str, close_epoch: int, event_ticker: str) -> KalshiMarket:
    from datetime import datetime, timezone

    close_iso = datetime.fromtimestamp(close_epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return KalshiMarket.model_validate(
        {
            "ticker": ticker, "event_ticker": event_ticker, "status": "settled", "result": "yes",
            "close_time": close_iso, "volume_fp": "100.00",
        }
    )


def _trades(ticker: str, n: int, base_epoch: int, outcome_rate: float = 1.0) -> list[KalshiTrade]:
    from datetime import datetime, timezone

    out = []
    n_populated = round(n * outcome_rate)
    for i in range(n):
        ts_iso = datetime.fromtimestamp(base_epoch + i * 3600, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        out.append(
            KalshiTrade.model_validate(
                {
                    "trade_id": f"{ticker}-t{i}", "ticker": ticker, "count_fp": "1.00",
                    "yes_price_dollars": "0.5000", "created_time": ts_iso,
                    "taker_outcome_side": "yes" if i < n_populated else None,
                    "taker_book_side": "bid" if i < n_populated else None,
                    "taker_side": "yes",  # legacy field always populated, matching real Kalshi behavior
                }
            )
        )
    return out


class FakeKalshiClient:
    """Configurable stand-in for KalshiClient -- no network calls."""

    def __init__(
        self,
        *,
        auth_required_on: frozenset[str] = frozenset(),
        bracketing_supported: bool = True,
        candlestick_quote_tickers: frozenset[str] | None = None,
        zero_trade_tickers: frozenset[str] = frozenset(),
        taker_population_rate_2021_2022: float = 1.0,
    ) -> None:
        self.auth_required_on = auth_required_on
        self.bracketing_supported = bracketing_supported
        self.zero_trade_tickers = zero_trade_tickers
        self.calls: list[str] = []

        all_2021_tickers = CANDIDATE_2021_2022
        self._markets: dict[str, KalshiMarket] = {}
        for i, t in enumerate(all_2021_tickers):
            close_epoch = BOUND_2021_2022[0] + i * 86400 * 30
            self._markets[t] = _market(t, close_epoch, f"EV-{t}")
        self._markets["KX2023A"] = _market("KX2023A", BOUND_2023[0] + 86400 * 10, "EV-KX2023A")
        self._markets["KX2024A"] = _market("KX2024A", BOUND_2024[0] + 86400 * 10, "EV-KX2024A")
        self._markets["KX2025A"] = _market("KX2025A", BOUND_2025[0] + 86400 * 10, "EV-KX2025A")

        self._events: dict[str, KalshiEvent] = {
            m.event_ticker: KalshiEvent(event_ticker=m.event_ticker, series_ticker=f"SER-{t}")
            for t, m in self._markets.items()
        }

        self._trades: dict[str, list[KalshiTrade]] = {}
        for i, t in enumerate(all_2021_tickers):
            if t in zero_trade_tickers:
                self._trades[t] = []
            else:
                self._trades[t] = _trades(
                    t, 12, BOUND_2021_2022[0] + i * 86400 * 30, taker_population_rate_2021_2022
                )
        self._trades["KX2023A"] = _trades("KX2023A", 10, BOUND_2023[0] + 86400 * 10)
        self._trades["KX2024A"] = _trades("KX2024A", 10, BOUND_2024[0] + 86400 * 10)
        self._trades["KX2025A"] = _trades("KX2025A", 10, BOUND_2025[0] + 86400 * 10)

        # tickers with a genuine two-sided quote available (live candlesticks);
        # None (default) = every ticker has a quote.
        self._quote_tickers = candlestick_quote_tickers

    def _check_auth(self, name: str) -> None:
        if name in self.auth_required_on:
            raise _auth_error(401)

    async def list_markets(self, status=None, min_close_ts=None, max_close_ts=None,
                            min_settled_ts=None, max_settled_ts=None, series_ticker=None,
                            event_ticker=None, cursor=None, limit=100):
        self.calls.append("list_markets")
        self._check_auth("list_markets")
        window = (min_close_ts, max_close_ts)
        if window == BOUND_2021_2022:
            return [self._markets[t] for t in CANDIDATE_2021_2022], None
        if window == BOUND_2023:
            return [self._markets["KX2023A"]], None
        if window == BOUND_2024:
            return [self._markets["KX2024A"]], None
        if window == BOUND_2025:
            return [self._markets["KX2025A"]], None
        return [], None

    async def get_market(self, ticker: str):
        self.calls.append("get_market")
        self._check_auth("get_market")
        return self._markets.get(ticker)

    async def get_event(self, event_ticker: str):
        self.calls.append("get_event")
        self._check_auth("get_event")
        return self._events.get(event_ticker)

    async def list_historical_markets(self, tickers=None, event_ticker=None, series_ticker=None,
                                       cursor=None, limit=100):
        self.calls.append("list_historical_markets")
        self._check_auth("list_historical_markets")
        if tickers and tickers in self._markets:
            return [self._markets[tickers]], None
        return [], None

    async def get_historical_cutoff(self):
        self.calls.append("get_historical_cutoff")
        self._check_auth("get_historical_cutoff")
        return None

    async def get_trades(self, ticker=None, min_ts=None, max_ts=None, cursor=None, limit=100):
        self.calls.append("get_trades")
        self._check_auth("get_trades")
        full = self._trades.get(ticker, [])
        if min_ts is None and max_ts is None:
            return full, None
        if not self.bracketing_supported:
            return full, None  # params silently ignored -- the FAIL scenario
        filtered = [t for t in full if min_ts <= _iso_epoch(t.created_time) <= max_ts]
        return filtered, None

    async def get_historical_trades(self, ticker=None, min_ts=None, max_ts=None, cursor=None, limit=100):
        self.calls.append("get_historical_trades")
        self._check_auth("get_historical_trades")
        return [], None  # in these fixtures, live always has data; historical fallback unused

    async def get_candlesticks(self, series_ticker, ticker, start_ts, end_ts, period_interval=1440):
        self.calls.append("get_candlesticks")
        self._check_auth("get_candlesticks")
        has_quote = self._quote_tickers is None or ticker in self._quote_tickers
        if not has_quote:
            return []
        return [
            KalshiCandlestick.model_validate(
                {"end_period_ts": end_ts, "yes_bid": {"close_dollars": "0.45"},
                 "yes_ask": {"close_dollars": "0.47"}}
            )
        ]

    async def get_historical_candlesticks(self, ticker, start_ts, end_ts, period_interval=1440):
        self.calls.append("get_historical_candlesticks")
        self._check_auth("get_historical_candlesticks")
        return []  # no fallback quote data in these fixtures

    async def list_series(self, category=None, limit=200):
        self.calls.append("list_series")
        self._check_auth("list_series")
        return []

    async def aclose(self) -> None:
        pass


def _iso_epoch(value: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _config():
    cfg = load_config()
    return cfg


# --- (i) clean success -------------------------------------------------------


def test_clean_success_is_go():
    fake = FakeKalshiClient()

    async def _run():
        return await run_step_zero(_config(), client=fake)

    report = asyncio.run(_run())
    assert report.verdict == "GO"
    assert report.stop_reason is None
    statuses = {c.id: c.status for c in report.checks}
    assert statuses[1] == "PASS"
    assert statuses[2] == "PASS"
    assert statuses[3] == "PASS"
    assert statuses[4] == "PASS"
    assert statuses[5] == "PASS"


# --- (ii) injected 401 on /trades -> STOP ------------------------------------


def test_401_on_trades_trips_stop():
    fake = FakeKalshiClient(auth_required_on=frozenset({"get_trades"}))

    async def _run():
        return await run_step_zero(_config(), client=fake)

    report = asyncio.run(_run())
    assert report.verdict == "STOP"
    assert report.checks[0].id == 1
    assert report.checks[0].status == "AUTH_REQUIRED"
    assert "STOP" in render_findings_markdown(report)


# --- (iii) zero-trades-despite-volume market -> check 2 FAIL ----------------


def test_zero_trades_market_fails_check2():
    fake = FakeKalshiClient(zero_trade_tickers=frozenset({"KX2021A"}))

    async def _run():
        return await run_step_zero(_config(), client=fake)

    report = asyncio.run(_run())
    c2 = next(c for c in report.checks if c.id == 2)
    assert c2.status == "FAIL"
    a_entry = next(m for m in c2.evidence["markets"] if m["ticker"] == "KX2021A")
    assert a_entry["ok"] is False
    assert a_entry["trade_count"] == 0


# --- (iv) partial taker_outcome_side population by era -> check 3 PARTIAL ---


def test_partial_taker_population_flags_era():
    fake = FakeKalshiClient(taker_population_rate_2021_2022=0.4)

    async def _run():
        return await run_step_zero(_config(), client=fake)

    report = asyncio.run(_run())
    c3 = next(c for c in report.checks if c.id == 3)
    assert c3.status == "PARTIAL"
    era = c3.evidence["eras"]["2021-2022"]
    assert era["taker_outcome_side_population"] < 0.95
    # unaffected eras stay fully populated
    assert c3.evidence["eras"]["2023"]["taker_outcome_side_population"] == 1.0


# --- (v) identical bracketed/unbounded results -> check 4 FAIL --------------


def test_unsupported_bracketing_fails_check4():
    fake = FakeKalshiClient(bracketing_supported=False)

    async def _run():
        return await run_step_zero(_config(), client=fake)

    report = asyncio.run(_run())
    c4 = next(c for c in report.checks if c.id == 4)
    assert c4.status == "FAIL"
    assert c4.evidence["full_count"] == c4.evidence["bracketed_count"]


def test_supported_bracketing_passes_check4():
    fake = FakeKalshiClient(bracketing_supported=True)

    async def _run():
        return await run_step_zero(_config(), client=fake)

    report = asyncio.run(_run())
    c4 = next(c for c in report.checks if c.id == 4)
    assert c4.status == "PASS"
    assert 0 < c4.evidence["bracketed_count"] < c4.evidence["full_count"]


# --- (vi) pre-2023 empty candlesticks -> check 5 PARTIAL, cutoff captured ---


def test_pre_2023_missing_quotes_partial_check5():
    fake = FakeKalshiClient(candlestick_quote_tickers=frozenset({"KX2023A", "KX2024A", "KX2025A"}))

    async def _run():
        return await run_step_zero(_config(), client=fake)

    report = asyncio.run(_run())
    c5 = next(c for c in report.checks if c.id == 5)
    assert c5.status == "PARTIAL"
    assert c5.evidence["eras"]["2021-2022"]["has_quote"] is False
    assert c5.evidence["eras"]["2023"]["has_quote"] is True


# --- CLI exit codes ------------------------------------------------------------


def _write_isolated_config(tmp_path):
    """A minimal real config.yaml under tmp_path -- load_config()'s default
    resolves PROJECT_ROOT / "config.yaml" fresh on every call (not a
    module-level constant frozen at import time), so once PROJECT_ROOT is
    monkeypatched to tmp_path, a config.yaml must actually exist there."""
    (tmp_path / "config.yaml").write_text(
        """
kalshi:
  base_url: "https://external-api.kalshi.com/trade-api/v2"
  rate_limit: { requests_per_second: 10, burst: 20 }
  known_early_tickers: []
dates:
  r1_window: { start: "2021-01-01", end: "2025-04-30" }
  r2_window: { start: "2025-05-01", end: "2026-06-30" }
  boundaries: { fee_change: "2025-05-01", publication: "2025-09-08" }
  r3_window_start: "2026-07-04"
storage:
  db_path: data/kmt.db
  raw_dir: data/raw
  parquet_dir: data/parquet
  logs_dir: data/logs
  reports_dir: reports
logging:
  level: INFO
""",
        encoding="utf-8",
    )


def test_cli_exits_3_on_auth_required(monkeypatch, tmp_path):
    from kalshi_mt import cli
    from kalshi_mt import util

    fake = FakeKalshiClient(auth_required_on=frozenset({"get_trades"}))

    async def _fake_run_step_zero(config, client=None):
        return await run_step_zero(config, client=fake)

    import kalshi_mt.stepzero.report as report_mod

    monkeypatch.setattr(report_mod, "run_step_zero", _fake_run_step_zero)
    # Never let a fake fixture's findings.md land in the real, committed
    # reports/step_zero/ directory -- redirect to an isolated tmp_path.
    monkeypatch.setattr(util, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(report_mod, "PROJECT_ROOT", tmp_path)
    _write_isolated_config(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["step-zero"])
    assert result.exit_code == 3


def test_cli_exits_0_on_clean_success(monkeypatch, tmp_path):
    from kalshi_mt import cli
    from kalshi_mt import util

    fake = FakeKalshiClient()

    async def _fake_run_step_zero(config, client=None):
        return await run_step_zero(config, client=fake)

    import kalshi_mt.stepzero.report as report_mod

    monkeypatch.setattr(report_mod, "run_step_zero", _fake_run_step_zero)
    monkeypatch.setattr(util, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(report_mod, "PROJECT_ROOT", tmp_path)
    _write_isolated_config(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["step-zero"])
    assert result.exit_code == 0
    assert (tmp_path / "reports" / "step_zero" / "findings.md").exists()
