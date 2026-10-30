"""Server-side page rendering for the document viewer (PDF pages → PNG; other formats → text card)."""

from __future__ import annotations

from functools import lru_cache

from app.core.errors import NotFound
from app.extraction.parsers import filetype


def render_page_png(data: bytes, content_type: str, page_no: int, zoom: float = 1.5) -> bytes:
    if content_type != filetype.PDF:
        raise NotFound("Page images are available for PDF documents only.")
    return _render(data, page_no, zoom)


@lru_cache(maxsize=64)
def _render(data: bytes, page_no: int, zoom: float) -> bytes:
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    if page_no < 1 or page_no > doc.page_count:
        raise NotFound("Page not found.")
    pix = doc[page_no - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    return pix.tobytes("png")
