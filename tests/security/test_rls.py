"""PostgreSQL row-level security: isolation holds even when application code forgets the organization filter."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.usefixtures("seeded")


def test_every_tenant_table_has_rls_and_a_policy(db):
    from app.db.tenancy import system_scope

    with system_scope():
        missing = db.execute(text("""
            SELECT c.table_name FROM information_schema.columns c
            JOIN pg_class k ON k.relname = c.table_name AND k.relkind = 'r'
            JOIN pg_namespace n ON n.oid = k.relnamespace AND n.nspname = 'public'
            WHERE c.table_schema = 'public' AND c.column_name = 'organization_id'
              AND (NOT k.relrowsecurity OR NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = k.oid))
        """)).scalars().all()
    assert missing == [], f"tables without row-level security: {missing}"


def test_app_runs_as_non_bypassing_role(db):
    user, bypass, sup = db.execute(text("SELECT current_user, rolbypassrls, rolsuper FROM pg_roles WHERE rolname = current_user")).one()
    assert (user, bypass, sup) == ("tenderpilot_app", False, False)


def test_unfiltered_queries_only_see_the_bound_tenant(seeded, db):
    from app.db.models import Document, Organization
    from app.db.tenancy import system_scope

    volt, kav = seeded.contexts["owner@voltedge.example"], seeded.contexts["owner@kaveri.example"]
    with system_scope():
        total = db.scalar(text("SELECT count(DISTINCT organization_id) FROM documents"))
    assert total >= 2

    volt.bind()
    # Deliberately missing the organization filter — the database still scopes it.
    orgs = {d.organization_id for d in db.scalars(select(Document))}
    assert orgs == {volt.organization_id}
    assert db.scalar(text("SELECT count(*) FROM documents WHERE organization_id = :k"), {"k": kav.organization_id}) == 0
    assert [o.id for o in db.scalars(select(Organization))] == [volt.organization_id]

    kav.bind()  # switching scope mid-transaction re-applies the settings
    assert {d.organization_id for d in db.scalars(select(Document))} == {kav.organization_id}
    db.rollback()


def test_unscoped_code_sees_nothing_and_cannot_write(seeded, db):
    from app.db import tenancy
    from app.db.models import Notification

    tenancy.reset()
    assert db.scalar(text("SELECT count(*) FROM tenders")) == 0
    assert db.scalar(text("SELECT count(*) FROM organizations")) == 0
    db.add(Notification(organization_id=seeded.contexts["owner@voltedge.example"].organization_id,
                        user_id=seeded.contexts["owner@voltedge.example"].user_id, kind="test", title="unscoped write"))
    with pytest.raises(ProgrammingError, match="row-level security"):
        db.flush()
    db.rollback()


def test_cross_tenant_write_rejected_by_database(seeded, db):
    from app.db.models import BidTask, Notification

    volt, kav = seeded.contexts["owner@voltedge.example"], seeded.contexts["owner@kaveri.example"]
    volt.bind()
    db.add(Notification(organization_id=kav.organization_id, user_id=kav.user_id, kind="test", title="smuggled"))
    with pytest.raises(ProgrammingError, match="row-level security"):
        db.flush()
    db.rollback()

    # Updating another tenant's rows silently matches nothing.
    volt.bind()
    n = db.execute(text("UPDATE tenders SET title = 'hijacked' WHERE organization_id = :k"), {"k": kav.organization_id}).rowcount
    assert n == 0
    assert db.scalar(select(BidTask).where(BidTask.organization_id == kav.organization_id)) is None
    db.rollback()


def test_session_keeps_tenant_for_commit_from_another_context(seeded):
    """FastAPI commits in dependency teardown (a different context): the session keeps the tenant it acted for."""
    import contextvars

    from app.db import tenancy
    from app.db.models import Notification
    from app.db.session import get_session_factory

    volt = seeded.contexts["owner@voltedge.example"]
    s = get_session_factory()()
    try:
        volt.bind()
        s.execute(text("SELECT 1"))
        s.add(Notification(organization_id=volt.organization_id, user_id=volt.user_id, kind="test", title="sticky",
                           dedup_key=f"sticky-{uuid.uuid4().hex}"))
        contextvars.Context().run(s.flush)  # empty context: no tenant bound, no bypass
    finally:
        s.rollback()
        s.close()
        tenancy.reset()
