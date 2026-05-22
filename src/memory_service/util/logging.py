from __future__ import annotations

import logging
import sys

try:
    # python-json-logger >=4 moved JsonFormatter to a new module.
    from pythonjsonlogger.json import JsonFormatter
except ImportError:                                         # pragma: no cover - older versions
    from pythonjsonlogger.jsonlogger import JsonFormatter


def configure_logging(level: str = "INFO") -> None:
    """Configure root logger as JSON to stdout. Idempotent."""
    root = logging.getLogger()
    if any(getattr(h, "_memory_service_handler", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
        )
    )
    handler._memory_service_handler = True  # type: ignore[attr-defined]
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # quiet down noisy libraries
    logging.getLogger("uvicorn.access").setLevel("WARNING")
    logging.getLogger("httpx").setLevel("WARNING")
