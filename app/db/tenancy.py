"""PostgreSQL row-level security plumbing (defence in depth under the application-layer tenant filters).

Every transaction opened by an app Session switches to the non-owner role ``tenderpilot_app`` (``SET LOCAL ROLE``) and sets
two transaction-local settings that the RLS policies read:

* ``app.org_id``     — the organization the current code is acting for;
* ``app.rls_bypass`` — ``on`` only inside an explicit :func:`system_scope` (pre-authentication identity lookups,
  scheduled maintenance, seeding, platform-admin actions).

With neither set, tenant tables return no rows and reject writes (deny by default). The scope lives in context
variables: it is bound by ``TenantContext.require()/can()`` (so any authorization check also scopes the database), by
request/identity resolution, and per job by the worker. If the scope changes mid-transaction (e.g. a test or a
background job switching organization), the settings are re-applied before the next statement or flush.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

APP_ROLE = "tenderpilot_app"
_org: ContextVar[uuid.UUID | None] = ContextVar("tp_rls_org", default=None)
_bypass: ContextVar[bool] = ContextVar("tp_rls_bypass", default=False)
_fallbacks: list[Callable[[], uuid.UUID | None]] = []


def bind_tenant(org_id: uuid.UUID | None) -> None:
    """Scope this execution context (task/thread) to one organization."""
    _org.set(org_id)


def current() -> tuple[uuid.UUID | None, bool]:
    org = _org.get()
    if org is None and not _bypass.get():
        for fb in _fallbacks:  # e.g. the NiceGUI client whose event is being handled
            if (org := fb()) is not None:
                break
    return org, _bypass.get()


def register_fallback(fn: Callable[[], uuid.UUID | None]) -> None:
    _fallbacks.append(fn)


def reset() -> None:
    _org.set(None)
    _bypass.set(False)


@contextmanager
def system_scope() -> Iterator[None]:
    """Explicit, audited-by-code bypass for cross-tenant system work. Keep these blocks small."""
    token = _bypass.set(True)
    try:
        yield
    finally:
        _bypass.reset(token)


@contextmanager
def tenant_scope(org_id: uuid.UUID | None) -> Iterator[None]:
    t_org, t_bypass = _org.set(org_id), _bypass.set(False)
    try:
        yield
    finally:
        _org.reset(t_org)
        _bypass.reset(t_bypass)


def system_scoped[F: Callable[..., Any]](fn: F) -> F:
    """Run fn in system scope. Rows it adds are flushed before the scope ends (a later flush would be checked against
    whatever scope is current then)."""
    @wraps(fn)
    def wrapper(*a, **kw):
        db = kw.get("db") or next((x for x in a if isinstance(x, Session)), None)
        with system_scope():
            out = fn(*a, **kw)
            if db is not None and (db.new or db.dirty or db.deleted):
                db.flush()
            return out

    return wrapper  # type: ignore[return-value]


# ---------------- connection / session hooks ----------------

def _state(org: uuid.UUID | None, bypass: bool) -> tuple[str, str]:
    return (str(org) if org else "", "on" if bypass else "off")


def _apply(conn: Connection, state: tuple[str, str], set_role: bool) -> None:
    if set_role:
        conn.exec_driver_sql(f"SET LOCAL ROLE {APP_ROLE}")
    conn.exec_driver_sql("SELECT set_config('app.org_id', %(o)s, true), set_config('app.rls_bypass', %(b)s, true)", {"o": state[0], "b": state[1]})
    conn.info["tp_rls"] = state


def _reapply_if_changed(session: Session) -> None:
    """Align the transaction's settings with the current scope. A session that has acted for a tenant keeps that
    tenant when it is flushed/committed from a context without one (e.g. FastAPI dependency teardown runs in a
    different context than the endpoint). Bypass is never sticky."""
    if not session.in_transaction():
        return
    state = _desired(session)
    conn = session.connection()
    if conn.info.get("tp_rls") != state:
        _apply(conn, state, set_role=False)


def _desired(session: Session) -> tuple[str, str]:
    org, bypass = current()
    if org is not None and not bypass:
        session.info["tp_tenant"] = org
    elif org is None and not bypass:
        org = session.info.get("tp_tenant")
    return _state(org, bypass)


def install(engine: Engine) -> None:
    @event.listens_for(Session, "after_begin")
    def _on_begin(session: Session, _txn, conn: Connection) -> None:
        if conn.engine is engine:
            _apply(conn, _desired(session), set_role=True)

    @event.listens_for(Session, "do_orm_execute")
    def _on_execute(state) -> None:
        _reapply_if_changed(state.session)

    @event.listens_for(Session, "before_flush")
    def _on_flush(session: Session, _ctx, _instances) -> None:
        _reapply_if_changed(session)
