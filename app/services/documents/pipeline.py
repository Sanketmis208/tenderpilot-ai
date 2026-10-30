"""document.process job: parse → pages → chunks/embeddings → classify → metadata/facts → index.

Idempotent: re-running for the same version replaces that version's derived rows only.
Facts from earlier versions of the same document are marked superseded, never deleted, so
historical assessments keep pointing at the exact evidence they used.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.ai.retrieval.chunking import chunk_document
from app.ai.retrieval.embeddings import get_embedder
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import (
    CompanyCertification,
    CompanyFinancialRecord,
    CompanyProduct,
    CompanyProfile,
    CompanyProject,
    CompanyRegistration,
    Document,
    DocumentChunk,
    DocumentField,
    DocumentPage,
    DocumentScope,
    DocumentVersion,
    FactStatus,
    ProcessingJob,
    ProcessingStatus,
)
from app.extraction.classify import classify
from app.extraction.company_facts import extract_company_facts
from app.extraction.parsers.formats import parse
from app.extraction.parsers.types import ParsedDocument
from app.services.billing import entitlements
from app.services.jobs import handler
from app.storage.backend import get_storage

log = get_logger(__name__)

CATEGORY_REVIEW_THRESHOLD = 0.5
FACT_TABLES = (CompanyCertification, CompanyFinancialRecord, CompanyProject, CompanyProduct, CompanyRegistration)


def _clear_derived(db: Session, version_id) -> None:
    db.execute(delete(DocumentChunk).where(DocumentChunk.version_id == version_id))
    db.execute(delete(DocumentPage).where(DocumentPage.version_id == version_id))
    db.execute(delete(DocumentField).where(DocumentField.version_id == version_id))
    for model in FACT_TABLES:
        db.execute(delete(model).where(model.source_document_version_id == version_id))


def _reuse_identical(db: Session, ver: DocumentVersion) -> DocumentVersion | None:
    """Hash dedup: an identical, already-processed file in the same org lets us copy chunks/embeddings."""
    return db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.organization_id == ver.organization_id,
            DocumentVersion.sha256 == ver.sha256,
            DocumentVersion.id != ver.id,
            DocumentVersion.processing_status.in_([ProcessingStatus.READY, ProcessingStatus.NEEDS_REVIEW]),
        )
    )


def _store_pages(db: Session, ver: DocumentVersion, parsed: ParsedDocument) -> None:
    for p in parsed.pages:
        db.add(
            DocumentPage(
                organization_id=ver.organization_id,
                version_id=ver.id,
                page_no=p.page_no,
                text=p.text,
                blocks=[{"text": b.text, "bbox": b.bbox} for b in p.blocks][:2000],
                tables=p.tables[:20],
                width=p.width,
                height=p.height,
                method=p.method,
                ocr_confidence=p.ocr_confidence,
            )
        )


def _store_chunks(db: Session, ver: DocumentVersion, parsed: ParsedDocument, twin: DocumentVersion | None) -> int:
    embedder = get_embedder()
    if twin is not None:
        rows = db.scalars(select(DocumentChunk).where(DocumentChunk.version_id == twin.id, DocumentChunk.embedding_model == embedder.model_name)).all()
        if rows:
            for r in rows:
                db.add(
                    DocumentChunk(
                        organization_id=ver.organization_id,
                        document_id=ver.document_id,
                        version_id=ver.id,
                        chunk_index=r.chunk_index,
                        page_no=r.page_no,
                        section=r.section,
                        kind=r.kind,
                        text=r.text,
                        bbox=r.bbox,
                        token_count=r.token_count,
                        embedding=r.embedding,
                        embedding_model=r.embedding_model,
                    )
                )
            return len(rows)
    chunks = chunk_document(parsed)
    vectors = embedder.embed([(c.section or "") + "\n" + c.text for c in chunks]) if chunks else []
    for c, vec in zip(chunks, vectors, strict=True):
        db.add(
            DocumentChunk(
                organization_id=ver.organization_id,
                document_id=ver.document_id,
                version_id=ver.id,
                chunk_index=c.index,
                page_no=c.page_no,
                section=c.section,
                kind=c.kind,
                text=c.text,
                bbox=c.bbox,
                token_count=len(c.text) // 4,
                embedding=vec,
                embedding_model=embedder.model_name,
            )
        )
    return len(chunks)


def _supersede_previous_facts(db: Session, doc: Document, ver: DocumentVersion) -> None:
    for model in FACT_TABLES:
        db.execute(
            update(model)
            .where(
                model.document_id == doc.id,
                model.source_document_version_id != ver.id,
                model.status != FactStatus.SUPERSEDED,
            )
            .values(status=FactStatus.SUPERSEDED)
        )


def _persist_facts(db: Session, doc: Document, ver: DocumentVersion, parsed: ParsedDocument) -> int:
    fx = extract_company_facts(parsed, doc.category)
    ver.issued_on, ver.expires_on = fx.issued_on, fx.expires_on
    ver.identifiers = fx.identifiers
    for field in fx.fields:
        db.add(
            DocumentField(
                organization_id=ver.organization_id,
                version_id=ver.id,
                name=field["name"],
                value=field["value"],
                confidence=field["confidence"],
                needs_confirmation=field["confidence"] < 0.8,
            )
        )
    common = lambda f: {  # noqa: E731
        "organization_id": ver.organization_id,
        "document_id": doc.id,
        "source_document_version_id": ver.id,
        "source_page": f.page,
        "source_text": f.source_text[:4000],
        "source_bbox": f.bbox,
        "extraction_confidence": f.confidence,
    }
    if doc.scope != DocumentScope.COMPANY:
        return 0
    _supersede_previous_facts(db, doc, ver)
    n = 0
    for f in fx.facts:
        v = f.values
        if f.kind == "certification":
            db.add(CompanyCertification(**common(f), cert_type=v["cert_type"], certificate_number=v.get("certificate_number"),
                                        issuer=v.get("issuer"), scope=v.get("scope"), valid_from=v.get("valid_from"), valid_until=v.get("valid_until")))
        elif f.kind == "financial":
            db.add(CompanyFinancialRecord(**common(f), fiscal_year=v["fiscal_year"], metric=v["metric"], amount=Decimal(v["amount"]), audited=bool(v.get("audited"))))
        elif f.kind == "project":
            db.add(CompanyProject(**common(f), title=v["title"], client_name=v.get("client_name"), contract_value=v.get("contract_value"),
                                  start_date=v.get("start_date"), completion_date=v.get("completion_date"), description=v.get("description")))
        elif f.kind == "product":
            db.add(CompanyProduct(**common(f), name=v["name"], model_number=v.get("model_number"), category=doc.category))
        elif f.kind == "registration":
            db.add(CompanyRegistration(**common(f), kind=v["kind"], identifier=v["identifier"], issued_on=fx.issued_on,
                                       valid_until=fx.expires_on, attributes=v.get("attributes", {})))
        n += 1
    _enrich_profile(db, ver, fx)
    return n


def _enrich_profile(db: Session, ver: DocumentVersion, fx) -> None:
    """Fill *empty* profile identifiers from documents; never overwrite user-entered values."""
    prof = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == ver.organization_id))
    if not prof:
        return
    ids = fx.identifiers
    if ids.get("gstin") and not prof.gstin:
        prof.gstin = ids["gstin"]
        if not prof.pan:
            prof.pan = ids["gstin"][2:12]  # PAN is embedded in GSTIN positions 3–12
    if ids.get("cin") and not prof.cin:
        prof.cin = ids["cin"]
        if not prof.year_established and ids["cin"][8:12].isdigit():
            prof.year_established = int(ids["cin"][8:12])
    if ids.get("udyam") and not prof.udyam_number:
        prof.udyam_number = ids["udyam"]
        for f in fx.facts:
            cat = (f.values.get("attributes") or {}).get("msme_category")
            if cat and not prof.msme_category:
                prof.msme_category = cat


@handler("document.process")
def process_document(db: Session, job: ProcessingJob, progress: Callable[[int, str], None]) -> dict:
    ver = db.get(DocumentVersion, job.subject_id)
    if ver is None or ver.organization_id != job.organization_id:
        raise RuntimeError("Document version not found for job.")
    doc = db.get(Document, ver.document_id)
    assert doc is not None
    ver.processing_status = ProcessingStatus.PROCESSING
    db.commit()
    progress(10, "reading file")
    data = get_storage().get(ver.storage_key)
    try:
        parsed = parse(data, ver.content_type)
    except ValueError as exc:
        ver.processing_status, ver.processing_error = ProcessingStatus.FAILED, str(exc)[:2000]
        db.commit()
        raise
    _clear_derived(db, ver.id)
    progress(35, "extracting text and layout")
    _store_pages(db, ver, parsed)
    ver.page_count = len(parsed.pages)
    ver.extraction_method, ver.ocr_used, ver.quality_notes = parsed.method, parsed.ocr_used, parsed.quality_notes
    ver.language, ver.language_confidence = parsed.language, parsed.language_confidence
    progress(55, "indexing for search")
    n_chunks = _store_chunks(db, ver, parsed, _reuse_identical(db, ver))
    progress(75, "classifying")
    cls = classify(parsed.full_text, ver.original_filename)
    ver.detected_category = cls.category
    if not doc.category_confirmed:
        doc.category, doc.category_confidence = cls.category, cls.confidence
    progress(85, "extracting facts")
    n_facts = _persist_facts(db, doc, ver, parsed)
    needs_review = (
        (not doc.category_confirmed and cls.confidence < CATEGORY_REVIEW_THRESHOLD)
        or bool(parsed.quality_notes and any("NOT extracted" in q for q in parsed.quality_notes))
        or (parsed.language in ("hi", "mixed"))
    )
    if parsed.language in ("hi", "mixed"):
        ver.quality_notes = [*ver.quality_notes, "Hindi/mixed-language content detected: rule-based extraction is English-only; review extracted fields."]
    ver.needs_review = needs_review
    ver.processing_status = ProcessingStatus.NEEDS_REVIEW if needs_review else ProcessingStatus.READY
    ver.processed_at = utcnow()
    entitlements.increment_usage(db, ver.organization_id, "documents_processed")
    db.flush()
    if doc.scope == DocumentScope.TENDER and doc.tender_id:
        from app.services.tenders.pipeline import maybe_start_extraction

        maybe_start_extraction(db, doc.tender_id, job.created_by_id)
    return {"pages": len(parsed.pages), "chunks": n_chunks, "facts": n_facts, "category": cls.category,
            "category_confidence": cls.confidence, "needs_review": needs_review, "processed_on": date.today().isoformat()}
