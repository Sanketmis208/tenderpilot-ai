"""Bid preparation agent (LangGraph): drafts, verified summary, attachments, approval interrupt, resume, isolation."""

from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("seeded")


def test_unsupported_claims_detector():
    from app.ai.workflows.bid_preparation import unsupported_claims

    facts = "FY 2023-24: turnover ₹5,20,00,000 [Source: P&L]. ISO 9001 valid until 31 Mar 2027."
    assert unsupported_claims("Turnover of ₹5,20,00,000 and ISO 9001 certification.", facts) == []
    bad = unsupported_claims("We completed 14 projects worth ₹9,00,00,000 and hold ISO 14001.", facts)
    assert "14" in bad and any("9,00,00,000" in b for b in bad) and any("14001" in b for b in bad)


def test_bid_preparation_agent_interrupts_then_resumes(seeded, db):
    from app.ai.workflows import bid_preparation
    from app.core.errors import NotFound
    from app.db.models import AgentRun, BidApproval, BidDraft
    from app.services.bids import service as bids
    from app.services.eligibility import service as elig

    ctx = seeded.contexts["owner@kaveri.example"]
    tid = seeded.tenders["kaveri_wtp"].id
    elig.start_assessment(db, ctx, tid)
    db.commit()
    ws = bids.create_workspace(db, ctx, tid)
    db.commit()

    run, job = bid_preparation.start(db, ctx, ws.id)
    db.commit()  # dispatches the (eager) job
    db.expire_all()
    run = db.get(AgentRun, run.id)
    assert job is not None and run.status == "interrupted", run.error
    assert [h["node"] for h in run.history] == ["plan_structure", "draft_documents", "executive_summary", "attachments", "risk_check"]
    assert run.mode == "rules" and run.result["summary"]["mode"] == "rule_based"
    assert run.result["attachments"] and run.result["findings"]

    drafts = [d for d in bids.drafts(db, ctx, ws.id) if d.is_current]
    assert {d.generator for d in drafts} == {"agent"}
    tech = next(d for d in drafts if d.kind == "technical_response")
    assert tech.sections[0]["heading"] == "Executive Summary"
    assert tech.evidence_snapshot["agent_run_id"] == str(run.id)

    # A second start while paused returns the same run, never a duplicate.
    again, job2 = bid_preparation.start(db, ctx, ws.id)
    assert again.id == run.id and job2 is None

    # Other organizations cannot see or resume it.
    other = seeded.contexts["owner@voltedge.example"]
    with pytest.raises(NotFound):
        bid_preparation.latest_run(db, other, ws.id)
    with pytest.raises(NotFound):
        bid_preparation.resume(db, other, run.id, "submit")

    run = bid_preparation.resume(db, ctx, run.id, "keep_draft")
    assert run.status == "completed" and run.result["decision"] == "keep_draft"
    db.refresh(ws)
    assert ws.status == "draft"
    n_versions = len(db.scalars(select(BidDraft).where(BidDraft.workspace_id == ws.id)).all())

    run2, _ = bid_preparation.start(db, ctx, ws.id)
    db.commit()
    db.expire_all()
    assert db.get(AgentRun, run2.id).status == "interrupted"
    assert len(db.scalars(select(BidDraft).where(BidDraft.workspace_id == ws.id)).all()) == n_versions + 2  # one new version per kind
    run2 = bid_preparation.resume(db, ctx, run2.id, "submit", "Ready for review")
    db.refresh(ws)
    assert run2.status == "completed" and ws.status == "internal_review"
    assert db.scalar(select(BidApproval).where(BidApproval.workspace_id == ws.id, BidApproval.action == "submit_for_review"))


def test_bid_preparation_api_and_permissions(seeded, client):
    from app.ai.workflows import bid_preparation

    assert bid_preparation.build_graph().compile() is not None
    r = client.post("/api/v1/preparation/00000000-0000-0000-0000-000000000000/decision", json={"decision": "submit"})
    assert r.status_code == 401
