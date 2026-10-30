"""Eligibility assessments: run, persist the evidence graph, review, report.

An assessment is immutable history: re-running creates run_no+1 and marks the previous run
superseded; earlier decisions, links and snapshots remain queryable.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.retrieval.search import SearchFilters, hybrid_search
from app.auth.context import TenantContext
from app.auth.permissions import Perm
from app.core.errors import NotFound, ValidationFailed
from app.db.base import utcnow
from app.db.models import (
    AssessmentStatus,
    DecisionStatus,
    EligibilityAssessment,
    EligibilityDecision,
    EvidenceConflict,
    ProcessingJob,
    RequirementEvidenceLink,
    ReviewEvent,
    Role,
    Tender,
    TenderRequirement,
    TenderStatus,
)
from app.extraction.normalization.values import IST
from app.services import audit, jobs
from app.services.billing import entitlements
from app.services.eligibility import rules
from app.services.evidence.gather import load_company_evidence, req_view
from app.services.jobs import handler

REVIEWABLE_TO = {s.value for s in DecisionStatus}


def reference_date(tender: Tender):
    """Validity is evaluated at the submission deadline (IST date); today if no deadline is known."""
    if tender.submission_deadline:
        return tender.submission_deadline.astimezone(IST).date()
    return datetime.now(IST).date()


def enqueue_assessment(db: Session, org_id: uuid.UUID, tender_id: uuid.UUID, user_id: uuid.UUID | None, *,
                       trigger: str = "manual", reason: str | None = None) -> tuple[EligibilityAssessment, ProcessingJob]:
    tender = db.get(Tender, tender_id)
    if not tender or tender.organization_id != org_id:
        raise NotFound("Tender not found.")
    n_req = db.scalar(select(func.count()).where(TenderRequirement.tender_id == tender_id, TenderRequirement.is_current.is_(True))) or 0
    if not n_req:
        raise ValidationFailed("No requirements have been extracted for this tender yet.")
    entitlements.check_quota(db, org_id, "tenders_analysed")
    run_no = (db.scalar(select(func.max(EligibilityAssessment.run_no)).where(EligibilityAssessment.tender_id == tender_id)) or 0) + 1
    prev = db.scalar(select(EligibilityAssessment).where(EligibilityAssessment.tender_id == tender_id).order_by(EligibilityAssessment.run_no.desc()))
    a = EligibilityAssessment(
        organization_id=org_id, tender_id=tender_id, tender_version_no=tender.current_version_no, run_no=run_no,
        trigger=trigger, triggered_by_id=user_id,
        reference_date=datetime.combine(reference_date(tender), datetime.min.time(), tzinfo=UTC),
        policy=rules.Policy().__dict__, previous_assessment_id=prev.id if prev else None,
        summary={"trigger_reason": reason} if reason else {},
    )
    db.add(a)
    db.flush()
    job = jobs.enqueue(db, kind="eligibility.assess", organization_id=org_id, subject_type="eligibility_assessment", subject_id=a.id,
                       idempotency_key=f"eligibility.assess:{a.id}", created_by_id=user_id)
    return a, job


def start_assessment(db: Session, ctx: TenantContext, tender_id: uuid.UUID) -> tuple[EligibilityAssessment, ProcessingJob]:
    ctx.require(Perm.ELIGIBILITY_RUN)
    a, job = enqueue_assessment(db, ctx.organization_id, tender_id, ctx.user_id)
    audit.record(db, "eligibility.assessment_started", ctx=ctx, subject_type="tender", subject_id=tender_id, data={"assessment_id": str(a.id)})
    return a, job


def _worker_ctx(a: EligibilityAssessment) -> TenantContext:
    # Read-only retrieval context scoped to the assessment's own organization.
    return TenantContext(user_id=a.triggered_by_id or uuid.UUID(int=0), organization_id=a.organization_id, role=Role.VIEWER, channel="worker")


def compute_decisions(db: Session, a: EligibilityAssessment, progress: Callable[[int, str], None] | None = None) -> dict[str, Any]:
    """Deterministic core used by the LangGraph eligibility node. Idempotent per assessment."""
    if db.scalar(select(func.count()).where(EligibilityDecision.assessment_id == a.id)):
        return {"skipped": "decisions exist"}
    reqs = list(db.scalars(select(TenderRequirement).where(TenderRequirement.tender_id == a.tender_id, TenderRequirement.is_current.is_(True)).order_by(TenderRequirement.code)))
    standards = sorted({s.strip() for r in reqs if r.req_type == "technical" for s in ((r.parameters or {}).get("standard") or "").split(";") if s.strip()})
    ev, snapshot = load_company_evidence(db, a.organization_id, standards)
    ref = a.reference_date.date()
    ctx = _worker_ctx(a)
    out: list[tuple[str, str, str]] = []
    for i, r in enumerate(reqs):
        if progress and i % 3 == 0:
            progress(20 + int(60 * i / max(len(reqs), 1)), f"evaluating {r.code}")
        res = rules.evaluate(req_view(r), ev, ref)
        d = EligibilityDecision(
            organization_id=a.organization_id, assessment_id=a.id, requirement_id=r.id, status=res.status, machine_status=res.status,
            rule_id=res.rule_id, explanation=res.explanation, computation=_json(res.computation), evidence_confidence=res.evidence_confidence,
            risk_level=res.risk, gaps=res.gaps,
        )
        db.add(d)
        db.flush()
        links = []
        for use in res.evidence:
            f = use.fact
            link = RequirementEvidenceLink(
                organization_id=a.organization_id, decision_id=d.id, requirement_id=r.id, evidence_kind=f.kind, evidence_ref_id=f.id,
                document_id=f.document_id, document_version_id=f.version_id, page_no=f.page, excerpt=(f.excerpt or "")[:2000], bbox=f.bbox,
                relation=use.relation, confidence=1.0 if f.confirmed else f.confidence, origin="rule", label=f.label[:300],
            )
            db.add(link)
            links.append(link)
        # Retrieval evidence: related passages for reviewer context (never used as proof by rules).
        if res.status in (DecisionStatus.NEEDS_HUMAN_REVIEW, DecisionStatus.INSUFFICIENT_EVIDENCE) or not res.evidence:
            seen_docs = {lk.document_id for lk in links}
            for h in hybrid_search(db, ctx, f"{r.title} {r.source_text[:300]}", filters=SearchFilters(scope="company"), limit=3):
                if h.document_id in seen_docs or h.signals.get("term_overlap", 0) < 0.25:
                    continue
                db.add(RequirementEvidenceLink(
                    organization_id=a.organization_id, decision_id=d.id, requirement_id=r.id, evidence_kind="chunk", evidence_ref_id=h.chunk_id,
                    document_id=h.document_id, document_version_id=h.version_id, page_no=h.page_no, excerpt=h.text[:600], bbox=h.bbox,
                    relation="related", confidence=round(min(1.0, h.signals["term_overlap"]), 2), origin="retrieval", label=h.document_title[:300],
                ))
        db.flush()
        if res.conflicts:
            db.add(EvidenceConflict(organization_id=a.organization_id, decision_id=d.id, description=" ".join(res.conflicts),
                                    evidence_link_ids=[str(lk.id) for lk in links if lk.relation == "contradicts"]))
        out.append((res.status, r.mandatory, res.risk))
    a.evidence_snapshot = snapshot
    _refresh_summary(db, a)
    _supersede_older(db, a)
    return {"decisions": len(out)}


def _supersede_older(db: Session, a: EligibilityAssessment) -> None:
    """Older runs stay queryable as history but are no longer the current assessment."""
    for p in db.scalars(select(EligibilityAssessment).where(EligibilityAssessment.tender_id == a.tender_id, EligibilityAssessment.run_no < a.run_no,
                                                            EligibilityAssessment.status != AssessmentStatus.SUPERSEDED)):
        p.status = AssessmentStatus.SUPERSEDED


def _json(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json(v) for v in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj if isinstance(obj, (str, int, float, bool, type(None))) else str(obj)


def _refresh_summary(db: Session, a: EligibilityAssessment) -> None:
    rows = db.execute(
        select(EligibilityDecision.status, TenderRequirement.mandatory, EligibilityDecision.risk_level, EligibilityDecision.review_state)
        .join(TenderRequirement, TenderRequirement.id == EligibilityDecision.requirement_id)
        .where(EligibilityDecision.assessment_id == a.id)
    ).all()
    score, overall, counts = rules.summarise([(s, m, r) for s, m, r, _ in rows])
    unresolved = sum(1 for s, m, _, rs in rows if s == DecisionStatus.NEEDS_HUMAN_REVIEW and rs == "pending")
    a.readiness_score, a.overall_status = score, overall
    a.summary = {**{k: v for k, v in (a.summary or {}).items() if k == "trigger_reason"}, **counts, "pending_reviews": unresolved,
                 "high_risk": sum(1 for _, _, r, _ in rows if r == "high")}


@handler("eligibility.assess")
def run_assessment_job(db: Session, job: ProcessingJob, progress: Callable[[int, str], None]) -> dict:
    from app.ai.workflows.tender_analysis import run_tender_analysis

    a = db.get(EligibilityAssessment, job.subject_id)
    if a is None or a.organization_id != job.organization_id:
        raise RuntimeError("Assessment not found")
    progress(5, "starting analysis workflow")
    result = run_tender_analysis(db, a, progress)
    entitlements.increment_usage(db, a.organization_id, "tenders_analysed")
    return result


def finalize(db: Session, a: EligibilityAssessment) -> None:
    _refresh_summary(db, a)
    _supersede_older(db, a)
    a.status, a.completed_at = AssessmentStatus.COMPLETED, utcnow()
    tender = db.get(Tender, a.tender_id)
    if tender and tender.status in (TenderStatus.READY_FOR_REVIEW,):
        from app.services.tenders import lifecycle

        lifecycle.transition(db, None, tender, TenderStatus.UNDER_EVALUATION, "Eligibility assessed", system=True)


# ---- read & review ----

def get_assessment(db: Session, ctx: TenantContext, assessment_id: uuid.UUID) -> EligibilityAssessment:
    ctx.require(Perm.TENDERS_READ)
    a = db.scalar(select(EligibilityAssessment).where(EligibilityAssessment.id == assessment_id, EligibilityAssessment.organization_id == ctx.organization_id))
    if not a:
        raise NotFound("Assessment not found.")
    return a


def latest_assessment(db: Session, ctx: TenantContext, tender_id: uuid.UUID) -> EligibilityAssessment | None:
    ctx.require(Perm.TENDERS_READ)
    return db.scalar(select(EligibilityAssessment).where(EligibilityAssessment.tender_id == tender_id, EligibilityAssessment.organization_id == ctx.organization_id)
                     .order_by(EligibilityAssessment.run_no.desc()))


def list_assessments(db: Session, ctx: TenantContext, tender_id: uuid.UUID) -> list[EligibilityAssessment]:
    ctx.require(Perm.TENDERS_READ)
    return list(db.scalars(select(EligibilityAssessment).where(EligibilityAssessment.tender_id == tender_id, EligibilityAssessment.organization_id == ctx.organization_id)
                           .order_by(EligibilityAssessment.run_no.desc())))


def report(db: Session, ctx: TenantContext, assessment_id: uuid.UUID) -> dict[str, Any]:
    """Full requirement-by-requirement report with evidence links, conflicts and review history."""
    a = get_assessment(db, ctx, assessment_id)
    rows = db.execute(
        select(EligibilityDecision, TenderRequirement).join(TenderRequirement, TenderRequirement.id == EligibilityDecision.requirement_id)
        .where(EligibilityDecision.assessment_id == a.id).order_by(TenderRequirement.code)
    ).all()
    dec_ids = [d.id for d, _ in rows]
    links: dict[uuid.UUID, list] = {}
    for lk in db.scalars(select(RequirementEvidenceLink).where(RequirementEvidenceLink.decision_id.in_(dec_ids))):
        links.setdefault(lk.decision_id, []).append(lk)
    conflicts: dict[uuid.UUID, list] = {}
    for c in db.scalars(select(EvidenceConflict).where(EvidenceConflict.decision_id.in_(dec_ids))):
        conflicts.setdefault(c.decision_id, []).append(c)
    reviews: dict[uuid.UUID, list] = {}
    for e in db.scalars(select(ReviewEvent).where(ReviewEvent.subject_type == "decision", ReviewEvent.subject_id.in_(dec_ids)).order_by(ReviewEvent.created_at)):
        reviews.setdefault(e.subject_id, []).append(e)
    items = []
    for d, r in rows:
        items.append({"decision": d, "requirement": r, "links": links.get(d.id, []), "conflicts": conflicts.get(d.id, []), "reviews": reviews.get(d.id, [])})
    missing_docs = sorted({g for it in items for g in it["decision"].gaps if g.lower().startswith(("upload", "prepare"))})
    return {"assessment": a, "items": items, "missing_documents": missing_docs}


def review_decision(db: Session, ctx: TenantContext, decision_id: uuid.UUID, *, action: str, new_status: str | None = None,
                    note: str | None = None) -> EligibilityDecision:
    """accept (agree with machine result) | override (set a different status, note required) | annotate."""
    ctx.require(Perm.ELIGIBILITY_REVIEW)
    d = db.scalar(select(EligibilityDecision).where(EligibilityDecision.id == decision_id, EligibilityDecision.organization_id == ctx.organization_id))
    if not d:
        raise NotFound("Decision not found.")
    a = db.get(EligibilityAssessment, d.assessment_id)
    if a and a.status == AssessmentStatus.SUPERSEDED:
        raise ValidationFailed("This assessment has been superseded; review the latest run instead.")
    before = d.status
    if action == "accept":
        d.review_state = "accepted"
    elif action == "override":
        if new_status not in REVIEWABLE_TO:
            raise ValidationFailed("Choose a valid status.")
        if not note or len(note.strip()) < 5:
            raise ValidationFailed("An override requires a justification note.")
        if d.machine_status == DecisionStatus.VERIFIED_NOT_SATISFIED and new_status == DecisionStatus.VERIFIED_SATISFIED and ctx.role not in (Role.OWNER, Role.ADMIN, Role.BID_MANAGER):
            raise ValidationFailed("Only a bid manager or administrator can override a failed mandatory check.")
        d.status, d.review_state = new_status, "overridden"
    elif action == "annotate":
        if not note:
            raise ValidationFailed("Note required.")
    else:
        raise ValidationFailed("Unknown action.")
    if note:
        d.reviewer_note = note[:4000]
    d.reviewed_by_id, d.reviewed_at = ctx.user_id, utcnow()
    db.add(ReviewEvent(organization_id=ctx.organization_id, subject_type="decision", subject_id=d.id, action=action,
                       from_value=before, to_value=d.status, note=note, actor_id=ctx.user_id))
    if a:
        _refresh_summary(db, a)
    audit.record(db, f"eligibility.decision_{action}", ctx=ctx, subject_type="decision", subject_id=d.id, data={"from": before, "to": d.status})
    return d


def resolve_conflict(db: Session, ctx: TenantContext, conflict_id: uuid.UUID, note: str) -> EvidenceConflict:
    ctx.require(Perm.ELIGIBILITY_REVIEW)
    c = db.scalar(select(EvidenceConflict).where(EvidenceConflict.id == conflict_id, EvidenceConflict.organization_id == ctx.organization_id))
    if not c:
        raise NotFound("Conflict not found.")
    c.resolved_at, c.resolved_by_id, c.resolution_note = utcnow(), ctx.user_id, note[:2000]
    audit.record(db, "eligibility.conflict_resolved", ctx=ctx, subject_type="conflict", subject_id=c.id)
    return c


def compare_assessments(db: Session, ctx: TenantContext, older_id: uuid.UUID, newer_id: uuid.UUID) -> list[dict[str, Any]]:
    """Which requirement outcomes changed between two runs (matched by requirement code)."""
    def by_code(aid):
        get_assessment(db, ctx, aid)
        return {r.code: d for d, r in db.execute(select(EligibilityDecision, TenderRequirement).join(TenderRequirement, TenderRequirement.id == EligibilityDecision.requirement_id)
                                                  .where(EligibilityDecision.assessment_id == aid)).all()}
    old, new = by_code(older_id), by_code(newer_id)
    out = []
    for code in sorted(set(old) | set(new)):
        o, n = old.get(code), new.get(code)
        if not o or not n or o.status != n.status:
            out.append({"code": code, "before": o.status if o else None, "after": n.status if n else None,
                        "explanation": n.explanation if n else None})
    return out
