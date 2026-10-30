"""Plans, entitlements, quotas and usage metering.

Payment processing sits behind PaymentProvider. Until a real processor is configured, plan
changes are recorded as `plan_change_requested` billing events and applied by an administrator;
no payment is ever reported as successful without a provider confirmation.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.errors import QuotaExceeded, ServiceUnavailable, ValidationFailed
from app.db.base import utcnow
from app.db.models import BillingEvent, DocumentVersion, OrganizationMember, Plan, Subscription, UsageCounter

PLAN_DEFINITIONS: list[dict[str, Any]] = [
    {
        "code": "starter",
        "name": "Starter",
        "price_inr_monthly": Decimal("4999"),
        "limits": {
            "members": 3,
            "storage_bytes": 2 * 1024**3,
            "documents_processed_per_month": 150,
            "tenders_analysed_per_month": 15,
            "ai_tokens_per_month": 500_000,
        },
        "features": ["tender_analysis", "document_vault", "eligibility", "copilot"],
    },
    {
        "code": "professional",
        "name": "Professional",
        "price_inr_monthly": Decimal("19999"),
        "limits": {
            "members": 15,
            "storage_bytes": 25 * 1024**3,
            "documents_processed_per_month": 2000,
            "tenders_analysed_per_month": 150,
            "ai_tokens_per_month": 5_000_000,
        },
        "features": [
            "tender_analysis",
            "document_vault",
            "eligibility",
            "copilot",
            "collaboration",
            "evidence_graph",
            "boq",
            "document_generation",
            "mcp_access",
        ],
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "price_inr_monthly": None,
        "limits": {
            "members": 500,
            "storage_bytes": 1024 * 1024**3,
            "documents_processed_per_month": 100_000,
            "tenders_analysed_per_month": 10_000,
            "ai_tokens_per_month": 200_000_000,
        },
        "features": [
            "tender_analysis",
            "document_vault",
            "eligibility",
            "copilot",
            "collaboration",
            "evidence_graph",
            "boq",
            "document_generation",
            "mcp_access",
            "integrations",
            "advanced_permissions",
            "priority_support",
        ],
    },
]

DEFAULT_PLAN = "professional"  # 14-day trial on Professional
TRIAL_DAYS = 14


def ensure_plans(db: Session) -> None:
    for p in PLAN_DEFINITIONS:
        stmt = insert(Plan).values(**p).on_conflict_do_update(
            index_elements=[Plan.code], set_={"limits": p["limits"], "features": p["features"], "name": p["name"]}
        )
        db.execute(stmt)


def period_key() -> str:
    return utcnow().strftime("%Y-%m")


def create_trial_subscription(db: Session, org_id: uuid.UUID) -> Subscription:
    ensure_plans(db)
    now = utcnow()
    sub = Subscription(
        organization_id=org_id,
        plan_code=DEFAULT_PLAN,
        status="trialing",
        current_period_start=now,
        current_period_end=now + timedelta(days=TRIAL_DAYS),
    )
    db.add(sub)
    return sub


def get_subscription(db: Session, org_id: uuid.UUID) -> tuple[Subscription | None, Plan | None]:
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == org_id))
    plan = db.get(Plan, sub.plan_code) if sub else None
    return sub, plan


def limits_for(db: Session, org_id: uuid.UUID) -> dict[str, Any]:
    sub, plan = get_subscription(db, org_id)
    if not plan or not sub:
        return dict(PLAN_DEFINITIONS[0]["limits"])
    return {**plan.limits, **(sub.custom_limits or {})}


def has_feature(db: Session, org_id: uuid.UUID, feature: str) -> bool:
    _, plan = get_subscription(db, org_id)
    return bool(plan and feature in plan.features)


def get_usage(db: Session, org_id: uuid.UUID, metric: str, period: str | None = None) -> int:
    return int(
        db.scalar(
            select(UsageCounter.value).where(
                UsageCounter.organization_id == org_id,
                UsageCounter.metric == metric,
                UsageCounter.period == (period or period_key()),
            )
        )
        or 0
    )


def increment_usage(db: Session, org_id: uuid.UUID, metric: str, amount: int = 1) -> None:
    stmt = insert(UsageCounter).values(
        id=uuid.uuid4(), organization_id=org_id, metric=metric, period=period_key(), value=amount
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["organization_id", "metric", "period"],
        set_={"value": UsageCounter.value + amount, "updated_at": utcnow()},
    )
    db.execute(stmt)


def storage_used(db: Session, org_id: uuid.UUID) -> int:
    return int(
        db.scalar(select(func.coalesce(func.sum(DocumentVersion.size_bytes), 0)).where(
            DocumentVersion.organization_id == org_id
        ))
        or 0
    )


def check_quota(db: Session, org_id: uuid.UUID, metric: str, add: int = 1) -> None:
    limits = limits_for(db, org_id)
    if metric == "storage_bytes":
        used, limit = storage_used(db, org_id), limits.get("storage_bytes")
    elif metric == "members":
        used = int(
            db.scalar(
                select(func.count()).where(
                    OrganizationMember.organization_id == org_id, OrganizationMember.is_active.is_(True)
                )
            )
            or 0
        )
        limit = limits.get("members")
    else:
        used, limit = get_usage(db, org_id, metric), limits.get(f"{metric}_per_month")
    if limit is not None and used + add > int(limit):
        raise QuotaExceeded(
            f"Plan limit reached for {metric.replace('_', ' ')} ({used}/{limit}). Upgrade your plan to continue.",
            details={"metric": metric, "used": used, "limit": limit},
        )


class PaymentProvider(Protocol):
    name: str

    def create_checkout(self, org_id: uuid.UUID, plan_code: str) -> str: ...


class UnconfiguredPaymentProvider:
    name = "none"

    def create_checkout(self, org_id: uuid.UUID, plan_code: str) -> str:
        raise ServiceUnavailable(
            "No payment processor is configured. Plan change requests are recorded for an administrator."
        )


def request_plan_change(db: Session, org_id: uuid.UUID, plan_code: str, actor_id: uuid.UUID) -> BillingEvent:
    ensure_plans(db)
    if not db.get(Plan, plan_code):
        raise ValidationFailed(f"Unknown plan '{plan_code}'.")
    sub, _ = get_subscription(db, org_id)
    if sub is None:
        sub = create_trial_subscription(db, org_id)
    if sub.plan_code == plan_code:
        raise ValidationFailed("Organization is already on this plan.")
    sub.pending_plan_code = plan_code
    event = BillingEvent(
        organization_id=org_id,
        kind="plan_change_requested",
        status="pending_admin",
        data={"from": sub.plan_code, "to": plan_code},
        actor_id=actor_id,
    )
    db.add(event)
    return event


def apply_plan_change(db: Session, org_id: uuid.UUID, actor_id: uuid.UUID) -> Subscription:
    """Platform-admin action that applies a pending plan change (no payment implied)."""
    sub, _ = get_subscription(db, org_id)
    if not sub or not sub.pending_plan_code:
        raise ValidationFailed("No pending plan change.")
    previous = sub.plan_code
    sub.plan_code, sub.pending_plan_code = sub.pending_plan_code, None
    db.add(
        BillingEvent(
            organization_id=org_id,
            kind="plan_changed",
            status="applied_by_admin",
            data={"from": previous, "to": sub.plan_code},
            actor_id=actor_id,
        )
    )
    return sub
