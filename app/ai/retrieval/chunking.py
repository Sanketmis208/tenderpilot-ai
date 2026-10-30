"""Structure-preserving chunking: headings carried forward, clauses and tables never split mid-unit."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.extraction.parsers.types import ParsedDocument

TARGET_CHARS = 900
MAX_CHARS = 1800
HEADING_RE = re.compile(
    r"^(##\s+.+|(?i:section|chapter|annexure|appendix|part)\s+[\w.-]+.*|\d+(\.\d+){0,3}\.?\s+[A-Z][^.]{2,80}|[A-Z][A-Z &/-]{5,80})$"
)


@dataclass
class Chunk:
    index: int
    page_no: int
    section: str | None
    kind: str
    text: str
    bbox: list[float] | None


def _union(a: list[float] | None, b: list[float] | None) -> list[float] | None:
    if not a:
        return b
    if not b:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def chunk_document(doc: ParsedDocument) -> list[Chunk]:
    chunks: list[Chunk] = []
    section: str | None = None

    def emit(page: int, kind: str, text: str, bbox: list[float] | None) -> None:
        text = text.strip()
        if text:
            chunks.append(Chunk(len(chunks), page, section, kind, text, bbox))

    for page in doc.pages:
        buf: list[str] = []
        bbox: list[float] | None = None
        for block in page.blocks or []:
            first_line = block.text.strip().splitlines()[0] if block.text.strip() else ""
            if HEADING_RE.match(first_line) and len(block.text) < 120:
                if buf:
                    emit(page.page_no, "text", "\n".join(buf), bbox)
                    buf, bbox = [], None
                section = first_line.lstrip("# ").strip()[:300]
                continue
            if sum(len(b) for b in buf) + len(block.text) > TARGET_CHARS and buf:
                emit(page.page_no, "text", "\n".join(buf), bbox)
                buf, bbox = [], None
            if len(block.text) > MAX_CHARS:
                # Oversized single block: split on sentence boundaries, never inside a number.
                parts = re.split(r"(?<=[.;])\s+(?=[A-Z(])", block.text)
                cur = ""
                for p in parts:
                    if len(cur) + len(p) > TARGET_CHARS and cur:
                        emit(page.page_no, "text", cur, block.bbox)
                        cur = ""
                    cur = f"{cur} {p}".strip()
                if cur:
                    buf, bbox = [cur], block.bbox
                continue
            buf.append(block.text)
            bbox = _union(bbox, block.bbox)
        if not page.blocks and page.text.strip():
            buf.append(page.text)
        if buf:
            emit(page.page_no, "text", "\n".join(buf), bbox)
        for table in page.tables:
            rows = [" | ".join(str(c) for c in r) for r in table]
            # Tables stay whole up to a hard cap; header row is repeated in continuation chunks.
            header, body = rows[:1], rows[1:]
            piece: list[str] = []
            for r in body:
                if sum(len(x) for x in piece) + len(r) > MAX_CHARS and piece:
                    emit(page.page_no, "table", "\n".join(header + piece), None)
                    piece = []
                piece.append(r)
            emit(page.page_no, "table", "\n".join(header + piece), None)
    return chunks
