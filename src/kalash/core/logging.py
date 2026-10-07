"""Logging configuration for CLI and headless modes."""

from __future__ import annotations

import logging
import sys
from typing import Any

from kalash.core.privacy import scrub_metadata, scrub_text


class _RedactingFormatter(logging.Formatter):
    def __init__(self, wrapped: logging.Formatter) -> None:
        super().__init__()
        self.wrapped = wrapped

    def format(self, record: logging.LogRecord) -> str:
        return scrub_text(self.wrapped.format(record))


def _scrub_event(_logger: object, _method: str, event: Any) -> Any:
    return scrub_metadata(event)


def _scrub_rendered(_logger: object, _method: str, rendered: Any) -> str:
    return scrub_text(str(rendered))


def configure_logging(*, level: int = logging.WARNING) -> None:
    """Send stdlib and structlog output to stderr.

    Headless JSON output must own stdout exclusively so ``kalash -p`` can be
    piped to ``jq`` without log lines breaking the parse.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(stream=sys.stderr, level=level, format="%(message)s")
    for handler in root.handlers:
        if not isinstance(handler.formatter, _RedactingFormatter):
            handler.setFormatter(_RedactingFormatter(handler.formatter or logging.Formatter()))

    try:
        import structlog
    except ImportError:
        return

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _scrub_event,
            structlog.dev.ConsoleRenderer(),
            _scrub_rendered,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
