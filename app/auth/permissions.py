"""Role-based permission policy.

Roles and permissions are defined in code (versioned, reviewable, testable) rather than in
mutable DB tables; organization_members.role stores the role per membership.
"""

from __future__ import annotations

from enum import StrEnum

from app.db.models.identity import Role


class Perm(StrEnum):
    ORG_READ = "org:read"
    ORG_MANAGE = "org:manage"
    ORG_BILLING = "org:billing"
    ORG_DELETE = "org:delete"
    MEMBERS_MANAGE = "members:manage"
    COMPANY_READ = "company:read"
    COMPANY_WRITE = "company:write"
    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_UPLOAD = "documents:upload"
    DOCUMENTS_MANAGE = "documents:manage"
    TENDERS_READ = "tenders:read"
    TENDERS_WRITE = "tenders:write"
    ELIGIBILITY_RUN = "eligibility:run"
    ELIGIBILITY_REVIEW = "eligibility:review"
    BIDS_READ = "bids:read"
    BIDS_WRITE = "bids:write"
    BIDS_MANAGE = "bids:manage"
    BIDS_APPROVE = "bids:approve"
    TASKS_WRITE = "tasks:write"
    COPILOT_USE = "copilot:use"
    ANALYTICS_READ = "analytics:read"
    AUDIT_READ = "audit:read"
    INTEGRATIONS_MANAGE = "integrations:manage"
    API_TOKENS = "api_tokens:manage"


_READ = {
    Perm.ORG_READ,
    Perm.COMPANY_READ,
    Perm.DOCUMENTS_READ,
    Perm.TENDERS_READ,
    Perm.BIDS_READ,
    Perm.ANALYTICS_READ,
}
_CONTRIBUTOR = _READ | {Perm.DOCUMENTS_UPLOAD, Perm.TASKS_WRITE, Perm.BIDS_WRITE, Perm.COPILOT_USE, Perm.API_TOKENS}
_REVIEWER = _CONTRIBUTOR | {Perm.ELIGIBILITY_REVIEW, Perm.BIDS_APPROVE}
_BID_MANAGER = _REVIEWER | {
    Perm.TENDERS_WRITE,
    Perm.ELIGIBILITY_RUN,
    Perm.BIDS_MANAGE,
    Perm.DOCUMENTS_MANAGE,
}
_ADMIN = _BID_MANAGER | {
    Perm.ORG_MANAGE,
    Perm.MEMBERS_MANAGE,
    Perm.COMPANY_WRITE,
    Perm.AUDIT_READ,
    Perm.INTEGRATIONS_MANAGE,
}
_OWNER = _ADMIN | {Perm.ORG_BILLING, Perm.ORG_DELETE}

ROLE_PERMISSIONS: dict[str, frozenset[Perm]] = {
    Role.OWNER: frozenset(_OWNER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.BID_MANAGER: frozenset(_BID_MANAGER),
    Role.REVIEWER: frozenset(_REVIEWER),
    Role.CONTRIBUTOR: frozenset(_CONTRIBUTOR),
    Role.VIEWER: frozenset(_READ),
}

ROLE_RANK = {
    Role.VIEWER: 0,
    Role.CONTRIBUTOR: 1,
    Role.REVIEWER: 2,
    Role.BID_MANAGER: 3,
    Role.ADMIN: 4,
    Role.OWNER: 5,
}

ROLE_LABELS = {
    Role.OWNER: "Organization Owner",
    Role.ADMIN: "Administrator",
    Role.BID_MANAGER: "Bid Manager",
    Role.REVIEWER: "Reviewer",
    Role.CONTRIBUTOR: "Contributor",
    Role.VIEWER: "Viewer",
}


def has_permission(role: str, perm: Perm) -> bool:
    return perm in ROLE_PERMISSIONS.get(role, frozenset())


def can_assign_role(actor_role: str, target_role: str) -> bool:
    """Prevent role escalation: nobody can grant a role above their own; only owners grant owner."""
    if target_role == Role.OWNER:
        return actor_role == Role.OWNER
    return ROLE_RANK.get(Role(actor_role), -1) >= ROLE_RANK.get(Role(target_role), 99)
