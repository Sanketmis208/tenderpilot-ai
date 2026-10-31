"""Engine and session factory. Sync SQLAlchemy keeps API, workers and agents on one code path."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    s = get_settings()
    engine = create_engine(
        s.database_url,
        pool_size=s.database_pool_size,
        max_overflow=s.database_pool_size,
        pool_pre_ping=True,
        future=True,
        connect_args={"options": "-c timezone=UTC"},
    )
    if s.db_rls_enabled:
        from app.db import tenancy

        tenancy.install(engine)
    return engine


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Unit of work: commit on success, rollback on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
