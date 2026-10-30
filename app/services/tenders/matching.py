"""Opportunity matching: transparent, weighted relevance — kept separate from verified eligibility."""

from __future__ import annotations

import math
import re
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.ai.retrieval.embeddings import get_embedder
from app.auth.context import TenantContext
from app.auth.permissions import Perm
from app.db.base import utcnow
from app.db.models import (
    CompanyProduct,
    CompanyProfile,
    CompanyProject,
    EligibilityAssessment,
    FactStatus,
    Tender,
    TenderMatch,
    TenderStatus,
)
from app.extraction.normalization.values import format_inr_short

DEFAULT_WEIGHTS = {"relevance": 0.35, "geography": 0.15, "financial": 0.15, "experience": 0.15, "deadline": 0.10, "eligibility": 0.10}
CLOSED = {TenderStatus.ARCHIVED, TenderStatus.CANCELLED, TenderStatus.WON, TenderStatus.LOST, TenderStatus.SUBMITTED}
_W = re.compile(r"[a-z]{3,}")
STOP = {"supply", "installation", "work", "works", "the", "and", "for", "with", "including", "various", "tender", "construction", "testing", "imported"}


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def score_tender(t: Tender, prof: CompanyProfile | None, products: list[str], projects: list[CompanyProject], assessment: EligibilityAssessment | None,
                 weights: dict[str, float]) -> tuple[float, dict[str, float], list[str]]:
    reasons: list[str] = []
    interests = " ".join((prof.tender_interests or []) + (prof.capabilities or []) + (prof.product_categories or []) + products) if prof else " ".join(products)
    ttext = f"{t.title} {t.description or ''} {t.category or ''} {' '.join(t.keywords or [])}".lower()
    itoks, ttoks = set(_W.findall(interests.lower())) - STOP, set(_W.findall(ttext)) - STOP
    overlap = itoks & ttoks
    emb = get_embedder()
    sim = _cos(*emb.embed([interests or "-", ttext or "-"])) if interests else 0.0
    relevance = min(1.0, 0.6 * min(1.0, len(overlap) / 3) + 0.4 * max(0.0, sim) * 2)
    breakdown = {"relevance": round(relevance, 2)}
    if overlap:
        reasons.append(f"Catalogue/capability terms match: {', '.join(sorted(overlap)[:5])}")
    else:
        reasons.append("Low overlap with your declared capabilities")
    states = [s.lower() for s in ([prof.state] if prof and prof.state else []) + (prof.operational_states if prof else [])]
    loc = (t.location_state or "").lower()
    geo = 1.0 if loc and any(s in loc for s in states) else (0.5 if not loc else 0.1)
    breakdown["geography"] = geo
    reasons.append("Within your operating geography" if geo == 1.0 else ("Location not stated" if geo == 0.5 else f"Outside your listed states ({t.location_state})"))
    fin = 0.5
    if t.estimated_value and prof and prof.typical_project_max:
        fin = 1.0 if t.estimated_value <= prof.typical_project_max * Decimal("1.5") else 0.3
    breakdown["financial"] = fin
    if t.estimated_value:
        reasons.append(f"Estimated value {format_inr_short(t.estimated_value)} — financial requirements need verification" if fin <= 0.5 else
                       f"Value {format_inr_short(t.estimated_value)} fits your typical project size")
    similar = [p for p in projects if p.contract_value and t.estimated_value and p.contract_value >= t.estimated_value * Decimal("0.3")
               and (set(_W.findall((p.title or "").lower())) - STOP) & ttoks]
    exp = 1.0 if len(similar) >= 2 else (0.6 if similar else 0.2)
    breakdown["experience"] = exp
    reasons.append(f"{len(similar)} similar past project(s) identified" if similar else "No clearly similar past project on record")
    if t.submission_deadline:
        days = (t.submission_deadline - utcnow()).total_seconds() / 86400
        dl = 0.0 if days < 0 else (0.4 if days < 5 else (1.0 if days < 45 else 0.7))
        reasons.append("Deadline passed" if days < 0 else f"{int(days)} days to submission")
    else:
        dl = 0.5
    breakdown["deadline"] = dl
    elig = {"eligible_per_policy": 1.0, "needs_review": 0.7, "gaps_found": 0.4, "not_eligible": 0.1}.get((assessment.overall_status or "") if assessment else "", 0.5)
    breakdown["eligibility"] = elig
    reasons.append(f"Latest assessment: {assessment.overall_status.replace('_', ' ')}" if assessment and assessment.overall_status else "Eligibility not yet assessed")
    total = sum(weights.get(k, 0) * v for k, v in breakdown.items()) / max(sum(weights.values()), 1e-9)
    return round(100 * total, 1), breakdown, reasons


