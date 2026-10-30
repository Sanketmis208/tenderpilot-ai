"""Documents, immutable versions, pages, chunks (pgvector) and extracted fields; processing jobs."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Computed, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin

EMBED_DIM = get_settings().embedding_dim


class DocumentCategory(StrEnum):
    COMPANY_REGISTRATION = "company_registration"
    GST_CERTIFICATE = "gst_certificate"
    MSME_REGISTRATION = "msme_registration"
    ISO_CERTIFICATE = "iso_certificate"
    FINANCIAL_STATEMENT = "financial_statement"
    TURNOVER_CERTIFICATE = "turnover_certificate"
    EXPERIENCE_CERTIFICATE = "experience_certificate"
    COMPLETION_CERTIFICATE = "completion_certificate"
    PURCHASE_ORDER = "purchase_order"
    PREVIOUS_BID = "previous_bid"
    PRODUCT_CATALOGUE = "product_catalogue"
    TECHNICAL_DATASHEET = "technical_datasheet"
    AUTHORIZATION_LETTER = "authorization_letter"
    OEM_CERTIFICATE = "oem_certificate"
    LEGAL_COMPLIANCE = "legal_compliance"
    TENDER_NOTICE = "tender_notice"
    TENDER_ANNEXURE = "tender_annexure"
    BOQ = "boq"
    CORRIGENDUM = "corrigendum"
    GENERATED = "generated"
    OTHER = "other"


class DocumentScope(StrEnum):
    COMPANY = "company"
    TENDER = "tender"
    BID = "bid"
    GENERATED = "generated"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"
    FAILED = "failed"


class Document(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "documents"

    scope: Mapped[str] = mapped_column(String(16), default=DocumentScope.COMPANY, index=True)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(40), default=DocumentCategory.OTHER, index=True)
    category_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    category_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    tender_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="SET NULL", use_alter=True)
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    tags: Mapped[list[Any]] = mapped_column(default=list)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None]

    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", foreign_keys="DocumentVersion.document_id", order_by="DocumentVersion.version_no"
    )


class DocumentVersion(IdMixin, TimestampMixin, TenantMixin, Base):
    """Immutable once processed. Assessments reference versions, never the mutable Document."""

    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_no"),)

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int]
    storage_key: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(32), default="upload")  # upload | zip | url | generated | seed
    source_url: Mapped[str | None] = mapped_column(String(1000))
    processing_status: Mapped[str] = mapped_column(String(24), default=ProcessingStatus.PENDING)
    processing_error: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str | None] = mapped_column(String(40))
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_notes: Mapped[list[Any]] = mapped_column(default=list)
    page_count: Mapped[int | None]
    language: Mapped[str | None] = mapped_column(String(16))
    language_confidence: Mapped[float | None] = mapped_column(Float)
    detected_category: Mapped[str | None] = mapped_column(String(40))
    issued_on: Mapped[date | None]
    expires_on: Mapped[date | None]
    identifiers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_at: Mapped[datetime | None]

    document: Mapped[Document] = relationship(back_populates="versions", foreign_keys=[document_id])


class DocumentPage(IdMixin, TenantMixin, Base):
    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("version_id", "page_no"),)

    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int]
    text: Mapped[str] = mapped_column(Text, default="")
    blocks: Mapped[list[Any]] = mapped_column(default=list)  # [{"text","bbox":[x0,y0,x1,y1]}]
    tables: Mapped[list[Any]] = mapped_column(default=list)
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(24), default="text")  # text | ocr_textract | none
    ocr_confidence: Mapped[float | None] = mapped_column(Float)


class DocumentChunk(IdMixin, TenantMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_document_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_document_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_document_chunks_org_doc", "organization_id", "document_id"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int]
    page_no: Mapped[int]
    section: Mapped[str | None] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(16), default="text")  # text | table | clause
    text: Mapped[str] = mapped_column(Text)
    bbox: Mapped[list[Any] | None]
    token_count: Mapped[int] = mapped_column(default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(80))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(section,'') || ' ' || text)", persisted=True)
    )


class DocumentField(IdMixin, TimestampMixin, TenantMixin, Base):
    """Extracted metadata field with provenance; uncertain fields request user confirmation."""

    __tablename__ = "document_fields"

    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    page_no: Mapped[int | None]
    source_text: Mapped[str | None] = mapped_column(Text)
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[datetime | None]
    corrected_value: Mapped[str | None] = mapped_column(Text)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # failed after exhausting retries (dead-lettered)
    RETRYING = "retrying"
    CANCELLED = "cancelled"


class ProcessingJob(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "processing_jobs"

    kind: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED, index=True)
    subject_type: Mapped[str] = mapped_column(String(48))
    subject_id: Mapped[uuid.UUID] = mapped_column(index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    progress: Mapped[int] = mapped_column(default=0)
    stage: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    celery_task_id: Mapped[str | None] = mapped_column(String(80))
    queue: Mapped[str] = mapped_column(String(32), default="default")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
