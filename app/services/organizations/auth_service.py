"""Registration, login, sessions, email verification, password reset, API tokens."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.core import security
from app.core.config import get_settings
from app.core.errors import Conflict, PermissionDenied, Unauthenticated, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import ApiToken, AuthToken, OrganizationMember, User, UserSession
from app.services import audit
from app.services.notifications.email import get_email_provider

log = get_logger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_FAILED_LOGINS = 8
LOCKOUT_MINUTES = 15


def normalise_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 320:
        raise ValidationFailed("Enter a valid email address.")
    return email


def register_user(db: Session, *, email: str, password: str, full_name: str) -> User:
    email = normalise_email(email)
    if problems := security.validate_password_strength(password):
        raise ValidationFailed(" ".join(problems))
    if not full_name.strip():
        raise ValidationFailed("Full name is required.")
    if db.scalar(select(User).where(User.email == email)):
        raise Conflict("An account with this email already exists.")
    user = User(email=email, full_name=full_name.strip()[:200], password_hash=security.hash_password(password))
    db.add(user)
    db.flush()
    audit.record(db, "user.registered", actor_id=user.id, subject_type="user", subject_id=user.id)
    send_verification_email(db, user)
    return user


def _issue_auth_token(db: Session, user: User, purpose: str, ttl: timedelta) -> str:
    raw = security.new_token()
    db.add(AuthToken(user_id=user.id, purpose=purpose, token_hash=security.hash_token(raw), expires_at=utcnow() + ttl))
    return raw


def _consume_auth_token(db: Session, raw: str, purpose: str) -> User:
    tok = db.scalar(
        select(AuthToken).where(AuthToken.token_hash == security.hash_token(raw), AuthToken.purpose == purpose)
    )
    if not tok or tok.used_at or tok.expires_at < utcnow():
        raise ValidationFailed("This link is invalid or has expired.")
    tok.used_at = utcnow()
    user = db.get(User, tok.user_id)
    if not user or not user.is_active:
        raise ValidationFailed("This link is invalid or has expired.")
    return user


def send_verification_email(db: Session, user: User) -> str:
    raw = _issue_auth_token(db, user, "email_verify", timedelta(days=2))
    s = get_settings()
    url = f"{s.base_url}/verify-email?token={raw}"
    result = get_email_provider().send(user.email, "Verify your TenderPilot AI email", f"Verify your email: {url}")
    if s.environment == "development" and result.status == "skipped_no_provider":
        log.info("dev.email_capture", purpose="email_verify", link=url)
    return raw


def verify_email(db: Session, raw: str) -> User:
    user = _consume_auth_token(db, raw, "email_verify")
    user.email_verified_at = utcnow()
    audit.record(db, "user.email_verified", actor_id=user.id, subject_type="user", subject_id=user.id)
    return user


def request_password_reset(db: Session, email: str) -> str | None:
    """Always behaves the same externally to avoid account enumeration."""
    try:
        email = normalise_email(email)
    except ValidationFailed:
        return None
    user = db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
    if not user:
        return None
    s = get_settings()
    raw = _issue_auth_token(db, user, "password_reset", timedelta(minutes=s.password_reset_ttl_minutes))
    url = f"{s.base_url}/reset-password?token={raw}"
    result = get_email_provider().send(user.email, "Reset your TenderPilot AI password", f"Reset link: {url}")
    if s.environment == "development" and result.status == "skipped_no_provider":
        log.info("dev.email_capture", purpose="password_reset", link=url)
    audit.record(db, "user.password_reset_requested", actor_id=user.id, subject_type="user", subject_id=user.id)
    return raw


def reset_password(db: Session, raw: str, new_password: str) -> User:
    if problems := security.validate_password_strength(new_password):
        raise ValidationFailed(" ".join(problems))
    user = _consume_auth_token(db, raw, "password_reset")
    user.password_hash = security.hash_password(new_password)
    user.failed_login_count, user.locked_until = 0, None
    # Invalidate every existing session after a password change.
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    audit.record(db, "user.password_reset", actor_id=user.id, subject_type="user", subject_id=user.id)
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    try:
        email = normalise_email(email)
    except ValidationFailed:
        raise Unauthenticated("Invalid email or password.")
    user = db.scalar(select(User).where(User.email == email))
    now = utcnow()
    if user and user.locked_until and user.locked_until > now:
        raise Unauthenticated("Too many failed attempts. Try again later.")
    if not user or not user.is_active or not security.verify_password(user.password_hash, password):
        if user:
            user.failed_login_count += 1
            if user.failed_login_count >= MAX_FAILED_LOGINS:
                user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            audit.record(db, "user.login_failed", actor_id=user.id, subject_type="user", subject_id=user.id)
        else:
            security.hash_password(password)  # equalise timing
        raise Unauthenticated("Invalid email or password.")
    if get_settings().email_verification_required and not user.email_verified_at:
        raise Unauthenticated("Verify your email address before signing in.")
    if security.password_needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(password)
    user.failed_login_count, user.locked_until, user.last_login_at = 0, None, now
    return user


@dataclass
class IssuedSession:
    token: str
    csrf_token: str
    session: UserSession


def create_session(db: Session, user: User, *, ip: str | None = None, user_agent: str | None = None) -> IssuedSession:
    s = get_settings()
    raw, csrf = security.new_token(), security.new_token(24)
    membership = db.scalar(
        select(OrganizationMember)
        .where(OrganizationMember.user_id == user.id, OrganizationMember.is_active.is_(True))
        .order_by(OrganizationMember.created_at)
    )
    now = utcnow()
    sess = UserSession(
        user_id=user.id,
        token_hash=security.hash_token(raw),
        csrf_token=csrf,
        active_organization_id=membership.organization_id if membership else None,
        expires_at=now + timedelta(hours=s.session_ttl_hours),
        last_seen_at=now,
        ip_address=ip,
        user_agent=(user_agent or "")[:400],
    )
    db.add(sess)
    db.flush()
    audit.record(
        db,
        "user.login",
        actor_id=user.id,
        organization_id=sess.active_organization_id,
        subject_type="session",
        subject_id=sess.id,
    )
    return IssuedSession(raw, csrf, sess)


def resolve_session(db: Session, raw: str | None) -> tuple[UserSession, User] | None:
    if not raw:
        return None
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == security.hash_token(raw)))
    now = utcnow()
    s = get_settings()
    if not sess or sess.revoked_at or sess.expires_at < now:
        return None
    if sess.last_seen_at < now - timedelta(hours=s.session_idle_timeout_hours):
        sess.revoked_at = now
        return None
    user = db.get(User, sess.user_id)
    if not user or not user.is_active or user.deleted_at:
        return None
    if (now - sess.last_seen_at).total_seconds() > 60:
        sess.last_seen_at = now
    return sess, user


def revoke_session(db: Session, raw: str | None) -> None:
    if not raw:
        return
    db.execute(
        update(UserSession)
        .where(UserSession.token_hash == security.hash_token(raw), UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def membership_for(db: Session, user_id: uuid.UUID, org_id: uuid.UUID) -> OrganizationMember | None:
    return db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.user_id == user_id,
            OrganizationMember.organization_id == org_id,
            OrganizationMember.is_active.is_(True),
        )
    )


def switch_organization(db: Session, sess: UserSession, org_id: uuid.UUID) -> None:
    if not membership_for(db, sess.user_id, org_id):
        raise PermissionDenied("You are not a member of that organization.")
    sess.active_organization_id = org_id
    audit.record(db, "session.org_switched", actor_id=sess.user_id, organization_id=org_id)


# ---- API tokens (MCP / programmatic access) ----

API_TOKEN_PREFIX = "tpk_"  # noqa: S105 (public prefix, not a secret)


def create_api_token(
    db: Session, ctx: TenantContext, name: str, scopes: list[str] | None = None, ttl_days: int | None = 90
) -> tuple[ApiToken, str]:
    raw = API_TOKEN_PREFIX + security.new_token(32)
    tok = ApiToken(
        organization_id=ctx.organization_id,
        user_id=ctx.user_id,
        name=name.strip()[:120] or "API token",
        token_hash=security.hash_token(raw),
        token_prefix=raw[:10],
        scopes=scopes or [],
        expires_at=utcnow() + timedelta(days=ttl_days) if ttl_days else None,
    )
    db.add(tok)
    db.flush()
    audit.record(db, "api_token.created", ctx=ctx, subject_type="api_token", subject_id=tok.id)
    return tok, raw


def resolve_api_token(db: Session, raw: str | None, *, channel: str = "api") -> TenantContext | None:
    if not raw or not raw.startswith(API_TOKEN_PREFIX):
        return None
    tok = db.scalar(select(ApiToken).where(ApiToken.token_hash == security.hash_token(raw)))
    now = utcnow()
    if not tok or tok.revoked_at or (tok.expires_at and tok.expires_at < now):
        return None
    member = membership_for(db, tok.user_id, tok.organization_id)
    user = db.get(User, tok.user_id)
    if not member or not user or not user.is_active:
        return None
    from app.db.models import Organization  # local import avoids cycle at module import time

    org = db.get(Organization, tok.organization_id)
    if not org or org.status != "active":
        return None
    tok.last_used_at = now
    return TenantContext(
        user_id=user.id,
        organization_id=tok.organization_id,
        role=member.role,
        email=user.email,
        channel=channel,
        scopes=frozenset(tok.scopes or []),
    )


def revoke_api_token(db: Session, ctx: TenantContext, token_id: uuid.UUID) -> None:
    tok = db.scalar(
        select(ApiToken).where(ApiToken.id == token_id, ApiToken.organization_id == ctx.organization_id)
    )
    if not tok or (tok.user_id != ctx.user_id and ctx.role not in ("owner", "admin")):
        raise PermissionDenied("Token not found.")
    tok.revoked_at = utcnow()
    audit.record(db, "api_token.revoked", ctx=ctx, subject_type="api_token", subject_id=tok.id)
