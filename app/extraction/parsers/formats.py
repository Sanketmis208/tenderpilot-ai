"""Format-specific parsers producing page-addressed text with layout coordinates."""

from __future__ import annotations

import csv
import io
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.extraction.normalization.values import detect_language
from app.extraction.parsers import filetype
from app.extraction.parsers.types import ParsedBlock, ParsedDocument, ParsedPage

log = get_logger(__name__)


def parse_pdf(data: bytes) -> ParsedDocument:
    import pymupdf

    s = get_settings()
    notes: list[str] = []
    pages: list[ParsedPage] = []
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"PDF could not be opened: {exc}")
    if doc.needs_pass:
        raise ValueError("PDF is password-protected.")
    from app.extraction.ocr.textract import get_ocr

    ocr = None
    ocr_checked = False
    ocr_used = False
    scanned_pages: list[int] = []
    for idx, page in enumerate(doc):
        raw_blocks = page.get_text("blocks", sort=True)
        blocks = [ParsedBlock(b[4].strip(), [round(b[0], 1), round(b[1], 1), round(b[2], 1), round(b[3], 1)])
                  for b in raw_blocks if b[6] == 0 and b[4].strip()]
        text = "\n".join(b.text for b in blocks)
        method, conf = "text", None
        if len(text.strip()) < s.ocr_min_text_chars_per_page:
            if not ocr_checked:
                ocr, ocr_checked = get_ocr(), True
            if ocr is not None:
                pix = page.get_pixmap(dpi=200)
                res = ocr.ocr_image(pix.tobytes("png"), page.rect.width, page.rect.height)
                text, blocks, method, conf, ocr_used = res.text, res.blocks, "ocr_textract", res.confidence, True
            else:
                scanned_pages.append(idx + 1)
                method = "none"
        tables: list[list[list[Any]]] = []
        if method == "text":
            try:
                for t in page.find_tables().tables[:5]:
                    rows = t.extract()
                    if rows and len(rows) > 1:
                        tables.append([[(c or "").strip() for c in r] for r in rows])
            except Exception:  # table detection is best-effort
                log.debug("pdf.table_detection_failed", page=idx + 1)
        pages.append(ParsedPage(idx + 1, text, blocks, tables, page.rect.width, page.rect.height, method, conf))
    if scanned_pages:
        notes.append(
            f"Pages {', '.join(map(str, scanned_pages[:20]))} appear scanned and OCR is not configured; "
            "their content was NOT extracted. Enable Amazon Textract or upload a text PDF."
        )
    lang, lconf = detect_language("\n".join(p.text for p in pages)[:20000])
    return ParsedDocument(pages, "pymupdf+textract" if ocr_used else "pymupdf", ocr_used, notes, lang, lconf)


def parse_docx(data: bytes) -> ParsedDocument:
    import docx

    d = docx.Document(io.BytesIO(data))
    # DOCX has no fixed pages; we emit logical pages of ~40 paragraphs, splitting on explicit page breaks.
    pages: list[ParsedPage] = []
    current: list[ParsedBlock] = []

    def flush() -> None:
        if current:
            pages.append(ParsedPage(len(pages) + 1, "\n".join(b.text for b in current), list(current)))
            current.clear()

    for para in d.paragraphs:
        txt = para.text.strip()
        has_break = any(r._r.xml.find('w:br w:type="page"') >= 0 for r in para.runs)
        if txt:
            style = (para.style.name or "").lower() if para.style is not None else ""
            current.append(ParsedBlock(("## " + txt) if style.startswith("heading") else txt))
        if has_break or len(current) >= 40:
            flush()
    tables = []
    for t in d.tables:
        rows = [[c.text.strip() for c in r.cells] for r in t.rows]
        if rows:
            tables.append(rows)
            current.append(ParsedBlock("\n".join(" | ".join(r) for r in rows)))
    flush()
    if pages and tables:
        pages[-1].tables = tables
    if not pages:
        pages = [ParsedPage(1, "")]
    lang, lconf = detect_language("\n".join(p.text for p in pages)[:20000])
    return ParsedDocument(pages, "python-docx", False, ["DOCX page numbers are logical sections, not printed pages."], lang, lconf)


def parse_xlsx(data: bytes) -> ParsedDocument:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    pages = []
    for idx, ws in enumerate(wb.worksheets):
        rows = []
        for row in ws.iter_rows(values_only=True):
            if any(v not in (None, "") for v in row):
                rows.append(["" if v is None else str(v) for v in row])
            if len(rows) >= 5000:
                break
        text = f"## Sheet: {ws.title}\n" + "\n".join(" | ".join(r) for r in rows)
        pages.append(ParsedPage(idx + 1, text, [ParsedBlock(text)], [rows] if rows else []))
    wb.close()
    return ParsedDocument(pages, "openpyxl", False, ["Each worksheet is treated as one page."], "en", 0.5)


def parse_csv(data: bytes) -> ParsedDocument:
    text = _decode(data)
    rows = list(csv.reader(io.StringIO(text)))[:5000]
    body = "\n".join(" | ".join(r) for r in rows)
    return ParsedDocument([ParsedPage(1, body, [ParsedBlock(body)], [rows])], "csv", False, [], "en", 0.5)


def parse_txt(data: bytes) -> ParsedDocument:
    text = _decode(data)
    # Form-feed separated pages if present; otherwise ~60-line logical pages.
    chunks = text.split("\f") if "\f" in text else ["\n".join(text.splitlines()[i : i + 60]) for i in range(0, max(len(text.splitlines()), 1), 60)]
    pages = [ParsedPage(i + 1, c, [ParsedBlock(p) for p in c.split("\n\n") if p.strip()]) for i, c in enumerate(chunks)]
    lang, lconf = detect_language(text[:20000])
    return ParsedDocument(pages, "text", False, [], lang, lconf)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


PARSERS = {
    filetype.PDF: parse_pdf,
    filetype.DOCX: parse_docx,
    filetype.XLSX: parse_xlsx,
    filetype.CSV: parse_csv,
    filetype.TXT: parse_txt,
}


def parse(data: bytes, content_type: str) -> ParsedDocument:
    parser = PARSERS.get(content_type)
    if not parser:
        raise ValueError(f"No parser for {content_type}")
    return parser(data)
