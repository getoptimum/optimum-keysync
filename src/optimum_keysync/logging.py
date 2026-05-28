"""Structured logging setup.

`structlog` is the only logger; `print()` is never used in this package.
Two output formats:

  - "console" (default) — human-readable colored output for terminal use.
  - "json" — one JSON object per line, suited to cron output ingested by
    journald / docker logs / k8s.
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

import structlog

LogFormat = Literal["console", "json"]


def configure(level: str = "INFO", fmt: LogFormat = "console") -> None:
    """Idempotent logger setup. Safe to call multiple times in tests."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    # structlog.get_logger returns Any; the cast pins it to BoundLogger so
    # callers get autocomplete + type-checking on .info/.error/etc.
    return structlog.get_logger(name)  # type: ignore[no-any-return]
