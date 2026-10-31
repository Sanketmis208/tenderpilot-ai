"""Every MCP read tool against seeded data, mutation gating, argument validation; task collaboration rules."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("seeded")


def _call(db, ctx, name, args=None, **kw):
    from app.mcp.tools.registry import call_tool

    return call_tool(db, ctx, name, args or {}, channel="test", **kw)


def test_all_read_tools_return_tenant_scoped_data(seeded, db):
    from app.db.models import Document, EligibilityDecision, TenderRequirement
    from app.services.eligibility import service as elig

    ctx = seeded.contexts["review@voltedge.example"]
    tid = str(seeded.tenders["lt_panels"].id)
    other = str(seeded.tenders["street_lighting"].id)
    if not elig.latest_assessment(db, ctx, seeded.tenders["lt_panels"].id):
        elig.start_assessment(db, seeded.contexts["bids@voltedge.example"], seeded.tenders["lt_panels"].id)
        db.commit()
    assert _call(db, ctx, "get_company_profile")
    assert _call(db, ctx, "get_tender_details", {"tender_id": tid})["tender"]["title"]
    reqs = _call(db, ctx, "get_tender_requirements", {"tender_id": tid})
    assert reqs["requirements"]
    req_id = db.scalar(select(TenderRequirement.id).where(TenderRequirement.tender_id == seeded.tenders["lt_panels"].id, TenderRequirement.is_current.is_(True)))
    assert "evidence" in _call(db, ctx, "search_supporting_evidence", {"requirement_id": str(req_id)})
    doc_id = db.scalar(select(Document.id).where(Document.organization_id == ctx.organization_id, Document.scope == "company"))
    assert _call(db, ctx, "get_document_metadata", {"document_id": str(doc_id)})
    assert "certificates" in _call(db, ctx, "get_certificate_validity", {"standard": "ISO 9001"})
    assert _call(db, ctx, "get_bid_workspace", {"tender_id": tid}) is not None
    assert _call(db, ctx, "list_missing_documents", {"tender_id": tid}) is not None
    assert "products" in _call(db, ctx, "get_catalogue_products")
    assert "projects" in _call(db, ctx, "list_relevant_past_projects", {"query": "substation", "min_value_inr": 0})
    assert _call(db, ctx, "list_upcoming_deadlines", {"days": 120}) is not None
    assert _call(db, ctx, "compare_tenders", {"tender_a": tid, "tender_b": other})

    # Cross-tenant ids are not found, and arbitrary org ids cannot be passed (extra="forbid").
    from app.core.errors import AppError, NotFound, ValidationFailed

    with pytest.raises(AppError):
        _call(db, ctx, "get_tender_details", {"tender_id": str(seeded.tenders["kaveri_wtp"].id)})
    with pytest.raises(ValidationFailed):
        _call(db, ctx, "get_tender_details", {"tender_id": tid, "organization_id": str(ctx.organization_id)})
    with pytest.raises(ValidationFailed):
        _call(db, ctx, "no_such_tool")
    with pytest.raises((NotFound, ValidationFailed)):
        _call(db, ctx, "get_document_metadata", {"document_id": "00000000-0000-0000-0000-000000000000"})

    dec = db.scalar(select(EligibilityDecision.id).where(EligibilityDecision.organization_id == ctx.organization_id))
    assert dec is not None


def test_mutation_tools_require_explicit_grant_and_role(seeded, db):
    from app.core.errors import PermissionDenied
    from app.db.models import AuditEvent, Document, EligibilityDecision, ToolExecutionLog
    from app.services.bids import service as bids

    reviewer = seeded.contexts["review@voltedge.example"]
    viewer = seeded.contexts["viewer@voltedge.example"]
    tid = seeded.tenders["lt_panels"].id
    bids.create_workspace(db, seeded.contexts["bids@voltedge.example"], tid)
    db.commit()
    with pytest.raises(PermissionDenied):
        _call(db, reviewer, "request_human_review", {"decision_id": "00000000-0000-0000-0000-000000000000", "note": "please check"})
    with pytest.raises(PermissionDenied):
        _call(db, viewer, "create_remediation_task", {"tender_id": str(tid), "title": "Viewer task"}, allow_mutations=True)
    dec = db.scalar(select(EligibilityDecision.id).where(EligibilityDecision.organization_id == reviewer.organization_id))
    doc = db.scalar(select(Document.id).where(Document.organization_id == reviewer.organization_id, Document.scope == "company"))
    assert _call(db, reviewer, "request_human_review", {"decision_id": str(dec), "note": "Please verify turnover"}, allow_mutations=True)["requested"]
    out = _call(db, reviewer, "attach_evidence_to_requirement", {"decision_id": str(dec), "document_id": str(doc), "page_no": 1,
                                                                 "excerpt": "Manually attached passage"}, allow_mutations=True)
    assert "unchanged" in out["note"]
    task = _call(db, seeded.contexts["bids@voltedge.example"], "create_remediation_task", {"tender_id": str(tid), "title": "Obtain renewed ISO 9001"},
                 allow_mutations=True)
    db.flush()
    assert task["task_id"]
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "tool.create_remediation_task"))
    assert db.scalar(select(ToolExecutionLog).where(ToolExecutionLog.status == "denied", ToolExecutionLog.user_id == viewer.user_id))
    db.rollback()


def test_task_collaboration_rules(seeded, db):
    from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
    from app.db.models import Notification
    from app.services.bids import service as bids

    bm = seeded.contexts["bids@voltedge.example"]
    ws = bids.create_workspace(db, bm, seeded.tenders["lt_panels"].id)
    contrib = seeded.contexts["docs@voltedge.example"]
    with pytest.raises(ValidationFailed):
        bids.create_task(db, bm, ws.id, title="  ")
    with pytest.raises(ValidationFailed):
        bids.create_task(db, bm, ws.id, title="x", priority="critical")
    with pytest.raises(ValidationFailed):  # assignee from another organization
        bids.create_task(db, bm, ws.id, title="x", assignee_id=seeded.contexts["owner@kaveri.example"].user_id)
    with pytest.raises(PermissionDenied):
        bids.create_task(db, seeded.contexts["viewer@voltedge.example"], ws.id, title="nope")
    with pytest.raises(NotFound):
        bids.create_task(db, seeded.contexts["owner@kaveri.example"], ws.id, title="cross-tenant")

    t = bids.create_task(db, bm, ws.id, title="Collect audited P&L FY 2024-25", priority="urgent", assignee_id=contrib.user_id,
                         due_date=date.today() - timedelta(days=1))
    db.flush()
    assert db.scalar(select(Notification).where(Notification.user_id == contrib.user_id, Notification.kind == "task_assigned"))
    assert bids.create_task(db, bm, ws.id, title="dup", dedup_key="k1").id == bids.create_task(db, bm, ws.id, title="dup", dedup_key="k1").id

    lv = t.lock_version
    t = bids.update_task(db, contrib, t.id, {"status": "in_progress"}, lv)
    db.flush()
    with pytest.raises(Conflict):  # stale lock version from a concurrent editor
        bids.update_task(db, bm, t.id, {"priority": "low"}, lv)
    with pytest.raises(ValidationFailed):
        bids.update_task(db, bm, t.id, {"workspace_id": ws.id}, t.lock_version)
    with pytest.raises(ValidationFailed):
        bids.update_task(db, bm, t.id, {"status": "finished"}, t.lock_version)
    t = bids.update_task(db, bm, t.id, {"status": "done"}, t.lock_version)
    assert t.completed_at and [h.get("event") or "update" for h in t.history] == ["created", "update", "update"]
    tasks = bids.list_tasks(db, bm, ws.id)
    assert tasks[-1].status == "done" or all(x.status == "done" for x in tasks)

    with pytest.raises(ValidationFailed):
        bids.add_comment(db, bm, ws.id, "   ")
    bids.add_comment(db, bm, ws.id, "Uploaded the draft P&L", task_id=t.id)
    bids.add_comment(db, bm, ws.id, "General note")
    db.flush()
    assert [c.body for c in bids.comments(db, bm, ws.id, task_id=t.id)] == ["Uploaded the draft P&L"]
    counts = {w.id: c for w, _, c in bids.list_workspaces(db, bm)}
    assert counts[ws.id]["done"] >= 1
    db.rollback()
