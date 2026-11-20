"""Structured logging: JSON lines to a file, readable text to stderr.

Progress output is treated as a correctness feature here rather than a nicety. A
scan across tens of thousands of files runs for a long time, and a silent process
is one you eventually assume has hung and kill — losing work and teaching you to
distrust the tool. `ProgressReporter` exists to make that not happen.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

LOG_FILENAME = "photofix.jsonl"

_EXTRA_BLOCKLIST = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per line, with arbitrary `extra=` fields folded in."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        for key, value in record.__dict__.items():
            if key not in _EXTRA_BLOCKLIST and not key.startswith("_"):
                payload[key] = _jsonable(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """Terse stderr lines. Context fields are appended as key=value."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname[0]} {record.getMessage()}"
        extras = [
            f"{k}={_compact(v)}"
            for k, v in record.__dict__.items()
            if k not in _EXTRA_BLOCKLIST and not k.startswith("_")
        ]
        if extras:
            base = f"{base}  ({', '.join(extras)})"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _compact(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def setup_logging(
    out_dir: Path | None = None,
    *,
    verbose: bool = False,
    quiet: bool = False,
) -> logging.Logger:
    """Configure the `photofix` logger. Safe to call more than once."""
    logger = logging.getLogger("photofix")
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = False

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO))
    stderr.setFormatter(HumanFormatter())
    logger.addHandler(stderr)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(out_dir / LOG_FILENAME, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonLinesFormatter())
        logger.addHandler(file_handler)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("photofix")


class ProgressReporter:
    """Emits a progress line on a fixed interval.

    `total` may be None for the walk stage, which cannot know the count ahead of
    time; in that case it reports the running count and current context instead
    of a percentage and ETA.
    """

    def __init__(
        self,
        label: str,
        *,
        total: int | None = None,
        interval_seconds: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.label = label
        self.total = total
        self.interval = interval_seconds
        self.log = logger or get_logger()
        self.count = 0
        self._started = time.monotonic()
        self._last_emit = self._started

    def advance(self, n: int = 1, *, context: str | None = None) -> None:
        self.count += n
        now = time.monotonic()
        if now - self._last_emit >= self.interval:
            self._last_emit = now
            self._emit(now, context=context, final=False)

    def finish(self, *, context: str | None = None) -> None:
        self._emit(time.monotonic(), context=context, final=True)

    def _emit(self, now: float, *, context: str | None, final: bool) -> None:
        elapsed = max(now - self._started, 1e-9)
        rate = self.count / elapsed
        fields: dict[str, Any] = {
            "count": self.count,
            "elapsed_s": round(elapsed, 1),
            "rate_per_s": round(rate, 1),
        }
        if self.total:
            remaining = max(self.total - self.count, 0)
            fields["total"] = self.total
            fields["pct"] = round(100.0 * self.count / self.total, 1)
            fields["eta_s"] = round(remaining / rate, 1) if rate > 0 else None
        if context:
            fields["at"] = context
        self.log.info(f"{self.label} {'complete' if final else 'progress'}", extra=fields)
