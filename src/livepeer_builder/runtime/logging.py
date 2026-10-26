"""Structured logging. Code logs with ``logger.info("message", extra={"event": "domain.action", ...})``.

``print`` is forbidden outside the CLI (tests/structure/test_taste.py), so every runtime signal is a
JSON line an agent or log pipeline can query.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({key: value for key, value in vars(record).items() if key not in _RESERVED})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(*, json_lines: bool = True, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if json_lines else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs every request at INFO with its URL, and an RPC provider's URL carries its key (#46 review).
    # The engine's and the discovery service's own events say what an operator needs.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
