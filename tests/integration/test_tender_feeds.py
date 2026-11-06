"""Feed subscriptions, scheduled polling, portal email alerts (upload + inbound webhook), de-duplication, permissions
and tenant isolation."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.errors import NotFound, PermissionDenied, ValidationFailed
from app.integrations import url_fetch
from tests.conftest import login

pytestmark = pytest.mark.usefixtures("seeded")
FX = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"


@pytest.fixture
def fake_net(monkeypatch):
    """No real network: DNS validation passes for public-looking hosts and fetch returns the fixture we choose."""
    calls: list[dict] = []
    state = {"payload": (FX / "provider_feed.rss").read_bytes(), "error": None}
    monkeypatch.setattr(url_fetch, "validate_url", lambda url: ("feeds.example.org", "93.184.216.34", 443))

    def fake_fetch(url, *, headers=None, user_agent=""):
        calls.append({"url": url, "headers": headers, "ua": user_agent})
        if state["error"]:
            raise ValidationFailed(state["error"])
        return url_fetch.Fetched(url, state["payload"], "application/rss+xml", "feed.rss")

    monkeypatch.setattr(url_fetch, "fetch", fake_fetch)
    return calls, state


def test_feed_lifecycle_poll_dedup_notify_and_secret_storage(seeded, db, fake_net):
    from app.db.models import IntegrationConnection, Notification, Tender
    from app.services.tenders import feeds

    calls, _ = fake_net
    owner, bids, viewer = (seeded.contexts[e] for e in ("owner@voltedge.example", "bids@voltedge.example", "viewer@voltedge.example"))

    with pytest.raises(PermissionDenied):  # bid managers poll feeds but cannot add outbound integrations
        feeds.create_feed(db, bids, name="x", url="https://feeds.example.org/rss")
    with pytest.raises(ValidationFailed):
        feeds.create_feed(db, owner, name="Plain http", url="http://feeds.example.org/rss")
    with pytest.raises(ValidationFailed):
        feeds.create_feed(db, owner, name="Bad header", url="https://feeds.example.org/rss", auth_header_name="Cookie", auth_header_value="x")

    c = feeds.create_feed(db, owner, name="Synthetic provider", url="https://feeds.example.org/rss", poll_minutes=30,
                          auth_header_name="X-Api-Key", auth_header_value="test-provider-key-123")
    db.commit()
    stored = db.get(IntegrationConnection, c.id)
    assert "test-provider-key-123" not in str(stored.config) and "test-provider-key-123" not in (stored.encrypted_credentials or "")
    assert feeds.feed_view(stored)["has_auth_header"] is True

    res = feeds.poll_feed(db, bids, c.id)
    db.commit()
    assert len(res.created) == 2 and res.duplicates == 0
    assert calls[-1]["headers"] == {"X-Api-Key": "test-provider-key-123"}
    gem = db.scalar(select(Tender).where(Tender.organization_id == owner.organization_id, Tender.reference_number == "GEM/2026/B/9100001"))
    assert gem.status == "discovered" and gem.source_platform == "Synthetic provider"
    assert gem.extraction["source"]["feed_id"] == str(c.id) and gem.extraction["manual_fields"] == []
    no_ref = db.scalar(select(Tender).where(Tender.organization_id == owner.organization_id, Tender.extraction["source_item_id"].astext == "provider-item-42"))
    assert no_ref is not None and no_ref.reference_number is None

    again = feeds.poll_feed(db, bids, c.id)
    db.commit()
    assert again.created == [] and again.duplicates == 2  # by reference number and by feed item id
    assert db.get(IntegrationConnection, c.id).config["last_result"] == {"created": 0, "duplicates": 2, "items": 2}

    note = db.scalar(select(Notification).where(Notification.user_id == bids.user_id, Notification.kind == "tender_discovered"))
    assert note is not None and note.title.startswith("2 new tenders from Synthetic provider")

    with pytest.raises(PermissionDenied):
        feeds.poll_feed(db, viewer, c.id)
    with pytest.raises(NotFound):  # another organization cannot see or poll it
        feeds.poll_feed(db, seeded.contexts["owner@kaveri.example"], c.id)
    assert feeds.list_feeds(db, seeded.contexts["owner@kaveri.example"]) == []

    feeds.set_feed_enabled(db, owner, c.id, False)
    feeds.delete_feed(db, owner, c.id)
    db.commit()
    assert db.get(IntegrationConnection, c.id) is None


def test_scheduled_polling_respects_interval_and_records_errors(seeded, db, fake_net):
    from app.db.base import utcnow
    from app.db.models import IntegrationConnection
    from app.db.tenancy import tenant_scope
    from app.services.tenders import feeds

    calls, state = fake_net
    owner = seeded.contexts["owner@kaveri.example"]
    state["payload"] = (FX / "ocds_release_package.json").read_bytes()
    c = feeds.create_feed(db, owner, name="Open data OCDS", url="https://feeds.example.org/ocds.json", fmt="ocds", poll_minutes=60)
    db.commit()
    cid = c.id

    stats = feeds.poll_due_feeds(db)
    assert stats["polled"] >= 1 and stats["created"] >= 1
    n_calls = len(calls)
    assert feeds.poll_due_feeds(db)["polled"] == 0 and len(calls) == n_calls  # not due again for an hour

    with tenant_scope(owner.organization_id):
        db.get(IntegrationConnection, cid).last_sync_at = utcnow() - timedelta(hours=2)
        db.commit()
    state["error"] = "The source returned HTTP 503."
    assert feeds.poll_due_feeds(db)["failed"] == 1
    with tenant_scope(owner.organization_id):
        row = db.get(IntegrationConnection, cid)
        assert row.status == "error" and row.last_error == "The source returned HTTP 503."


def test_inbound_email_webhook_dedup_sender_allowlist_and_rotation(seeded, client, db):
    from app.db.models import IntegrationConnection, Tender
    from app.db.tenancy import system_scope

    c, csrf = login(client, "owner@voltedge.example")
    r = c.post("/api/v1/email-alerts/enable", json={"allowed_sender_domains": []}, headers=csrf)
    assert r.status_code == 200, r.text
    url = r.json()["inbound_url"]
    path = url.split("://", 1)[1].split("/", 1)[1]
    token = path.rsplit("/", 1)[1]
    with system_scope():
        cfg = db.scalar(select(IntegrationConnection.config).where(IntegrationConnection.provider == "email_alerts",
                                                                   IntegrationConnection.organization_id == seeded.contexts["owner@voltedge.example"].organization_id))
    assert token not in str(cfg) and cfg["token_hint"] == token[-4:]

    gem = (FX / "gem_alert.eml").read_bytes()
    r = client.post(f"/{path}", content=gem, headers={"Content-Type": "message/rfc822"})  # no cookie, no CSRF: the token is the credential
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2
    r = client.post(f"/{path}", files={"body-mime": ("m.eml", gem, "message/rfc822")})  # same message again, multipart shape
    assert r.json() == {"accepted": True, "created": 0, "duplicates": 2, "items": 2}

    with system_scope():
        t = db.scalar(select(Tender).where(Tender.reference_number == "GEM/2026/B/9000001"))
    assert t.organization_id == seeded.contexts["owner@voltedge.example"].organization_id
    assert t.source_platform == "GeM" and t.extraction["source"]["channel"] == "inbound"
    assert t.extraction["source"]["sender"] == "alerts@gem.gov.in"

    assert client.post("/api/v1/inbound/email/not-a-real-token", content=gem).status_code == 404

    # Restrict senders and rotate: the old address stops working; a CPPP mail from nic.in is refused.
    r = c.post("/api/v1/email-alerts/enable", json={"allowed_sender_domains": ["gem.gov.in"]}, headers=csrf)
    new_path = r.json()["inbound_url"].split("://", 1)[1].split("/", 1)[1]
    assert client.post(f"/{path}", content=gem).status_code == 404
    assert client.post(f"/{new_path}", content=(FX / "cppp_alert.eml").read_bytes()).status_code == 403

    assert c.post("/api/v1/email-alerts/disable", headers=csrf).status_code == 204
    assert client.post(f"/{new_path}", content=gem).status_code == 404


def test_file_imports_through_sources_and_permissions(seeded, client):
    c, csrf = login(client, "owner@kaveri.example")
    r = c.post("/api/v1/tender-sources/email_alert/import", files={"file": ("a.eml", (FX / "cppp_alert.eml").read_bytes())}, headers=csrf)
    assert r.status_code == 201, r.text
    assert len(r.json()["created"]) == 2
    r = c.post("/api/v1/tender-sources/feed_file/import", files={"file": ("p.rss", (FX / "provider_feed.rss").read_bytes())}, headers=csrf)
    assert r.status_code == 201 and len(r.json()["created"]) == 2
    r = c.post("/api/v1/tender-sources/feed_file/import", files={"file": ("p.rss", (FX / "provider_feed.rss").read_bytes())}, headers=csrf)
    assert r.json()["created"] == []
    keys = {s["key"]: s for s in c.get("/api/v1/tender-sources").json()}
    assert keys["feed_file"]["supported"] and keys["email_alert"]["supported"] and not keys["gem"]["supported"]
    assert "email alerts" in keys["gem"]["note"]

    v, vcsrf = login(client, "viewer@voltedge.example")
    assert v.post("/api/v1/email-alerts/enable", json={}, headers=vcsrf).status_code == 403
    assert v.post("/api/v1/tender-feeds", json={"name": "x", "url": "https://feeds.example.org/x"}, headers=vcsrf).status_code == 403


def test_inbound_token_is_redacted_from_logs():
    import logging

    from app.core.logging import _RedactPathFilter, redact_path

    assert redact_path("/api/v1/inbound/email/abcDEF123_-x") == "/api/v1/inbound/email/[redacted]"
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1", "POST", "/api/v1/inbound/email/secret123", "1.1", 200), None)
    _RedactPathFilter().filter(rec)
    assert "secret123" not in rec.getMessage()
