"""Analytics computed from persisted records only (fresh organizations get empty states, never fake numbers)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.permissions import Perm
from app.db.base import utcnow
from app.db.models import (
    AiUsageRecord,
    BidTask,
    BidWorkspace,
    DecisionStatus,
    EligibilityAssessment,
    EligibilityDecision,
    OrganizationMember,
    TaskStatus,
    Tender,
    TenderMatch,
    TenderRequirement,
    TenderStatus,
    User,
)

ACTIVE_BID = [TenderStatus.BID_PREPARATION, TenderStatus.AWAITING_APPROVAL, TenderStatus.READY_FOR_SUBMISSION, TenderStatus.SHORTLISTED]


def dashboard(db: Session, ctx: TenantContext) -> dict[str, Any]:
    ctx.require(Perm.ANALYTICS_READ)
    org = ctx.organization_id
    now = utcnow()
    live = (Tender.organization_id == org) & Tender.deleted_at.is_(None)
    by_status = dict(db.execute(select(Tender.status, func.count()).where(live).group_by(Tender.status)).all())
    total = sum(by_status.values())
    submitted = sum(by_status.get(s, 0) for s in (TenderStatus.SUBMITTED, TenderStatus.WON, TenderStatus.LOST))
    won, lost = by_status.get(TenderStatus.WON, 0), by_status.get(TenderStatus.LOST, 0)
    upcoming = db.scalars(select(Tender).where(live, Tender.submission_deadline > now, Tender.submission_deadline < now + timedelta(days=30),
                                               Tender.status.notin_([TenderStatus.ARCHIVED, TenderStatus.CANCELLED, TenderStatus.LOST]))
                          .order_by(Tender.submission_deadline).limit(8)).all()
    active_value = db.scalar(select(func.coalesce(func.sum(Tender.estimated_value), 0)).where(live, Tender.status.in_(ACTIVE_BID))) or 0
    latest = (select(EligibilityAssessment.tender_id, func.max(EligibilityAssessment.run_no).label("run_no"))
              .where(EligibilityAssessment.organization_id == org).group_by(EligibilityAssessment.tender_id).subquery())
    latest_ids = select(EligibilityAssessment.id).join(latest, (latest.c.tender_id == EligibilityAssessment.tender_id) & (latest.c.run_no == EligibilityAssessment.run_no))
    decision_counts = dict(db.execute(select(EligibilityDecision.status, func.count()).where(EligibilityDecision.assessment_id.in_(latest_ids))
                                      .group_by(EligibilityDecision.status)).all())
    gap_categories = dict(db.execute(
        select(TenderRequirement.req_type, func.count()).join(EligibilityDecision, EligibilityDecision.requirement_id == TenderRequirement.id)
        .where(EligibilityDecision.assessment_id.in_(latest_ids),
               EligibilityDecision.status.in_([DecisionStatus.INSUFFICIENT_EVIDENCE, DecisionStatus.VERIFIED_NOT_SATISFIED, DecisionStatus.CONFLICTING_EVIDENCE]))
        .group_by(TenderRequirement.req_type)).all())
    awaiting = db.scalar(select(func.count()).select_from(EligibilityAssessment).where(EligibilityAssessment.organization_id == org,
                                                                                         EligibilityAssessment.status == "awaiting_review")) or 0
    awaiting += db.scalar(select(func.count()).select_from(BidWorkspace).where(BidWorkspace.organization_id == org, BidWorkspace.status == "internal_review")) or 0
    workload = db.execute(
        select(User.full_name, func.count().filter(BidTask.status != TaskStatus.DONE), func.count().filter((BidTask.status != TaskStatus.DONE) & (BidTask.due_date < now.date())))
        .join(BidTask, BidTask.assignee_id == User.id).where(BidTask.organization_id == org).group_by(User.full_name)
    ).all()
    month = func.date_trunc("month", Tender.created_at)
    monthly = db.execute(select(month, func.count(), func.count().filter(Tender.status.in_([TenderStatus.SUBMITTED, TenderStatus.WON, TenderStatus.LOST])))
                         .where(live, Tender.created_at > now - timedelta(days=365)).group_by(month).order_by(month)).all()
    cost = db.execute(select(func.coalesce(func.sum(AiUsageRecord.cost_usd), 0), func.coalesce(func.sum(AiUsageRecord.tokens_in + AiUsageRecord.tokens_out), 0))
                      .where(AiUsageRecord.organization_id == org, AiUsageRecord.created_at > now - timedelta(days=30))).one()
    relevant = db.scalar(select(func.count()).select_from(TenderMatch).where(TenderMatch.organization_id == org, TenderMatch.score >= 60)) or 0
    open_tasks = db.scalar(select(func.count()).select_from(BidTask).where(BidTask.organization_id == org, BidTask.status != TaskStatus.DONE)) or 0
    return {
        "has_data": total > 0,
        "kpis": {
            "tenders_total": total, "relevant_opportunities": relevant,
            "active_workspaces": db.scalar(select(func.count()).select_from(BidWorkspace).where(BidWorkspace.organization_id == org)) or 0,
            "upcoming_deadlines": len(upcoming), "awaiting_reviews": awaiting,
            "verified_conditions": decision_counts.get(DecisionStatus.VERIFIED_SATISFIED, 0),
            "missing_evidence": decision_counts.get(DecisionStatus.INSUFFICIENT_EVIDENCE, 0),
            "active_bid_value": str(active_value), "submitted": submitted, "won": won, "lost": lost,
            "win_rate": round(100 * won / (won + lost), 1) if (won + lost) else None,
            "ai_cost_usd_30d": round(float(cost[0]), 4), "ai_tokens_30d": int(cost[1]), "open_tasks": open_tasks,
        },
        "pipeline": {str(k): v for k, v in by_status.items()},
        "decisions": {str(k): v for k, v in decision_counts.items()},
        "gap_categories": {str(k): v for k, v in gap_categories.items()},
        "workload": [{"user": u, "open": o, "overdue": od} for u, o, od in workload],
        "monthly": [{"month": m.strftime("%b %Y"), "tenders": n, "submitted": s} for m, n, s in monthly],
        "upcoming": upcoming,
    }


def members_count(db: Session, ctx: TenantContext) -> int:
    return db.scalar(select(func.count()).where(OrganizationMember.organization_id == ctx.organization_id, OrganizationMember.is_active.is_(True))) or 0


def ai_cost_per_tender(db: Session, ctx: TenantContext) -> list[dict[str, Any]]:
    ctx.require(Perm.ANALYTICS_READ)
    rows = db.execute(select(Tender.title, func.sum(AiUsageRecord.cost_usd), func.sum(AiUsageRecord.tokens_in + AiUsageRecord.tokens_out))
                      .join(Tender, Tender.id == AiUsageRecord.tender_id).where(AiUsageRecord.organization_id == ctx.organization_id)
                      .group_by(Tender.title).order_by(func.sum(AiUsageRecord.cost_usd).desc()).limit(20)).all()
    return [{"tender": t, "cost_usd": round(float(c or 0), 4), "tokens": int(n or 0)} for t, c, n in rows]


_ = case  # imported for potential CASE-based metrics