def recompute(db: Session, ctx: TenantContext) -> int:
    ctx.require(Perm.TENDERS_READ)
    from app.db.models import Organization

    org = db.get(Organization, ctx.organization_id)
    weights = {**DEFAULT_WEIGHTS, **(((org.settings if org else None) or {}).get("match_weights") or {})}
    prof = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == ctx.organization_id))
    products = list(db.scalars(select(CompanyProduct.name).where(CompanyProduct.organization_id == ctx.organization_id, CompanyProduct.status != FactStatus.SUPERSEDED)))
    projects = list(db.scalars(select(CompanyProject).where(CompanyProject.organization_id == ctx.organization_id, CompanyProject.status != FactStatus.SUPERSEDED)))
    n = 0
    for t in db.scalars(select(Tender).where(Tender.organization_id == ctx.organization_id, Tender.deleted_at.is_(None), Tender.status.notin_(CLOSED))):
        a = db.scalar(select(EligibilityAssessment).where(EligibilityAssessment.tender_id == t.id).order_by(EligibilityAssessment.run_no.desc()))
        score, breakdown, reasons = score_tender(t, prof, products, projects, a, weights)
        stmt = insert(TenderMatch).values(id=uuid.uuid4(), organization_id=ctx.organization_id, tender_id=t.id, score=score, breakdown=breakdown,
                                          reasons=reasons, computed_at=utcnow())
        db.execute(stmt.on_conflict_do_update(index_elements=["organization_id", "tender_id"],
                                              set_={"score": score, "breakdown": breakdown, "reasons": reasons, "computed_at": utcnow()}))
        n += 1
    return n


def list_matches(db: Session, ctx: TenantContext, *, include_dismissed: bool = False, min_score: float = 0) -> list[tuple[TenderMatch, Tender]]:
    ctx.require(Perm.TENDERS_READ)
    q = (select(TenderMatch, Tender).join(Tender, Tender.id == TenderMatch.tender_id)
         .where(TenderMatch.organization_id == ctx.organization_id, TenderMatch.score >= min_score, Tender.deleted_at.is_(None)))
    if not include_dismissed:
        q = q.where((TenderMatch.user_action.is_(None)) | (TenderMatch.user_action != "dismissed"))
    return [(m, t) for m, t in db.execute(q.order_by(TenderMatch.score.desc()))]


def set_action(db: Session, ctx: TenantContext, tender_id: uuid.UUID, action: str | None, assignee_id: uuid.UUID | None = None) -> TenderMatch:
    ctx.require(Perm.TENDERS_WRITE)
    m = db.scalar(select(TenderMatch).where(TenderMatch.organization_id == ctx.organization_id, TenderMatch.tender_id == tender_id))
    if not m:
        recompute(db, ctx)
        m = db.scalar(select(TenderMatch).where(TenderMatch.organization_id == ctx.organization_id, TenderMatch.tender_id == tender_id))
    from app.core.errors import NotFound, ValidationFailed

    if m is None:
        raise NotFound("Tender not found.")
    if action not in (None, "shortlisted", "dismissed"):
        raise ValidationFailed("Unknown action.")
    m.user_action = action
    if assignee_id:
        m.assigned_to_id = assignee_id
    if action == "shortlisted":
        t = db.get(Tender, tender_id)
        from app.services.tenders import lifecycle

        if t and TenderStatus.SHORTLISTED in lifecycle.allowed_next(t.status):
            lifecycle.transition(db, ctx, t, TenderStatus.SHORTLISTED, "Shortlisted from opportunities")
    return m


def explain(breakdown: dict[str, Any]) -> str:
    return ", ".join(f"{k} {math.floor(v * 100)}%" for k, v in breakdown.items())
