"""Bid workspaces, tasks, comments, drafts, approvals, generated artifacts and BOQ."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class BidStatus(StrEnum):
    DRAFT = "draft"
    INTERNAL_REVIEW = "internal_review"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    READY_FOR_SUBMISSION = "ready_for_external_submission"


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"


class BidWorkspace(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "bid_workspaces"
    __table_args__ = (UniqueConstraint("organization_id", "tender_id"),)

    tender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(40), default=BidStatus.DRAFT)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("eligibility_assessments.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    lock_version: Mapped[int] = mapped_column(default=1)

    __mapper_args__ = {"version_id_col": lock_version}


class BidTask(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "bid_tasks"
    __table_args__ = (
        # Idempotency for system-created remediation tasks (retries/resumes cannot duplicate them).
        Index(
            "uq_bid_tasks_dedup",
            "workspace_id",
            "dedup_key",
            unique=True,
            postgresql_where=text("dedup_key IS NOT NULL"),
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bid_workspaces.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.TODO)
    priority: Mapped[str] = mapped_column(String(8), default="medium")  # low | medium | high | urgent
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    due_date: Mapped[date | None]
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tender_requirements.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(24), default="manual")  # manual | remediation | amendment | copilot
    dedup_key: Mapped[str | None] = mapped_column(String(200))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    completed_at: Mapped[datetime | None]
    history: Mapped[list[Any]] = mapped_column(default=list)
    lock_version: Mapped[int] = mapped_column(default=1)

    __mapper_args__ = {"version_id_col": lock_version}


class BidComment(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "bid_comments"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bid_workspaces.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bid_tasks.id", ondelete="CASCADE"))
    draft_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bid_drafts.id", ondelete="CASCADE"))
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    attachment_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))


class BidDraft(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "bid_drafts"
    __table_args__ = (UniqueConstraint("workspace_id", "kind", "version_no"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bid_workspaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # technical_response | capability_summary | ...
    title: Mapped[str] = mapped_column(String(300))
    version_no: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(32), default=BidStatus.DRAFT)
    sections: Mapped[list[Any]] = mapped_column(default=list)  # [{heading, body, citations, placeholders}]
    evidence_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    generator: Mapped[str] = mapped_column(String(24), default="template")  # template | llm
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    lock_version: Mapped[int] = mapped_column(default=1)

    __mapper_args__ = {"version_id_col": lock_version}


class BidApproval(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "bid_approvals"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bid_workspaces.id", ondelete="CASCADE"), index=True)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bid_drafts.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40))
    draft_version_no: Mapped[int | None]
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(Text)


class BidArtifact(IdMixin, TimestampMixin, TenantMixin, Base):
    """A generated, downloadable file (DOCX/PDF/XLSX/ZIP) tied to the evidence snapshot used."""

    __tablename__ = "bid_artifacts"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bid_workspaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    format: Mapped[str] = mapped_column(String(8))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    draft_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bid_drafts.id", ondelete="SET NULL"))
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("eligibility_assessments.id", ondelete="SET NULL")
    )
    evidence_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class BoqImport(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "boq_imports"

    tender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bid_workspaces.id", ondelete="SET NULL"))
    document_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"))
    sheet_name: Mapped[str] = mapped_column(String(120))
    header_row: Mapped[int]
    column_map: Mapped[dict[str, Any]] = mapped_column(default=dict)
    item_count: Mapped[int] = mapped_column(default=0)
    issues: Mapped[list[Any]] = mapped_column(default=list)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="ready")


class BoqItem(IdMixin, TenantMixin, Base):
    __tablename__ = "boq_items"
    __table_args__ = (UniqueConstraint("boq_import_id", "row_no"),)

    boq_import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("boq_imports.id", ondelete="CASCADE"), index=True)
    row_no: Mapped[int]
    item_code: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(32))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    quoted_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    rate_cell: Mapped[str | None] = mapped_column(String(16))
    amount_cell: Mapped[str | None] = mapped_column(String(16))
    amount_formula: Mapped[str | None] = mapped_column(String(500))
    rate_cell_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_heading: Mapped[bool] = mapped_column(Boolean, default=False)
    is_subtotal: Mapped[bool] = mapped_column(Boolean, default=False)
    issues: Mapped[list[Any]] = mapped_column(default=list)


class BoqScenario(IdMixin, TimestampMixin, TenantMixin, Base):
    """Internal cost estimate — kept distinct from the procuring entity's prescribed BOQ template."""

    __tablename__ = "boq_scenarios"

    boq_import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("boq_imports.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    rates: Mapped[dict[str, Any]] = mapped_column(default=dict)  # {item_id: "123.45"}
    markup_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0"))
    gst_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("18"))
    totals: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
