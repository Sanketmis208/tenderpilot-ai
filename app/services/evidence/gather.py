"""Load an organization's current, authorised evidence into rule-engine views (+ snapshot ids)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import (
    CompanyCertification,
    CompanyFinancialRecord,
    CompanyProduct,
    CompanyProfile,
    CompanyProject,
    CompanyRegistration,
    Document,
    DocumentScope,
    FactStatus,
    TenderRequirement,
)
from app.services.eligibility.rules import CompanyEvidence, Fact, ReqView

LIVE = (FactStatus.EXTRACTED, FactStatus.CONFIRMED)


def _fact(kind: str, row, values: dict[str, Any], titles: dict[uuid.UUID, str]) -> Fact:
    return Fact(
        kind=kind,
        id=row.id,
        values=values,
        document_id=row.document_id,
        version_id=row.source_document_version_id,
        page=row.source_page,
        excerpt=row.source_text,
        bbox=row.source_bbox,
        confidence=row.extraction_confidence,
        confirmed=row.status == FactStatus.CONFIRMED,
        label=titles.get(row.document_id, "document") if row.document_id else "company profile",
    )


def load_company_evidence(db: Session, org_id: uuid.UUID, standards: list[str] | None = None) -> tuple[CompanyEvidence, dict[str, Any]]:
    docs = db.execute(
        select(Document.id, Document.title, Document.category, Document.current_version_id)
        .where(Document.organization_id == org_id, Document.scope == DocumentScope.COMPANY, Document.deleted_at.is_(None))
    ).all()
    titles = {d.id: d.title for d in docs}
    live_docs = {d.id for d in docs}

    def rows(model):
        return [r for r in db.scalars(select(model).where(model.organization_id == org_id, model.status.in_(LIVE))) if r.document_id in live_docs]

    prof = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == org_id))
    profile = {
        "legal_name": prof.legal_name if prof else None,
        "state": prof.state if prof else None,
        "operational_states": prof.operational_states if prof else [],
        "year_established": prof.year_established if prof else None,
        "msme_category": prof.msme_category if prof else None,
        "capabilities": prof.capabilities if prof else [],
        "product_categories": prof.product_categories if prof else [],
    }
    ev = CompanyEvidence(profile=profile)
    for r in rows(CompanyCertification):
        ev.certifications.append(_fact("certification", r, {"cert_type": r.cert_type, "certificate_number": r.certificate_number,
                                                             "issuer": r.issuer, "valid_from": r.valid_from, "valid_until": r.valid_until}, titles))
    for r in rows(CompanyFinancialRecord):
        ev.financials.append(_fact("financial", r, {"fiscal_year": r.fiscal_year, "metric": r.metric, "amount": r.amount, "audited": r.audited}, titles))
    for r in rows(CompanyProject):
        ev.projects.append(_fact("project", r, {"title": r.title, "client_name": r.client_name, "contract_value": r.contract_value,
                                                "start_date": r.start_date, "completion_date": r.completion_date, "description": r.description}, titles))
    for r in rows(CompanyRegistration):
        ev.registrations.append(_fact("registration", r, {"kind": r.kind, "identifier": r.identifier, "issued_on": r.issued_on,
                                                          "valid_until": r.valid_until, "attributes": r.attributes}, titles))
    for r in rows(CompanyProduct):
        ev.products.append(_fact("product", r, {"name": r.name, "model_number": r.model_number}, titles))
    for d in docs:
        ev.documents_by_category.setdefault(d.category, []).append(
            Fact("document", d.id, {"category": d.category}, d.id, d.current_version_id, None, d.title, None, 0.9, False, d.title)
        )
    for std in standards or []:
        hits = db.execute(
            text(
                "SELECT c.id, c.document_id, c.version_id, c.page_no, c.text, c.bbox, d.title FROM document_chunks c "
                "JOIN documents d ON d.id = c.document_id AND d.current_version_id = c.version_id "
                "WHERE c.organization_id = :org AND d.scope = 'company' AND d.deleted_at IS NULL "
                "AND regexp_replace(c.text, '\\s+', '', 'g') ILIKE :pat LIMIT 3"
            ),
            {"org": org_id, "pat": "%" + std.replace(" ", "") + "%"},
        ).all()
        ev.text_hits[std] = [Fact("chunk", h.id, {"standard": std}, h.document_id, h.version_id, h.page_no, _excerpt(h.text, std), h.bbox, 0.6, False, h.title) for h in hits]
    snapshot = {
        "document_versions": sorted({str(d.current_version_id) for d in docs if d.current_version_id}),
        "facts": {
            "certifications": [str(f.id) for f in ev.certifications],
            "financials": [str(f.id) for f in ev.financials],
            "projects": [str(f.id) for f in ev.projects],
            "registrations": [str(f.id) for f in ev.registrations],
        },
        "profile": {k: v for k, v in profile.items() if k in ("state", "year_established", "msme_category")},
    }
    return ev, snapshot


def _excerpt(text_: str, needle: str, width: int = 220) -> str:
    idx = text_.replace(" ", "").lower().find(needle.replace(" ", "").lower())
    if idx < 0:
        return text_[:width]
    start = max(0, idx - width // 2)
    return ("…" if start else "") + text_[start : start + width] + "…"


def req_view(r: TenderRequirement) -> ReqView:
    return ReqView(
        id=r.id, code=r.code, req_type=r.req_type, title=r.title, metric=r.metric, operator=r.operator, threshold=r.threshold_value,
        unit=r.threshold_unit, period_years=r.period_years, aggregation=r.aggregation, mandatory=r.mandatory, conditions=r.conditions or [],
        parameters=r.parameters or {}, source_text=r.source_text, review_status=r.review_status,
    )
