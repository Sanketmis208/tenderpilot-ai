"""Append-only audit trail."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.core.logging import get_logger, request_id_var
from app.db.base import utcnow
from app.db.models import AuditEvent

log = get_logger(__name__)


def record(
    db: Session,
    action: str,
    *,
    ctx: TenantContext | None = None,
    organization_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    subject_type: str | None = None,
    subject_id: uuid.UUID | None = None,
    data: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        organization_id=organization_id or (ctx.organization_id if ctx else None),
        actor_id=actor_id or (ctx.user_id if ctx else None),
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        data=data or {},
        ip_address=ctx.ip_address if ctx else None,
        request_id=request_id_var.get(),
        created_at=utcnow(),
    )
    db.add(event)
    log.info("audit", action=action, subject_type=subject_type, subject_id=str(subject_id) if subject_id else None)
    return event
