"""Structured logging: one JSON object per line on stderr.

Why JSON lines and not pretty text: the arbiter, three satellites and the display all
log concurrently, and at 3am someone will want to `grep '"event": "revoke"'` across
all of them and sort by time. Extra keyword fields on a log call land as top-level
keys via ``extra={"fields": {...}}``; the ``log()`` helper below hides that.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "t": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            doc.update(extra)
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str, separators=(",", ":"))


def setup(level: str = "INFO", stream: Any = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(stream or sys.stderr)
    h.setFormatter(JsonFormatter())
    root.addHandler(h)
    root.setLevel(level.upper())


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """``log(lg, logging.INFO, "grant", to="sat-a", item_id=7)`` → one JSON line with those keys."""
    if logger.isEnabledFor(level):
        logger.log(level, msg, extra={"fields": fields})


def now_s() -> float:
    return time.time()
