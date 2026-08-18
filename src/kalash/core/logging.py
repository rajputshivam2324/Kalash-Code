"""Logging configuration for CLI and headless modes."""

from __future__ import annotations

import logging
import sys


def configure_logging(*, level: int = logging.WARNING) -> None:
    """Send stdlib and structlog output to stderr.

    Headless JSON output must own stdout exclusively so ``kalash -p`` can be
    piped to ``jq`` without log lines breaking the parse.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(stream=sys.stderr, level=level, format="%(message)s")

    try:
        import structlog
    except ImportError:
        return

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
