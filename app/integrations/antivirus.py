"""Malware scanning boundary.

ClamdScanner speaks the clamd INSTREAM protocol over TCP (works with the official clamav/clamav image,
or any clamd-compatible service). Without TP_CLAMAV_HOST the NullScanner reports "not_scanned" — uploads
are never described as virus-checked when no engine looked at them.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings

CHUNK = 64 * 1024


@dataclass(frozen=True)
class ScanResult:
    status: str  # clean | infected | not_scanned | error
    engine: str
    signature: str | None = None
    detail: str | None = None


class Scanner(Protocol):
    name: str

    def scan(self, data: bytes) -> ScanResult: ...

    def ping(self) -> bool: ...


class NullScanner:
    name = "none"

    def scan(self, data: bytes) -> ScanResult:
        return ScanResult("not_scanned", self.name, detail="No antivirus engine configured (TP_CLAMAV_HOST).")

    def ping(self) -> bool:
        return False


class ClamdScanner:
    name = "clamav"

    def __init__(self, host: str, port: int = 3310, timeout: float = 30.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout

    def _connect(self) -> socket.socket:
        return socket.create_connection((self.host, self.port), timeout=self.timeout)

    def ping(self) -> bool:
        try:
            with self._connect() as s:
                s.sendall(b"zPING\0")
                return s.recv(64).rstrip(b"\0\n") == b"PONG"
        except OSError:
            return False

    def scan(self, data: bytes) -> ScanResult:
        try:
            with self._connect() as s:
                s.sendall(b"zINSTREAM\0")
                for i in range(0, len(data), CHUNK):
                    part = data[i:i + CHUNK]
                    s.sendall(struct.pack("!L", len(part)) + part)
                s.sendall(struct.pack("!L", 0))
                reply = b""
                while not reply.endswith(b"\0") and len(reply) < 4096:
                    got = s.recv(4096)
                    if not got:
                        break
                    reply += got
        except OSError as exc:
            return ScanResult("error", self.name, detail=f"clamd unreachable: {type(exc).__name__}")
        text = reply.rstrip(b"\0\n").decode("utf-8", "replace")
        # Replies: "stream: OK" | "stream: <Signature> FOUND" | "INSTREAM size limit exceeded. ERROR"
        if text.endswith(" OK"):
            return ScanResult("clean", self.name)
        if text.endswith(" FOUND"):
            return ScanResult("infected", self.name, signature=text.split(":", 1)[-1].removesuffix(" FOUND").strip())
        return ScanResult("error", self.name, detail=text[:200] or "empty reply")


@lru_cache
def get_scanner() -> Scanner:
    s = get_settings()
    if s.clamav_host:
        return ClamdScanner(s.clamav_host, s.clamav_port, s.clamav_timeout_seconds)
    return NullScanner()
