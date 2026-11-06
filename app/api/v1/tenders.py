"""Tenders, ingestion, requirements, matching, eligibility assessments and the evidence graph."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.deps import ctx_dep
from app.auth.permissions import Perm
from app.core import ratelimit
from app.core.config import get_settings
from app.db.session import get_db
from app.integrations import email_alerts
from app.integrations.sources import SOURCES
from app.schemas import api as s
from app.services.eligibility import service as elig
from app.services.evidence import graph as evgraph
from app.services.tenders import feeds as feedsvc
from app.services.tenders import lifecycle, matching
from app.services.tenders import service as tenders

router = APIRouter(tags=["tenders & eligibility"])


@router.post("/tenders", response_model=s.TenderOut, status_code=201)
def create(body: s.TenderIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return tenders.create_tender(db, ctx, body.model_dump(exclude_none=True))


@router.get("/tenders", response_model=s.Page[s.TenderOut])
def list_(q: str | None = None, status: list[str] | None = Query(None), state: str | None = None, min_value: Decimal | None = None,
          max_value: Decimal | None = None, closing_before: datetime | None = None, sort: str = "deadline", offset: int = Query(0, ge=0),
          limit: int = Query(25, ge=1, le=100), ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    rows, total = tenders.list_tenders(db, ctx, q=q, status=status, state=state, min_value=min_value, max_value=max_value,
                                       closing_before=closing_before, sort=sort, offset=offset, limit=limit)
    return s.Page(items=rows, total=total, offset=offset, limit=limit)


@router.get("/tenders/{tender_id}", response_model=s.TenderOut)
def get(tender_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return tenders.get_tender(db, ctx, tender_id)


@router.patch("/tenders/{tender_id}", response_model=s.TenderOut)
def patch(tender_id: uuid.UUID, body: s.TenderPatch, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return tenders.update_tender(db, ctx, tender_id, body.changes, body.lock_version)


@router.post("/tenders/{tender_id}/documents", status_code=201)
async def attach(tender_id: uuid.UUID, file: UploadFile = File(...), role: str = Form("main"), ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    from app.api.v1.documents import _read_upload

    t = tenders.get_tender(db, ctx, tender_id)
    results = tenders.attach_document(db, ctx, t, await _read_upload(file), file.filename or "tender", role)
    return [{"document_id": str(r.document.id), "version_id": str(r.version.id), "job_id": str(r.job.id) if r.job else None} for r in results]


@router.post("/tenders/import-url", status_code=201)
def import_url(body: s.UrlImportIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    t = tenders.get_tender(db, ctx, body.tender_id) if body.tender_id else None
    t, results = tenders.import_from_url(db, ctx, body.url, t)
    return {"tender_id": str(t.id), "jobs": [str(r.job.id) for r in results if r.job]}


@router.get("/tender-sources")
def sources():
    return [{"key": k, "name": v.name, "supported": v.supported, "note": v.status_note} for k, v in SOURCES.items()]


@router.post("/tender-sources/{key}/import", status_code=201)
async def import_feed(key: str, file: UploadFile | None = File(None), ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    payload = await file.read(5 * 1024 * 1024) if file else None
    created = tenders.import_feed(db, ctx, key, payload)
    return {"created": [str(t.id) for t in created]}


@router.get("/tender-feeds")
def list_feeds(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [feedsvc.feed_view(c) for c in feedsvc.list_feeds(db, ctx)]


@router.post("/tender-feeds", status_code=201)
def create_feed(body: s.TenderFeedIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    c = feedsvc.create_feed(db, ctx, name=body.name, url=body.url, fmt=body.format, poll_minutes=body.poll_minutes,
                            auth_header_name=body.auth_header_name, auth_header_value=body.auth_header_value,
                            licence_note=body.licence_note, field_map=body.field_map)
    return feedsvc.feed_view(c)


@router.post("/tender-feeds/{feed_id}/poll")
def poll_feed(feed_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    res = feedsvc.poll_feed(db, ctx, feed_id)
    return {**res.summary(), "tender_ids": [str(t.id) for t in res.created]}


@router.post("/tender-feeds/{feed_id}/pause")
def pause_feed(feed_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return feedsvc.feed_view(feedsvc.set_feed_enabled(db, ctx, feed_id, False))


@router.post("/tender-feeds/{feed_id}/resume")
def resume_feed(feed_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return feedsvc.feed_view(feedsvc.set_feed_enabled(db, ctx, feed_id, True))


@router.delete("/tender-feeds/{feed_id}", status_code=204)
def delete_feed(feed_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    feedsvc.delete_feed(db, ctx, feed_id)


@router.get("/email-alerts")
def email_alerts_settings(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return feedsvc.email_view(feedsvc.email_settings(db, ctx))


@router.post("/email-alerts/enable")
def enable_email_alerts(body: s.EmailAlertsIn, request: Request, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    """Creates or rotates the private inbound address. The token is shown once."""
    _c, token = feedsvc.enable_email_alerts(db, ctx, body.allowed_sender_domains)
    return {"inbound_url": f"{get_settings().base_url.rstrip('/')}/api/v1/inbound/email/{token}", "token_shown_once": True}


@router.post("/email-alerts/disable", status_code=204)
def disable_email_alerts(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    feedsvc.disable_email_alerts(db, ctx)


@router.post("/email-alerts/upload", status_code=201)
async def upload_alert_email(file: UploadFile = File(...), ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    raw = await file.read(email_alerts.MAX_EMAIL_BYTES + 1)
    alert, res = feedsvc.ingest_email(db, ctx, raw, channel="upload")
    return {"portal": alert.portal, **res.summary(), "tender_ids": [str(t.id) for t in res.created]}


@router.post("/inbound/email/{token}", include_in_schema=False)
async def inbound_email(token: str, request: Request, db: Session = Depends(get_db)):
    """Inbound-email webhook (unauthenticated; the unguessable token in the path is the credential).

    Accepts the raw RFC 822 message as the request body (``message/rfc822`` or ``text/plain``), or a multipart form
    with the raw message in a field named ``email``, ``body-mime`` or ``message`` (the shapes common inbound-mail
    services post)."""
    ratelimit.hit(f"inbound:{token[:12]}", 60)
    ctype = request.headers.get("content-type", "")
    raw = b""
    if ctype.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        form = await request.form()
        for key in ("email", "body-mime", "message"):
            v = form.get(key)
            if v is not None:
                raw = await v.read(email_alerts.MAX_EMAIL_BYTES + 1) if hasattr(v, "read") else str(v).encode()
                break
    else:
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > email_alerts.MAX_EMAIL_BYTES:
                break
    res = feedsvc.receive_inbound(db, token, raw)
    return {"accepted": True, **res.summary()}


@router.post("/tenders/{tender_id}/transition", response_model=s.TenderOut)
def transition(tender_id: uuid.UUID, body: s.TransitionIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    t = tenders.get_tender(db, ctx, tender_id)
    return lifecycle.transition(db, ctx, t, body.to, body.note)


@router.get("/tenders/{tender_id}/requirements", response_model=list[s.RequirementOut])
def requirements(tender_id: uuid.UUID, include_superseded: bool = False, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return tenders.requirements(db, ctx, tender_id, current_only=not include_superseded)


@router.post("/requirements/{requirement_id}/review", response_model=s.RequirementOut)
def review_requirement(requirement_id: uuid.UUID, body: s.RequirementReviewIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return tenders.review_requirement(db, ctx, requirement_id, body.action, body.changes, body.note)


@router.get("/tenders/{tender_id}/amendments")
def amendments(tender_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [{"id": str(a.id), "number": a.number, "status": a.status, "summary": a.summary, "from_version": a.from_version_no, "to_version": a.to_version_no,
             "field_changes": a.field_changes, "requirement_changes": a.requirement_changes, "impact": a.impact} for a in tenders.amendments(db, ctx, tender_id)]


@router.get("/opportunities")
def opportunities(recompute: bool = False, include_dismissed: bool = False, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    if recompute:
        matching.recompute(db, ctx)
    return [{"tender": s.TenderOut.model_validate(t).model_dump(mode="json"), "score": m.score, "breakdown": m.breakdown, "reasons": m.reasons,
             "user_action": m.user_action, "note": "Match relevance is not verified eligibility."} for m, t in matching.list_matches(db, ctx, include_dismissed=include_dismissed)]


@router.post("/opportunities/{tender_id}/action")
def opportunity_action(tender_id: uuid.UUID, action: str | None = Form(None), ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    m = matching.set_action(db, ctx, tender_id, action)
    return {"tender_id": str(tender_id), "user_action": m.user_action}


@router.post("/tenders/{tender_id}/assessments", response_model=s.AssessmentOut, status_code=202)
def start_assessment(tender_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    a, _job = elig.start_assessment(db, ctx, tender_id)
    return a


@router.get("/tenders/{tender_id}/assessments", response_model=list[s.AssessmentOut])
def assessments(tender_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return elig.list_assessments(db, ctx, tender_id)


@router.get("/assessments/{assessment_id}", response_model=s.ReportOut)
def assessment_report(assessment_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    rep = elig.report(db, ctx, assessment_id)
    return s.ReportOut(
        assessment=rep["assessment"], missing_documents=rep["missing_documents"],
        items=[s.ReportItemOut(requirement=it["requirement"], decision=it["decision"], links=it["links"],
                               conflicts=[{"id": str(c.id), "description": c.description, "resolved": bool(c.resolved_at)} for c in it["conflicts"]])
               for it in rep["items"]],
    )


@router.post("/decisions/{decision_id}/review", response_model=s.DecisionOut)
def review_decision(decision_id: uuid.UUID, body: s.DecisionReviewIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return elig.review_decision(db, ctx, decision_id, action=body.action, new_status=body.new_status, note=body.note)


@router.post("/conflicts/{conflict_id}/resolve", status_code=204)
def resolve_conflict(conflict_id: uuid.UUID, body: s.CompleteReviewIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    elig.resolve_conflict(db, ctx, conflict_id, body.note or "Resolved")


@router.post("/assessments/{assessment_id}/complete-review", response_model=s.AssessmentOut)
def complete_review(assessment_id: uuid.UUID, body: s.CompleteReviewIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    """Resume the paused LangGraph workflow (human-in-the-loop) after reviewers resolved findings."""
    ctx.require(Perm.ELIGIBILITY_REVIEW)
    from app.ai.workflows.tender_analysis import resume_tender_analysis

    a = elig.get_assessment(db, ctx, assessment_id)
    resume_tender_analysis(db, a, ctx.user_id, body.note)
    from app.services import audit

    audit.record(db, "eligibility.review_completed", ctx=ctx, subject_type="eligibility_assessment", subject_id=a.id, data={"note": body.note})
    db.refresh(a)
    return a


@router.get("/assessments/{older}/compare/{newer}")
def compare(older: uuid.UUID, newer: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return elig.compare_assessments(db, ctx, older, newer)


@router.get("/assessments/{assessment_id}/graph")
def evidence_graph(assessment_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    """Requirements-to-Evidence graph (nodes + edges) for visualisation and integrations."""
    return evgraph.build(db, ctx, assessment_id)
