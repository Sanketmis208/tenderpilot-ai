"""Parser output types shared by every format."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedBlock:
    text: str
    bbox: list[float] | None = None  # PDF points: x0, y0, x1, y1


@dataclass
class ParsedPage:
    page_no: int
    text: str
    blocks: list[ParsedBlock] = field(default_factory=list)
    tables: list[list[list[Any]]] = field(default_factory=list)
    width: float | None = None
    height: float | None = None
    method: str = "text"  # text | ocr_textract | none
    ocr_confidence: float | None = None


@dataclass
class ParsedDocument:
    pages: list[ParsedPage]
    method: str
    ocr_used: bool = False
    quality_notes: list[str] = field(default_factory=list)
    language: str | None = None
    language_confidence: float | None = None

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)
