"""Tender feed subscriptions and portal email alerts: the authorised ways tenders flow in automatically.

* **Feed subscriptions** (``integration_connections.provider = 'tender_feed'``): a URL from a licensed data provider,
  an open-data publication or a partner system, in OCDS, RSS/Atom, JSON or CSV. Polled on a schedule through the
  SSRF-hardened fetcher. An optional auth header is stored encrypted.
* **Email alerts** (``provider = 'email_alerts'``, one per organization): the organization's own CPPP/GeM/state portal
  alert emails, forwarded to a private inbound URL (``POST /api/v1/inbound/email/{token}``) or uploaded as ``.eml``.
  Only an HMAC of the token is stored.

New items become ``discovered`` tenders, de-duplicated by reference number, then source item id, then source URL. The
match scores are recomputed and bid managers get one notification per import. Nothing here logs in to a portal,
downloads behind a login/CAPTCHA, or scrapes portal pages.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.permissions import Perm, has_permission
from app.core import security
from app.core.errors import AppError, NotFound, PermissionDenied, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import IntegrationConnection, Organization, OrganizationMember, Tender, TenderStatus
from app.db.models.identity import OrgStatus, Role
from app.db.tenancy import system_scope, tenant_scope
from app.integrations import email_alerts, feeds, url_fetch
from app.integrations.sources import SourceTender
from app.services import audit

log = get_logger(__name__)

FEED = "tender_feed"
EMAIL = "email_alerts"
POLL_CHOICES = (15, 30, 60, 180, 360, 720, 1440)
MAX_FEEDS_PER_ORG = 20
_RECENT_MESSAGE_IDS = 300


@dataclass
class IngestResult:
    created: list[Tender] = field(default_factory=list)
    duplicates: int = 0
    total: int = 0

    def summary(self) -> dict[str, int]:
        return {"created": len(self.created), "duplicates": self.duplicates, "items": self.total}


# ---------------------------------------------------------------- shared ingestion


def _existing(db: Session, org_id: uuid.UUID, st: SourceTender) -> bool:
    conds = []
    if st.reference_number:
        conds.append(Tender.reference_number == st.reference_number)
    if st.external_id:
        conds.append(Tender.extraction["source_item_id"].astext == st.external_id)
    if st.source_url and not st.reference_number and not st.external_id:
        conds.append(Tender.source_url == st.source_url)
    if not conds:
        conds.append((Tender.title == st.title) & (Tender.submission_deadline == st.submission_deadline))
    return db.scalar(select(Tender.id).where(Tender.organization_id == org_id, Tender.deleted_at.is_(None), or_(*conds)).limit(1)) is not None


def ingest(db: Session, ctx: TenantContext, items: list[SourceTender], *, source_name: str,
           provenance: dict[str, Any] | None = None, notify: bool = True) -> IngestResult:
    """Create `discovered` tenders for items not seen before. Feed values are recorded as source data, not as manual
    edits, so later extraction from the tender documents can still fill and correct them."""
    from app.services.tenders import service as tenders

    ctx.require(Perm.TENDERS_WRITE)
    res = IngestResult(total=len(items))
    seen: set[str] = set()
    for st in items:
        key = st.reference_number or st.external_id or st.source_url or f"{st.title}|{st.submission_deadline}"
        if key in seen or _existing(db, ctx.organization_id, st):
            res.duplicates += 1
            continue
        seen.add(key)
        t = tenders.create_tender(db, ctx, {
            "title": st.title, "reference_number": st.reference_number, "procuring_organization": st.procuring_organization,
            "source_url": st.source_url, "estimated_value": st.estimated_value, "location_state": st.location_state,
            "submission_deadline": st.submission_deadline, "category": st.category, "description": st.description,
            "source_platform": (st.source_platform or source_name)[:80],
        })
        t.status = TenderStatus.DISCOVERED
        t.extraction = {"manual_fields": [], "source": {"kind": "feed", "name": source_name, **(provenance or {})},
                        **({"source_item_id": st.external_id} if st.external_id else {})}
        res.created.append(t)
    db.flush()
    if res.created:
        from app.services.tenders import matching

        try:
            matching.recompute(db, ctx)
        except AppError as e:  # matching is a convenience; never lose an import over it
            log.warning("feeds.matching_failed", error=str(e))
        if notify:
            from app.services.notifications.service import notify_roles

            n = len(res.created)
            notify_roles(db, ctx.organization_id, {Role.OWNER, Role.ADMIN, Role.BID_MANAGER}, kind="tender_discovered",
                         title=f"{n} new tender{'s' if n != 1 else ''} from {source_name}"[:300],
                         body=", ".join(t.title for t in res.created[:3])[:500] + (" …" if n > 3 else ""),
                         link="/tenders?status=discovered", dedup_key=f"discovered:{uuid.uuid4()}")
    return res


# ---------------------------------------------------------------- feed subscriptions


def _get(db: Session, ctx: TenantContext, conn_id: uuid.UUID, provider: str) -> IntegrationConnection:
    c = db.scalar(select(IntegrationConnection).where(IntegrationConnection.id == conn_id, IntegrationConnection.provider == provider,
                                                      IntegrationConnection.organization_id == ctx.organization_id))
    if not c:
        raise NotFound("Feed not found.")
    return c


def list_feeds(db: Session, ctx: TenantContext) -> list[IntegrationConnection]:
    ctx.require(Perm.TENDERS_READ)
    return list(db.scalars(select(IntegrationConnection).where(IntegrationConnection.organization_id == ctx.organization_id,
                                                               IntegrationConnection.provider == FEED).order_by(IntegrationConnection.created_at)))


def feed_view(c: IntegrationConnection) -> dict[str, Any]:
    cfg = c.config or {}
    return {"id": str(c.id), "name": c.display_name, "url": cfg.get("url"), "format": cfg.get("format", "auto"),
            "poll_minutes": cfg.get("poll_minutes"), "status": c.status, "has_auth_header": bool(c.encrypted_credentials),
            "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None, "last_error": c.last_error,
            "last_result": cfg.get("last_result"), "licence_note": cfg.get("licence_note")}


def create_feed(db: Session, ctx: TenantContext, *, name: str, url: str, fmt: str = "auto", poll_minutes: int = 60,
                auth_header_name: str | None = None, auth_header_value: str | None = None, licence_note: str | None = None,
                field_map: dict[str, list[str]] | None = None) -> IntegrationConnection:
    ctx.require(Perm.INTEGRATIONS_MANAGE)
    name = (name or "").strip()[:120]
    if not name:
        raise ValidationFailed("Give the feed a name.")
    if fmt not in feeds.FORMATS:
        raise ValidationFailed(f"Format must be one of: {', '.join(feeds.FORMATS)}.")
    if poll_minutes not in POLL_CHOICES:
        raise ValidationFailed(f"Poll interval must be one of {', '.join(map(str, POLL_CHOICES))} minutes.")
    if not url.lower().startswith("https://"):
        raise ValidationFailed("Feed URLs must use https.")
    url_fetch.validate_url(url)
    if len(list_feeds(db, ctx)) >= MAX_FEEDS_PER_ORG:
        raise ValidationFailed(f"At most {MAX_FEEDS_PER_ORG} feeds per organization.")
    creds = None
    if auth_header_value:
        hname = (auth_header_name or "Authorization").strip()
        if not hname.replace("-", "").isalnum() or hname.lower() in ("host", "cookie", "content-length", "transfer-encoding"):
            raise ValidationFailed("That header name is not allowed.")
        creds = security.encrypt_secret(f"{hname}\n{auth_header_value.strip()}")
    c = IntegrationConnection(organization_id=ctx.organization_id, provider=FEED, display_name=name, status="active",
                              config={"url": url.strip(), "format": fmt, "poll_minutes": poll_minutes, "field_map": field_map or {},
                                      "licence_note": (licence_note or "").strip()[:300] or None},
                              encrypted_credentials=creds, created_by_id=ctx.user_id)
    db.add(c)
    db.flush()
    audit.record(db, "tender_feed.created", ctx=ctx, subject_type="integration", subject_id=c.id, data={"host": url_fetch.urlsplit(url).hostname, "format": fmt})
    return c


def set_feed_enabled(db: Session, ctx: TenantContext, conn_id: uuid.UUID, enabled: bool) -> IntegrationConnection:
    ctx.require(Perm.INTEGRATIONS_MANAGE)
    c = _get(db, ctx, conn_id, FEED)
    c.status = "active" if enabled else "paused"
    audit.record(db, "tender_feed.resumed" if enabled else "tender_feed.paused", ctx=ctx, subject_type="integration", subject_id=c.id)
    return c


def delete_feed(db: Session, ctx: TenantContext, conn_id: uuid.UUID) -> None:
    ctx.require(Perm.INTEGRATIONS_MANAGE)
    c = _get(db, ctx, conn_id, FEED)
    audit.record(db, "tender_feed.deleted", ctx=ctx, subject_type="integration", subject_id=c.id)
    db.delete(c)


def poll_feed(db: Session, ctx: TenantContext, conn_id: uuid.UUID) -> IngestResult:
    ctx.require(Perm.TENDERS_WRITE)
    c = _get(db, ctx, conn_id, FEED)
    cfg = dict(c.config or {})
    headers = None
    if c.encrypted_credentials:
        hname, _, hval = security.decrypt_secret(c.encrypted_credentials).partition("\n")
        headers = {hname: hval}
    try:
        fetched = url_fetch.fetch(cfg["url"], headers=headers, user_agent="TenderPilotAI-FeedSync/1.0 (+authorised feed)")
        items = feeds.parse_feed(fetched.content, cfg.get("format", "auto"), cfg.get("field_map"))
        res = ingest(db, ctx, items, source_name=c.display_name, provenance={"feed_id": str(c.id)})
    except AppError as e:
        c.status = "error" if c.status != "paused" else c.status
        c.last_error = e.message[:1000]
        c.last_sync_at = utcnow()
        log.info("tender_feed.poll_failed", feed_id=str(c.id), error=e.message)
        raise
    c.status = "active" if c.status != "paused" else c.status
    c.last_error = None
    c.last_sync_at = utcnow()
    c.config = {**cfg, "last_result": res.summary()}
    audit.record(db, "tender_feed.polled", ctx=ctx, subject_type="integration", subject_id=c.id, data=res.summary())
    return res


def import_file(db: Session, ctx: TenantContext, payload: bytes, fmt: str = "auto", *, source_name: str = "Feed file") -> IngestResult:
    """One-off import of a downloaded feed file (OCDS JSON, RSS/Atom, JSON or CSV)."""
    ctx.require(Perm.TENDERS_WRITE)
    items = feeds.parse_feed(payload, fmt)
    res = ingest(db, ctx, items, source_name=source_name)
    audit.record(db, "tender.feed_imported", ctx=ctx, data={"source": source_name, **res.summary()})
    return res


def _actor(db: Session, c: IntegrationConnection) -> TenantContext:
    """Background work acts as the member who set the integration up, with their current role. If they left or lost
    tender-write permission, the integration stops instead of acting with more rights than any member granted it."""
    m = db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == c.organization_id,
                                                   OrganizationMember.user_id == c.created_by_id, OrganizationMember.is_active.is_(True))) if c.created_by_id else None
    if not m or not has_permission(m.role, Perm.TENDERS_WRITE):
        raise PermissionDenied("The member who set up this integration no longer has permission to add tenders. Ask an admin to set it up again.")
    return TenantContext(user_id=m.user_id, organization_id=c.organization_id, role=m.role, channel="worker")


def poll_due_feeds(db: Session) -> dict[str, int]:
    """Beat task body: poll every active feed whose interval has elapsed. Each feed commits on its own."""
    now = utcnow()
    with system_scope():
        rows = db.execute(
            select(IntegrationConnection.id, IntegrationConnection.organization_id, IntegrationConnection.config, IntegrationConnection.last_sync_at)
            .join(Organization, Organization.id == IntegrationConnection.organization_id)
            .where(IntegrationConnection.provider == FEED, IntegrationConnection.status.in_(("active", "error")), Organization.status == OrgStatus.ACTIVE)
        ).all()
    stats = {"polled": 0, "failed": 0, "created": 0}
    for conn_id, org_id, cfg, last in rows:
        interval = timedelta(minutes=int((cfg or {}).get("poll_minutes") or 60))
        if last and now - last < interval:
            continue
        with tenant_scope(org_id):
            try:
                c = db.get(IntegrationConnection, conn_id)
                if c is None:
                    continue
                res = poll_feed(db, _actor(db, c), conn_id)
                db.commit()
                stats["polled"] += 1
                stats["created"] += len(res.created)
            except AppError as e:
                db.rollback()
                with tenant_scope(org_id):
                    c = db.get(IntegrationConnection, conn_id)
                    if c is not None:
                        c.status, c.last_error, c.last_sync_at = "error", e.message[:1000], utcnow()
                        db.commit()
                stats["failed"] += 1
            except Exception:  # noqa: BLE001 - one broken feed must not stop the others
                db.rollback()
                log.exception("tender_feed.poll_crashed", feed_id=str(conn_id))
                stats["failed"] += 1
    return stats


# ---------------------------------------------------------------- email alerts


def email_settings(db: Session, ctx: TenantContext) -> IntegrationConnection | None:
    ctx.require(Perm.TENDERS_READ)
    return db.scalar(select(IntegrationConnection).where(IntegrationConnection.organization_id == ctx.organization_id,
                                                         IntegrationConnection.provider == EMAIL))


def email_view(c: IntegrationConnection | None) -> dict[str, Any]:
    if not c:
        return {"enabled": False}
    cfg = c.config or {}
    return {"enabled": c.status == "active", "status": c.status, "token_hint": cfg.get("token_hint"), "allowed_sender_domains": cfg.get("allowed_sender_domains", []),
            "last_received_at": c.last_sync_at.isoformat() if c.last_sync_at else None, "last_error": c.last_error, "last_result": cfg.get("last_result")}


def enable_email_alerts(db: Session, ctx: TenantContext, allowed_sender_domains: list[str] | None = None) -> tuple[IntegrationConnection, str]:
    """Create or rotate the private inbound token. Returns the raw token once; only its HMAC is stored."""
    ctx.require(Perm.INTEGRATIONS_MANAGE)
    domains = sorted({d.strip().lower().lstrip("@") for d in (allowed_sender_domains or []) if d.strip()})
    token = security.new_token(24)
    c = email_settings(db, ctx)
    cfg = {"token_hash": security.hash_token(token), "token_hint": token[-4:], "allowed_sender_domains": domains}
    if c is None:
        c = IntegrationConnection(organization_id=ctx.organization_id, provider=EMAIL, display_name="Portal email alerts", status="active",
                                  config=cfg, created_by_id=ctx.user_id)
        db.add(c)
    else:
        c.config = {**(c.config or {}), **cfg}
        c.status, c.created_by_id = "active", ctx.user_id
    db.flush()
    audit.record(db, "email_alerts.enabled", ctx=ctx, subject_type="integration", subject_id=c.id, data={"allowed_sender_domains": domains})
    return c, token


def disable_email_alerts(db: Session, ctx: TenantContext) -> None:
    ctx.require(Perm.INTEGRATIONS_MANAGE)
    c = email_settings(db, ctx)
    if c:
        c.status = "disabled"
        c.config = {k: v for k, v in (c.config or {}).items() if k != "token_hash"}
        audit.record(db, "email_alerts.disabled", ctx=ctx, subject_type="integration", subject_id=c.id)


def ingest_email(db: Session, ctx: TenantContext, raw: bytes, *, channel: str = "upload",
                 allowed_sender_domains: list[str] | None = None) -> tuple[email_alerts.AlertEmail, IngestResult]:
    ctx.require(Perm.TENDERS_WRITE)
    alert = email_alerts.parse_alert_email(raw)
    if allowed_sender_domains:
        dom = alert.sender.rpartition("@")[2]
        if not any(dom == d or dom.endswith("." + d) for d in allowed_sender_domains):
            raise PermissionDenied("This sender is not on the allowed list for portal alerts.")
    c = email_settings(db, ctx)
    recent = list(((c.config or {}).get("recent_message_ids") or []) if c else [])
    if alert.message_id in recent:
        return alert, IngestResult(duplicates=len(alert.tenders), total=len(alert.tenders))
    label = f"{alert.portal} email alert" if alert.portal else "Email alert"
    res = ingest(db, ctx, alert.tenders, source_name=label,
                 provenance={"channel": channel, "sender": alert.sender[:200], "subject": alert.subject[:300], "message_id": alert.message_id[:300],
                             "received_at": alert.received_at.isoformat() if alert.received_at else None})
    if c is not None:
        c.config = {**(c.config or {}), "recent_message_ids": [*recent, alert.message_id][-_RECENT_MESSAGE_IDS:], "last_result": res.summary()}
        c.last_sync_at, c.last_error = utcnow(), None
    audit.record(db, "email_alerts.ingested", ctx=ctx, data={"channel": channel, "portal": alert.portal, "sender_domain": alert.sender.rpartition("@")[2], **res.summary()})
    return alert, res


def receive_inbound(db: Session, token: str, raw: bytes) -> IngestResult:
    """Inbound email webhook. The token identifies the organization; it is compared by HMAC, never stored in clear."""
    if not token or len(token) > 200:
        raise NotFound("Unknown inbound address.")
    digest = security.hash_token(token)
    with system_scope():
        c = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == EMAIL, IntegrationConnection.status == "active",
                                                          IntegrationConnection.config["token_hash"].astext == digest))
        org = db.get(Organization, c.organization_id) if c else None
        if not c or not org or org.status != OrgStatus.ACTIVE:
            raise NotFound("Unknown inbound address.")
        org_id, conn_id = c.organization_id, c.id
    with tenant_scope(org_id):
        c = db.get(IntegrationConnection, conn_id)
        assert c is not None
        ctx = _actor(db, c)
        _alert, res = ingest_email(db, ctx, raw, channel="inbound", allowed_sender_domains=(c.config or {}).get("allowed_sender_domains") or None)
        return res
