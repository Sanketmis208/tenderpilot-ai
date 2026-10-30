"""TenderPilot tool registry — the single authorised surface exposed to MCP clients and the copilot.

Each tool declares an argument model, a required permission, and whether it mutates state.
`call_tool` enforces, in order: identity present → organization active → permission → token scope
for mutations → argument validation → rate limit → execution → output minimisation → logging.
The organization is ALWAYS taken from the verified TenantContext, never from tool arguments.
Retrieved document text is returned inside `untrusted_text` fields so agents treat it as data.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.retrieval.search import SearchFilters, hybrid_search, retrieval_confidence
from app.auth.context import TenantContext
from app.auth.permissions import Perm
from app.core import ratelimit
from app.core.errors import AppError, PermissionDenied, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import (
    BidDraft,
    BidWorkspace,
    CompanyProduct,
    CompanyProfile,
    CompanyProject,
    Document,
    EligibilityDecision,
    FactStatus,
    Organization,
    RequirementEvidenceLink,
    ReviewEvent,
    Tender,
    TenderStatus,
    ToolExecutionLog,
)
from app.extraction.normalization.values import IST, format_inr
from app.services import audit
from app.services.bids import service as bids
from app.services.eligibility import rules
from app.services.eligibility import service as elig
from app.services.evidence.gather import load_company_evidence, req_view
from app.services.tenders import service as tenders

log = get_logger(__name__)
WRITE_SCOPE = "mcp:write"
TOOL_RATE_PER_MIN = 60


class NoArgs(BaseModel):
    model_config = {"extra": "forbid"}


class SearchArgs(NoArgs):
    query: str = Field(min_length=2, max_length=500)
    category: str | None = None
    limit: int = Field(6, ge=1, le=20)


class TenderArgs(NoArgs):
    tender_id: uuid.UUID


class RequirementArgs(NoArgs):
    requirement_id: uuid.UUID


class DocumentArgs(NoArgs):
    document_id: uuid.UUID


class CertArgs(NoArgs):
    standard: str | None = Field(None, max_length=40)
    on_date: date | None = None


class ProjectsArgs(NoArgs):
    query: str | None = Field(None, max_length=200)
    min_value_inr: float | None = Field(None, ge=0)


class DeadlineArgs(NoArgs):
    days: int = Field(7, ge=1, le=120)


class CompareArgs(NoArgs):
    tender_a: uuid.UUID
    tender_b: uuid.UUID


class TaskArgs(NoArgs):
    tender_id: uuid.UUID
    title: str = Field(min_length=3, max_length=300)
    description: str | None = Field(None, max_length=4000)
    priority: str = Field("medium", pattern="^(low|medium|high|urgent)$")


class AttachArgs(NoArgs):
    decision_id: uuid.UUID
    document_id: uuid.UUID
    page_no: int | None = Field(None, ge=1)
    excerpt: str = Field(min_length=3, max_length=2000)


class DraftArgs(NoArgs):
    tender_id: uuid.UUID
    heading: str = Field(min_length=2, max_length=200)
    body: str = Field(min_length=2, max_length=20000)


class ReviewArgs(NoArgs):
    decision_id: uuid.UUID
    note: str = Field(min_length=3, max_length=2000)


@dataclass
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    perm: Perm
    fn: Callable[[Session, TenantContext, Any], Any]
    mutation: bool = False


def _untrusted(text: str | None, limit: int = 800) -> dict[str, str]:
    return {"untrusted_text": (text or "")[:limit]}


def _dt(v) -> str | None:
    return v.astimezone(IST).strftime("%d %b %Y %H:%M IST") if v else None


# ---------------- read tools ----------------

def t_search_documents(db: Session, ctx: TenantContext, a: SearchArgs):
    hits = hybrid_search(db, ctx, a.query, filters=SearchFilters(scope="company", categories=[a.category] if a.category else None), limit=a.limit)
    return {"retrieval_confidence": retrieval_confidence(hits), "results": [
        {"document_id": str(h.document_id), "document_version_id": str(h.version_id), "document": h.document_title, "category": h.category,
         "page": h.page_no, "section": h.section, "score": round(h.score, 3), **_untrusted(h.text)} for h in hits]}


def t_company_profile(db: Session, ctx: TenantContext, a: NoArgs):
    p = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == ctx.organization_id))
    if not p:
        return {"profile": None}
    return {"profile": {"legal_name": p.legal_name, "industry": p.industry, "state": p.state, "operational_states": p.operational_states,
                        "year_established": p.year_established, "msme_category": p.msme_category, "capabilities": p.capabilities,
                        "product_categories": p.product_categories, "gstin_on_file": bool(p.gstin), "udyam_on_file": bool(p.udyam_number)},
            "note": "Identifiers are as stated in uploaded documents and are not verified against government registries."}


def t_tender_details(db: Session, ctx: TenantContext, a: TenderArgs):
    t = tenders.get_tender(db, ctx, a.tender_id)
    return {"tender": {"id": str(t.id), "title": t.title, "reference": t.reference_number, "procuring_organization": t.procuring_organization,
                       "status": t.status, "estimated_value": format_inr(t.estimated_value) if t.estimated_value else None,
                       "submission_deadline": _dt(t.submission_deadline), "clarification_deadline": _dt(t.clarification_deadline),
                       "emd": format_inr(t.emd_amount) if t.emd_amount else None, "location": t.location_state, "version": t.current_version_no,
                       "risk_clauses": [(c or "")[:300] for c in (t.extraction or {}).get("risk_clauses", [])[:8]]}}


def t_requirements(db: Session, ctx: TenantContext, a: TenderArgs):
    return {"requirements": [
        {"id": str(r.id), "code": r.code, "type": r.req_type, "title": r.title, "mandatory": r.mandatory,
         "threshold": str(r.threshold_value) if r.threshold_value is not None else None, "unit": r.threshold_unit, "period_years": r.period_years,
         "conditions": [(c or "")[:400] for c in r.conditions], "source_page": r.source_page, "review_status": r.review_status,
         "extraction_confidence": r.extraction_confidence, **_untrusted(r.source_text, 500)}
        for r in tenders.requirements(db, ctx, a.tender_id)]}


def t_supporting_evidence(db: Session, ctx: TenantContext, a: RequirementArgs):
    from app.db.models import TenderRequirement

    r = db.scalar(select(TenderRequirement).where(TenderRequirement.id == a.requirement_id, TenderRequirement.organization_id == ctx.organization_id))
    if not r:
        raise ValidationFailed("Requirement not found.")
    latest = elig.latest_assessment(db, ctx, r.tender_id)
    links = []
    if latest:
        d = db.scalar(select(EligibilityDecision).where(EligibilityDecision.assessment_id == latest.id, EligibilityDecision.requirement_id == r.id))
        if d:
            links = [{"document": lk.label, "document_version_id": str(lk.document_version_id) if lk.document_version_id else None, "page": lk.page_no,
                      "relation": lk.relation, "origin": lk.origin, "confidence": lk.confidence, **_untrusted(lk.excerpt, 400)}
                     for lk in db.scalars(select(RequirementEvidenceLink).where(RequirementEvidenceLink.decision_id == d.id))]
            return {"requirement": r.title, "status": d.status, "explanation": d.explanation, "gaps": d.gaps, "evidence": links}
    hits = hybrid_search(db, ctx, f"{r.title} {r.source_text[:200]}", filters=SearchFilters(scope="company"), limit=5)
    return {"requirement": r.title, "status": "not_assessed", "evidence": [{"document": h.document_title, "page": h.page_no, "relation": "related",
                                                                          "origin": "retrieval", **_untrusted(h.text, 400)} for h in hits]}


def t_document_metadata(db: Session, ctx: TenantContext, a: DocumentArgs):
    from app.services.documents import service as docs

    d = docs.get_document(db, ctx, a.document_id)
    v = docs.get_version(db, ctx, d.current_version_id) if d.current_version_id else None
    return {"document": {"id": str(d.id), "title": d.title, "category": d.category, "category_confirmed": d.category_confirmed,
                         "version": v.version_no if v else None, "pages": v.page_count if v else None, "status": v.processing_status if v else None,
                         "issued_on": v.issued_on.isoformat() if v and v.issued_on else None, "expires_on": v.expires_on.isoformat() if v and v.expires_on else None,
                         "extraction_method": v.extraction_method if v else None, "quality_notes": v.quality_notes if v else []}}


def t_certificate_validity(db: Session, ctx: TenantContext, a: CertArgs):
    ev, _ = load_company_evidence(db, ctx.organization_id)
    on = a.on_date or utcnow().astimezone(IST).date()
    out = []
    for f in ev.certifications:
        if a.standard and a.standard.upper().replace(" ", "") not in (f.values.get("cert_type") or "").upper().replace(" ", ""):
            continue
        vu, vf = f.values.get("valid_until"), f.values.get("valid_from")
        state = "unknown" if not vu else ("valid" if (not vf or vf <= on) and on <= vu else ("expired" if vu < on else "not_yet_valid"))
        out.append({"standard": f.values["cert_type"], "certificate_number": f.values.get("certificate_number"), "valid_from": vf.isoformat() if vf else None,
                    "valid_until": vu.isoformat() if vu else None, "state_on_date": state, "document": f.label, "page": f.page})
    return {"on_date": on.isoformat(), "certificates": out, "note": "Document-stated validity; issuer authenticity not externally verified."}


def t_run_rules(db: Session, ctx: TenantContext, a: TenderArgs):
    """Dry run of the deterministic rule engine — returns results without persisting anything."""
    t = tenders.get_tender(db, ctx, a.tender_id)
    reqs = tenders.requirements(db, ctx, t.id)
    stds = sorted({s.strip() for r in reqs if r.req_type == "technical" for s in ((r.parameters or {}).get("standard") or "").split(";") if s.strip()})
    ev, _ = load_company_evidence(db, ctx.organization_id, stds)
    ref = elig.reference_date(t)
    results = []
    for r in reqs:
        res = rules.evaluate(req_view(r), ev, ref)
        results.append({"code": r.code, "title": r.title, "mandatory": r.mandatory, "status": res.status, "explanation": res.explanation, "gaps": res.gaps})
    score, overall, counts = rules.summarise([(x["status"], x["mandatory"], "") for x in results])
    return {"reference_date": ref.isoformat(), "overall": overall, "readiness_score": score, "counts": counts, "results": results,
            "note": "Dry run; not saved. Readiness score is explanatory and never overrides mandatory conditions."}


def t_bid_workspace(db: Session, ctx: TenantContext, a: TenderArgs):
    tenders.get_tender(db, ctx, a.tender_id)
    ws = db.scalar(select(BidWorkspace).where(BidWorkspace.tender_id == a.tender_id, BidWorkspace.organization_id == ctx.organization_id))
    if not ws:
        return {"workspace": None}
    tasks = bids.list_tasks(db, ctx, ws.id)
    return {"workspace": {"id": str(ws.id), "status": ws.status, "open_tasks": [{"title": t.title, "priority": t.priority, "status": t.status,
                                                                                 "due": t.due_date.isoformat() if t.due_date else None} for t in tasks if t.status != "done"][:30],
                          "drafts": [{"kind": d.kind, "version": d.version_no, "status": d.status} for d in bids.drafts(db, ctx, ws.id)]}}


def t_missing_documents(db: Session, ctx: TenantContext, a: TenderArgs):
    latest = elig.latest_assessment(db, ctx, a.tender_id)
    if not latest:
        return {"assessed": False, "missing": [], "note": "Run an eligibility assessment to identify missing documents."}
    rep = elig.report(db, ctx, latest.id)
    return {"assessed": True, "assessment_run": latest.run_no, "missing": rep["missing_documents"],
            "items": [{"code": it["requirement"].code, "status": it["decision"].status, "gaps": it["decision"].gaps} for it in rep["items"] if it["decision"].gaps]}


def t_products(db: Session, ctx: TenantContext, a: NoArgs):
    rows = db.scalars(select(CompanyProduct).where(CompanyProduct.organization_id == ctx.organization_id, CompanyProduct.status != FactStatus.SUPERSEDED)).all()
    return {"products": [{"name": p.name, "model_number": p.model_number, "source_page": p.source_page} for p in rows]}


def t_projects(db: Session, ctx: TenantContext, a: ProjectsArgs):
    q = select(CompanyProject).where(CompanyProject.organization_id == ctx.organization_id, CompanyProject.status != FactStatus.SUPERSEDED)
    rows = list(db.scalars(q))
    if a.min_value_inr is not None:
        rows = [p for p in rows if p.contract_value and float(p.contract_value) >= a.min_value_inr]
    if a.query:
        terms = set(a.query.lower().split())
        rows = sorted(rows, key=lambda p: -len(terms & set((p.title or "").lower().split())))
    return {"projects": [{"title": p.title, "client": p.client_name, "value": format_inr(p.contract_value) if p.contract_value else None,
                          "completed": p.completion_date.isoformat() if p.completion_date else None, "source_page": p.source_page} for p in rows[:20]]}


def t_deadlines(db: Session, ctx: TenantContext, a: DeadlineArgs):
    now = utcnow()
    rows = db.scalars(select(Tender).where(Tender.organization_id == ctx.organization_id, Tender.deleted_at.is_(None),
                                           Tender.submission_deadline > now, Tender.submission_deadline <= now + timedelta(days=a.days),
                                           Tender.status.notin_([TenderStatus.ARCHIVED, TenderStatus.CANCELLED])).order_by(Tender.submission_deadline)).all()
    return {"within_days": a.days, "tenders": [{"id": str(t.id), "title": t.title, "reference": t.reference_number, "deadline": _dt(t.submission_deadline),
                                                "status": t.status} for t in rows]}


def t_compare(db: Session, ctx: TenantContext, a: CompareArgs):
    out = []
    for tid in (a.tender_a, a.tender_b):
        t = tenders.get_tender(db, ctx, tid)
        latest = elig.latest_assessment(db, ctx, t.id)
        out.append({"title": t.title, "reference": t.reference_number, "value": format_inr(t.estimated_value) if t.estimated_value else None,
                    "deadline": _dt(t.submission_deadline), "requirements": len(tenders.requirements(db, ctx, t.id)),
                    "overall": latest.overall_status if latest else "not_assessed", "readiness": latest.readiness_score if latest else None})
    return {"comparison": out}


# ---------------- mutation tools ----------------

def m_create_task(db: Session, ctx: TenantContext, a: TaskArgs):
    tenders.get_tender(db, ctx, a.tender_id)
    ws = db.scalar(select(BidWorkspace).where(BidWorkspace.tender_id == a.tender_id, BidWorkspace.organization_id == ctx.organization_id))
    if not ws:
        raise ValidationFailed("Create a bid workspace for this tender first.")
    task = bids.create_task(db, ctx, ws.id, title=a.title, description=a.description, priority=a.priority, source="copilot",
                            dedup_key=f"tool:{ctx.user_id}:{a.title.lower()[:150]}")
    return {"task_id": str(task.id), "title": task.title}


def m_attach_evidence(db: Session, ctx: TenantContext, a: AttachArgs):
    ctx.require(Perm.ELIGIBILITY_REVIEW)
    d = db.scalar(select(EligibilityDecision).where(EligibilityDecision.id == a.decision_id, EligibilityDecision.organization_id == ctx.organization_id))
    doc = db.scalar(select(Document).where(Document.id == a.document_id, Document.organization_id == ctx.organization_id, Document.deleted_at.is_(None)))
    if not d or not doc:
        raise ValidationFailed("Decision or document not found.")
    link = RequirementEvidenceLink(organization_id=ctx.organization_id, decision_id=d.id, requirement_id=d.requirement_id, evidence_kind="document",
                                   evidence_ref_id=doc.id, document_id=doc.id, document_version_id=doc.current_version_id, page_no=a.page_no,
                                   excerpt=a.excerpt, relation="supports", confidence=0.5, origin="manual", label=doc.title, created_by_id=ctx.user_id)
    db.add(link)
    db.add(ReviewEvent(organization_id=ctx.organization_id, subject_type="decision", subject_id=d.id, action="evidence_attached", note=a.excerpt[:500], actor_id=ctx.user_id))
    db.flush()
    return {"link_id": str(link.id), "note": "Manual evidence recorded; the decision status is unchanged until a reviewer acts."}


def m_save_draft(db: Session, ctx: TenantContext, a: DraftArgs):
    ws = db.scalar(select(BidWorkspace).where(BidWorkspace.tender_id == a.tender_id, BidWorkspace.organization_id == ctx.organization_id))
    if not ws:
        raise ValidationFailed("Create a bid workspace for this tender first.")
    prev = db.scalar(select(BidDraft).where(BidDraft.workspace_id == ws.id, BidDraft.kind == "copilot_notes", BidDraft.is_current.is_(True)))
    sections = (prev.sections if prev else []) + [{"heading": a.heading, "body": a.body, "citations": [], "placeholders": [], "origin": "copilot"}]
    if prev:
        prev.is_current = False
    d = BidDraft(organization_id=ctx.organization_id, workspace_id=ws.id, kind="copilot_notes", title="Copilot notes & draft responses",
                 version_no=(prev.version_no + 1) if prev else 1, sections=sections, generator="llm", created_by_id=ctx.user_id)
    db.add(d)
    db.flush()
    return {"draft_id": str(d.id), "version": d.version_no}


def m_request_review(db: Session, ctx: TenantContext, a: ReviewArgs):
    d = db.scalar(select(EligibilityDecision).where(EligibilityDecision.id == a.decision_id, EligibilityDecision.organization_id == ctx.organization_id))
    if not d:
        raise ValidationFailed("Decision not found.")
    db.add(ReviewEvent(organization_id=ctx.organization_id, subject_type="decision", subject_id=d.id, action="review_requested", note=a.note, actor_id=ctx.user_id))
    from app.db.models import Role
    from app.services.notifications.service import notify_roles

    notify_roles(db, ctx.organization_id, {Role.REVIEWER, Role.BID_MANAGER}, kind="review_requested", title="Review requested on an eligibility finding",
                 body=a.note, link=None, dedup_key=f"review_req:{d.id}:{utcnow():%Y%m%d%H%M}")
    return {"requested": True}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("search_company_documents", "Hybrid search over the organization's own company documents.", SearchArgs, Perm.DOCUMENTS_READ, t_search_documents),
    Tool("get_company_profile", "Company profile summary (capabilities, geography, registrations on file).", NoArgs, Perm.COMPANY_READ, t_company_profile),
    Tool("get_tender_details", "Key fields, deadlines (IST) and risk clauses for a tender.", TenderArgs, Perm.TENDERS_READ, t_tender_details),
    Tool("get_tender_requirements", "Extracted requirements with source pages and review status.", TenderArgs, Perm.TENDERS_READ, t_requirements),
    Tool("search_supporting_evidence", "Evidence linked to a requirement in the latest assessment.", RequirementArgs, Perm.TENDERS_READ, t_supporting_evidence),
    Tool("get_document_metadata", "Metadata, validity dates and processing quality notes for a document.", DocumentArgs, Perm.DOCUMENTS_READ, t_document_metadata),
    Tool("get_certificate_validity", "Document-stated validity of certifications on a date.", CertArgs, Perm.COMPANY_READ, t_certificate_validity),
    Tool("run_eligibility_rules", "Dry-run the deterministic eligibility rules for a tender (not persisted).", TenderArgs, Perm.TENDERS_READ, t_run_rules),
    Tool("get_bid_workspace", "Bid workspace status, open tasks and drafts for a tender.", TenderArgs, Perm.BIDS_READ, t_bid_workspace),
    Tool("list_missing_documents", "Missing documents and gaps from the latest assessment.", TenderArgs, Perm.TENDERS_READ, t_missing_documents),
    Tool("get_catalogue_products", "Products extracted from the company's catalogues.", NoArgs, Perm.COMPANY_READ, t_products),
    Tool("list_relevant_past_projects", "Past projects with value and completion dates.", ProjectsArgs, Perm.COMPANY_READ, t_projects),
    Tool("list_upcoming_deadlines", "Tenders with submission deadlines within N days.", DeadlineArgs, Perm.TENDERS_READ, t_deadlines),
    Tool("compare_tenders", "Side-by-side comparison of two tenders.", CompareArgs, Perm.TENDERS_READ, t_compare),
    Tool("create_remediation_task", "Create a task in the tender's bid workspace.", TaskArgs, Perm.TASKS_WRITE, m_create_task, mutation=True),
    Tool("attach_evidence_to_requirement", "Attach a document passage as manual evidence to a decision.", AttachArgs, Perm.ELIGIBILITY_REVIEW, m_attach_evidence, mutation=True),
    Tool("save_draft_response", "Save a draft response section into the bid workspace.", DraftArgs, Perm.BIDS_WRITE, m_save_draft, mutation=True),
    Tool("request_human_review", "Ask reviewers to look at an eligibility finding.", ReviewArgs, Perm.TENDERS_READ, m_request_review, mutation=True),
]}


def call_tool(db: Session, ctx: TenantContext | None, name: str, arguments: dict[str, Any] | None, *, channel: str,
              agent_run_id: uuid.UUID | None = None, allow_mutations: bool = False) -> dict[str, Any]:
    if ctx is None:
        raise PermissionDenied("Authentication required.")
    tool = TOOLS.get(name)
    started = time.monotonic()
    status, error, result = "ok", None, None
    safe_args = {k: (str(v)[:200] if v is not None else None) for k, v in (arguments or {}).items()}
    try:
        if tool is None:
            raise ValidationFailed(f"Unknown tool '{name}'.")
        org = db.get(Organization, ctx.organization_id)
        if not org or org.status != "active":
            raise PermissionDenied("Organization is not active.")
        ctx.require(tool.perm)
        # Mutations need an explicit grant: a write-scoped token (external MCP) or an approved copilot action.
        if tool.mutation and not allow_mutations and not (ctx.scopes and (WRITE_SCOPE in ctx.scopes or "*" in ctx.scopes)):
            raise PermissionDenied(f"'{name}' modifies data and requires explicit approval or an mcp:write token scope.")
        try:
            args = tool.args.model_validate(arguments or {})
        except ValidationError as exc:
            raise ValidationFailed("Invalid tool arguments.", details=exc.errors(include_url=False, include_input=False))
        ratelimit.hit(f"tool:{ctx.user_id}", TOOL_RATE_PER_MIN)
        result = tool.fn(db, ctx, args)
        if tool.mutation:
            audit.record(db, f"tool.{name}", ctx=ctx, data={"channel": channel, "args": safe_args})
        return result
    except AppError as exc:
        status, error = ("denied" if isinstance(exc, PermissionDenied) else "rate_limited" if exc.code == "rate_limited" else "error"), exc.message
        raise
    except Exception as exc:
        status, error = "error", f"{type(exc).__name__}"
        log.error("tool.failed", tool=name, error=str(exc))
        raise
    finally:
        # Logged in an independent transaction so denied/failed calls are recorded even when the caller rolls back.
        from app.db.session import session_scope

        with session_scope() as ldb:
            ldb.add(ToolExecutionLog(organization_id=ctx.organization_id, user_id=ctx.user_id, agent_run_id=agent_run_id, tool=name, channel=channel,
                                arguments=safe_args, status=status, error=error, duration_ms=int((time.monotonic() - started) * 1000),
                                     is_mutation=bool(tool and tool.mutation)))
