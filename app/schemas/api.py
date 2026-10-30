"""Pydantic request/response schemas for /api/v1."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

T = TypeVar("T")


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# ---- auth / orgs ----
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    full_name: str = Field(min_length=1, max_length=200)
    organization_name: str | None = Field(None, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)


class SessionOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    csrf_token: str | None
    active_organization_id: uuid.UUID | None
    role: str | None
    organizations: list[dict[str, Any]]


class TokenIn(BaseModel):
    token: str
    password: str | None = None


class EmailIn(BaseModel):
    email: EmailStr


class OrgIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)


class OrgOut(ORM):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    is_demo: bool


class InviteIn(BaseModel):
    email: EmailStr
    role: str


class MemberOut(BaseModel):
    member_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool


class RoleIn(BaseModel):
    role: str


class ApiTokenIn(BaseModel):
    name: str = Field(max_length=120)
    scopes: list[str] = []
    ttl_days: int | None = Field(90, ge=1, le=365)


class ProfileIn(BaseModel):
    legal_name: str | None = None
    industry: str | None = None
    business_category: str | None = None
    gstin: str | None = Field(None, pattern=r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
    pan: str | None = Field(None, pattern=r"^[A-Z]{5}\d{4}[A-Z]$")
    cin: str | None = None
    udyam_number: str | None = None
    msme_category: str | None = None
    address: str | None = None
    state: str | None = None
    operational_states: list[str] | None = None
    year_established: int | None = Field(None, ge=1800, le=2100)
    capabilities: list[str] | None = None
    product_categories: list[str] | None = None
    typical_project_min: Decimal | None = None
    typical_project_max: Decimal | None = None
    tender_interests: list[str] | None = None


class ProfileOut(ORM, ProfileIn):
    id: uuid.UUID
    onboarding_completed_at: datetime | None = None


# ---- documents ----
class DocumentOut(ORM):
    id: uuid.UUID
    title: str
    scope: str
    category: str
    category_confidence: float
    category_confirmed: bool
    tender_id: uuid.UUID | None
    current_version_id: uuid.UUID | None
    created_at: datetime


class VersionOut(ORM):
    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    processing_status: str
    processing_error: str | None
    extraction_method: str | None
    ocr_used: bool
    quality_notes: list[Any]
    page_count: int | None
    language: str | None
    issued_on: date | None
    expires_on: date | None
    identifiers: dict[str, Any]
    needs_review: bool
    created_at: datetime


class UploadOut(BaseModel):
    document: DocumentOut
    version: VersionOut
    job_id: uuid.UUID | None
    deduplicated: bool
    warnings: list[str] | None = None


class CategoryIn(BaseModel):
    category: str


class FieldConfirmIn(BaseModel):
    corrected_value: str | None = None


class JobOut(ORM):
    id: uuid.UUID
    kind: str
    status: str
    progress: int
    stage: str | None
    error: str | None
    attempts: int
    subject_type: str
    subject_id: uuid.UUID
    created_at: datetime
    finished_at: datetime | None


# ---- tenders ----
class TenderIn(BaseModel):
    title: str = Field(min_length=3, max_length=500)
    reference_number: str | None = Field(None, max_length=200)
    procuring_organization: str | None = None
    source_platform: str | None = None
    source_url: str | None = None
    category: str | None = None
    estimated_value: Decimal | None = Field(None, ge=0)
    location_state: str | None = None
    submission_deadline: datetime | None = None
    clarification_deadline: datetime | None = None
    prebid_meeting_at: datetime | None = None
    description: str | None = None


class TenderPatch(BaseModel):
    changes: dict[str, Any]
    lock_version: int


class TenderOut(ORM):
    id: uuid.UUID
    title: str
    reference_number: str | None
    procuring_organization: str | None
    source_platform: str | None
    source_url: str | None
    category: str | None
    estimated_value: Decimal | None
    currency: str
    location_state: str | None
    publication_date: date | None
    prebid_meeting_at: datetime | None
    clarification_deadline: datetime | None
    submission_deadline: datetime | None
    bid_opening_at: datetime | None
    emd_amount: Decimal | None
    tender_fee: Decimal | None
    status: str
    current_version_no: int
    extraction_method: str | None
    lock_version: int
    created_at: datetime


class TransitionIn(BaseModel):
    to: str
    note: str | None = None


class UrlImportIn(BaseModel):
    url: str = Field(max_length=2000)
    tender_id: uuid.UUID | None = None


class RequirementOut(ORM):
    id: uuid.UUID
    code: str
    req_type: str
    title: str
    metric: str | None
    operator: str | None
    threshold_value: Decimal | None
    threshold_unit: str | None
    period_years: int | None
    aggregation: str | None
    mandatory: str
    conditions: list[Any]
    parameters: dict[str, Any]
    source_document_version_id: uuid.UUID | None
    source_page: int | None
    source_text: str
    source_bbox: list[Any] | None
    extraction_confidence: float
    extraction_method: str
    ambiguity_notes: str | None
    review_status: str
    tender_version_no: int
    is_current: bool


class RequirementReviewIn(BaseModel):
    action: str = Field(pattern="^(confirm|edit|reject)$")
    changes: dict[str, Any] | None = None
    note: str | None = None


# ---- eligibility ----
class AssessmentOut(ORM):
    id: uuid.UUID
    tender_id: uuid.UUID
    run_no: int
    status: str
    trigger: str
    tender_version_no: int
    reference_date: datetime
    readiness_score: float | None
    overall_status: str | None
    summary: dict[str, Any]
    created_at: datetime
    completed_at: datetime | None


class LinkOut(ORM):
    id: uuid.UUID
    evidence_kind: str
    document_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    page_no: int | None
    excerpt: str | None
    bbox: list[Any] | None
    relation: str
    confidence: float
    origin: str
    label: str | None


class DecisionOut(ORM):
    id: uuid.UUID
    requirement_id: uuid.UUID
    status: str
    machine_status: str
    rule_id: str | None
    explanation: str
    computation: dict[str, Any]
    evidence_confidence: float
    risk_level: str
    gaps: list[Any]
    review_state: str
    reviewer_note: str | None


class ReportItemOut(BaseModel):
    requirement: RequirementOut
    decision: DecisionOut
    links: list[LinkOut]
    conflicts: list[dict[str, Any]]


class ReportOut(BaseModel):
    assessment: AssessmentOut
    items: list[ReportItemOut]
    missing_documents: list[str]


class DecisionReviewIn(BaseModel):
    action: str = Field(pattern="^(accept|override|annotate)$")
    new_status: str | None = None
    note: str | None = Field(None, max_length=4000)


class CompleteReviewIn(BaseModel):
    note: str | None = None


# ---- bids ----
class WorkspaceOut(ORM):
    id: uuid.UUID
    tender_id: uuid.UUID
    name: str
    status: str
    owner_id: uuid.UUID | None
    assessment_id: uuid.UUID | None
    lock_version: int


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    priority: str = "medium"
    assignee_id: uuid.UUID | None = None
    reviewer_id: uuid.UUID | None = None
    due_date: date | None = None
    requirement_id: uuid.UUID | None = None


class TaskPatch(BaseModel):
    changes: dict[str, Any]
    lock_version: int


class TaskOut(ORM):
    id: uuid.UUID
    title: str
    description: str | None
    status: str
    priority: str
    assignee_id: uuid.UUID | None
    reviewer_id: uuid.UUID | None
    due_date: date | None
    requirement_id: uuid.UUID | None
    source: str
    lock_version: int
    created_at: datetime


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=8000)
    task_id: uuid.UUID | None = None
    draft_id: uuid.UUID | None = None


class DraftOut(ORM):
    id: uuid.UUID
    kind: str
    title: str
    version_no: int
    status: str
    sections: list[Any]
    generator: str
    is_current: bool
    lock_version: int
    created_at: datetime


class DraftEditIn(BaseModel):
    sections: list[dict[str, Any]]
    lock_version: int


class ApprovalIn(BaseModel):
    action: str = Field(pattern="^(submit_for_review|request_changes|approve|mark_ready|reopen)$")
    note: str | None = None


class ArtifactOut(ORM):
    id: uuid.UUID
    kind: str
    format: str
    document_id: uuid.UUID
    created_at: datetime


class ScenarioIn(BaseModel):
    name: str = Field(max_length=120)
    rates: dict[str, str] = {}
    markup_pct: str = "0"
    gst_pct: str = "18"


# ---- copilot ----
class ConversationIn(BaseModel):
    tender_id: uuid.UUID | None = None


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class MessageOut(ORM):
    id: uuid.UUID
    role: str
    content: str
    citations: list[Any]
    tool_calls: list[Any]
    suggestions: list[Any]
    mode: str
    created_at: datetime


class NotificationOut(ORM):
    id: uuid.UUID
    kind: str
    title: str
    body: str | None
    link: str | None
    read_at: datetime | None
    email_status: str
    created_at: datetime
