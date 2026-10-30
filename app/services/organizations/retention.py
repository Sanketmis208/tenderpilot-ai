"""Data retention: purge organizations whose deletion grace period has elapsed (DB rows + stored objects)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import DocumentVersion, Organization, OrgStatus
from app.services import audit
from app.storage.backend import get_storage

log = get_logger(__name__)
GRACE_DAYS = 30


def purge_due_organizations(db: Session) -> int:
    n = 0
    for org in db.scalars(select(Organization).where(Organization.status == OrgStatus.PENDING_DELETION)):
        requested = (org.settings or {}).get("deletion_requested_at")
        if not requested or datetime.fromisoformat(requested) > utcnow() - timedelta(days=GRACE_DAYS):
            continue
        storage = get_storage()
        for key in db.scalars(select(DocumentVersion.storage_key).where(DocumentVersion.organization_id == org.id)):
            try:
                storage.delete(key)
            except Exception as exc:  # keep going; object lifecycle rules are the backstop
                log.warning("retention.object_delete_failed", error=str(exc))
        audit.record(db, "organization.purged", organization_id=None, data={"organization_id": str(org.id), "name": org.name})
        db.delete(org)  # ON DELETE CASCADE removes all tenant rows
        n += 1
    return n
