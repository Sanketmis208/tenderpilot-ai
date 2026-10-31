"""Antivirus boundary: clamd INSTREAM client against a protocol-faithful fake clamd, and upload enforcement."""

from __future__ import annotations

import socket
import struct
import threading

import pytest

EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class FakeClamd:
    """Implements zPING and zINSTREAM like clamd; flags the EICAR test string."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.scanned = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                cmd = b""
                while not cmd.endswith(b"\0"):
                    cmd += conn.recv(1)
                if cmd == b"zPING\0":
                    conn.sendall(b"PONG\0")
                    continue
                data = b""
                while True:
                    hdr = b""
                    while len(hdr) < 4:
                        hdr += conn.recv(4 - len(hdr))
                    (n,) = struct.unpack("!L", hdr)
                    if n == 0:
                        break
                    while n:
                        part = conn.recv(n)
                        data, n = data + part, n - len(part)
                self.scanned += 1
                conn.sendall(b"stream: Eicar-Test-Signature FOUND\0" if EICAR in data else b"stream: OK\0")

    def close(self):
        self.sock.close()


@pytest.fixture
def clamd():
    f = FakeClamd()
    yield f
    f.close()


def test_clamd_client_protocol(clamd):
    from app.integrations.antivirus import ClamdScanner

    sc = ClamdScanner("127.0.0.1", clamd.port, timeout=5)
    assert sc.ping()
    assert sc.scan(b"%PDF-1.7 harmless" * 10000).status == "clean"  # multi-chunk stream
    r = sc.scan(b"prefix " + EICAR)
    assert (r.status, r.signature) == ("infected", "Eicar-Test-Signature")


def test_unreachable_scanner_reports_error():
    from app.integrations.antivirus import ClamdScanner

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = ClamdScanner("127.0.0.1", port, timeout=1).scan(b"x")
    assert r.status == "error" and not ClamdScanner("127.0.0.1", port, timeout=1).ping()


def test_upload_enforces_scanner(seeded, db, clamd, monkeypatch):
    from app.core.errors import ServiceUnavailable, ValidationFailed
    from app.integrations import antivirus
    from app.services.documents import service as documents

    ctx = seeded.contexts["docs@voltedge.example"]
    monkeypatch.setattr(antivirus, "get_scanner", lambda: antivirus.ClamdScanner("127.0.0.1", clamd.port, timeout=5))
    monkeypatch.setattr(documents, "get_scanner", lambda: antivirus.ClamdScanner("127.0.0.1", clamd.port, timeout=5))
    with pytest.raises(ValidationFailed, match="Eicar"):
        documents.upload(db, ctx, b"Plain notes\n" + EICAR, "notes.txt", process=False)
    res = documents.upload(db, ctx, b"Plain clean notes about a tender\n", "clean-notes.txt", process=False)
    assert res and clamd.scanned == 2

    clamd.close()
    with pytest.raises(ServiceUnavailable):
        documents.upload(db, ctx, b"Another clean file\n", "another.txt", process=False)
    db.rollback()


def test_without_engine_upload_is_marked_not_scanned(seeded, db):
    from sqlalchemy import select

    from app.db.models import AuditEvent
    from app.services.documents import service as documents

    ctx = seeded.contexts["docs@voltedge.example"]
    res = documents.upload(db, ctx, b"Unscanned but harmless text\n", "unscanned.txt", process=False)
    db.flush()
    ev = db.scalar(select(AuditEvent).where(AuditEvent.action == "document.uploaded", AuditEvent.subject_id == res[0].document.id))
    assert ev.data["antivirus"] == {"status": "not_scanned", "engine": "none"}
    db.rollback()
