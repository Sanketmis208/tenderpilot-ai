"""Tender management: manual creation, document import, URL import, feeds, edits, requirement review."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.auth.context import TenantContext
from app.auth.permissions import Perm
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db.base import utcnow
from app.db.models import (
    DocumentCategory,
    DocumentScope,
    ReviewEvent,
    ReviewStatus,
    Tender,
    TenderAmendment,
    TenderDocument,
    TenderRequirement,
    TenderStatus,
    TenderStatusEvent,
    TenderVersion,
)
from app.integrations import url_fetch
from app.integrations.sources import SOURCES
from app.services import audit
from app.services.billing import entitlements
from app.services.documents import service as documents
from app.services.tenders import lifecycle

EDITABLE_FIELDS = {
    "title", "reference_number", "procuring_organization", "source_platform", "source_url", "category",
    "estimated_value", "location_state", "publication_date", "prebid_meeting_at", "clarification_deadline",
    "submission_deadline", "bid_opening_at", "description", "emd_amount", "emd_text", "tender_fee",
    "performance_security_text", "keywords",
}


def create_tender(db: Session, ctx: TenantContext, data: dict[str, Any], *, is_synthetic: bool = False) -> Tender:
    ctx.require(Perm.TENDERS_WRITE)
    if not (data.get("title") or "").strip():
        raise ValidationFailed("Tender title is required.")
    ref = (data.get("reference_number") or "").strip() or None
    if ref and db.scalar(select(Tender.id).where(Tender.organization_id == ctx.organization_id, Tender.reference_number == ref, Tender.deleted_at.is_(None))):
        raise Conflict(f"A tender with reference {ref} already exists.")
    t = Tender(
        organization_id=ctx.organization_id,
        created_by_id=ctx.user_id,
        status=TenderStatus.IMPORTED,
        is_synthetic=is_synthetic,
        extraction={"manual_fields": sorted(k for k in data if k in EDITABLE_FIELDS and data[k] not in (None, ""))},
        **{k: v for k, v in data.items() if k in EDITABLE_FIELDS and k != "reference_number"},
    )
    t.reference_number = ref
    db.add(t)
    db.flush()
    db.add(TenderVersion(organization_id=ctx.organization_id, tender_id=t.id, version_no=1, reason="initial", snapshot=snapshot(t), created_by_id=ctx.user_id))
    db.add(TenderStatusEvent(organization_id=ctx.organization_id, tender_id=t.id, from_status=None, to_status=t.status, actor_id=ctx.user_id))
    audit.record(db, "tender.created", ctx=ctx, subject_type="tender", subject_id=t.id)
    return t


def snapshot(t: Tender) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in EDITABLE_FIELDS:
        v = getattr(t, f)
        out[f] = v.isoformat() if hasattr(v, "isoformat") else (str(v) if isinstance(v, Decimal) else v)
    return out


ROLE_CATEGORY = {"main": DocumentCategory.TENDER_NOTICE, "annexure": DocumentCategory.TENDER_ANNEXURE,
                 "boq": DocumentCategory.BOQ, "corrigendum": DocumentCategory.CORRIGENDUM}


def attach_document(db: Session, ctx: TenantContext, tender: Tender, data: bytes, filename: str, role: str = "main",
                    *, source: str = "upload", source_url: str | None = None, is_synthetic: bool = False):
    ctx.require(Perm.TENDERS_WRITE)
    if role not in ROLE_CATEGORY:
        raise ValidationFailed("Unknown document role.")
    if filename.lower().endswith((".xlsx",)) and role == "main":
        role = "boq"
    results = documents.upload(db, ctx, data, filename, scope=DocumentScope.TENDER, tender_id=tender.id,
                               category=ROLE_CATEGORY[role], source=source, source_url=source_url, is_synthetic=is_synthetic)
    for r in results:
        if not db.scalar(select(TenderDocument.id).where(TenderDocument.tender_id == tender.id, TenderDocument.document_id == r.document.id)):
            r_role = "boq" if r.version.content_type.endswith("spreadsheetml.sheet") else role
            if r_role != role:
                r.document.category = ROLE_CATEGORY[r_role]
            db.add(TenderDocument(organization_id=tender.organization_id, tender_id=tender.id, document_id=r.document.id,
                                  role=r_role, added_in_version_no=tender.current_version_no))
    if role == "corrigendum":
        for r in results:
            db.add(TenderAmendment(organization_id=tender.organization_id, tender_id=tender.id, document_id=r.document.id,
                                   from_version_no=tender.current_version_no, status="pending", created_by_id=ctx.user_id))
    elif tender.status in (TenderStatus.IMPORTED, TenderStatus.DISCOVERED, TenderStatus.READY_FOR_REVIEW):
        lifecycle.transition(db, ctx, tender, TenderStatus.PROCESSING, "Documents uploaded", system=True)
    entitlements.increment_usage(db, ctx.organization_id, "tenders_imported")
    return results


def import_from_url(db: Session, ctx: TenantContext, url: str, tender: Tender | None = None):
    ctx.require(Perm.TENDERS_WRITE)
    fetched = url_fetch.fetch(url)
    if tender is None:
        tender = create_tender(db, ctx, {"title": f"Imported from {url_fetch.urlsplit(url).hostname}", "source_url": url, "source_platform": "url_import"})
    return tender, attach_document(db, ctx, tender, fetched.content, fetched.filename, "main", source="url", source_url=fetched.url)


def import_feed(db: Session, ctx: TenantContext, source_key: str, payload: bytes | None) -> list[Tender]:
    ctx.require(Perm.TENDERS_WRITE)
    src = SOURCES.get(source_key)
    if not src:
        raise NotFound("Unknown source.")
    if source_key == "email_alert" and payload:
        from app.services.tenders import feeds

        return feeds.ingest_email(db, ctx, payload, channel="upload")[1].created
    from app.services.tenders.feeds import ingest

    res = ingest(db, ctx, src.fetch(payload), source_name=src.name)
    audit.record(db, "tender.feed_imported", ctx=ctx, data={"source": source_key, "count": len(res.created), "duplicates": res.duplicates})
    return res.created


def get_tender(db: Session, ctx: TenantContext, tender_id: uuid.UUID) -> Tender:
    ctx.require(Perm.TENDERS_READ)
    t = db.scalar(select(Tender).where(Tender.id == tender_id, Tender.organization_id == ctx.organization_id, Tender.deleted_at.is_(None)))
    if not t:
        raise NotFound("Tender not found.")
    return t


def list_tenders(db: Session, ctx: TenantContext, *, q: str | None = None, status: list[str] | None = None,
                 state: str | None = None, min_value: Decimal | None = None, max_value: Decimal | None = None,
                 closing_before: datetime | None = None, sort: str = "deadline", offset: int = 0, limit: int = 25) -> tuple[list[Tender], int]:
    ctx.require(Perm.TENDERS_READ)
    base = select(Tender).where(Tender.organization_id == ctx.organization_id, Tender.deleted_at.is_(None))
    if q:
        like = f"%{q.strip()[:100]}%"
        base = base.where(or_(Tender.title.ilike(like), Tender.reference_number.ilike(like), Tender.procuring_organization.ilike(like)))
    if status:
        base = base.where(Tender.status.in_(status))
    if state:
        base = base.where(Tender.location_state.ilike(f"%{state}%"))
    if min_value is not None:
        base = base.where(Tender.estimated_value >= min_value)
    if max_value is not None:
        base = base.where(Tender.estimated_value <= max_value)
    if closing_before:
        base = base.where(Tender.submission_deadline <= closing_before)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    order: Any = {
        "deadline": Tender.submission_deadline.asc().nulls_last(),
        "value": Tender.estimated_value.desc().nulls_last(),
        "newest": Tender.created_at.desc(),
        "title": Tender.title.asc(),
    }.get(sort, Tender.submission_deadline.asc().nulls_last())
    rows = db.scalars(base.order_by(order).offset(offset).limit(min(limit, 100))).all()
    return list(rows), int(total)


def update_tender(db: Session, ctx: TenantContext, tender_id: uuid.UUID, changes: dict[str, Any], expected_lock_version: int | None) -> Tender:
    ctx.require(Perm.TENDERS_WRITE)
    t = get_tender(db, ctx, tender_id)
    if expected_lock_version is not None and t.lock_version != expected_lock_version:
        raise Conflict("This tender was changed by someone else. Reload and try again.")
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise ValidationFailed(f"Fields not editable: {', '.join(sorted(unknown))}")
    for k, v in changes.items():
        setattr(t, k, v)
    t.extraction = {**(t.extraction or {}), "manual_fields": sorted(set((t.extraction or {}).get("manual_fields", [])) | set(changes))}
    try:
        db.flush()
    except StaleDataError:
        raise Conflict("This tender was changed by someone else. Reload and try again.")
    audit.record(db, "tender.updated", ctx=ctx, subject_type="tender", subject_id=t.id, data={"fields": sorted(changes)})
    return t


def tender_documents(db: Session, ctx: TenantContext, tender_id: uuid.UUID):
    get_tender(db, ctx, tender_id)
    from app.db.models import Document, DocumentVersion

    return db.execute(
        select(TenderDocument, Document, DocumentVersion)
        .join(Document, Document.id == TenderDocument.document_id)
        .outerjoin(DocumentVersion, DocumentVersion.id == Document.current_version_id)
        .where(TenderDocument.tender_id == tender_id, Document.deleted_at.is_(None))
        .order_by(TenderDocument.created_at)
    ).all()


def requirements(db: Session, ctx: TenantContext, tender_id: uuid.UUID, *, current_only: bool = True) -> list[TenderRequirement]:
    get_tender(db, ctx, tender_id)
    q = select(TenderRequirement).where(TenderRequirement.tender_id == tender_id, TenderRequirement.organization_id == ctx.organization_id)
    if current_only:
        q = q.where(TenderRequirement.is_current.is_(True))
    return list(db.scalars(q.order_by(TenderRequirement.code)))


def review_requirement(db: Session, ctx: TenantContext, requirement_id: uuid.UUID, action: str,
                       changes: dict[str, Any] | None = None, note: str | None = None) -> TenderRequirement:
    """confirm | edit | reject. Edits keep the original clause; only the interpretation changes."""
    ctx.require(Perm.ELIGIBILITY_REVIEW)
    r = db.scalar(select(TenderRequirement).where(TenderRequirement.id == requirement_id, TenderRequirement.organization_id == ctx.organization_id))
    if not r:
        raise NotFound("Requirement not found.")
    before = r.review_status
    if action == "confirm":
        r.review_status = ReviewStatus.CONFIRMED
    elif action == "reject":
        r.review_status = ReviewStatus.REJECTED
    elif action == "edit":
        allowed = {"title", "metric", "operator", "threshold_value", "threshold_unit", "period_years", "aggregation", "mandatory", "parameters", "conditions"}
        for k, v in (changes or {}).items():
            if k not in allowed:
                raise ValidationFailed(f"Cannot edit {k}.")
            if k == "threshold_value" and v not in (None, ""):
                v = Decimal(str(v))
            setattr(r, k, v)
        r.review_status = ReviewStatus.EDITED
    else:
        raise ValidationFailed("Unknown review action.")
    r.reviewed_by_id, r.reviewed_at = ctx.user_id, utcnow()
    db.add(ReviewEvent(organization_id=ctx.organization_id, subject_type="requirement", subject_id=r.id, action=action,
                       from_value=before, to_value=r.review_status, note=note, actor_id=ctx.user_id))
    audit.record(db, f"requirement.{action}", ctx=ctx, subject_type="requirement", subject_id=r.id)
    return r


def amendments(db: Session, ctx: TenantContext, tender_id: uuid.UUID) -> list[TenderAmendment]:
    get_tender(db, ctx, tender_id)
    return list(db.scalars(select(TenderAmendment).where(TenderAmendment.tender_id == tender_id).order_by(TenderAmendment.created_at)))


def status_history(db: Session, ctx: TenantContext, tender_id: uuid.UUID) -> list[TenderStatusEvent]:
    get_tender(db, ctx, tender_id)
    return list(db.scalars(select(TenderStatusEvent).where(TenderStatusEvent.tender_id == tender_id).order_by(TenderStatusEvent.created_at)))
