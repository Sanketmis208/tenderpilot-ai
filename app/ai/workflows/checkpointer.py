"""Durable LangGraph checkpoints in PostgreSQL (langgraph-checkpoint-postgres)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver

from app.core.config import get_settings

_setup_done = False


@contextmanager
def checkpointer() -> Iterator[PostgresSaver]:
    global _setup_done
    with PostgresSaver.from_conn_string(get_settings().sync_psycopg_dsn) as saver:
        if not _setup_done:
            saver.setup()  # idempotent migrations for checkpoint tables
            _setup_done = True
        yield saver
