"""Service-level integration: MCP in-process, copilot approval gate, BOQ export, storage, matching, analytics, notifications."""

from __future__ import annotations

import io
import json

import openpyxl
import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("seeded")


async def test_mcp_client_lists_and_calls_tools(seeded):
    from mcp import Client

    from app.mcp.server.app import current_identity, mcp_server

    current_identity.set(seeded.contexts["review@voltedge.example"])
    async with Client(mcp_server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"search_company_documents", "run_eligibility_rules", "create_remediation_task"} <= names
        res = await c.call_tool("run_eligibility_rules", {"tender_id": str(seeded.tenders["street_lighting"].id)})
        data = json.loads(res.content[0].text)
        assert data["results"] and "Dry run" in data["note"]


async def test_copilot_mutation_requires_approval(seeded, db):
    from app.ai.agents import copilot
    from app.db.models import BidWorkspace, CopilotMessage
    from app.services.bids import service as bids

    ctx = seeded.contexts["bids@voltedge.example"]
    tid = seeded.tenders["street_lighting"].id
    bids.create_workspace(db, ctx, tid)
    db.commit()
    cid = copilot.start_conversation(db, ctx, tid).id
    db.commit()
    mid = await copilot.ask(ctx, cid, "Draft a technical response using verified evidence")
    msg = db.get(CopilotMessage, mid)
    db.refresh(msg)
    idx = next(i for i, c in enumerate(msg.tool_calls) if c.get("status") == "pending_approval")
    ws = db.scalar(select(BidWorkspace).where(BidWorkspace.tender_id == tid))
    assert not bids.drafts(db, ctx, ws.id)  # nothing written before approval
    copilot.execute_pending_action(db, ctx, mid, idx)
    db.commit()
    assert any(d.kind == "copilot_notes" for d in bids.drafts(db, ctx, ws.id))
    with pytest.raises(Exception):
        copilot.execute_pending_action(db, ctx, mid, idx)  # cannot replay


def test_boq_import_scenario_and_export_preserves_template(seeded, db):
    from app.services.boq import service as boq
    from app.services.documents import service as documents

    ctx = seeded.contexts["bids@voltedge.example"]
    imp = boq.import_boq(db, ctx, seeded.tenders["lt_panels"].id)
    items = [i for i in boq.items(db, ctx, imp.id) if not (i.is_heading or i.is_subtotal)]
    rates = {str(i.id): "1000.50" for i in items}
    sc = boq.save_scenario(db, ctx, imp.id, "Test", rates, "10", "18")
    assert not sc.totals["complete"]  # earth pit has no quantity
    ver = boq.export_priced_copy(db, ctx, sc.id)
    db.commit()
    data, _ = documents.read_bytes(db, ctx, ver.id)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb[imp.sheet_name]
    first = items[0]
    assert ws[first.rate_cell].value == 1000.5
    assert str(ws[first.amount_cell].value).startswith("=")  # formulas untouched
    assert "TenderPilot_Notes" in wb.sheetnames
    orig, _ = documents.read_bytes(db, ctx, imp.document_version_id)
    assert openpyxl.load_workbook(io.BytesIO(orig))[imp.sheet_name][first.rate_cell].value is None  # original preserved


def test_matching_and_analytics(seeded, db):
    from app.services.analytics import service as analytics
    from app.services.tenders import matching

    ctx = seeded.contexts["bids@voltedge.example"]
    assert matching.recompute(db, ctx) >= 1
    matches = matching.list_matches(db, ctx)
    assert all(m.reasons for m, _ in matches)
    d = analytics.dashboard(db, ctx)
    assert d["kpis"]["tenders_total"] >= 2 and d["has_data"]


def test_fresh_org_dashboard_has_no_fabricated_metrics(db):
    from app.auth.context import TenantContext
    from app.services.analytics import service as analytics
    from app.services.organizations import auth_service, org_service

    u = auth_service.register_user(db, email="fresh@example.com", password="FreshPass123", full_name="Fresh User")
    org = org_service.create_organization(db, u, "Fresh Co")
    db.flush()
    d = analytics.dashboard(db, TenantContext(u.id, org.id, "owner"))
    assert not d["has_data"] and d["kpis"]["tenders_total"] == 0 and d["kpis"]["win_rate"] is None


def test_notifications_dedup_and_email_status(seeded, db):
    from app.services.notifications.service import notify

    ctx = seeded.contexts["owner@voltedge.example"]
    kw = dict(organization_id=ctx.organization_id, user_id=ctx.user_id, kind="deadline", title="t", dedup_key="dedup-test")
    assert notify(db, **kw) is True
    assert notify(db, **kw) is False
    from app.db.models import Notification

    n = db.scalar(select(Notification).where(Notification.dedup_key == "dedup-test"))
    assert n.email_status == "skipped_no_provider"  # never reported as delivered without a provider


def test_document_version_dedup_by_hash(seeded, db):
    from pathlib import Path

    from app.services.documents import service as documents

    ctx = seeded.contexts["docs@voltedge.example"]
    data = (Path(__file__).resolve().parents[2] / "sample_data/voltedge/gst_registration_certificate.pdf").read_bytes()
    first = documents.upload(db, ctx, data, "gst_copy.pdf")[0]
    db.commit()
    again = documents.upload(db, ctx, data, "gst_copy.pdf", document_id=first.document.id)[0]
    assert again.deduplicated and again.version.id == first.version.id
