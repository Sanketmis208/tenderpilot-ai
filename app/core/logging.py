"""Structured logging configuration (structlog) with request correlation IDs."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def _add_request_id(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    rid = request_id_var.get()
    if rid:
        event_dict["request_id"] = rid
    return event_dict


_REDACT_KEYS = {"password", "token", "secret", "authorization", "api_key", "cookie"}


def _redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if any(k in key.lower() for k in _REDACT_KEYS):
            event_dict[key] = "***"
    return event_dict


def configure_logging(level: str = "INFO", json: bool = False) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_request_id,
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
