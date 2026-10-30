"""Tender lifecycle state machine. Every transition is validated and logged."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.permissions import Perm
from app.core.errors import InvalidTransition
from app.db.models import Tender, TenderStatus, TenderStatusEvent
from app.services import audit

S = TenderStatus
TRANSITIONS: dict[str, set[str]] = {
    S.DISCOVERED: {S.IMPORTED, S.ARCHIVED, S.CANCELLED},
    S.IMPORTED: {S.PROCESSING, S.READY_FOR_REVIEW, S.ARCHIVED, S.CANCELLED},
    S.PROCESSING: {S.READY_FOR_REVIEW, S.IMPORTED},
    S.READY_FOR_REVIEW: {S.UNDER_EVALUATION, S.SHORTLISTED, S.ARCHIVED, S.CANCELLED, S.PROCESSING},
    S.UNDER_EVALUATION: {S.SHORTLISTED, S.ARCHIVED, S.CANCELLED, S.READY_FOR_REVIEW, S.PROCESSING},
    S.SHORTLISTED: {S.BID_PREPARATION, S.UNDER_EVALUATION, S.ARCHIVED, S.CANCELLED, S.PROCESSING},
    S.BID_PREPARATION: {S.AWAITING_APPROVAL, S.SHORTLISTED, S.CANCELLED, S.PROCESSING},
    S.AWAITING_APPROVAL: {S.READY_FOR_SUBMISSION, S.BID_PREPARATION, S.CANCELLED},
    S.READY_FOR_SUBMISSION: {S.SUBMITTED, S.BID_PREPARATION, S.CANCELLED},
    S.SUBMITTED: {S.WON, S.LOST, S.CANCELLED},
    S.WON: {S.ARCHIVED},
    S.LOST: {S.ARCHIVED},
    S.CANCELLED: {S.ARCHIVED},
    S.ARCHIVED: set(),
}
# Transitions reserved for approvers (the external-submission gate).
APPROVER_ONLY = {S.READY_FOR_SUBMISSION, S.SUBMITTED}


def allowed_next(status: str) -> set[str]:
    return TRANSITIONS.get(status, set())


def transition(db: Session, ctx: TenantContext | None, tender: Tender, to: str, note: str | None = None, *, system: bool = False) -> Tender:
    if to not in {s.value for s in TenderStatus}:
        raise InvalidTransition(f"Unknown status '{to}'.")
    if tender.status == to:
        return tender
    if to not in allowed_next(tender.status):
        raise InvalidTransition(f"Cannot move a tender from '{tender.status}' to '{to}'.")
    if not system:
        assert ctx is not None
        ctx.require(Perm.BIDS_APPROVE if to in APPROVER_ONLY else Perm.TENDERS_WRITE)
    db.add(
        TenderStatusEvent(
            organization_id=tender.organization_id,
            tender_id=tender.id,
            from_status=tender.status,
            to_status=to,
            actor_id=ctx.user_id if ctx else None,
            note=note,
        )
    )
    audit.record(db, "tender.status_changed", ctx=ctx, organization_id=tender.organization_id, subject_type="tender",
                 subject_id=tender.id, data={"from": tender.status, "to": to, "system": system})
    tender.status = to
    return tender
