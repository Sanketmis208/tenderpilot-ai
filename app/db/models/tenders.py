"""Tenders, versions (corrigenda), attachments, requirements, matches."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class TenderStatus(StrEnum):
    DISCOVERED = "discovered"
    IMPORTED = "imported"
    PROCESSING = "processing"
    READY_FOR_REVIEW = "ready_for_review"
    UNDER_EVALUATION = "under_evaluation"
    SHORTLISTED = "shortlisted"
    BID_PREPARATION = "bid_preparation"
    AWAITING_APPROVAL = "awaiting_approval"
    READY_FOR_SUBMISSION = "ready_for_external_submission"
    SUBMITTED = "submitted_externally"
    WON = "won"
    LOST = "lost"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class RequirementType(StrEnum):
    FINANCIAL = "financial"
    EXPERIENCE = "experience"
    CERTIFICATION = "certification"
    DOCUMENT = "document"
    TECHNICAL = "technical"
    GEOGRAPHIC = "geographic"
    REGISTRATION = "registration"
    EMD = "emd"
    PERSONNEL = "personnel"
    CONSORTIUM = "consortium"
    OEM = "oem_authorization"
    OTHER = "other"


class Mandatory(StrEnum):
    MANDATORY = "mandatory"
    OPTIONAL = "optional"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    CONFIRMED = "confirmed"
    EDITED = "edited"
    REJECTED = "rejected"


class Tender(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "tenders"
    __table_args__ = (
        Index(
            "uq_tenders_org_reference",
            "organization_id",
            "reference_number",
            unique=True,
            postgresql_where=text("reference_number IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    title: Mapped[str] = mapped_column(String(500))
    reference_number: Mapped[str | None] = mapped_column(String(200))
    procuring_organization: Mapped[str | None] = mapped_column(String(300))
    source_platform: Mapped[str | None] = mapped_column(String(80))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    category: Mapped[str | None] = mapped_column(String(120))
    estimated_value: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    location_state: Mapped[str | None] = mapped_column(String(80))
    publication_date: Mapped[date | None]
    prebid_meeting_at: Mapped[datetime | None]
    clarification_deadline: Mapped[datetime | None]
    submission_deadline: Mapped[datetime | None] = mapped_column(index=True)
    bid_opening_at: Mapped[datetime | None]
    original_timezone: Mapped[str] = mapped_column(String(48), default="Asia/Kolkata")
    description: Mapped[str | None] = mapped_column(Text)
    emd_amount: Mapped[Decimal | None]
    emd_text: Mapped[str | None] = mapped_column(Text)
    tender_fee: Mapped[Decimal | None]
    performance_security_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default=TenderStatus.IMPORTED, index=True)
    current_version_no: Mapped[int] = mapped_column(default=1)
    extraction: Mapped[dict[str, Any]] = mapped_column(default=dict)  # full structured extraction + citations
    extraction_method: Mapped[str | None] = mapped_column(String(40))
    keywords: Mapped[list[Any]] = mapped_column(default=list)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    lock_version: Mapped[int] = mapped_column(default=1)
    deleted_at: Mapped[datetime | None]

    __mapper_args__ = {"version_id_col": lock_version}


class TenderVersion(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "tender_versions"
    __table_args__ = (UniqueConstraint("tender_id", "version_no"),)

    tender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int]
    reason: Mapped[str] = mapped_column(String(40), default="initial")  # initial | corrigendum | manual_edit
    amendment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tender_amendments.id", ondelete="SET NULL"))
    snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class TenderDocument(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "tender_documents"
    __table_args__ = (UniqueConstraint("tender_id", "document_id"),)

    tender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(24), default="main")  # main | annexure | boq | corrigendum
    added_in_version_no: Mapped[int] = mapped_column(default=1)


class TenderRequirement(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "tender_requirements"
    __table_args__ = (Index("ix_tender_requirements_current", "tender_id", "is_current"),)

    tender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    tender_version_no: Mapped[int] = mapped_column(default=1)
    code: Mapped[str] = mapped_column(String(16))
    req_type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(400))
    metric: Mapped[str | None] = mapped_column(String(80))
    operator: Mapped[str | None] = mapped_column(String(8))  # >= > <= < == in exists
    threshold_value: Mapped[Decimal | None]
    threshold_unit: Mapped[str | None] = mapped_column(String(32))  # INR | years | count
    threshold_text: Mapped[str | None] = mapped_column(String(300))
    period_text: Mapped[str | None] = mapped_column(String(200))
    period_years: Mapped[int | None]
    aggregation: Mapped[str | None] = mapped_column(String(16))  # average | each | any | sum
    mandatory: Mapped[str] = mapped_column(String(16), default=Mandatory.UNKNOWN)
    conditions: Mapped[list[Any]] = mapped_column(default=list)  # explicit conditional clauses
    parameters: Mapped[dict[str, Any]] = mapped_column(default=dict)  # e.g. {"standard": "ISO 9001"}
    source_document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="SET NULL")
    )
    source_page: Mapped[int | None]
    source_text: Mapped[str] = mapped_column(Text)
    source_bbox: Mapped[list[Any] | None]
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extraction_method: Mapped[str] = mapped_column(String(32), default="rules")
    ambiguity_notes: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(String(16), default=ReviewStatus.UNREVIEWED)
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None]
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tender_requirements.id", ondelete="SET NULL")
    )


class TenderAmendment(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "tender_amendments"

    tender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    number: Mapped[str | None] = mapped_column(String(80))
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending | applied | failed
    from_version_no: Mapped[int]
    to_version_no: Mapped[int | None]
    field_changes: Mapped[list[Any]] = mapped_column(default=list)
    requirement_changes: Mapped[list[Any]] = mapped_column(default=list)
    impact: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class TenderStatusEvent(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "tender_status_events"

    tender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(Text)


class TenderMatch(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "tender_matches"
    __table_args__ = (UniqueConstraint("organization_id", "tender_id"),)

    tender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)
    breakdown: Mapped[dict[str, Any]] = mapped_column(default=dict)
    reasons: Mapped[list[Any]] = mapped_column(default=list)
    user_action: Mapped[str | None] = mapped_column(String(16))  # shortlisted | dismissed
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    computed_at: Mapped[datetime]


class SavedSearch(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "saved_searches"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    query: Mapped[dict[str, Any]] = mapped_column(default=dict)
    notify: Mapped[bool] = mapped_column(Boolean, default=False)
