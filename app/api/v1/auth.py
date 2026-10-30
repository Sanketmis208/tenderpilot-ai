"""Authentication, sessions, organizations, members, invitations, API tokens."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.deps import Principal, client_ip, ctx_dep, principal_dep
from app.core import ratelimit
from app.core.config import get_settings
from app.db.models import ApiToken
from app.db.session import get_db
from app.schemas import api as s
from app.services.organizations import auth_service, org_service

router = APIRouter(tags=["auth"])


def set_session_cookie(response: Response, token: str) -> None:
    st = get_settings()
    response.set_cookie(st.session_cookie_name, token, max_age=st.session_ttl_hours * 3600, httponly=True, secure=st.cookie_secure,
                        samesite="lax", path="/")


def _session_out(db: Session, p: Principal, csrf: str | None) -> s.SessionOut:
    orgs = [{"id": str(o.id), "name": o.name, "role": r, "is_demo": o.is_demo} for o, r in org_service.list_user_organizations(db, p.user.id)]
    return s.SessionOut(user_id=p.user.id, email=p.user.email, full_name=p.user.full_name, csrf_token=csrf,
                        active_organization_id=p.ctx.organization_id if p.ctx else None, role=p.ctx.role if p.ctx else None, organizations=orgs)


@router.post("/auth/register", response_model=s.SessionOut, status_code=status.HTTP_201_CREATED)
def register(body: s.RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ratelimit.hit(f"auth:{client_ip(request)}", get_settings().auth_rate_limit_per_minute)
    user = auth_service.register_user(db, email=body.email, password=body.password, full_name=body.full_name)
    if body.organization_name:
        org_service.create_organization(db, user, body.organization_name)
        db.flush()
    issued = auth_service.create_session(db, user, ip=client_ip(request), user_agent=request.headers.get("user-agent"))
    set_session_cookie(response, issued.token)
    from app.auth.deps import _ctx_for

    return _session_out(db, Principal(user, issued.session, _ctx_for(db, user, issued.session, request)), issued.csrf_token)


@router.post("/auth/login", response_model=s.SessionOut)
def login(body: s.LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ratelimit.hit(f"auth:{client_ip(request)}", get_settings().auth_rate_limit_per_minute)
    user = auth_service.authenticate(db, body.email, body.password)
    issued = auth_service.create_session(db, user, ip=client_ip(request), user_agent=request.headers.get("user-agent"))
    set_session_cookie(response, issued.token)
    from app.auth.deps import _ctx_for

    return _session_out(db, Principal(user, issued.session, _ctx_for(db, user, issued.session, request)), issued.csrf_token)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, p: Principal = Depends(principal_dep), db: Session = Depends(get_db)):
    auth_service.revoke_session(db, request.cookies.get(get_settings().session_cookie_name))
    response.delete_cookie(get_settings().session_cookie_name, path="/")


@router.get("/auth/session", response_model=s.SessionOut)
def session(p: Principal = Depends(principal_dep), db: Session = Depends(get_db)):
    return _session_out(db, p, p.session.csrf_token if p.session else None)


@router.post("/auth/verify-email", status_code=204)
def verify_email(body: s.TokenIn, db: Session = Depends(get_db)):
    auth_service.verify_email(db, body.token)


@router.post("/auth/password-reset/request", status_code=202)
def request_reset(body: s.EmailIn, request: Request, db: Session = Depends(get_db)):
    ratelimit.hit(f"auth:{client_ip(request)}", get_settings().auth_rate_limit_per_minute)
    auth_service.request_password_reset(db, body.email)
    return {"message": "If an account exists for that email, a reset link has been sent."}


@router.post("/auth/password-reset/confirm", status_code=204)
def confirm_reset(body: s.TokenIn, db: Session = Depends(get_db)):
    auth_service.reset_password(db, body.token, body.password or "")


@router.post("/organizations", response_model=s.OrgOut, status_code=201)
def create_org(body: s.OrgIn, p: Principal = Depends(principal_dep), db: Session = Depends(get_db)):
    org = org_service.create_organization(db, p.user, body.name)
    if p.session:
        db.flush()
        p.session.active_organization_id = org.id
    return org


@router.get("/organizations", response_model=list[dict])
def my_orgs(p: Principal = Depends(principal_dep), db: Session = Depends(get_db)):
    return [{"id": str(o.id), "name": o.name, "role": r, "status": o.status} for o, r in org_service.list_user_organizations(db, p.user.id)]


@router.post("/organizations/{org_id}/switch", status_code=204)
def switch_org(org_id: uuid.UUID, p: Principal = Depends(principal_dep), db: Session = Depends(get_db)):
    if not p.session:
        from app.core.errors import ValidationFailed

        raise ValidationFailed("API tokens are bound to one organization; switching applies to browser sessions.")
    auth_service.switch_organization(db, p.session, org_id)


@router.get("/members", response_model=list[s.MemberOut])
def members(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [s.MemberOut(member_id=m.id, user_id=u.id, email=u.email, full_name=u.full_name, role=m.role, is_active=m.is_active)
            for m, u in org_service.list_members(db, ctx)]


@router.post("/members/invitations", status_code=201)
def invite(body: s.InviteIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    inv, raw = org_service.invite_member(db, ctx, body.email, body.role)
    out = {"invitation_id": str(inv.id), "expires_at": inv.expires_at.isoformat()}
    if get_settings().environment == "development":
        out["dev_invite_link"] = f"{get_settings().base_url}/invite?token={raw}"
    return out


@router.post("/invitations/accept", status_code=204)
def accept(body: s.TokenIn, p: Principal = Depends(principal_dep), db: Session = Depends(get_db)):
    m = org_service.accept_invitation(db, body.token, p.user)
    if p.session:
        p.session.active_organization_id = m.organization_id


@router.patch("/members/{member_id}", status_code=204)
def change_role(member_id: uuid.UUID, body: s.RoleIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    org_service.change_member_role(db, ctx, member_id, body.role)


@router.delete("/members/{member_id}", status_code=204)
def remove(member_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    org_service.remove_member(db, ctx, member_id)


@router.post("/api-tokens", status_code=201)
def create_token(body: s.ApiTokenIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    from app.auth.permissions import Perm

    ctx.require(Perm.API_TOKENS)
    tok, raw = auth_service.create_api_token(db, ctx, body.name, body.scopes, body.ttl_days)
    return {"id": str(tok.id), "token": raw, "note": "Copy this token now; it will not be shown again."}


@router.get("/api-tokens")
def list_tokens(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    from sqlalchemy import select

    rows = db.scalars(select(ApiToken).where(ApiToken.organization_id == ctx.organization_id, ApiToken.user_id == ctx.user_id))
    return [{"id": str(t.id), "name": t.name, "prefix": t.token_prefix, "scopes": t.scopes, "revoked": bool(t.revoked_at),
             "expires_at": t.expires_at.isoformat() if t.expires_at else None, "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None} for t in rows]


@router.delete("/api-tokens/{token_id}", status_code=204)
def revoke_token(token_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    auth_service.revoke_api_token(db, ctx, token_id)
