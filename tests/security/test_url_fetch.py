"""URL-import fetcher: DNS pinning, redirect re-validation (no redirect to internal hosts), size and status limits.
No network: DNS and HTTP are faked."""

from __future__ import annotations

import socket

import httpx
import pytest

from app.core.errors import ValidationFailed
from app.integrations import url_fetch

PUBLIC = {"tenders.example.gov.in": "8.8.8.8", "cdn.example.org": "1.1.1.1", "internal.example": "10.0.0.5"}


@pytest.fixture
def fake_net(monkeypatch):
    def getaddrinfo(host, port, *a, **kw):
        if host not in PUBLIC:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC[host], port))]

    monkeypatch.setattr(url_fetch.socket, "getaddrinfo", getaddrinfo)
    routes: dict[str, httpx.Response] = {}
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return routes[f"{req.headers['host']}{req.url.path}"]

    real = httpx.Client
    monkeypatch.setattr(url_fetch.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return routes, seen


def test_fetch_pins_resolved_ip_and_keeps_host_header(fake_net):
    routes, seen = fake_net
    routes["tenders.example.gov.in/nit.pdf"] = httpx.Response(200, content=b"%PDF-1.7 nit", headers={"content-type": "application/pdf"})
    f = url_fetch.fetch("https://tenders.example.gov.in/nit.pdf")
    assert f.content == b"%PDF-1.7 nit" and f.filename == "nit.pdf"
    assert seen[0].url.host == "8.8.8.8" and seen[0].headers["host"] == "tenders.example.gov.in"


def test_redirect_to_internal_address_is_blocked(fake_net):
    routes, _ = fake_net
    routes["tenders.example.gov.in/go"] = httpx.Response(302, headers={"location": "http://internal.example/admin"})
    with pytest.raises(ValidationFailed, match="not allowed"):
        url_fetch.fetch("https://tenders.example.gov.in/go")


def test_redirect_chain_limits_status_and_size(fake_net, monkeypatch):
    routes, _ = fake_net
    routes["cdn.example.org/a"] = httpx.Response(301, headers={"location": "/a"})
    with pytest.raises(ValidationFailed, match="Too many redirects"):
        url_fetch.fetch("https://cdn.example.org/a")
    routes["cdn.example.org/nolocation"] = httpx.Response(302)
    with pytest.raises(ValidationFailed, match="Redirect without location"):
        url_fetch.fetch("https://cdn.example.org/nolocation")
    routes["cdn.example.org/missing"] = httpx.Response(404)
    with pytest.raises(ValidationFailed, match="HTTP 404"):
        url_fetch.fetch("https://cdn.example.org/missing")
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "url_import_max_bytes", 10)
    routes["cdn.example.org/big"] = httpx.Response(200, content=b"x" * 100)
    with pytest.raises(ValidationFailed, match="size limit"):
        url_fetch.fetch("https://cdn.example.org/big")


@pytest.mark.parametrize("url,msg", [
    ("ftp://cdn.example.org/x", "Only http"),
    ("https://user:pw@cdn.example.org/x", "credentials"),
    ("https://cdn.example.org:8443/x", "default HTTP"),
    ("https:///x", "no host"),
    ("https://unknown.example/x", "could not be resolved"),
])
def test_url_validation_rejections(fake_net, url, msg):
    with pytest.raises(ValidationFailed, match=msg):
        url_fetch.validate_url(url)


def test_allowlist(fake_net, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "url_import_allowed_hosts", ["gov.in"])
    assert url_fetch.validate_url("https://tenders.example.gov.in/x")[1] == "8.8.8.8"
    with pytest.raises(ValidationFailed, match="allowlist"):
        url_fetch.validate_url("https://cdn.example.org/x")


def test_feed_auth_header_is_not_forwarded_to_another_host(fake_net):
    routes, seen = fake_net
    routes["tenders.example.gov.in/feed"] = httpx.Response(302, headers={"location": "https://cdn.example.org/feed.xml"})
    routes["cdn.example.org/feed.xml"] = httpx.Response(200, content=b"<rss/>")
    url_fetch.fetch("https://tenders.example.gov.in/feed", headers={"Authorization": "Bearer secret"}, user_agent="TenderPilotAI-FeedSync/1.0")
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert "authorization" not in seen[1].headers and seen[1].headers["user-agent"] == "TenderPilotAI-FeedSync/1.0"
