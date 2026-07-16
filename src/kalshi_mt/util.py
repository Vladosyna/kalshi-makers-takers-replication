"""Shared utilities: the single clock call, config loading, logging setup."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Spec S3: "all 'same time' comparisons in US Eastern Time." zoneinfo (not a
# fixed offset, not pytz) is required here specifically because DST-correct
# "same wall-clock time N calendar days back" arithmetic depends on it: a
# zoneinfo-aware datetime recomputes its UTC offset from its own wall-clock
# fields on demand, so `dt - timedelta(days=N)` on an ET-aware datetime lands
# on the correct wall-clock instant even across a DST transition -- a fixed
# UTC offset (or pytz's eager-localization model) would silently drift by an
# hour on exactly the days that matter most for this kind of panel.
ET = ZoneInfo("America/New_York")


def epoch_to_et(epoch: int) -> datetime:
    """A Unix epoch second as an ET-aware datetime."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(ET)


def et_to_epoch(dt_et: datetime) -> int:
    """An ET-aware (or ET-naive, assumed ET) datetime back to a Unix epoch second."""
    if dt_et.tzinfo is None:
        dt_et = dt_et.replace(tzinfo=ET)
    return int(dt_et.timestamp())


def shift_et_calendar_days(dt_et: datetime, days: int) -> datetime:
    """`days` calendar days back (or forward if negative -days), same
    wall-clock time, DST-correct (see the ET/zoneinfo note above)."""
    return dt_et - timedelta(days=days)


def et_day_start(dt_et: datetime) -> datetime:
    """00:00:00 ET on `dt_et`'s own calendar date."""
    return dt_et.replace(hour=0, minute=0, second=0, microsecond=0)


def iso_to_epoch(value: str | None) -> int | None:
    """Parse a Kalshi ISO-8601 timestamp string ('...Z' or '+00:00') to a
    Unix epoch second, or None if unparseable/absent."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def use_stable_event_loop() -> None:
    """Select a Windows-stable asyncio event loop before any ``asyncio.run``.

    The default Windows Proactor loop crashes with a native access violation
    under sustained async HTTP traffic. Our request pattern is sequential and
    rate-limited, so the Selector loop is both stable and sufficient. No-op
    off Windows.
    """
    if not sys.platform.startswith("win"):
        return
    import asyncio

    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is not None and not isinstance(asyncio.get_event_loop_policy(), policy):
        asyncio.set_event_loop_policy(policy())


def now_utc() -> datetime:
    """The only clock call allowed in this codebase."""
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    """Current UTC time as ISO-8601 with second precision."""
    return now_utc().isoformat(timespec="seconds")


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Defaults to PROJECT_ROOT / "config.yaml", resolved fresh on every
    call (a plain reference to the module-global PROJECT_ROOT, not a
    separately-cached constant) so that test monkeypatching of
    util.PROJECT_ROOT actually takes effect -- a constant derived once at
    import time would freeze to whatever PROJECT_ROOT was at first import
    and silently ignore any later monkeypatch.setattr."""
    config_path = path or (PROJECT_ROOT / "config.yaml")
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class JsonLinesFormatter(logging.Formatter):
    """Structured logging: one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "ctx", None)
        if extra:
            entry["ctx"] = extra
        return json.dumps(entry, ensure_ascii=False)


# Query-string secrets that must never reach a log line. No Kalshi endpoint in
# scope passes a key this way today, but httpx logs full request URLs at INFO
# level -- cheap, forward-looking insurance against KALSHI_API_KEY_ID ever
# leaking into data/logs/kmt.jsonl if a future auth path is added.
_SECRET_QUERY_PARAM_RE = re.compile(r"(?<=[?&])(api_key|apikey|access_token)=[^&\s\"']+", re.I)


class RedactSecretsFilter(logging.Filter):
    """Strips URL-embedded API keys from every log record before it's emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = _SECRET_QUERY_PARAM_RE.sub(r"\1=***REDACTED***", msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(config: dict[str, Any] | None = None, level: int = logging.INFO) -> None:
    """Console handler (human-readable) + rotating JSONL file handler."""
    config = config or load_config()
    logs_dir = PROJECT_ROOT / config["storage"]["logs_dir"]
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if root.handlers:  # idempotent: safe to call more than once
        return
    root.setLevel(level)
    redact = RedactSecretsFilter()

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console.addFilter(redact)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "kmt.jsonl", maxBytes=20_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonLinesFormatter())
    file_handler.addFilter(redact)
    root.addHandler(file_handler)
