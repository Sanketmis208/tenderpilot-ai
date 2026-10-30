"""Content-based file identification and upload validation (never trust the client MIME type)."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from app.core.errors import ValidationFailed

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ZIP = "application/zip"
CSV = "text/csv"
TXT = "text/plain"

ALLOWED = {PDF, DOCX, XLSX, ZIP, CSV, TXT}
EXT_FOR = {PDF: "pdf", DOCX: "docx", XLSX: "xlsx", ZIP: "zip", CSV: "csv", TXT: "txt"}


@dataclass
class FileIdentity:
    content_type: str
    extension: str


def identify(data: bytes, filename: str) -> FileIdentity:
    if not data:
        raise ValidationFailed("The file is empty.")
    name = filename.lower()
    if data[:5] == b"%PDF-":
        return FileIdentity(PDF, "pdf")
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            raise ValidationFailed("The archive is corrupted.")
        if "word/document.xml" in names:
            return FileIdentity(DOCX, "docx")
        if "xl/workbook.xml" in names:
            return FileIdentity(XLSX, "xlsx")
        if "[Content_Types].xml" in names:
            raise ValidationFailed("This Office format is not supported. Upload PDF, DOCX or XLSX.")
        return FileIdentity(ZIP, "zip")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValidationFailed("Legacy .doc/.xls files are not supported. Save as DOCX/XLSX or PDF.")
    # Text: must decode and contain no NUL bytes.
    sample = data[:65536]
    if b"\x00" in sample:
        raise ValidationFailed("Unsupported binary file type.")
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        try:
            sample.decode("cp1252")
        except UnicodeDecodeError:
            raise ValidationFailed("Unsupported file encoding.")
    if name.endswith(".csv"):
        return FileIdentity(CSV, "csv")
    if name.endswith((".txt", ".md", ".text")) or "." not in name:
        return FileIdentity(TXT, "txt")
    raise ValidationFailed("Unsupported file type. Allowed: PDF, DOCX, XLSX, CSV, TXT, ZIP.")


# Content that should never be stored, regardless of claimed type.
_DANGEROUS_MARKERS = (b"/JavaScript", b"/Launch", b"/EmbeddedFile")


def basic_threat_scan(data: bytes, content_type: str) -> list[str]:
    """Lightweight heuristic scan. Not a replacement for an AV engine (see docs/security.md)."""
    findings: list[str] = []
    if content_type == PDF:
        head = data[: 4 * 1024 * 1024]
        for marker in _DANGEROUS_MARKERS:
            if marker in head:
                findings.append(f"PDF contains {marker.decode()} action")
    if content_type in (DOCX, XLSX):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if any(n.lower().endswith("vbaproject.bin") for n in zf.namelist()):
                findings.append("Office file contains macros")
    return findings
