"""Company profile and structured, source-linked company facts (evidence claims).

The company_* fact tables are the persisted "evidence claims": every row records the
document version, page and verbatim text it was extracted from, an extraction confidence,
and a verification status that distinguishes document-stated facts from user-confirmed ones.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class FactStatus(StrEnum):
    EXTRACTED = "extracted"  # machine-extracted, document-stated, unconfirmed
    CONFIRMED = "confirmed"  # a user confirmed the extracted value matches the document
    REJECTED = "rejected"  # a user rejected the extraction
    SUPERSEDED = "superseded"  # a newer document version replaced this fact


class SourceMixin:
    source_document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="SET NULL"), index=True
    )
    source_page: Mapped[int | None]
    source_text: Mapped[str | None] = mapped_column(Text)
    source_bbox: Mapped[list[Any] | None]
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default=FactStatus.EXTRACTED)
    confirmed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[datetime | None]


class CompanyProfile(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "company_profiles"
    __table_args__ = (UniqueConstraint("organization_id"),)

    legal_name: Mapped[str] = mapped_column(String(250))
    industry: Mapped[str | None] = mapped_column(String(120))
    business_category: Mapped[str | None] = mapped_column(String(120))
    gstin: Mapped[str | None] = mapped_column(String(15))
    pan: Mapped[str | None] = mapped_column(String(10))
    cin: Mapped[str | None] = mapped_column(String(21))
    udyam_number: Mapped[str | None] = mapped_column(String(32))
    msme_category: Mapped[str | None] = mapped_column(String(16))  # micro / small / medium
    address: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(80))
    operational_states: Mapped[list[Any]] = mapped_column(default=list)
    year_established: Mapped[int | None]
    capabilities: Mapped[list[Any]] = mapped_column(default=list)
    product_categories: Mapped[list[Any]] = mapped_column(default=list)
    typical_project_min: Mapped[Decimal | None]
    typical_project_max: Mapped[Decimal | None]
    tender_interests: Mapped[list[Any]] = mapped_column(default=list)
    onboarding_step: Mapped[int] = mapped_column(default=0)
    onboarding_completed_at: Mapped[datetime | None]


class CompanyCertification(IdMixin, TimestampMixin, TenantMixin, SourceMixin, Base):
    __tablename__ = "company_certifications"

    cert_type: Mapped[str] = mapped_column(String(80), index=True)  # e.g. "ISO 9001"
    certificate_number: Mapped[str | None] = mapped_column(String(120))
    issuer: Mapped[str | None] = mapped_column(String(250))
    scope: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[date | None]
    valid_until: Mapped[date | None]
    # Validity as stated by the document. External authenticity checks are a separate state.
    external_verification: Mapped[str] = mapped_column(String(32), default="not_verified")
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))


class CompanyProject(IdMixin, TimestampMixin, TenantMixin, SourceMixin, Base):
    __tablename__ = "company_projects"

    title: Mapped[str] = mapped_column(String(400))
    client_name: Mapped[str | None] = mapped_column(String(250))
    sector: Mapped[str | None] = mapped_column(String(120))
    contract_value: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    start_date: Mapped[date | None]
    completion_date: Mapped[date | None]
    description: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list[Any]] = mapped_column(default=list)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))


class CompanyFinancialRecord(IdMixin, TimestampMixin, TenantMixin, SourceMixin, Base):
    __tablename__ = "company_financial_records"

    fiscal_year: Mapped[str] = mapped_column(String(9), index=True)  # "2023-24"
    metric: Mapped[str] = mapped_column(String(40))  # turnover | net_worth | profit_after_tax
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    audited: Mapped[bool] = mapped_column(Boolean, default=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))


class CompanyProduct(IdMixin, TimestampMixin, TenantMixin, SourceMixin, Base):
    __tablename__ = "company_products"

    name: Mapped[str] = mapped_column(String(250))
    category: Mapped[str | None] = mapped_column(String(120))
    model_number: Mapped[str | None] = mapped_column(String(120))
    specifications: Mapped[dict[str, Any]] = mapped_column(default=dict)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))


class CompanyRegistration(IdMixin, TimestampMixin, TenantMixin, SourceMixin, Base):
    """Registration identifiers stated in uploaded documents (GST, Udyam, CIN...).

    Not independently verified against government registries.
    """

    __tablename__ = "company_registrations"

    kind: Mapped[str] = mapped_column(String(32))  # gst | udyam | cin | pan | other
    identifier: Mapped[str] = mapped_column(String(64))
    issued_on: Mapped[date | None]
    valid_until: Mapped[date | None]
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
