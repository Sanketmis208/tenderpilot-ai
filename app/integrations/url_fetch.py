"""SSRF-hardened URL retrieval for user-provided tender links.

- http/https only, default ports only; credentials in URLs rejected
- every hostname resolved and every resolved address checked (no private, loopback, link-local,
  multicast, reserved, CGNAT or cloud metadata addresses); the connection is pinned to the vetted IP
- redirects followed manually (max 3) with re-validation of each hop
- optional host allowlist (TP_URL_IMPORT_ALLOWED_HOSTS); size cap enforced while streaming
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import get_settings
from app.core.errors import ValidationFailed

BLOCKED_NETS = [ipaddress.ip_network(n) for n in ("100.64.0.0/10", "169.254.0.0/16", "fd00:ec2::/32")]


@dataclass
class Fetched:
    url: str
    content: bytes
    content_type: str
    filename: str


def _check_ip(ip: str) -> None:
    addr = ipaddress.ip_address(ip)
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or any(addr in n for n in BLOCKED_NETS)
    ):
        raise ValidationFailed("That address is not allowed for URL import.")


def validate_url(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise ValidationFailed("Only http and https URLs can be imported.")
    if parts.username or parts.password:
        raise ValidationFailed("URLs containing credentials are not allowed.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValidationFailed("URL has no host.")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in (80, 443):
        raise ValidationFailed("Only default HTTP(S) ports are allowed.")
    allowed = [h.lower() for h in get_settings().url_import_allowed_hosts]
    if allowed and not any(host == h or host.endswith("." + h) for h in allowed):
        raise ValidationFailed("This host is not on the URL-import allowlist. Upload an authorised copy instead.")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValidationFailed("Host could not be resolved.")
    ips = sorted({i[4][0] for i in infos})
    for ip in ips:
        _check_ip(ip)
    return host, ips[0], port


def fetch(url: str) -> Fetched:
    s = get_settings()
    current = url
    for _ in range(4):
        host, ip, port = validate_url(current)
        parts = urlsplit(current)
        # Pin to the vetted address; keep Host/SNI as the original hostname.
        target = parts._replace(netloc=f"{'[' + ip + ']' if ':' in ip else ip}:{port}").geturl()
        with httpx.Client(timeout=20, follow_redirects=False, verify=True) as client:
            with client.stream("GET", target, headers={"Host": host, "User-Agent": "TenderPilotAI-URLImport/1.0"},
                               extensions={"sni_hostname": host}) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        raise ValidationFailed("Redirect without location.")
                    current = urljoin(current, loc)
                    continue
                if resp.status_code != 200:
                    raise ValidationFailed(f"The source returned HTTP {resp.status_code}. Upload an authorised copy instead.")
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > s.url_import_max_bytes:
                        raise ValidationFailed("Remote file exceeds the import size limit.")
                name = parts.path.rsplit("/", 1)[-1] or "imported"
                return Fetched(current, bytes(buf), resp.headers.get("content-type", ""), name)
    raise ValidationFailed("Too many redirects.")
