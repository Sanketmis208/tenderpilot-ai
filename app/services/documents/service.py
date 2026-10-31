"""Document vault: secure upload, versioning, listing, download authorisation, review actions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.permissions import Perm
from app.core.config import get_settings
from app.core.errors import NotFound, ServiceUnavailable, ValidationFailed
from app.core.security import sha256_bytes
from app.db.base import utcnow
from app.db.models import (
    Document,
    DocumentCategory,
    DocumentField,
    DocumentPage,
    DocumentScope,
    DocumentVersion,
    ProcessingJob,
    ProcessingStatus,
)
from app.extraction.parsers import filetype
from app.extraction.parsers.archive import extract_zip
from app.integrations.antivirus import ScanResult, get_scanner
from app.services import audit, jobs
from app.services.billing import entitlements
from app.storage.backend import assert_key_in_org, get_storage, make_key, sanitise_filename


@dataclass
class UploadResult:
    document: Document
    version: DocumentVersion
    job: ProcessingJob | None
    deduplicated: bool = False
    warnings: list[str] | None = None


def _validate(data: bytes, filename: str) -> tuple[filetype.FileIdentity, ScanResult]:
    s = get_settings()
    if len(data) > s.max_upload_bytes:
        raise ValidationFailed(f"File exceeds the {s.max_upload_bytes // (1024 * 1024)} MB limit.")
    ident = filetype.identify(data, filename)
    if findings := filetype.basic_threat_scan(data, ident.content_type):
        raise ValidationFailed("File rejected by security scan: " + "; ".join(findings))
    scan = get_scanner().scan(data)
    if scan.status == "infected":
        raise ValidationFailed(f"File rejected: the antivirus scan detected {scan.signature}.")
    if scan.status == "error" and s.clamav_fail_closed:
        raise ServiceUnavailable("The antivirus scanner is unavailable, so the upload was not accepted. Try again shortly.")
    return ident, scan


def upload(
    db: Session,
    ctx: TenantContext,
    data: bytes,
    filename: str,
    *,
    scope: str = DocumentScope.COMPANY,
    title: str | None = None,
    category: str | None = None,
    tender_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    source: str = "upload",
    source_url: str | None = None,
    is_synthetic: bool = False,
    process: bool = True,
    expand_archives: bool = True,
) -> list[UploadResult]:
    """Upload a file (ZIP archives expand into one document per supported member)."""
    ctx.require(Perm.DOCUMENTS_UPLOAD)
    ident, scan = _validate(data, filename)
    if ident.content_type == filetype.ZIP and expand_archives:
        if document_id:
            raise ValidationFailed("A ZIP archive cannot be a new version of a single document.")
        members, warnings = extract_zip(data)
        results: list[UploadResult] = []
        for m in members:
            try:
                results.extend(
                    upload(db, ctx, m.data, m.name, scope=scope, tender_id=tender_id, source="zip", is_synthetic=is_synthetic, process=process)
                )
            except ValidationFailed as exc:
                warnings.append(f"{m.name}: {exc.message}")
        if results:
            results[0].warnings = warnings
        elif warnings:
            raise ValidationFailed("No supported files in archive. " + "; ".join(warnings[:5]))
        return results
    entitlements.check_quota(db, ctx.organization_id, "storage_bytes", len(data))
    if process:
        entitlements.check_quota(db, ctx.organization_id, "documents_processed")
    digest = sha256_bytes(data)
    if document_id:
        doc = db.scalar(select(Document).where(Document.id == document_id, Document.organization_id == ctx.organization_id, Document.deleted_at.is_(None)))
        if not doc:
            raise NotFound("Document not found.")
        cur = db.get(DocumentVersion, doc.current_version_id) if doc.current_version_id else None
        if cur and cur.sha256 == digest:
            return [UploadResult(doc, cur, None, deduplicated=True)]
    else:
        doc = Document(
            organization_id=ctx.organization_id,
            scope=scope,
            title=(title or sanitise_filename(filename).rsplit(".", 1)[0].replace("_", " "))[:300],
            category=category or DocumentCategory.OTHER,
            category_confirmed=bool(category),
            category_confidence=1.0 if category else 0.0,
            tender_id=tender_id,
            created_by_id=ctx.user_id,
            is_synthetic=is_synthetic,
        )
        db.add(doc)
        db.flush()
    version_no = (db.scalar(select(func.max(DocumentVersion.version_no)).where(DocumentVersion.document_id == doc.id)) or 0) + 1
    key = make_key(ctx.organization_id, scope, filename)
    get_storage().put(key, data, ident.content_type)
    ver = DocumentVersion(
        organization_id=ctx.organization_id,
        document_id=doc.id,
        version_no=version_no,
        storage_key=key,
        original_filename=sanitise_filename(filename),
        content_type=ident.content_type,
        size_bytes=len(data),
        sha256=digest,
        uploaded_by_id=ctx.user_id,
        source=source,
        source_url=source_url,
    )
    db.add(ver)
    db.flush()
    doc.current_version_id = ver.id
    audit.record(db, "document.uploaded", ctx=ctx, subject_type="document", subject_id=doc.id,
                 data={"version": version_no, "sha256": digest, "filename": ver.original_filename, "size": len(data),
                       "antivirus": {"status": scan.status, "engine": scan.engine}})
    job = None
    if process:
        job = jobs.enqueue(
            db,
            kind="document.process",
            organization_id=ctx.organization_id,
            subject_type="document_version",
            subject_id=ver.id,
            idempotency_key=f"document.process:{ver.id}",
            created_by_id=ctx.user_id,
        )
    return [UploadResult(doc, ver, job)]


def list_documents(
    db: Session,
    ctx: TenantContext,
    *,
    scope: str | None = DocumentScope.COMPANY,
    category: str | None = None,
    q: str | None = None,
    tender_id: uuid.UUID | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[tuple[Document, DocumentVersion | None]], int]:
    ctx.require(Perm.DOCUMENTS_READ)
    base = select(Document).where(Document.organization_id == ctx.organization_id, Document.deleted_at.is_(None))
    if scope:
        base = base.where(Document.scope == scope)
    if category:
        base = base.where(Document.category == category)
    if tender_id:
        base = base.where(Document.tender_id == tender_id)
    if q:
        base = base.where(Document.title.ilike(f"%{q.strip()[:100]}%"))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    docs = db.scalars(base.order_by(Document.updated_at.desc()).offset(offset).limit(min(limit, 200))).all()
    versions = {
        v.id: v
        for v in db.scalars(select(DocumentVersion).where(DocumentVersion.id.in_([d.current_version_id for d in docs if d.current_version_id])))
    }
    return [(d, versions.get(d.current_version_id) if d.current_version_id else None) for d in docs], int(total)


def get_document(db: Session, ctx: TenantContext, document_id: uuid.UUID) -> Document:
    ctx.require(Perm.DOCUMENTS_READ)
    doc = db.scalar(select(Document).where(Document.id == document_id, Document.organization_id == ctx.organization_id, Document.deleted_at.is_(None)))
    if not doc:
        raise NotFound("Document not found.")
    return doc


def get_version(db: Session, ctx: TenantContext, version_id: uuid.UUID) -> DocumentVersion:
    ctx.require(Perm.DOCUMENTS_READ)
    v = db.scalar(select(DocumentVersion).where(DocumentVersion.id == version_id, DocumentVersion.organization_id == ctx.organization_id))
    if not v:
        raise NotFound("Document version not found.")
    return v


def versions(db: Session, ctx: TenantContext, document_id: uuid.UUID) -> list[DocumentVersion]:
    get_document(db, ctx, document_id)
    return list(db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document_id).order_by(DocumentVersion.version_no.desc())))


def pages(db: Session, ctx: TenantContext, version_id: uuid.UUID) -> list[DocumentPage]:
    get_version(db, ctx, version_id)
    return list(db.scalars(select(DocumentPage).where(DocumentPage.version_id == version_id).order_by(DocumentPage.page_no)))


def fields(db: Session, ctx: TenantContext, version_id: uuid.UUID) -> list[DocumentField]:
    get_version(db, ctx, version_id)
    return list(db.scalars(select(DocumentField).where(DocumentField.version_id == version_id).order_by(DocumentField.name)))


def download_url(db: Session, ctx: TenantContext, version_id: uuid.UUID, *, inline: bool = False) -> str:
    v = get_version(db, ctx, version_id)
    assert_key_in_org(v.storage_key, ctx.organization_id)
    audit.record(db, "document.download_url_issued", ctx=ctx, subject_type="document_version", subject_id=v.id)
    return get_storage().presigned_get(v.storage_key, v.original_filename)


def read_bytes(db: Session, ctx: TenantContext, version_id: uuid.UUID) -> tuple[bytes, DocumentVersion]:
    v = get_version(db, ctx, version_id)
    assert_key_in_org(v.storage_key, ctx.organization_id)
    return get_storage().get(v.storage_key), v


def confirm_category(db: Session, ctx: TenantContext, document_id: uuid.UUID, category: str) -> Document:
    ctx.require(Perm.DOCUMENTS_UPLOAD)
    if category not in {c.value for c in DocumentCategory}:
        raise ValidationFailed("Unknown category.")
    doc = get_document(db, ctx, document_id)
    changed = doc.category != category
    old = doc.category
    doc.category, doc.category_confirmed, doc.category_confidence = category, True, 1.0
    audit.record(db, "document.category_confirmed", ctx=ctx, subject_type="document", subject_id=doc.id, data={"from": old, "to": category})
    if changed and doc.current_version_id:
        # Re-run fact extraction under the corrected category (idempotent per category).
        jobs.enqueue(
            db,
            kind="document.process",
            organization_id=ctx.organization_id,
            subject_type="document_version",
            subject_id=doc.current_version_id,
            idempotency_key=f"document.process:{doc.current_version_id}:{category}",
            created_by_id=ctx.user_id,
        )
    return doc


def confirm_field(db: Session, ctx: TenantContext, field_id: uuid.UUID, corrected_value: str | None) -> DocumentField:
    ctx.require(Perm.DOCUMENTS_UPLOAD)
    f = db.scalar(select(DocumentField).where(DocumentField.id == field_id, DocumentField.organization_id == ctx.organization_id))
    if not f:
        raise NotFound("Field not found.")
    f.confirmed_by_id, f.confirmed_at, f.needs_confirmation = ctx.user_id, utcnow(), False
    if corrected_value is not None and corrected_value != f.value:
        f.corrected_value = corrected_value[:2000]
    audit.record(db, "document.field_confirmed", ctx=ctx, subject_type="document_field", subject_id=f.id)
    return f


def soft_delete(db: Session, ctx: TenantContext, document_id: uuid.UUID) -> None:
    ctx.require(Perm.DOCUMENTS_MANAGE)
    doc = get_document(db, ctx, document_id)
    doc.deleted_at = utcnow()
    audit.record(db, "document.deleted", ctx=ctx, subject_type="document", subject_id=doc.id)


def processing_summary(db: Session, ctx: TenantContext, version_ids: list[uuid.UUID]) -> dict[str, int]:
    rows = db.execute(
        select(DocumentVersion.processing_status, func.count())
        .where(DocumentVersion.organization_id == ctx.organization_id, DocumentVersion.id.in_(version_ids))
        .group_by(DocumentVersion.processing_status)
    ).all()
    return {s: n for s, n in rows} | {"total": len(version_ids), "pending_like": sum(n for s, n in rows if s in (ProcessingStatus.PENDING, ProcessingStatus.PROCESSING))}
