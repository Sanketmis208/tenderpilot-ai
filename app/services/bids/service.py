"""Bid workspaces: tasks, comments, drafts, approval lifecycle, activity timeline."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.auth.context import TenantContext
from app.auth.permissions import Perm
from app.core.errors import Conflict, InvalidTransition, NotFound, PermissionDenied, ValidationFailed
from app.db.base import utcnow
from app.db.models import (
    AuditEvent,
    BidApproval,
    BidArtifact,
    BidComment,
    BidDraft,
    BidStatus,
    BidTask,
    BidWorkspace,
    OrganizationMember,
    TaskStatus,
    Tender,
    TenderStatus,
)
from app.services import audit
from app.services.notifications.service import notify, notify_roles
from app.services.tenders import lifecycle
from app.services.tenders.service import get_tender

B = BidStatus
# (from, action) -> (to, permission)
APPROVAL_FLOW: dict[tuple[str, str], tuple[str, Perm]] = {
    (B.DRAFT, "submit_for_review"): (B.INTERNAL_REVIEW, Perm.BIDS_WRITE),
    (B.CHANGES_REQUESTED, "submit_for_review"): (B.INTERNAL_REVIEW, Perm.BIDS_WRITE),
    (B.INTERNAL_REVIEW, "request_changes"): (B.CHANGES_REQUESTED, Perm.BIDS_APPROVE),
    (B.INTERNAL_REVIEW, "approve"): (B.APPROVED, Perm.BIDS_APPROVE),
    (B.APPROVED, "mark_ready"): (B.READY_FOR_SUBMISSION, Perm.BIDS_APPROVE),
    (B.APPROVED, "reopen"): (B.DRAFT, Perm.BIDS_MANAGE),
    (B.READY_FOR_SUBMISSION, "reopen"): (B.DRAFT, Perm.BIDS_MANAGE),
}


def create_workspace(db: Session, ctx: TenantContext, tender_id: uuid.UUID) -> BidWorkspace:
    ctx.require(Perm.BIDS_MANAGE)
    t = get_tender(db, ctx, tender_id)
    existing = db.scalar(select(BidWorkspace).where(BidWorkspace.tender_id == t.id, BidWorkspace.organization_id == ctx.organization_id))
    if existing:
        return existing
    from app.services.eligibility.service import latest_assessment

    a = latest_assessment(db, ctx, t.id)
    ws = BidWorkspace(organization_id=ctx.organization_id, tender_id=t.id, name=f"Bid: {t.title[:250]}", owner_id=ctx.user_id,
                      assessment_id=a.id if a else None)
    db.add(ws)
    db.flush()
    if t.status in (TenderStatus.READY_FOR_REVIEW, TenderStatus.UNDER_EVALUATION):
        lifecycle.transition(db, ctx, t, TenderStatus.SHORTLISTED, "Bid workspace created")
    if t.status == TenderStatus.SHORTLISTED:
        lifecycle.transition(db, ctx, t, TenderStatus.BID_PREPARATION, "Bid preparation started")
    if a:
        from app.ai.workflows.tender_analysis import create_remediation_tasks

        create_remediation_tasks(db, a, ws, ctx.user_id)
    audit.record(db, "bid.workspace_created", ctx=ctx, subject_type="bid_workspace", subject_id=ws.id)
    return ws


def get_workspace(db: Session, ctx: TenantContext, workspace_id: uuid.UUID) -> BidWorkspace:
    ctx.require(Perm.BIDS_READ)
    ws = db.scalar(select(BidWorkspace).where(BidWorkspace.id == workspace_id, BidWorkspace.organization_id == ctx.organization_id))
    if not ws:
        raise NotFound("Bid workspace not found.")
    return ws


def list_workspaces(db: Session, ctx: TenantContext) -> list[tuple[BidWorkspace, Tender, dict]]:
    ctx.require(Perm.BIDS_READ)
    rows = db.execute(select(BidWorkspace, Tender).join(Tender, Tender.id == BidWorkspace.tender_id)
                      .where(BidWorkspace.organization_id == ctx.organization_id).order_by(Tender.submission_deadline.asc().nulls_last())).all()
    counts = {
        wid: {"open": o, "done": d}
        for wid, o, d in db.execute(
            select(BidTask.workspace_id, func.count().filter(BidTask.status != TaskStatus.DONE), func.count().filter(BidTask.status == TaskStatus.DONE))
            .where(BidTask.organization_id == ctx.organization_id).group_by(BidTask.workspace_id)
        ).all()
    }
    return [(w, t, counts.get(w.id, {"open": 0, "done": 0})) for w, t in rows]


def _member(db: Session, ctx: TenantContext, user_id: uuid.UUID | None) -> None:
    if user_id and not db.scalar(select(OrganizationMember.id).where(OrganizationMember.organization_id == ctx.organization_id,
                                                                     OrganizationMember.user_id == user_id, OrganizationMember.is_active.is_(True))):
        raise ValidationFailed("Assignee must be an active member of this organization.")


def create_task(db: Session, ctx: TenantContext, workspace_id: uuid.UUID, *, title: str, description: str | None = None, priority: str = "medium",
                assignee_id: uuid.UUID | None = None, reviewer_id: uuid.UUID | None = None, due_date: date | None = None,
                requirement_id: uuid.UUID | None = None, source: str = "manual", dedup_key: str | None = None) -> BidTask:
    ctx.require(Perm.TASKS_WRITE)
    ws = get_workspace(db, ctx, workspace_id)
    if not title.strip():
        raise ValidationFailed("Task title is required.")
    if priority not in ("low", "medium", "high", "urgent"):
        raise ValidationFailed("Invalid priority.")
    _member(db, ctx, assignee_id)
    _member(db, ctx, reviewer_id)
    if dedup_key and (existing := db.scalar(select(BidTask).where(BidTask.workspace_id == ws.id, BidTask.dedup_key == dedup_key))):
        return existing
    task = BidTask(organization_id=ctx.organization_id, workspace_id=ws.id, title=title.strip()[:300], description=description, priority=priority,
                   assignee_id=assignee_id, reviewer_id=reviewer_id, due_date=due_date, requirement_id=requirement_id, source=source,
                   dedup_key=dedup_key, created_by_id=ctx.user_id, history=[{"at": utcnow().isoformat(), "by": str(ctx.user_id), "event": "created"}])
    db.add(task)
    db.flush()
    if assignee_id and assignee_id != ctx.user_id:
        notify(db, organization_id=ctx.organization_id, user_id=assignee_id, kind="task_assigned", title=f"New task: {task.title[:150]}",
               link=f"/bids/{ws.id}?tab=tasks", dedup_key=f"task_assigned:{task.id}:{assignee_id}")
    audit.record(db, "bid.task_created", ctx=ctx, subject_type="bid_task", subject_id=task.id)
    return task


def update_task(db: Session, ctx: TenantContext, task_id: uuid.UUID, changes: dict[str, Any], expected_lock_version: int) -> BidTask:
    ctx.require(Perm.TASKS_WRITE)
    task = db.scalar(select(BidTask).where(BidTask.id == task_id, BidTask.organization_id == ctx.organization_id))
    if not task:
        raise NotFound("Task not found.")
    if task.lock_version != expected_lock_version:
        raise Conflict("This task was updated by someone else. Reload to see the latest version.")
    allowed = {"title", "description", "status", "priority", "assignee_id", "reviewer_id", "due_date"}
    if bad := set(changes) - allowed:
        raise ValidationFailed(f"Cannot change {', '.join(sorted(bad))}.")
    if "status" in changes and changes["status"] not in {s.value for s in TaskStatus}:
        raise ValidationFailed("Invalid status.")
    if "assignee_id" in changes:
        _member(db, ctx, changes["assignee_id"])
    old_assignee = task.assignee_id
    entry = {"at": utcnow().isoformat(), "by": str(ctx.user_id), "changes": {k: str(v) for k, v in changes.items()}}
    for k, v in changes.items():
        setattr(task, k, v)
    if changes.get("status") == TaskStatus.DONE:
        task.completed_at = utcnow()
    task.history = [*task.history, entry]
    try:
        db.flush()
    except StaleDataError:
        raise Conflict("This task was updated by someone else. Reload to see the latest version.")
    if task.assignee_id and task.assignee_id != old_assignee and task.assignee_id != ctx.user_id:
        notify(db, organization_id=ctx.organization_id, user_id=task.assignee_id, kind="task_assigned", title=f"Task assigned: {task.title[:150]}",
               link=f"/bids/{task.workspace_id}?tab=tasks", dedup_key=f"task_assigned:{task.id}:{task.assignee_id}")
    return task


def list_tasks(db: Session, ctx: TenantContext, workspace_id: uuid.UUID) -> list[BidTask]:
    get_workspace(db, ctx, workspace_id)
    order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    tasks = list(db.scalars(select(BidTask).where(BidTask.workspace_id == workspace_id)))
    return sorted(tasks, key=lambda t: (t.status == TaskStatus.DONE, order.get(t.priority, 9), t.due_date or date.max))


def add_comment(db: Session, ctx: TenantContext, workspace_id: uuid.UUID, body: str, *, task_id: uuid.UUID | None = None,
                draft_id: uuid.UUID | None = None) -> BidComment:
    ctx.require(Perm.BIDS_WRITE)
    get_workspace(db, ctx, workspace_id)
    if not body.strip():
        raise ValidationFailed("Comment cannot be empty.")
    c = BidComment(organization_id=ctx.organization_id, workspace_id=workspace_id, task_id=task_id, draft_id=draft_id, author_id=ctx.user_id, body=body.strip()[:8000])
    db.add(c)
    return c


def comments(db: Session, ctx: TenantContext, workspace_id: uuid.UUID, *, task_id: uuid.UUID | None = None) -> list[BidComment]:
    get_workspace(db, ctx, workspace_id)
    q = select(BidComment).where(BidComment.workspace_id == workspace_id)
    if task_id:
        q = q.where(BidComment.task_id == task_id)
    return list(db.scalars(q.order_by(BidComment.created_at)))


def drafts(db: Session, ctx: TenantContext, workspace_id: uuid.UUID, *, current_only: bool = True) -> list[BidDraft]:
    get_workspace(db, ctx, workspace_id)
    q = select(BidDraft).where(BidDraft.workspace_id == workspace_id)
    if current_only:
        q = q.where(BidDraft.is_current.is_(True))
    return list(db.scalars(q.order_by(BidDraft.kind, BidDraft.version_no.desc())))


def get_draft(db: Session, ctx: TenantContext, draft_id: uuid.UUID) -> BidDraft:
    ctx.require(Perm.BIDS_READ)
    d = db.scalar(select(BidDraft).where(BidDraft.id == draft_id, BidDraft.organization_id == ctx.organization_id))
    if not d:
        raise NotFound("Draft not found.")
    return d


def edit_draft(db: Session, ctx: TenantContext, draft_id: uuid.UUID, sections: list[dict], expected_lock_version: int) -> BidDraft:
    """Edits create a new draft version (history preserved); approved drafts cannot be edited in place."""
    ctx.require(Perm.BIDS_WRITE)
    d = get_draft(db, ctx, draft_id)
    if not d.is_current:
        raise Conflict("A newer version of this draft exists.")
    if d.lock_version != expected_lock_version:
        raise Conflict("This draft was edited by someone else. Reload to merge your changes.")
    ws = get_workspace(db, ctx, d.workspace_id)
    if ws.status in (B.APPROVED, B.READY_FOR_SUBMISSION, B.INTERNAL_REVIEW):
        raise InvalidTransition("The bid is under review or approved; request changes or reopen it before editing.")
    d.is_current = False
    try:
        db.flush()
    except StaleDataError:
        raise Conflict("This draft was edited by someone else. Reload to merge your changes.")
    new = BidDraft(organization_id=d.organization_id, workspace_id=d.workspace_id, kind=d.kind, title=d.title, version_no=d.version_no + 1,
                   sections=[{**s, "edited_by_user": True} if s.get("body") != _orig(d, s) else s for s in sections],
                   evidence_snapshot=d.evidence_snapshot, generator=d.generator, created_by_id=ctx.user_id)
    db.add(new)
    db.flush()
    audit.record(db, "bid.draft_edited", ctx=ctx, subject_type="bid_draft", subject_id=new.id, data={"from_version": d.version_no})
    return new


def _orig(d: BidDraft, s: dict) -> str | None:
    return next((x.get("body") for x in d.sections if x.get("heading") == s.get("heading")), None)


def approval_action(db: Session, ctx: TenantContext, workspace_id: uuid.UUID, action: str, note: str | None = None) -> BidWorkspace:
    ws = get_workspace(db, ctx, workspace_id)
    key = (ws.status, action)
    if key not in APPROVAL_FLOW:
        raise InvalidTransition(f"'{action}' is not possible while the bid is {ws.status.replace('_', ' ')}.")
    to, perm = APPROVAL_FLOW[key]
    ctx.require(perm)
    current = [d for d in drafts(db, ctx, ws.id) if d.is_current]
    if action == "submit_for_review" and not current:
        raise ValidationFailed("Generate at least one draft before submitting for review.")
    if action == "approve":
        if any(d.created_by_id == ctx.user_id for d in current) and ctx.role not in ("owner", "admin"):
            raise PermissionDenied("Drafts must be approved by someone other than their author (four-eyes rule).")
        if action == "approve" and not note:
            note = "Approved"
    if action == "request_changes" and not (note and note.strip()):
        raise ValidationFailed("Explain which changes are needed.")
    from_status = ws.status
    ws.status = to
    for d in current:
        d.status = to
        db.add(BidApproval(organization_id=ws.organization_id, workspace_id=ws.id, draft_id=d.id, action=action, from_status=from_status,
                           to_status=to, draft_version_no=d.version_no, actor_id=ctx.user_id, note=note))
    if not current:
        db.add(BidApproval(organization_id=ws.organization_id, workspace_id=ws.id, action=action, from_status=from_status, to_status=to,
                           actor_id=ctx.user_id, note=note))
    try:
        db.flush()
    except StaleDataError:
        raise Conflict("The bid status changed meanwhile. Reload and retry.")
    tender = db.get(Tender, ws.tender_id)
    if tender:
        target: str | None = {B.INTERNAL_REVIEW: TenderStatus.AWAITING_APPROVAL, B.READY_FOR_SUBMISSION: TenderStatus.READY_FOR_SUBMISSION,
                  B.CHANGES_REQUESTED: TenderStatus.BID_PREPARATION, B.DRAFT: TenderStatus.BID_PREPARATION}.get(B(to))
        if target and target in lifecycle.allowed_next(tender.status):
            lifecycle.transition(db, ctx, tender, target, f"Bid {action.replace('_', ' ')}", system=True)
    if action == "submit_for_review":
        from app.db.models import Role

        notify_roles(db, ws.organization_id, {Role.REVIEWER, Role.BID_MANAGER, Role.ADMIN, Role.OWNER}, kind="review_requested",
                     title=f"Review requested: {ws.name[:150]}", link=f"/bids/{ws.id}?tab=review", dedup_key=f"review:{ws.id}:{utcnow():%Y%m%d%H%M%S}")
    elif ws.owner_id and ws.owner_id != ctx.user_id:
        notify(db, organization_id=ws.organization_id, user_id=ws.owner_id, kind="approval", title=f"Bid {action.replace('_', ' ')}: {ws.name[:150]}",
               body=note, link=f"/bids/{ws.id}?tab=review", dedup_key=f"approval:{ws.id}:{action}:{utcnow():%Y%m%d%H%M%S}")
    audit.record(db, f"bid.{action}", ctx=ctx, subject_type="bid_workspace", subject_id=ws.id, data={"from": from_status, "to": to, "note": note})
    return ws


def approvals(db: Session, ctx: TenantContext, workspace_id: uuid.UUID) -> list[BidApproval]:
    get_workspace(db, ctx, workspace_id)
    return list(db.scalars(select(BidApproval).where(BidApproval.workspace_id == workspace_id).order_by(BidApproval.created_at)))


def artifacts(db: Session, ctx: TenantContext, workspace_id: uuid.UUID) -> list[BidArtifact]:
    get_workspace(db, ctx, workspace_id)
    return list(db.scalars(select(BidArtifact).where(BidArtifact.workspace_id == workspace_id).order_by(BidArtifact.created_at.desc())))


def timeline(db: Session, ctx: TenantContext, workspace_id: uuid.UUID, limit: int = 100) -> list[AuditEvent]:
    ws = get_workspace(db, ctx, workspace_id)
    task_ids = select(BidTask.id).where(BidTask.workspace_id == ws.id)
    draft_ids = select(BidDraft.id).where(BidDraft.workspace_id == ws.id)
    return list(db.scalars(
        select(AuditEvent).where(AuditEvent.organization_id == ctx.organization_id)
        .where((AuditEvent.subject_id == ws.id) | (AuditEvent.subject_id.in_(task_ids)) | (AuditEvent.subject_id.in_(draft_ids)) | (AuditEvent.subject_id == ws.tender_id))
        .order_by(AuditEvent.created_at.desc()).limit(limit)
    ))
