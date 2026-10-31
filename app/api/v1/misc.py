"""Copilot, notifications, analytics, usage/billing, audit and platform administration."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.agents import copilot
from app.auth.context import TenantContext
from app.auth.deps import Principal, ctx_dep, principal_dep
from app.auth.permissions import Perm
from app.core.errors import PermissionDenied
from app.db.models import AuditEvent, BillingEvent
from app.db.session import get_db
from app.schemas import api as s
from app.services.analytics import service as analytics
from app.services.billing import entitlements
from app.services.notifications import service as notifications
from app.services.organizations import org_service

router = APIRouter()


@router.post("/copilot/conversations", tags=["copilot"], status_code=201)
def new_conversation(body: s.ConversationIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    c = copilot.start_conversation(db, ctx, body.tender_id)
    return {"id": str(c.id)}


@router.get("/copilot/conversations", tags=["copilot"])
def conversations(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [{"id": str(c.id), "title": c.title, "tender_id": str(c.tender_id) if c.tender_id else None, "updated_at": c.updated_at}
            for c in copilot.list_conversations(db, ctx)]


@router.get("/copilot/conversations/{conversation_id}/messages", tags=["copilot"], response_model=list[s.MessageOut])
def conversation_messages(conversation_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return copilot.messages(db, ctx, conversation_id)


@router.post("/copilot/conversations/{conversation_id}/ask", tags=["copilot"], response_model=s.MessageOut)
async def ask(conversation_id: uuid.UUID, body: s.AskIn, ctx: TenantContext = Depends(ctx_dep)):
    mid = await copilot.ask(ctx, conversation_id, body.question)
    from app.db.models import CopilotMessage
    from app.db.session import session_scope

    with session_scope() as db:
        return s.MessageOut.model_validate(db.get(CopilotMessage, mid))


@router.post("/copilot/messages/{message_id}/actions/{index}/approve", tags=["copilot"])
def approve_action(message_id: uuid.UUID, index: int, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return copilot.execute_pending_action(db, ctx, message_id, index)


@router.get("/notifications", tags=["notifications"], response_model=list[s.NotificationOut])
def list_notifications(unread_only: bool = False, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return notifications.list_for_user(db, ctx, unread_only=unread_only)


@router.post("/notifications/read", tags=["notifications"], status_code=204)
def mark_read(ids: list[uuid.UUID] | None = None, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    notifications.mark_read(db, ctx, ids)


@router.put("/notifications/preferences/{kind}", tags=["notifications"], status_code=204)
def preference(kind: str, in_app: bool = True, email: bool = True, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    notifications.set_preference(db, ctx, kind, in_app, email)


@router.get("/analytics/dashboard", tags=["analytics"])
def dashboard(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    d = analytics.dashboard(db, ctx)
    d["upcoming"] = [{"id": str(t.id), "title": t.title, "deadline": t.submission_deadline} for t in d["upcoming"]]
    return d


@router.get("/analytics/ai-costs", tags=["analytics"])
def ai_costs(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return analytics.ai_cost_per_tender(db, ctx)


@router.get("/usage", tags=["billing"])
def usage(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    sub, plan = entitlements.get_subscription(db, ctx.organization_id)
    limits = entitlements.limits_for(db, ctx.organization_id)
    return {"plan": plan.code if plan else None, "status": sub.status if sub else None, "pending_plan": sub.pending_plan_code if sub else None,
            "limits": limits, "usage": {m: entitlements.get_usage(db, ctx.organization_id, m) for m in
                                        ("documents_processed", "tenders_analysed", "ai_tokens", "tenders_imported")},
            "storage_bytes": entitlements.storage_used(db, ctx.organization_id), "members": analytics.members_count(db, ctx)}


@router.post("/billing/plan-change", tags=["billing"], status_code=202)
def plan_change(plan_code: str, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    ctx.require(Perm.ORG_BILLING)
    ev = entitlements.request_plan_change(db, ctx.organization_id, plan_code, ctx.user_id)
    return {"status": ev.status, "note": "No payment processor is configured; an administrator will apply the change."}


@router.get("/billing/history", tags=["billing"])
def billing_history(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    ctx.require(Perm.ORG_BILLING)
    return [{"kind": e.kind, "status": e.status, "amount": str(e.amount) if e.amount else None, "data": e.data, "at": e.created_at}
            for e in db.scalars(select(BillingEvent).where(BillingEvent.organization_id == ctx.organization_id).order_by(BillingEvent.created_at.desc()))]


@router.get("/audit", tags=["admin"])
def audit_log(limit: int = 100, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    ctx.require(Perm.AUDIT_READ)
    return [{"action": e.action, "actor_id": str(e.actor_id) if e.actor_id else None, "subject_type": e.subject_type,
             "subject_id": str(e.subject_id) if e.subject_id else None, "data": e.data, "at": e.created_at, "request_id": e.request_id}
            for e in db.scalars(select(AuditEvent).where(AuditEvent.organization_id == ctx.organization_id).order_by(AuditEvent.created_at.desc()).limit(min(limit, 500)))]


@router.post("/organization/deletion", tags=["admin"], status_code=202)
def request_deletion(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    org_service.request_deletion(db, ctx)
    return {"status": "pending_deletion", "grace_days": 30}


@router.post("/admin/organizations/{org_id}/suspend", tags=["admin"], status_code=204)
def suspend(org_id: uuid.UUID, suspended: bool = True, reason: str | None = None, p: Principal = Depends(principal_dep), db: Session = Depends(get_db)):
    org_service.set_suspension(db, p.user, org_id, suspended, reason)


@router.post("/admin/organizations/{org_id}/apply-plan-change", tags=["admin"], status_code=204)
def apply_plan(org_id: uuid.UUID, p: Principal = Depends(principal_dep), db: Session = Depends(get_db)):
    if not p.user.is_platform_admin:
        raise PermissionDenied("Platform administrator access required.")
    entitlements.apply_plan_change(db, org_id, p.user.id)  # system-scoped inside: cross-tenant platform-admin action
