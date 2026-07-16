"""Shared utilities: the single clock call, config loading, logging setup."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


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
    config_path = path or DEFAULT_CONFIG_PATH
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
