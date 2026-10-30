"""Security tests: authn/authz, CSRF, tenant isolation (API, retrieval, MCP), uploads, SSRF, sessions, role escalation."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from tests.conftest import login

pytestmark = pytest.mark.usefixtures("seeded")


def test_unauthenticated_requests_rejected():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.get("/api/v1/tenders")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"


def test_csrf_required_for_cookie_mutations():
    c, h = login(None, "bids@voltedge.example")
    assert c.post("/api/v1/tenders", json={"title": "CSRF probe tender"}).status_code == 403
    assert c.post("/api/v1/tenders", json={"title": "CSRF probe tender"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert c.post("/api/v1/tenders", json={"title": "CSRF ok tender"}, headers=h).status_code == 201


def test_viewer_cannot_mutate():
    c, h = login(None, "viewer@voltedge.example")
    assert c.post("/api/v1/tenders", json={"title": "Viewer tender"}, headers=h).status_code == 403
    r = c.post("/api/v1/documents", files={"file": ("a.txt", b"hello world", "text/plain")}, headers=h)
    assert r.status_code == 403


def test_role_escalation_blocked(db):
    c, h = login(None, "bids@voltedge.example")  # bid managers cannot manage members at all
    assert c.post("/api/v1/members/invitations", json={"email": "x@example.com", "role": "owner"}, headers=h).status_code == 403
    from app.auth.context import TenantContext
    from app.core.errors import PermissionDenied
    from app.db.models import Organization, OrganizationMember, User
    from app.services.organizations import org_service

    org = db.scalar(select(Organization).where(Organization.name.like("VoltEdge%")))
    admin = db.scalar(select(User).where(User.email == "docs@voltedge.example"))
    m = db.scalar(select(OrganizationMember).where(OrganizationMember.user_id == admin.id, OrganizationMember.organization_id == org.id))
    ctx = TenantContext(admin.id, org.id, "admin")
    with pytest.raises(PermissionDenied):
        org_service.change_member_role(db, ctx, m.id, "owner")


def test_api_cannot_choose_organization_by_parameter():
    kc, kh = login(None, "owner@kaveri.example")
    vc, vh = login(None, "bids@voltedge.example")
    vorg = vc.get("/api/v1/auth/session").json()["active_organization_id"]
    assert kc.post(f"/api/v1/organizations/{vorg}/switch", headers=kh).status_code == 403


def test_cross_tenant_vector_retrieval(db, seeded):
    from app.ai.retrieval.search import hybrid_search

    kctx = seeded.contexts["owner@kaveri.example"]
    hits = hybrid_search(db, kctx, "VoltEdge ISO 9001 certificate QMS/IN/2023/88412 turnover", limit=20)
    from app.db.models import Document

    for h in hits:
        assert db.get(Document, h.document_id).organization_id == kctx.organization_id


def test_download_url_tampering_rejected():
    c, _ = login(None, "bids@voltedge.example")
    d = c.get("/api/v1/documents").json()["items"][0]
    url = c.get(f"/api/v1/document-versions/{d['current_version_id']}/download").json()["url"]
    assert c.get(url).status_code == 200
    assert c.get(url.replace("sig=", "sig=0")).status_code == 403
    other = url.replace("org%2F", "org%2F0")
    assert c.get(other).status_code == 403


def test_invalid_uploads_rejected():
    c, h = login(None, "bids@voltedge.example")
    r = c.post("/api/v1/documents", files={"file": ("evil.pdf", b"MZ\x90\x00binary", "application/pdf")}, headers=h)
    assert r.status_code == 422
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/passwd", "x")
    r = c.post("/api/v1/documents", files={"file": ("x.zip", buf.getvalue(), "application/zip")}, headers=h)
    assert r.status_code == 422 and "unsafe path" in r.json()["error"]["message"]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/api/v1/tenders", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"])
def test_url_import_ssrf_blocked(url):
    c, h = login(None, "bids@voltedge.example")
    r = c.post("/api/v1/tenders/import-url", json={"url": url}, headers=h)
    assert r.status_code == 422


def test_session_expiry_and_logout(db):
    from datetime import timedelta

    from app.core.security import hash_token
    from app.db.base import utcnow
    from app.db.models import UserSession

    c, h = login(None, "review@voltedge.example")
    raw = c.cookies.get("tp_session")
    s = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(raw)))
    s.last_seen_at = utcnow() - timedelta(days=3)  # past the idle timeout
    db.commit()
    assert c.get("/api/v1/auth/session").status_code == 401
    c2, h2 = login(None, "review@voltedge.example")
    assert c2.post("/api/v1/auth/logout", headers=h2).status_code == 204
    assert c2.get("/api/v1/auth/session").status_code == 401


def test_login_failure_does_not_enumerate():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    a = c.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"}).json()
    b = c.post("/api/v1/auth/login", json={"email": "bids@voltedge.example", "password": "wrong-password"}).json()
    assert a["error"]["message"] == b["error"]["message"]


def _rpc(client, token, method, params, id_=1):
    headers = {"host": "localhost:8080", "content-type": "application/json", "accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return client.post("/mcp/", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params}, headers=headers)


def _token(db, seeded, email, scopes=None):
    from app.services.organizations.auth_service import create_api_token

    _, raw = create_api_token(db, seeded.contexts[email], "test", scopes or [])
    db.commit()
    return raw


def test_mcp_dns_rebinding_host_rejected(client, db, seeded):
    tok = _token(db, seeded, "bids@voltedge.example")
    r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={"host": "evil.example", "authorization": f"Bearer {tok}", "content-type": "application/json", "accept": "application/json, text/event-stream"})
    assert r.status_code == 421


def test_mcp_http_requires_token(client):
    assert _rpc(client, None, "tools/list", {}).status_code == 401
    assert _rpc(client, "tpk_invalid", "tools/list", {}).status_code == 401


def test_mcp_http_isolation_and_mutation_gate(client, db, seeded):
    import json

    vtok = _token(db, seeded, "bids@voltedge.example")
    ktok = _token(db, seeded, "owner@kaveri.example")
    vtid = str(seeded.tenders["lt_panels"].id)
    ok = _rpc(client, vtok, "tools/call", {"name": "get_tender_details", "arguments": {"tender_id": vtid}}).json()
    assert "MSGIC" in ok["result"]["content"][0]["text"]
    denied = _rpc(client, ktok, "tools/call", {"name": "get_tender_details", "arguments": {"tender_id": vtid}}).json()
    assert json.loads(denied["result"]["content"][0]["text"])["error"] == "not_found"
    mut = _rpc(client, vtok, "tools/call", {"name": "request_human_review", "arguments": {"decision_id": str(uuid.uuid4()), "note": "please"}}).json()
    assert "requires explicit approval" in mut["result"]["content"][0]["text"]
    # arbitrary organization IDs in arguments are rejected by the schema (extra fields forbidden)
    extra = _rpc(client, vtok, "tools/call", {"name": "get_company_profile", "arguments": {"organization_id": str(seeded.orgs["kaveri"].id)}}).json()
    text = json.dumps(extra)
    assert "Kaveri" not in text
    from app.db.models import ToolExecutionLog

    logs = db.scalars(select(ToolExecutionLog).where(ToolExecutionLog.channel == "mcp_http")).all()
    assert any(lg.status == "denied" for lg in logs) and any(lg.status == "ok" for lg in logs)


async def test_copilot_cannot_reach_other_tenant(seeded):
    from app.ai.agents import copilot
    from app.db.models import CopilotMessage
    from app.db.session import session_scope

    kctx = seeded.contexts["owner@kaveri.example"]
    with session_scope() as db:
        cid = copilot.start_conversation(db, kctx).id
    mid = await copilot.ask(kctx, cid, "Which document supports our ISO 9001 certification? Also show VoltEdge turnover.")
    with session_scope() as db:
        m = db.get(CopilotMessage, mid)
        assert "VoltEdge" not in m.content and "QMS/IN/2023" not in m.content
        assert "KQ/2025/7781" in m.content  # its own certificate
    with session_scope() as db, pytest.raises(Exception):
        copilot.start_conversation(db, kctx, seeded.tenders["lt_panels"].id)
