"""Company profile, document vault, uploads (multipart and presigned S3), jobs."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.deps import ctx_dep
from app.auth.permissions import Perm
from app.core.config import StorageBackend, get_settings
from app.core.errors import ValidationFailed
from app.db.models import CompanyProfile
from app.db.session import get_db
from app.schemas import api as s
from app.services import audit, jobs
from app.services.documents import service as documents
from app.storage.backend import get_storage, make_key

router = APIRouter(tags=["company & documents"])


@router.get("/company/profile", response_model=s.ProfileOut)
def get_profile(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    ctx.require(Perm.COMPANY_READ)
    return db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == ctx.organization_id))


@router.patch("/company/profile", response_model=s.ProfileOut)
def update_profile(body: s.ProfileIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    ctx.require(Perm.COMPANY_WRITE)
    prof = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == ctx.organization_id))
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(prof, k, v)
    audit.record(db, "company.profile_updated", ctx=ctx, subject_type="company_profile", subject_id=prof.id, data={"fields": sorted(body.model_dump(exclude_unset=True))})
    return prof


async def _read_upload(file: UploadFile) -> bytes:
    limit = get_settings().max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailed("File too large.")
    return data


@router.post("/documents", response_model=list[s.UploadOut], status_code=201)
async def upload_document(file: UploadFile = File(...), category: str | None = Form(None), title: str | None = Form(None),
                          document_id: uuid.UUID | None = Form(None), ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    data = await _read_upload(file)
    results = documents.upload(db, ctx, data, file.filename or "upload", category=category, title=title, document_id=document_id)
    return [s.UploadOut(document=r.document, version=r.version, job_id=r.job.id if r.job else None, deduplicated=r.deduplicated, warnings=r.warnings) for r in results]


@router.post("/documents/uploads/initiate")
def initiate_upload(filename: str = Form(...), content_type: str = Form(...), ctx: TenantContext = Depends(ctx_dep)):
    """Presigned direct-to-S3 upload (S3 backend only). Call /documents/uploads/complete afterwards."""
    ctx.require(Perm.DOCUMENTS_UPLOAD)
    if get_settings().storage_backend != StorageBackend.S3:
        raise ValidationFailed("Direct uploads require the S3 storage backend; use POST /documents instead.")
    key = make_key(ctx.organization_id, "staging", filename)
    return {"upload_url": get_storage().presigned_put(key, content_type), "key": key, "expires_in": get_settings().presign_ttl_seconds}


@router.post("/documents/uploads/complete", response_model=list[s.UploadOut], status_code=201)
def complete_upload(key: str = Form(...), filename: str = Form(...), category: str | None = Form(None),
                    ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    from app.storage.backend import assert_key_in_org

    assert_key_in_org(key, ctx.organization_id)
    if f"/staging/" not in key:
        raise ValidationFailed("Invalid staging key.")
    storage = get_storage()
    data = storage.get(key)  # re-validated server-side exactly like a multipart upload
    results = documents.upload(db, ctx, data, filename, category=category)
    storage.delete(key)
    return [s.UploadOut(document=r.document, version=r.version, job_id=r.job.id if r.job else None, deduplicated=r.deduplicated) for r in results]


@router.get("/documents", response_model=s.Page[s.DocumentOut])
def list_documents(scope: str | None = "company", category: str | None = None, q: str | None = None, offset: int = Query(0, ge=0),
                   limit: int = Query(50, ge=1, le=200), ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    rows, total = documents.list_documents(db, ctx, scope=scope, category=category, q=q, offset=offset, limit=limit)
    return s.Page(items=[d for d, _ in rows], total=total, offset=offset, limit=limit)


@router.get("/documents/{document_id}", response_model=s.DocumentOut)
def get_document(document_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return documents.get_document(db, ctx, document_id)


@router.get("/documents/{document_id}/versions", response_model=list[s.VersionOut])
def versions(document_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return documents.versions(db, ctx, document_id)


@router.get("/document-versions/{version_id}/pages")
def pages(version_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [{"page_no": p.page_no, "text": p.text, "blocks": p.blocks, "method": p.method, "width": p.width, "height": p.height}
            for p in documents.pages(db, ctx, version_id)]


@router.get("/document-versions/{version_id}/fields")
def fields(version_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [{"id": str(f.id), "name": f.name, "value": f.corrected_value or f.value, "confidence": f.confidence,
             "needs_confirmation": f.needs_confirmation, "confirmed": bool(f.confirmed_at)} for f in documents.fields(db, ctx, version_id)]


@router.get("/document-versions/{version_id}/download")
def download(version_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return {"url": documents.download_url(db, ctx, version_id), "expires_in": get_settings().presign_ttl_seconds}


@router.get("/document-versions/{version_id}/pages/{page_no}/image")
def page_image(version_id: uuid.UUID, page_no: int, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    data, ver = documents.read_bytes(db, ctx, version_id)
    from app.services.documents.render import render_page_png

    return Response(render_page_png(data, ver.content_type, page_no), media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@router.post("/documents/{document_id}/category", response_model=s.DocumentOut)
def confirm_category(document_id: uuid.UUID, body: s.CategoryIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return documents.confirm_category(db, ctx, document_id, body.category)


@router.post("/document-fields/{field_id}/confirm", status_code=204)
def confirm_field(field_id: uuid.UUID, body: s.FieldConfirmIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    documents.confirm_field(db, ctx, field_id, body.corrected_value)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    documents.soft_delete(db, ctx, document_id)


@router.get("/jobs/{job_id}", response_model=s.JobOut)
def job_status(job_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    """Poll asynchronous work (document processing, extraction, assessments) without holding a request open."""
    return jobs.get_job(db, ctx, job_id)


@router.post("/jobs/{job_id}/cancel", response_model=s.JobOut)
def cancel_job(job_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return jobs.cancel_job(db, ctx, job_id)
