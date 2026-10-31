"""Organizations, memberships, invitations, suspension and deletion workflows."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.permissions import Perm, can_assign_role
from app.core import security
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import (
    CompanyProfile,
    Invitation,
    Organization,
    OrganizationMember,
    OrgStatus,
    Role,
    User,
)
from app.services import audit
from app.services.billing import entitlements
from app.services.notifications.email import get_email_provider

log = get_logger(__name__)


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "org"
    return f"{base}-{secrets.token_hex(3)}"


def create_organization(db: Session, user: User, name: str, *, is_demo: bool = False) -> Organization:
    name = name.strip()
    if len(name) < 2:
        raise ValidationFailed("Organization name is too short.")
    org = Organization(name=name[:200], slug=_slugify(name), created_by_id=user.id, is_demo=is_demo)
    db.add(org)
    db.flush()
    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=Role.OWNER))
    db.add(CompanyProfile(organization_id=org.id, legal_name=name[:250]))
    entitlements.create_trial_subscription(db, org.id)
    audit.record(db, "organization.created", organization_id=org.id, actor_id=user.id, subject_type="organization", subject_id=org.id)
    return org


def list_user_organizations(db: Session, user_id: uuid.UUID) -> list[tuple[Organization, str]]:
    rows = db.execute(
        select(Organization, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == user_id, OrganizationMember.is_active.is_(True))
        .order_by(Organization.name)
    ).all()
    return [(o, r) for o, r in rows]


def list_members(db: Session, ctx: TenantContext) -> list[tuple[OrganizationMember, User]]:
    ctx.require(Perm.ORG_READ)
    rows = db.execute(
        select(OrganizationMember, User)
        .join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == ctx.organization_id)
        .order_by(User.full_name)
    ).all()
    return [(m, u) for m, u in rows]


def invite_member(db: Session, ctx: TenantContext, email: str, role: str) -> tuple[Invitation, str]:
    from app.services.organizations.auth_service import normalise_email

    ctx.require(Perm.MEMBERS_MANAGE)
    if role not in {r.value for r in Role}:
        raise ValidationFailed("Unknown role.")
    if not can_assign_role(ctx.role, role):
        raise PermissionDenied("You cannot grant a role higher than your own.")
    email = normalise_email(email)
    existing_user = db.scalar(select(User).where(User.email == email))
    if existing_user and db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == ctx.organization_id,
            OrganizationMember.user_id == existing_user.id,
            OrganizationMember.is_active.is_(True),
        )
    ):
        raise Conflict("That user is already a member.")
    entitlements.check_quota(db, ctx.organization_id, "members")
    raw = security.new_token()
    s = get_settings()
    inv = Invitation(
        organization_id=ctx.organization_id,
        email=email,
        role=role,
        token_hash=security.hash_token(raw),
        invited_by_id=ctx.user_id,
        expires_at=utcnow() + timedelta(days=s.invitation_ttl_days),
    )
    db.add(inv)
    db.flush()
    org = db.get(Organization, ctx.organization_id)
    url = f"{s.base_url}/invite?token={raw}"
    result = get_email_provider().send(
        email, f"You're invited to {org.name if org else 'an organization'} on TenderPilot AI", f"Accept: {url}"
    )
    if s.environment == "development" and result.status == "skipped_no_provider":
        log.info("dev.email_capture", purpose="invitation", link=url)
    audit.record(db, "member.invited", ctx=ctx, subject_type="invitation", subject_id=inv.id, data={"role": role})
    return inv, raw


def get_invitation(db: Session, raw: str) -> Invitation:
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == security.hash_token(raw)))
    if not inv or inv.revoked_at or inv.accepted_at:
        raise ValidationFailed("This invitation is no longer valid.")
    if inv.expires_at < utcnow():
        raise ValidationFailed("This invitation has expired. Ask an administrator to send a new one.")
    return inv


def accept_invitation(db: Session, raw: str, user: User) -> OrganizationMember:
    inv = get_invitation(db, raw)
    if inv.email != user.email:
        raise PermissionDenied("This invitation was sent to a different email address.")
    member = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == inv.organization_id, OrganizationMember.user_id == user.id
        )
    )
    if member:
        member.is_active, member.role = True, inv.role
    else:
        member = OrganizationMember(organization_id=inv.organization_id, user_id=user.id, role=inv.role)
        db.add(member)
    inv.accepted_at, inv.accepted_by_id = utcnow(), user.id
    if not user.email_verified_at:
        user.email_verified_at = utcnow()  # invitation link proves mailbox control
    db.flush()
    audit.record(
        db, "member.joined", organization_id=inv.organization_id, actor_id=user.id, subject_type="member", subject_id=member.id
    )
    return member


def revoke_invitation(db: Session, ctx: TenantContext, invitation_id: uuid.UUID) -> None:
    ctx.require(Perm.MEMBERS_MANAGE)
    inv = db.scalar(
        select(Invitation).where(Invitation.id == invitation_id, Invitation.organization_id == ctx.organization_id)
    )
    if not inv:
        raise NotFound("Invitation not found.")
    inv.revoked_at = utcnow()
    audit.record(db, "member.invitation_revoked", ctx=ctx, subject_type="invitation", subject_id=inv.id)


def list_invitations(db: Session, ctx: TenantContext) -> list[Invitation]:
    ctx.require(Perm.MEMBERS_MANAGE)
    return list(
        db.scalars(
            select(Invitation)
            .where(
                Invitation.organization_id == ctx.organization_id,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
            .order_by(Invitation.created_at.desc())
        )
    )


def _owner_count(db: Session, org_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count()).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.role == Role.OWNER,
                OrganizationMember.is_active.is_(True),
            )
        )
        or 0
    )


def change_member_role(db: Session, ctx: TenantContext, member_id: uuid.UUID, role: str) -> OrganizationMember:
    ctx.require(Perm.MEMBERS_MANAGE)
    member = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id, OrganizationMember.organization_id == ctx.organization_id
        )
    )
    if not member:
        raise NotFound("Member not found.")
    if role not in {r.value for r in Role}:
        raise ValidationFailed("Unknown role.")
    if not can_assign_role(ctx.role, role) or not can_assign_role(ctx.role, member.role):
        raise PermissionDenied("You cannot change this member's role.")
    if member.role == Role.OWNER and role != Role.OWNER and _owner_count(db, ctx.organization_id) <= 1:
        raise ValidationFailed("An organization must keep at least one owner.")
    old = member.role
    member.role = role
    audit.record(db, "member.role_changed", ctx=ctx, subject_type="member", subject_id=member.id, data={"from": old, "to": role})
    return member


def remove_member(db: Session, ctx: TenantContext, member_id: uuid.UUID) -> None:
    ctx.require(Perm.MEMBERS_MANAGE)
    member = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id, OrganizationMember.organization_id == ctx.organization_id
        )
    )
    if not member:
        raise NotFound("Member not found.")
    if not can_assign_role(ctx.role, member.role):
        raise PermissionDenied("You cannot remove this member.")
    if member.role == Role.OWNER and _owner_count(db, ctx.organization_id) <= 1:
        raise ValidationFailed("An organization must keep at least one owner.")
    member.is_active = False
    audit.record(db, "member.removed", ctx=ctx, subject_type="member", subject_id=member.id)


def update_org_settings(db: Session, ctx: TenantContext, *, name: str | None = None, settings: dict | None = None) -> Organization:
    ctx.require(Perm.ORG_MANAGE)
    org = db.get(Organization, ctx.organization_id)
    if not org:
        raise NotFound("Organization not found.")
    if name:
        org.name = name.strip()[:200]
    if settings is not None:
        org.settings = {**(org.settings or {}), **settings}
    audit.record(db, "organization.updated", ctx=ctx, subject_type="organization", subject_id=org.id)
    return org


HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
DEFAULT_BRAND_COLOR = "#1F2A44"


def branding(org: Organization) -> dict:
    """Organization branding for generated documents, with safe defaults."""
    b = (org.settings or {}).get("branding") or {}
    return {"letterhead_name": b.get("letterhead_name") or (org.settings or {}).get("letterhead_name") or org.name,
            "address_line": b.get("address_line") or "", "footer_text": b.get("footer_text") or "",
            "primary_color": b.get("primary_color") if HEX_COLOR.match(b.get("primary_color") or "") else DEFAULT_BRAND_COLOR}


def update_document_template(db: Session, ctx: TenantContext, *, branding_in: dict, standard_sections: list[dict]) -> Organization:
    """Branding and organization-authored standard sections (e.g. quality policy) for generated bid documents.
    Standard sections are the organization's own approved text; they are inserted verbatim and labelled as such."""
    ctx.require(Perm.ORG_MANAGE)
    org = db.get(Organization, ctx.organization_id)
    if not org:
        raise NotFound("Organization not found.")
    color = (branding_in.get("primary_color") or DEFAULT_BRAND_COLOR).strip()
    if not HEX_COLOR.match(color):
        raise ValidationFailed("Brand colour must be a hex value such as #1F2A44.")
    clean = {"letterhead_name": (branding_in.get("letterhead_name") or "").strip()[:200], "address_line": (branding_in.get("address_line") or "").strip()[:300],
             "footer_text": (branding_in.get("footer_text") or "").strip()[:300], "primary_color": color}
    secs = []
    for sec in standard_sections[:10]:
        h, body = (sec.get("heading") or "").strip()[:200], (sec.get("body") or "").strip()[:8000]
        if h and body:
            secs.append({"heading": h, "body": body})
    org.settings = {**(org.settings or {}), "branding": clean, "letterhead_name": clean["letterhead_name"] or (org.settings or {}).get("letterhead_name"),
                    "template": {"standard_sections": secs}}
    audit.record(db, "organization.document_template_updated", ctx=ctx, subject_type="organization", subject_id=org.id,
                 data={"standard_sections": len(secs), "primary_color": color})
    return org


def set_suspension(db: Session, actor: User, org_id: uuid.UUID, suspended: bool, reason: str | None = None) -> Organization:
    if not actor.is_platform_admin:
        raise PermissionDenied("Platform administrator access required.")
    org = db.get(Organization, org_id)
    if not org:
        raise NotFound("Organization not found.")
    org.status = OrgStatus.SUSPENDED if suspended else OrgStatus.ACTIVE
    org.suspended_reason = reason if suspended else None
    audit.record(db, "organization.suspended" if suspended else "organization.reactivated", organization_id=org.id, actor_id=actor.id)
    return org


def request_deletion(db: Session, ctx: TenantContext) -> Organization:
    """Marks the organization for deletion; a scheduled job purges data and storage after the grace period."""
    ctx.require(Perm.ORG_DELETE)
    org = db.get(Organization, ctx.organization_id)
    if not org:
        raise NotFound("Organization not found.")
    org.status = OrgStatus.PENDING_DELETION
    org.settings = {**(org.settings or {}), "deletion_requested_at": utcnow().isoformat()}
    audit.record(db, "organization.deletion_requested", ctx=ctx, subject_type="organization", subject_id=org.id)
    return org
