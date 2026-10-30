"""tender.extract and tender.amendment jobs."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.ai.llm.client import LLMRefused, get_llm
from app.core.config import AppMode, get_settings
from app.core.errors import ServiceUnavailable
from app.core.logging import get_logger
from app.db.models import (
    BidStatus,
    BidTask,
    BidWorkspace,
    Document,
    DocumentPage,
    DocumentVersion,
    EligibilityDecision,
    ProcessingJob,
    ProcessingStatus,
    Role,
    Tender,
    TenderAmendment,
    TenderDocument,
    TenderRequirement,
    TenderStatus,
    TenderVersion,
)
from app.extraction import tender_rules
from app.extraction.parsers.types import ParsedBlock, ParsedDocument, ParsedPage
from app.schemas.extraction import ExtractedRequirement, TenderFields
from app.services import audit, jobs
from app.services.jobs import handler
from app.services.notifications.service import notify_roles
from app.services.tenders import lifecycle
from app.services.tenders.service import snapshot

log = get_logger(__name__)


def load_parsed(db: Session, version_id: uuid.UUID) -> ParsedDocument:
    """Rebuild a ParsedDocument from stored pages (no re-parse, no re-OCR)."""
    pages = db.scalars(select(DocumentPage).where(DocumentPage.version_id == version_id).order_by(DocumentPage.page_no)).all()
    return ParsedDocument(
        [ParsedPage(p.page_no, p.text, [ParsedBlock(b["text"], b.get("bbox")) for b in p.blocks or []], p.tables or [], p.width, p.height, p.method, p.ocr_confidence) for p in pages],
        method="stored",
    )


def identity_key(r: ExtractedRequirement | TenderRequirement) -> str:
    if isinstance(r, TenderRequirement):
        std = (r.parameters or {}).get("standard")
        kind = (r.parameters or {}).get("document_kind")
        rtype, metric = r.req_type, r.metric
    else:
        std, kind, rtype, metric = r.standard, r.document_kind, r.req_type, r.metric
    if rtype in ("certification", "registration", "technical", "geographic"):
        return f"{rtype}:{(std or '').lower()}"
    if rtype == "document":
        return f"document:{kind or metric}"
    return f"{rtype}:{metric or ''}"


def _fingerprint(r: ExtractedRequirement) -> str:
    basis = f"{identity_key(r)}|{r.threshold_value}|{r.period_years}|{r.mandatory}|{re.sub(r'[^a-z0-9]', '', r.source_quote.lower())}"
    return hashlib.sha256(basis.encode()).hexdigest()


def _to_model(r: ExtractedRequirement, *, tender: Tender, version_no: int, code: str, source_version: DocumentVersion,
              parsed: ParsedDocument, method: str) -> TenderRequirement:
    bbox = None
    page = next((p for p in parsed.pages if p.page_no == r.source_page), None)
    if page:
        probe = re.sub(r"\s+", " ", r.source_quote[:60]).strip()
        for b in page.blocks:
            if probe and probe[:40] in re.sub(r"\s+", " ", b.text):
                bbox = b.bbox
                break
    params: dict[str, Any] = {"identity": identity_key(r)}
    if r.standard:
        params["standard"] = r.standard
    if r.document_kind:
        params["document_kind"] = r.document_kind
    if r.count_required:
        params["count_required"] = r.count_required
    if r.keywords:
        params["keywords"] = r.keywords
    try:
        threshold = Decimal(r.threshold_value) if r.threshold_value not in (None, "") else None
    except Exception:
        threshold = None
    return TenderRequirement(
        organization_id=tender.organization_id,
        tender_id=tender.id,
        tender_version_no=version_no,
        code=code,
        req_type=r.req_type,
        title=r.title[:400],
        metric=r.metric,
        operator=r.operator,
        threshold_value=threshold,
        threshold_unit=r.threshold_unit,
        period_years=r.period_years,
        aggregation=r.aggregation,
        mandatory=r.mandatory,
        conditions=r.conditions,
        parameters=params,
        source_document_version_id=source_version.id,
        source_page=r.source_page,
        source_text=r.source_quote[:4000],
        source_bbox=bbox,
        extraction_confidence=r.confidence,
        extraction_method=method,
        ambiguity_notes=r.ambiguity_notes,
        fingerprint=_fingerprint(r),
    )


def _extract(db: Session, tender: Tender, parsed: ParsedDocument, run_budget=None) -> tuple[TenderFields, list[ExtractedRequirement], dict]:
    """Rules first (cheap, deterministic); LLM adds what rules missed when configured."""
    fields = tender_rules.extract_fields(parsed)
    reqs = tender_rules.extract_requirements(parsed)
    meta: dict[str, Any] = {"method": "rules", "rule_requirements": len(reqs)}
    s = get_settings()
    llm = get_llm()
    if llm is None:
        if s.mode == AppMode.INTEGRATED:
            meta["llm_status"] = "unavailable"
            meta["warning"] = "LLM provider not configured: extraction used deterministic rules only."
        return fields, reqs, meta
    try:
        from app.extraction.tender_llm import extract_with_llm

        result, info = extract_with_llm(llm, parsed, db=db, org_id=tender.organization_id, tender_id=tender.id, budget=run_budget)
        known = {identity_key(r) for r in reqs}
        added = [r for r in result.requirements if identity_key(r) not in known]
        for r in added:
            r.confidence = min(r.confidence, 0.75)  # LLM-only requirements always go to review
        reqs.extend(added)
        for name, value in result.fields.model_dump(exclude={"citations", "risk_clauses"}).items():
            if getattr(fields, name) is None and value is not None and name in result.fields.citations:
                setattr(fields, name, value)
                fields.citations[name] = result.fields.citations[name]
        fields.risk_clauses = list(dict.fromkeys(fields.risk_clauses + result.fields.risk_clauses))[:20]
        meta.update({"method": "rules+llm", "llm_added": len(added), **info})
    except (ServiceUnavailable, LLMRefused) as exc:
        meta["llm_status"] = f"failed: {exc}"
    return fields, reqs, meta


def _apply_fields(tender: Tender, fields: TenderFields, *, overwrite_manual: bool = False) -> list[dict]:
    manual = set((tender.extraction or {}).get("manual_fields", []))
    changes = []
    for name in ("reference_number", "title", "procuring_organization", "location_state", "emd_text", "performance_security_text"):
        v = getattr(fields, name)
        if v and (overwrite_manual or name not in manual) and getattr(tender, name) != v:
            changes.append({"field": name, "from": getattr(tender, name), "to": v})
            setattr(tender, name, v)
    for name in ("estimated_value", "emd_amount", "tender_fee"):
        v = tender_rules.amount(getattr(fields, name))
        if v is not None and (overwrite_manual or name not in manual) and getattr(tender, name) != v:
            changes.append({"field": name, "from": str(getattr(tender, name)) if getattr(tender, name) is not None else None, "to": str(v)})
            setattr(tender, name, v)
    if fields.publication_date and "publication_date" not in manual:
        tender.publication_date = date.fromisoformat(fields.publication_date)
    for name in ("prebid_meeting_at", "clarification_deadline", "submission_deadline", "bid_opening_at"):
        raw = getattr(fields, name)
        if raw and (overwrite_manual or name not in manual):
            v = datetime.fromisoformat(raw)
            old = getattr(tender, name)
            if old != v:
                changes.append({"field": name, "from": old.isoformat() if old else None, "to": v.isoformat()})
                setattr(tender, name, v)
    return changes


def maybe_start_extraction(db: Session, tender_id: uuid.UUID, user_id: uuid.UUID | None) -> ProcessingJob | None:
    """Called after each tender document finishes processing; starts extraction once all are done."""
    rows = db.execute(
        select(TenderDocument.role, DocumentVersion)
        .join(Document, Document.id == TenderDocument.document_id)
        .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
        .where(TenderDocument.tender_id == tender_id, Document.deleted_at.is_(None))
    ).all()
    if not rows or any(v.processing_status in (ProcessingStatus.PENDING, ProcessingStatus.PROCESSING) for _, v in rows):
        return None
    tender = db.get(Tender, tender_id)
    assert tender is not None
    pending = db.scalars(select(TenderAmendment).where(TenderAmendment.tender_id == tender_id, TenderAmendment.status == "pending")).all()
    for a in pending:
        jobs.enqueue(db, kind="tender.amendment", organization_id=tender.organization_id, subject_type="tender_amendment",
                     subject_id=a.id, idempotency_key=f"tender.amendment:{a.id}", created_by_id=user_id)
    main_versions = sorted(str(v.id) for role, v in rows if role in ("main", "annexure"))
    if not main_versions:
        return None
    key = hashlib.sha256("|".join(main_versions).encode()).hexdigest()[:16]
    return jobs.enqueue(db, kind="tender.extract", organization_id=tender.organization_id, subject_type="tender",
                        subject_id=tender_id, idempotency_key=f"tender.extract:{tender_id}:{key}", created_by_id=user_id)


def _source_docs(db: Session, tender: Tender, roles: tuple[str, ...]) -> list[DocumentVersion]:
    return list(db.scalars(
        select(DocumentVersion)
        .join(Document, Document.current_version_id == DocumentVersion.id)
        .join(TenderDocument, TenderDocument.document_id == Document.id)
        .where(TenderDocument.tender_id == tender.id, TenderDocument.role.in_(roles), Document.deleted_at.is_(None))
        .order_by(TenderDocument.created_at)
    ))


@handler("tender.extract")
def extract_tender(db: Session, job: ProcessingJob, progress: Callable[[int, str], None]) -> dict:
    tender = db.get(Tender, job.subject_id)
    if tender is None or tender.organization_id != job.organization_id:
        raise RuntimeError("Tender not found")
    version_no = tender.current_version_no
    # Idempotency: never re-extract requirements that an assessment already references.
    existing = db.scalars(select(TenderRequirement.id).where(TenderRequirement.tender_id == tender.id, TenderRequirement.tender_version_no == version_no)).all()
    if existing and db.scalar(select(func.count()).where(EligibilityDecision.requirement_id.in_(existing))):
        return {"skipped": "requirements already used by an assessment"}
    db.execute(delete(TenderRequirement).where(TenderRequirement.tender_id == tender.id, TenderRequirement.tender_version_no == version_no))
    all_reqs: list[TenderRequirement] = []
    merged_citations: dict[str, Any] = {}
    meta_all: list[dict] = []
    risk: list[str] = []
    seen_ids: set[str] = set()
    sources = _source_docs(db, tender, ("main", "annexure"))
    for i, ver in enumerate(sources):
        progress(20 + 60 * i // max(1, len(sources)), f"analysing {ver.original_filename}")
        parsed = load_parsed(db, ver.id)
        fields, reqs, meta = _extract(db, tender, parsed)
        meta_all.append({"document_version_id": str(ver.id), **meta})
        changes = _apply_fields(tender, fields)
        merged_citations.update({k: {**v.model_dump(), "document_version_id": str(ver.id)} for k, v in fields.citations.items() if k not in merged_citations})
        risk.extend(fields.risk_clauses)
        for r in reqs:
            ident = identity_key(r)
            if ident in seen_ids:
                continue
            seen_ids.add(ident)
            all_reqs.append(_to_model(r, tender=tender, version_no=version_no, code=f"R-{len(all_reqs) + 1:03d}",
                                      source_version=ver, parsed=parsed, method=meta["method"] if "llm" not in meta["method"] or r.confidence > 0.75 else "llm"))
        log.info("tender.fields_applied", tender_id=str(tender.id), changes=len(changes))
    for model in all_reqs:
        db.add(model)
    tender.extraction = {
        **(tender.extraction or {}),
        "citations": merged_citations,
        "risk_clauses": risk[:20],
        "runs": meta_all,
        "requirements_found": len(all_reqs),
    }
    tender.extraction_method = "+".join(sorted({m["method"] for m in meta_all})) or "rules"
    tv = db.scalar(select(TenderVersion).where(TenderVersion.tender_id == tender.id, TenderVersion.version_no == version_no))
    if tv:
        tv.snapshot = snapshot(tender)
    if tender.status == TenderStatus.PROCESSING:
        lifecycle.transition(db, None, tender, TenderStatus.READY_FOR_REVIEW, "Extraction complete", system=True)
    if job.created_by_id:
        from app.services.notifications.service import notify

        notify(db, organization_id=tender.organization_id, user_id=job.created_by_id, kind="tender_ready",
               title=f"Tender analysed: {tender.title[:120]}", body=f"{len(all_reqs)} requirements extracted and ready for review.",
               link=f"/tenders/{tender.id}", dedup_key=f"tender_ready:{tender.id}:{version_no}")
    return {"requirements": len(all_reqs), "documents": len(meta_all)}


@handler("tender.amendment")
def apply_amendment(db: Session, job: ProcessingJob, progress: Callable[[int, str], None]) -> dict:
    amend = db.get(TenderAmendment, job.subject_id)
    if amend is None or amend.organization_id != job.organization_id:
        raise RuntimeError("Amendment not found")
    if amend.status == "applied":
        return {"skipped": "already applied"}
    tender = db.get(Tender, amend.tender_id)
    assert tender is not None
    doc = db.get(Document, amend.document_id)
    ver = db.get(DocumentVersion, doc.current_version_id) if doc else None
    if not ver:
        raise RuntimeError("Corrigendum document missing")
    progress(20, "reading corrigendum")
    parsed = load_parsed(db, ver.id)
    fields, new_reqs, meta = _extract(db, tender, parsed)
    num = re.search(r"corrigendum\s*(?:no\.?)?\s*([\w-]+)", parsed.full_text, re.I)
    amend.number = num.group(1) if num else None
    old_version = tender.current_version_no
    new_version = old_version + 1
    # Corrigendum fields override even manually-entered values: the issuing authority's text wins.
    field_changes = _apply_fields(tender, fields, overwrite_manual=True)
    progress(50, "comparing requirements")
    current = {identity_key(r): r for r in db.scalars(select(TenderRequirement).where(TenderRequirement.tender_id == tender.id, TenderRequirement.is_current.is_(True)))}
    req_changes: list[dict] = []
    next_code = (db.scalar(select(func.count()).where(TenderRequirement.tender_id == tender.id)) or 0) + 1
    for r in new_reqs:
        ident = identity_key(r)
        old = current.get(ident)
        fp = _fingerprint(r)
        if old and old.fingerprint == fp:
            continue
        model = _to_model(r, tender=tender, version_no=new_version, code=old.code if old else f"R-{next_code:03d}",
                          source_version=ver, parsed=parsed, method=meta["method"])
        if not old:
            next_code += 1
        db.add(model)
        db.flush()
        if old:
            old.is_current, old.superseded_by_id = False, model.id
        req_changes.append({
            "change": "modified" if old else "added",
            "identity": ident,
            "code": model.code,
            "title": model.title,
            "mandatory": model.mandatory,
            "before": {"threshold": str(old.threshold_value) if old and old.threshold_value is not None else None,
                       "text": old.source_text if old else None, "requirement_id": str(old.id) if old else None},
            "after": {"threshold": str(model.threshold_value) if model.threshold_value is not None else None,
                      "text": model.source_text, "requirement_id": str(model.id)},
        })
    tender.current_version_no = new_version
    db.add(TenderVersion(organization_id=tender.organization_id, tender_id=tender.id, version_no=new_version, reason="corrigendum",
                         amendment_id=amend.id, snapshot=snapshot(tender), created_by_id=job.created_by_id))
    amend.field_changes, amend.requirement_changes = field_changes, req_changes
    amend.to_version_no, amend.status = new_version, "applied"
    amend.summary = f"{len(field_changes)} field change(s), {len(req_changes)} requirement change(s)."
    progress(75, "analysing impact")
    impact = _impact(db, tender, amend, req_changes, field_changes)
    amend.impact = impact
    audit.record(db, "tender.amendment_applied", organization_id=tender.organization_id, actor_id=job.created_by_id,
                 subject_type="tender", subject_id=tender.id, data={"amendment_id": str(amend.id), "to_version": new_version})
    notify_roles(db, tender.organization_id, {Role.OWNER, Role.ADMIN, Role.BID_MANAGER, Role.REVIEWER}, kind="amendment",
                 title=f"Corrigendum applied: {tender.title[:120]}", body=amend.summary, link=f"/tenders/{tender.id}?tab=amendments",
                 dedup_key=f"amendment:{amend.id}")
    return {"field_changes": len(field_changes), "requirement_changes": len(req_changes), **impact}


def _impact(db: Session, tender: Tender, amend: TenderAmendment, req_changes: list[dict], field_changes: list[dict]) -> dict:
    """Flag affected work and (policy permitting) reopen approvals; then re-run eligibility."""
    from app.db.models import EligibilityAssessment, Organization

    org = db.get(Organization, tender.organization_id)
    policy = (org.settings or {}).get("amendment_policy", "reopen_approvals_on_mandatory_change") if org else "reopen_approvals_on_mandatory_change"
    mandatory_changed = any(c["mandatory"] in ("mandatory", "conditional") for c in req_changes)
    ws = db.scalar(select(BidWorkspace).where(BidWorkspace.tender_id == tender.id))
    out: dict[str, Any] = {"mandatory_requirement_changed": mandatory_changed, "policy": policy, "tasks_created": 0, "approvals_reopened": False}
    if ws:
        for c in req_changes:
            key = f"amendment:{amend.id}:{c['identity']}"
            if not db.scalar(select(BidTask.id).where(BidTask.workspace_id == ws.id, BidTask.dedup_key == key)):
                db.add(BidTask(organization_id=ws.organization_id, workspace_id=ws.id, title=f"Review corrigendum change: {c['title'][:200]}",
                               description=f"Before: {c['before']['text'] or '—'}\nAfter: {c['after']['text']}", priority="high",
                               source="amendment", dedup_key=key, requirement_id=uuid.UUID(c["after"]["requirement_id"])))
                out["tasks_created"] += 1
        if any(f["field"] == "submission_deadline" for f in field_changes):
            key = f"amendment:{amend.id}:deadline"
            if not db.scalar(select(BidTask.id).where(BidTask.workspace_id == ws.id, BidTask.dedup_key == key)):
                db.add(BidTask(organization_id=ws.organization_id, workspace_id=ws.id, title="Submission deadline changed — update the bid plan",
                               priority="medium", source="amendment", dedup_key=key))
                out["tasks_created"] += 1
        if mandatory_changed and policy == "reopen_approvals_on_mandatory_change" and ws.status in (BidStatus.APPROVED, BidStatus.READY_FOR_SUBMISSION):
            ws.status = BidStatus.INTERNAL_REVIEW
            out["approvals_reopened"] = True
    latest = db.scalar(select(EligibilityAssessment).where(EligibilityAssessment.tender_id == tender.id).order_by(EligibilityAssessment.run_no.desc()))
    if latest:
        from app.services.eligibility.service import enqueue_assessment

        enqueue_assessment(db, tender.organization_id, tender.id, amend.created_by_id, trigger="amendment", reason=f"amendment:{amend.id}")
        out["reassessment_queued"] = True
    return out
