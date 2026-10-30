"""Notifications, audit trail, plans, subscriptions, billing records and usage counters."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Notification(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "dedup_key"),
        Index("ix_notifications_user_unread", "user_id", "read_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    dedup_key: Mapped[str] = mapped_column(String(200))
    read_at: Mapped[datetime | None]
    # not_requested | queued | accepted | failed | skipped_no_provider | skipped_preference
    email_status: Mapped[str] = mapped_column(String(24), default="not_requested")
    email_attempts: Mapped[int] = mapped_column(default=0)
    email_error: Mapped[str | None] = mapped_column(Text)
    email_provider_message_id: Mapped[str | None] = mapped_column(String(200))


class NotificationPreference(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", "kind"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(48))
    in_app: Mapped[bool] = mapped_column(Boolean, default=True)
    email: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditEvent(IdMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_org_created", "organization_id", "created_at"),)

    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(80), index=True)
    subject_type: Mapped[str | None] = mapped_column(String(48))
    subject_id: Mapped[uuid.UUID | None]
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime]


class Plan(TimestampMixin, Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    price_inr_monthly: Mapped[Decimal | None]
    limits: Mapped[dict[str, Any]] = mapped_column(default=dict)
    features: Mapped[list[Any]] = mapped_column(default=list)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)


class Subscription(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("organization_id"),)

    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"))
    status: Mapped[str] = mapped_column(String(24), default="trialing")  # trialing | active | past_due | cancelled
    current_period_start: Mapped[datetime]
    current_period_end: Mapped[datetime]
    pending_plan_code: Mapped[str | None] = mapped_column(String(32))
    custom_limits: Mapped[dict[str, Any]] = mapped_column(default=dict)
    payment_provider: Mapped[str | None] = mapped_column(String(32))
    provider_reference: Mapped[str | None] = mapped_column(String(120))


class BillingEvent(IdMixin, TimestampMixin, TenantMixin, Base):
    """Billing history. Rows are only written from real provider callbacks or admin actions."""

    __tablename__ = "billing_events"

    kind: Mapped[str] = mapped_column(String(32))  # plan_change_requested | plan_changed | invoice | payment
    amount: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(24))
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_reference: Mapped[str | None] = mapped_column(String(120))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class UsageCounter(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "usage_counters"
    __table_args__ = (UniqueConstraint("organization_id", "metric", "period"),)

    metric: Mapped[str] = mapped_column(String(48))  # documents_processed | ai_tokens | storage_bytes | tenders_analysed
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM, or "total" for gauges
    value: Mapped[int] = mapped_column(BigInteger, default=0)
