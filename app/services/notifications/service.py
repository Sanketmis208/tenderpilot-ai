"""In-app notifications with dedup, preferences, email fan-out and scheduled reminders."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import (
    BidTask,
    BidWorkspace,
    Document,
    DocumentVersion,
    Notification,
    NotificationPreference,
    Organization,
    OrganizationMember,
    Role,
    Tender,
    TenderStatus,
    User,
)
from app.services.notifications.email import get_email_provider

log = get_logger(__name__)

EMAIL_KINDS = {"deadline", "document_expiry", "task_assigned", "review_requested", "approval", "amendment", "job_failed", "invitation"}
MAX_EMAIL_ATTEMPTS = 4


def _prefs(db: Session, org_id: uuid.UUID, user_id: uuid.UUID, kind: str) -> tuple[bool, bool]:
    p = db.scalar(
        select(NotificationPreference).where(
            NotificationPreference.organization_id == org_id,
            NotificationPreference.user_id == user_id,
            NotificationPreference.kind == kind,
        )
    )
    return (p.in_app, p.email) if p else (True, kind in EMAIL_KINDS)


def notify(
    db: Session,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    dedup_key: str,
) -> bool:
    """Insert once per (user, dedup_key). Returns False if suppressed as duplicate."""
    in_app, email = _prefs(db, organization_id, user_id, kind)
    stmt = (
        insert(Notification)
        .values(
            id=uuid.uuid4(),
            organization_id=organization_id,
            user_id=user_id,
            kind=kind,
            title=title[:300],
            body=body,
            link=link,
            dedup_key=dedup_key[:200],
            email_status="queued" if email else "skipped_preference",
            read_at=None if in_app else utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["user_id", "dedup_key"])
        .returning(Notification.id)
    )
    new_id = db.execute(stmt).scalar()
    if new_id and email:
        _deliver_email(db, new_id)
    return new_id is not None


def notify_roles(db: Session, org_id: uuid.UUID, roles: set[str], **kw) -> int:
    members = db.scalars(
        select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.is_active.is_(True),
            OrganizationMember.role.in_(roles),
        )
    ).all()
    base_key = kw.pop("dedup_key")
    return sum(notify(db, organization_id=org_id, user_id=uid, dedup_key=base_key, **kw) for uid in members)


def _deliver_email(db: Session, notification_id: uuid.UUID) -> None:
    n = db.get(Notification, notification_id)
    if not n:
        return
    user = db.get(User, n.user_id)
    if not user:
        return
    res = get_email_provider().send(user.email, n.title, (n.body or "") + (f"\n\nOpen: {n.link}" if n.link else ""))
    n.email_attempts += 1
    n.email_status = res.status
    n.email_error = res.error
    n.email_provider_message_id = res.message_id


def retry_failed_emails(db: Session) -> int:
    rows = db.scalars(
        select(Notification).where(Notification.email_status == "failed", Notification.email_attempts < MAX_EMAIL_ATTEMPTS).limit(200)
    ).all()
    for n in rows:
        _deliver_email(db, n.id)
    return len(rows)


def list_for_user(db: Session, ctx: TenantContext, *, unread_only: bool = False, limit: int = 50) -> list[Notification]:
    q = select(Notification).where(Notification.organization_id == ctx.organization_id, Notification.user_id == ctx.user_id)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    return list(db.scalars(q.order_by(Notification.created_at.desc()).limit(limit)))


def unread_count(db: Session, ctx: TenantContext) -> int:
    return int(
        db.scalar(
            select(func.count()).where(
                Notification.organization_id == ctx.organization_id,
                Notification.user_id == ctx.user_id,
                Notification.read_at.is_(None),
            )
        )
        or 0
    )


def mark_read(db: Session, ctx: TenantContext, ids: list[uuid.UUID] | None = None) -> None:
    q = update(Notification).where(
        Notification.organization_id == ctx.organization_id, Notification.user_id == ctx.user_id, Notification.read_at.is_(None)
    )
    if ids:
        q = q.where(Notification.id.in_(ids))
    db.execute(q.values(read_at=utcnow()))


def set_preference(db: Session, ctx: TenantContext, kind: str, in_app: bool, email: bool) -> None:
    stmt = insert(NotificationPreference).values(
        id=uuid.uuid4(), organization_id=ctx.organization_id, user_id=ctx.user_id, kind=kind, in_app=in_app, email=email
    ).on_conflict_do_update(index_elements=["organization_id", "user_id", "kind"], set_={"in_app": in_app, "email": email})
    db.execute(stmt)


# ---- scheduled reminders (timezone-aware: deadlines are stored UTC, rendered IST) ----

REMINDER_WINDOWS = [(timedelta(days=7), "7d"), (timedelta(days=3), "3d"), (timedelta(days=1), "24h")]
ACTIVE = [TenderStatus.SHORTLISTED, TenderStatus.BID_PREPARATION, TenderStatus.AWAITING_APPROVAL, TenderStatus.UNDER_EVALUATION, TenderStatus.READY_FOR_SUBMISSION]


def send_deadline_reminders(db: Session) -> int:
    from app.extraction.normalization.values import IST

    now = utcnow()
    sent = 0
    tenders = db.scalars(
        select(Tender)
        .join(Organization, Organization.id == Tender.organization_id)
        .where(Tender.status.in_(ACTIVE), Tender.deleted_at.is_(None), Organization.status == "active")
        .where(
            ((Tender.submission_deadline > now) & (Tender.submission_deadline < now + timedelta(days=7, hours=1)))
            | ((Tender.clarification_deadline > now) & (Tender.clarification_deadline < now + timedelta(days=3, hours=1)))
        )
    ).all()
    for t in tenders:
        for field, label in (("submission_deadline", "Submission"), ("clarification_deadline", "Clarification")):
            dl = getattr(t, field)
            if not dl or dl <= now:
                continue
            for window, tag in REMINDER_WINDOWS:
                if dl - now <= window:
                    sent += notify_roles(
                        db,
                        t.organization_id,
                        {Role.OWNER, Role.ADMIN, Role.BID_MANAGER},
                        kind="deadline",
                        title=f"{label} deadline in {tag}: {t.title[:120]}",
                        body=f"{label} deadline {dl.astimezone(IST):%d %b %Y %H:%M} IST.",
                        link=f"/tenders/{t.id}",
                        dedup_key=f"deadline:{t.id}:{field}:{tag}:{dl.isoformat()}",
                    )
                    break
    # Overdue tasks
    overdue = db.execute(
        select(BidTask, BidWorkspace)
        .join(BidWorkspace, BidWorkspace.id == BidTask.workspace_id)
        .where(BidTask.status != "done", BidTask.due_date < now.date(), BidTask.assignee_id.is_not(None))
    ).all()
    for task, ws in overdue:
        sent += notify(
            db,
            organization_id=task.organization_id,
            user_id=task.assignee_id,  # type: ignore[arg-type]
            kind="task_overdue",
            title=f"Overdue task: {task.title[:150]}",
            link=f"/bids/{ws.id}",
            dedup_key=f"overdue:{task.id}:{task.due_date}",
        )
    return sent


def send_expiry_warnings(db: Session) -> int:
    today = utcnow().date()
    rows = db.execute(
        select(DocumentVersion, Document)
        .join(Document, and_(Document.current_version_id == DocumentVersion.id))
        .where(
            Document.deleted_at.is_(None),
            Document.scope == "company",
            DocumentVersion.expires_on.is_not(None),
            DocumentVersion.expires_on <= today + timedelta(days=60),
        )
    ).all()
    sent = 0
    for v, d in rows:
        state = "has expired" if v.expires_on < today else f"expires on {v.expires_on:%d %b %Y}"
        sent += notify_roles(
            db,
            d.organization_id,
            {Role.OWNER, Role.ADMIN, Role.BID_MANAGER},
            kind="document_expiry",
            title=f"{d.title} {state}",
            body="Upload a renewed document to keep eligibility assessments current.",
            link=f"/vault/{d.id}",
            dedup_key=f"expiry:{v.id}:{'expired' if v.expires_on < today else 'soon'}",
        )
    return sent
