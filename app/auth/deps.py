"""Request → TenantContext resolution for the API and UI (session cookie or bearer API token)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.core import security
from app.core.config import get_settings
from app.core.errors import PermissionDenied, Unauthenticated
from app.core.logging import request_id_var
from app.db import tenancy
from app.db.models import Organization, User, UserSession
from app.db.session import get_db
from app.services.organizations import auth_service

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Principal:
    user: User
    session: UserSession | None
    ctx: TenantContext | None  # None when the user has no organization yet


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def resolve_principal(request: Request, db: Session, *, enforce_csrf: bool = True) -> Principal:
    """Identity lookups run before any tenant is known (system scope); the result is then bound as the tenant."""
    with tenancy.system_scope():
        p = _resolve(request, db, enforce_csrf=enforce_csrf)
    tenancy.bind_tenant(p.ctx.organization_id if p.ctx else None)
    return p


def _resolve(request: Request, db: Session, *, enforce_csrf: bool) -> Principal:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        ctx = auth_service.resolve_api_token(db, auth[7:].strip(), channel="api")
        if not ctx:
            raise Unauthenticated("Invalid or expired API token.")
        user = db.get(User, ctx.user_id)
        if user is None:
            raise Unauthenticated("Invalid or expired API token.")
        return Principal(user, None, TenantContext(**{**ctx.__dict__, "request_id": request_id_var.get(), "ip_address": client_ip(request)}))
    raw = request.cookies.get(get_settings().session_cookie_name)
    resolved = auth_service.resolve_session(db, raw)
    if not resolved:
        raise Unauthenticated("Sign in to continue.")
    sess, user = resolved
    if enforce_csrf and request.method not in SAFE_METHODS:
        token = request.headers.get(get_settings().csrf_header_name, "")
        if not token or not security.constant_time_equals(token, sess.csrf_token):
            raise PermissionDenied("Missing or invalid CSRF token.", code="csrf_failed")
    return Principal(user, sess, _ctx_for(db, user, sess, request))


@tenancy.system_scoped
def _ctx_for(db: Session, user: User, sess: UserSession, request: Request | None) -> TenantContext | None:
    if not sess.active_organization_id:
        return None
    m = auth_service.membership_for(db, user.id, sess.active_organization_id)
    org = db.get(Organization, sess.active_organization_id)
    if not m or not org:
        sess.active_organization_id = None
        return None
    if org.status != "active":
        raise PermissionDenied(f"Organization is {org.status.replace('_', ' ')}.", code="organization_inactive")
    return TenantContext(user_id=user.id, organization_id=org.id, role=m.role, email=user.email, channel="web",
                         request_id=request_id_var.get(), ip_address=client_ip(request) if request else None).bind()


def _principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    return resolve_principal(request, db)


async def principal_dep(p: Principal = Depends(_principal)) -> Principal:
    # Sync dependencies run in a copied context, so their tenant binding does not reach the endpoint. This async
    # dependency runs in the request task, whose context the (sync) endpoint inherits.
    tenancy.bind_tenant(p.ctx.organization_id if p.ctx else None)
    return p


async def ctx_dep(p: Principal = Depends(principal_dep)) -> TenantContext:
    if p.ctx is None:
        raise PermissionDenied("Create or select an organization first.", code="no_organization")
    return p.ctx


def ui_principal(request: Request, db: Session) -> Principal | None:
    """For NiceGUI pages: cookie session only, no CSRF (page loads are GETs; UI events run server-side)."""
    try:
        return resolve_principal(request, db, enforce_csrf=False)
    except (Unauthenticated, PermissionDenied):
        return None


def parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        from app.core.errors import NotFound

        raise NotFound("Not found.")
