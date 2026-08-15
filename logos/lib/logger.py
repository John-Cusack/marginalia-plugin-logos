"""Structured logging via structlog."""

from __future__ import annotations

import structlog

logger = structlog.get_logger("logos")


def log(*args: object) -> None:
    logger.info(" ".join(str(a) for a in args))


def log_error(*args: object) -> None:
    logger.error(" ".join(str(a) for a in args))


def log_debug(*args: object) -> None:
    logger.debug(" ".join(str(a) for a in args))
