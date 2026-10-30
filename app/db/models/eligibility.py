"""Requirements-to-Evidence graph: assessments, decisions, evidence links, conflicts, review events."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class DecisionStatus(StrEnum):
    VERIFIED_SATISFIED = "verified_satisfied"
    VERIFIED_NOT_SATISFIED = "verified_not_satisfied"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    NOT_APPLICABLE = "not_applicable"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class AssessmentStatus(StrEnum):
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class EligibilityAssessment(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "eligibility_assessments"
    __table_args__ = (UniqueConstraint("tender_id", "run_no"),)

    tender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    tender_version_no: Mapped[int]
    run_no: Mapped[int]
    status: Mapped[str] = mapped_column(String(24), default=AssessmentStatus.RUNNING)
    trigger: Mapped[str] = mapped_column(String(48), default="manual")  # manual | evidence_update | amendment
    triggered_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reference_date: Mapped[datetime]  # date against which validity is evaluated (submission deadline)
    policy: Mapped[dict[str, Any]] = mapped_column(default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    readiness_score: Mapped[float | None] = mapped_column(Float)
    overall_status: Mapped[str | None] = mapped_column(String(32))
    evidence_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)  # doc versions + facts used
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"))
    previous_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("eligibility_assessments.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None]


class EligibilityDecision(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "eligibility_decisions"
    __table_args__ = (UniqueConstraint("assessment_id", "requirement_id"),)

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("eligibility_assessments.id", ondelete="CASCADE"), index=True
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tender_requirements.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32))
    machine_status: Mapped[str] = mapped_column(String(32))  # rule-engine result, never overwritten
    rule_id: Mapped[str | None] = mapped_column(String(64))
    explanation: Mapped[str] = mapped_column(Text)
    computation: Mapped[dict[str, Any]] = mapped_column(default=dict)
    evidence_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    risk_level: Mapped[str] = mapped_column(String(12), default="medium")  # low | medium | high
    gaps: Mapped[list[Any]] = mapped_column(default=list)
    review_state: Mapped[str] = mapped_column(String(24), default="pending")  # pending | accepted | overridden
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None]


class RequirementEvidenceLink(IdMixin, TimestampMixin, TenantMixin, Base):
    """Edge in the Requirements-to-Evidence graph, with an immutable snapshot of the cited source."""

    __tablename__ = "requirement_evidence_links"
    __table_args__ = (Index("ix_rel_decision", "decision_id"),)

    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eligibility_decisions.id", ondelete="CASCADE"))
    requirement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tender_requirements.id", ondelete="CASCADE"))
    evidence_kind: Mapped[str] = mapped_column(String(32))  # certification | financial | project | registration | document | chunk | product
    evidence_ref_id: Mapped[uuid.UUID | None]
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="SET NULL")
    )
    page_no: Mapped[int | None]
    excerpt: Mapped[str | None] = mapped_column(Text)
    bbox: Mapped[list[Any] | None]
    relation: Mapped[str] = mapped_column(String(16), default="supports")  # supports | contradicts | partial | related
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    origin: Mapped[str] = mapped_column(String(16), default="rule")  # rule | retrieval | manual | agent
    label: Mapped[str | None] = mapped_column(String(300))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class EvidenceConflict(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "evidence_conflicts"

    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eligibility_decisions.id", ondelete="CASCADE"))
    description: Mapped[str] = mapped_column(Text)
    evidence_link_ids: Mapped[list[Any]] = mapped_column(default=list)
    resolved_at: Mapped[datetime | None]
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolution_note: Mapped[str | None] = mapped_column(Text)


class ReviewEvent(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "review_events"
    __table_args__ = (Index("ix_review_events_subject", "subject_type", "subject_id"),)

    subject_type: Mapped[str] = mapped_column(String(40))
    subject_id: Mapped[uuid.UUID]
    action: Mapped[str] = mapped_column(String(32))
    from_value: Mapped[str | None] = mapped_column(String(64))
    to_value: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
