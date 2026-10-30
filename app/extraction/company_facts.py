"""Deterministic extraction of company facts from classified documents.

Every fact keeps the page and verbatim line it came from. Values that cannot be located are
omitted, never guessed. Confidence reflects how the value was found (labelled field vs. inference).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.db.models.documents import DocumentCategory as C
from app.extraction.normalization.values import find_all_dates, normalise_fiscal_year, parse_date, parse_inr
from app.extraction.parsers.types import ParsedDocument

GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
CIN_RE = re.compile(r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b")
UDYAM_RE = re.compile(r"\bUDYAM-[A-Z]{2}-\d{2}-\d{7}\b", re.I)
ISO_RE = re.compile(r"\bISO(?:/IEC)?\s*(\d{4,5})(?::\s*(\d{4}))?", re.I)


@dataclass
class Fact:
    kind: str  # certification | financial | project | registration | product
    values: dict[str, Any]
    page: int
    source_text: str
    confidence: float
    bbox: list[float] | None = None


@dataclass
class FactExtraction:
    facts: list[Fact] = field(default_factory=list)
    fields: list[dict[str, Any]] = field(default_factory=list)  # document-level metadata fields
    issued_on: date | None = None
    expires_on: date | None = None
    identifiers: dict[str, str] = field(default_factory=dict)


def _lines(doc: ParsedDocument):
    for page in doc.pages:
        for block in page.blocks or []:
            for line in block.text.splitlines():
                if line.strip():
                    yield page.page_no, line.strip(), block.bbox
        if not page.blocks:
            for line in page.text.splitlines():
                if line.strip():
                    yield page.page_no, line.strip(), None


def _labelled(doc: ParsedDocument, labels: str) -> tuple[str, int, str, list[float] | None] | None:
    """Find 'Label: value' (same line, or value on the next line)."""
    rx = re.compile(rf"(?:{labels})\s*[:\-–]?\s*(.*)$", re.I)
    lines = list(_lines(doc))
    for i, (page, line, bbox) in enumerate(lines):
        m = rx.search(line)
        if m:
            value = m.group(1).strip(" :-–")
            if not value and i + 1 < len(lines):
                value = lines[i + 1][1]
                line = f"{line} {value}"
            if value:
                return value, page, line, bbox
    return None


def _identifiers(doc: ParsedDocument, out: FactExtraction) -> None:
    for page, line, bbox in _lines(doc):
        for kind, rx in (("gstin", GSTIN_RE), ("cin", CIN_RE), ("udyam", UDYAM_RE)):
            for m in rx.finditer(line.upper()):
                if kind not in out.identifiers:
                    out.identifiers[kind] = m.group(0).upper()
                    out.facts.append(Fact("registration", {"kind": kind, "identifier": m.group(0).upper()}, page, line, 0.9, bbox))
        if "pan" not in out.identifiers and re.search(r"\bPAN\b", line):
            m = PAN_RE.search(line.upper())
            if m:
                out.identifiers["pan"] = m.group(0)
                out.facts.append(Fact("registration", {"kind": "pan", "identifier": m.group(0)}, page, line, 0.85, bbox))


def _validity(doc: ParsedDocument) -> tuple[tuple[date, int, str] | None, tuple[date, int, str] | None]:
    start = end = None
    for page, line, _ in _lines(doc):
        low = line.lower()
        dates = find_all_dates(line)
        if not dates:
            continue
        if end is None and re.search(r"valid (until|till|upto|up to)|expir|valid to|date of expiry", low):
            # "Valid from X to Y" → last date is the end.
            end = (dates[-1][0], page, line)
            if start is None and len(dates) > 1 and "from" in low:
                start = (dates[0][0], page, line)
        elif start is None and re.search(r"(issue|issued|effective|valid from|incorporat|date of (issue|certification|registration|udyam registration)|original approval)", low):
            start = (dates[0][0], page, line)
    return start, end


def _certification(doc: ParsedDocument, out: FactExtraction) -> None:
    std = None
    for page, line, bbox in _lines(doc):
        m = ISO_RE.search(line)
        if m:
            std = (f"ISO {m.group(1)}", m.group(2), page, line, bbox)
            break
    if not std:
        return
    start, end = _validity(doc)
    cert_no = _labelled(doc, r"certificate\s*(?:no\.?|number|#)|registration\s*no\.?")
    issuer = _labelled(doc, r"issued by|certification body|certifying body")
    scope = _labelled(doc, r"scope(?: of certification)?")
    values = {
        "cert_type": std[0],
        "standard_year": std[1],
        "certificate_number": cert_no[0][:120] if cert_no else None,
        "issuer": issuer[0][:250] if issuer else None,
        "scope": scope[0] if scope else None,
        "valid_from": start[0] if start else None,
        "valid_until": end[0] if end else None,
    }
    conf = 0.6 + (0.2 if end else 0) + (0.1 if cert_no else 0) + (0.05 if start else 0)
    src_line = end[2] if end else std[3]
    src_page = end[1] if end else std[2]
    out.facts.append(Fact("certification", values, src_page, f"{std[3]} … {src_line}" if end else std[3], round(min(conf, 0.95), 2), std[4]))
    out.issued_on = start[0] if start else None
    out.expires_on = end[0] if end else None


_TURNOVER_RE = re.compile(r"turnover|revenue from operations|total revenue|gross receipts", re.I)


def _financials(doc: ParsedDocument, out: FactExtraction) -> None:
    audited = bool(re.search(r"\baudited\b|chartered accountants?|\budin\b", doc.full_text, re.I))
    seen: set[str] = set()
    for page in doc.pages:
        # Tables: rows containing a fiscal year and an amount.
        for table in page.tables:
            header = " ".join(str(c) for c in table[0]).lower() if table else ""
            if not _TURNOVER_RE.search(header) and not any(_TURNOVER_RE.search(" ".join(map(str, r))) for r in table):
                continue
            for row in table[1:]:
                joined = " ".join(str(c) for c in row)
                fy = normalise_fiscal_year(joined)
                if not fy or fy in seen:
                    continue
                amt = _row_amount(row, header)
                if amt is not None:
                    seen.add(fy)
                    out.facts.append(Fact("financial", {"fiscal_year": fy, "metric": "turnover", "amount": amt, "audited": audited}, page.page_no, joined, 0.85 if audited else 0.7))
    for page_no, line, bbox in _lines(doc):
        if not _TURNOVER_RE.search(line):
            continue
        fy = normalise_fiscal_year(line)
        money = parse_inr(line)
        if fy and money and fy not in seen:
            seen.add(fy)
            out.facts.append(Fact("financial", {"fiscal_year": fy, "metric": "turnover", "amount": money[0], "audited": audited}, page_no, line, 0.8 if audited else 0.65, bbox))


def _row_amount(row: list[Any], header: str) -> Decimal | None:
    unit = Decimal(1)
    if "crore" in header or "(cr" in header or "in cr" in header:
        unit = Decimal("10000000")
    elif "lakh" in header or "lac" in header:
        unit = Decimal("100000")
    for cell in reversed(row):
        s = str(cell)
        if normalise_fiscal_year(s):
            continue
        money = parse_inr(s)
        if money:
            return money[0]
        cleaned = s.replace(",", "").strip()
        if re.fullmatch(r"\d+(\.\d+)?", cleaned):
            return (Decimal(cleaned) * unit).quantize(Decimal("0.01"))
    return None


def _project(doc: ParsedDocument, out: FactExtraction) -> None:
    title = _labelled(doc, r"name of (?:the )?(?:work|project)|project (?:name|title)|work description|subject")
    client = _labelled(doc, r"name of (?:the )?client|client|employer|issued by|owner")
    value = _labelled(doc, r"(?:contract|order|work order|project) value|value of (?:the )?(?:work|contract|order)")
    completed = _labelled(doc, r"date of completion|completed on|completion date|actual completion")
    started = _labelled(doc, r"date of commencement|commenced on|start date|date of start")
    if not title:
        return
    amount = parse_inr(value[0]) if value else None
    values = {
        "title": title[0][:400],
        "client_name": client[0][:250] if client else None,
        "contract_value": amount[0] if amount else None,
        "completion_date": parse_date(completed[0]) if completed else None,
        "start_date": parse_date(started[0]) if started else None,
        "description": title[0],
    }
    conf = 0.55 + (0.15 if amount else 0) + (0.15 if completed else 0) + (0.05 if client else 0)
    src = " | ".join(x[2] for x in (title, value, completed) if x)
    out.facts.append(Fact("project", values, title[1], src, round(conf, 2), title[3]))


_MODEL_RE = re.compile(r"^(?P<name>[A-Z][\w &/().-]{3,80}?)\s*[-–:|]\s*model\s*(?:no\.?|number)?\s*[:#]?\s*(?P<model>[A-Z0-9][\w/.-]{1,40})", re.I)


def _products(doc: ParsedDocument, out: FactExtraction) -> None:
    for page, line, bbox in _lines(doc):
        m = _MODEL_RE.search(line)
        if m:
            out.facts.append(Fact("product", {"name": m.group("name").strip(), "model_number": m.group("model")}, page, line, 0.7, bbox))


def _msme(doc: ParsedDocument, out: FactExtraction) -> None:
    cat = re.search(r"(?:type of enterprise|enterprise type|category)\s*[:\-]?\s*(micro|small|medium)\b", doc.full_text[:6000], re.I)
    for f in out.facts:
        if f.kind == "registration" and f.values.get("kind") == "udyam" and cat:
            f.values["attributes"] = {"msme_category": cat.group(1).lower()}
    start, _ = _validity(doc)
    if start:
        out.issued_on = start[0]


def extract_company_facts(doc: ParsedDocument, category: str) -> FactExtraction:
    out = FactExtraction()
    _identifiers(doc, out)
    if category in (C.ISO_CERTIFICATE, C.OEM_CERTIFICATE):
        _certification(doc, out)
    elif category in (C.FINANCIAL_STATEMENT, C.TURNOVER_CERTIFICATE):
        _financials(doc, out)
    elif category in (C.COMPLETION_CERTIFICATE, C.EXPERIENCE_CERTIFICATE, C.PURCHASE_ORDER):
        _project(doc, out)
    elif category in (C.PRODUCT_CATALOGUE, C.TECHNICAL_DATASHEET):
        _products(doc, out)
    elif category == C.MSME_REGISTRATION:
        _msme(doc, out)
    elif category == C.COMPANY_REGISTRATION:
        start, _ = _validity(doc)
        out.issued_on = start[0] if start else None
    elif category == C.GST_CERTIFICATE:
        start, _ = _validity(doc)
        out.issued_on = start[0] if start else None
    if out.expires_on is None and category not in (C.FINANCIAL_STATEMENT, C.TURNOVER_CERTIFICATE):
        _, end = _validity(doc)
        if end:
            out.expires_on = end[0]
    for name, val in (("issued_on", out.issued_on), ("expires_on", out.expires_on)):
        if val:
            out.fields.append({"name": name, "value": val.isoformat(), "confidence": 0.75})
    for k, v in out.identifiers.items():
        out.fields.append({"name": k, "value": v, "confidence": 0.9})
    return out
