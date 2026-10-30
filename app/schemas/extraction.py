"""Structured extraction contracts (shared by rule-based and LLM extractors)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ReqType = Literal[
    "financial",
    "experience",
    "certification",
    "document",
    "technical",
    "geographic",
    "registration",
    "emd",
    "personnel",
    "consortium",
    "oem_authorization",
    "other",
]


# Controlled vocabulary shared with the deterministic rule engine (rules.evaluate dispatches on these).
Metric = Literal[
    "average_annual_turnover",  # financial, threshold in INR, aggregation average
    "annual_turnover",  # financial, threshold in INR, aggregation each
    "net_worth",  # financial
    "similar_work_value",  # experience: value of each similar completed work (INR); count_required = number of works
    "years_in_business",  # experience: minimum years of experience/operation; threshold = years
    "certificate_valid",  # certification: standard = e.g. "ISO 9001"
    "registration",  # registration: standard = "GST" | "PAN" | "UDYAM" | "CIN"
    "emd",  # earnest money deposit; threshold = amount in INR
    "document",  # a document to submit; document_kind names it
    "standard_compliance",  # technical: products conform to standards (IS/IEC/BIS codes) — not a certification
    "state_presence",  # geographic; standard = state name
    "consortium_allowed",  # consortium/JV; threshold "1" if permitted, "0" if not permitted
]


class ExtractedRequirement(BaseModel):
    req_type: ReqType
    title: str = Field(description="Short human-readable summary of the condition")
    metric: Metric | None = Field(None, description="Use the controlled vocabulary; null only for requirements no metric fits")
    operator: Literal[">=", ">", "<=", "<", "==", "exists", "in"] | None = None
    threshold_value: str | None = Field(None, description="Numeric threshold as a plain decimal string in base units (rupees, years, count)")
    threshold_unit: str | None = Field(None, description="INR | years | count | percent")
    period_years: int | None = None
    aggregation: Literal["average", "each", "any", "sum", "count"] | None = None
    mandatory: Literal["mandatory", "optional", "conditional", "unknown"] = "unknown"
    conditions: list[str] = Field(default_factory=list, description="Explicit conditions/exemptions verbatim, never simplified")
    standard: str | None = Field(None, description="Certification/standard named, e.g. 'ISO 9001', 'IS 8623'")
    document_kind: str | None = Field(None, description="Document category required, if a document requirement")
    count_required: int | None = None
    keywords: list[str] = Field(default_factory=list)
    source_quote: str = Field(description="Exact verbatim clause from the tender text")
    source_page: int | None = None
    confidence: float = Field(0.5, ge=0, le=1)
    ambiguity_notes: str | None = None


class FieldCitation(BaseModel):
    page: int | None = None
    text: str


class TenderFields(BaseModel):
    reference_number: str | None = None
    title: str | None = None
    procuring_organization: str | None = None
    category: str | None = None
    estimated_value: str | None = Field(None, description="Rupees as plain decimal string")
    location_state: str | None = None
    publication_date: str | None = Field(None, description="ISO date")
    prebid_meeting_at: str | None = Field(None, description="ISO datetime with +05:30 offset")
    clarification_deadline: str | None = None
    submission_deadline: str | None = None
    bid_opening_at: str | None = None
    emd_amount: str | None = None
    emd_text: str | None = None
    tender_fee: str | None = None
    performance_security_text: str | None = None
    risk_clauses: list[str] = Field(default_factory=list)
    citations: dict[str, FieldCitation] = Field(default_factory=dict)


class LLMRequirementBatch(BaseModel):
    requirements: list[ExtractedRequirement]


class LLMTenderExtraction(BaseModel):
    fields: TenderFields
    requirements: list[ExtractedRequirement]
