"""Deterministic tender field + requirement extraction (no LLM).

Precision-oriented: it recognises the clause shapes common in Indian NIT/RFP documents and
records every match with page, verbatim clause and bbox. Anything it does not understand is
left for the LLM extractor (when configured) or for human review — never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.extraction.normalization.values import (
    find_all_inr,
    parse_date,
    parse_datetime_ist,
    parse_inr,
)
from app.extraction.parsers.types import ParsedDocument
from app.schemas.extraction import ExtractedRequirement, FieldCitation, TenderFields

EMD_NOTE = "Exemption applies only if the stated conditions are met; verify eligibility and the proof required."
WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10}
_NUM = r"(\d+|one|two|three|four|five|six|seven|ten)"


def _n(s: str) -> int:
    return int(s) if s.isdigit() else WORD_NUM[s.lower()]


@dataclass
class Clause:
    page: int
    text: str
    bbox: list[float] | None


def clauses(doc: ParsedDocument) -> list[Clause]:
    """Split pages into clause-sized units (numbered items / sentences), keeping bbox per block."""
    out: list[Clause] = []
    for page in doc.pages:
        blocks = page.blocks or []
        if not blocks and page.text:
            from app.extraction.parsers.types import ParsedBlock

            blocks = [ParsedBlock(t) for t in page.text.split("\n\n")]
        for b in blocks:
            text = re.sub(r"\s+", " ", b.text).strip()
            if not text:
                continue
            # Split long blocks on numbered sub-clauses "(a)", "a)", "i." and sentence ends.
            parts = re.split(r"(?<=[.;])\s+(?=(?:\(?[a-z0-9ivx]{1,4}[).]\s)|[A-Z])", text)
            for p in parts:
                if len(p) > 12 and not _is_heading(p):
                    out.append(Clause(page.page_no, p.strip(), b.bbox))
    return out


def _is_heading(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    upper = sum(ch.isupper() for ch in letters) / max(len(letters), 1)
    return (upper > 0.85 and len(text) < 140) or text.startswith("SYNTHETIC SAMPLE DOCUMENT")


def _mandatory(text: str) -> str:
    low = text.lower()
    if re.search(r"\b(exempt|exemption|subject to|provided that|unless|in case of|relaxation)\b", low):
        return "conditional"
    if re.search(r"\b(shall|must|mandatory|required|compulsory|essential|should have|will be rejected|disqualif)", low):
        return "mandatory"
    if re.search(r"\b(may|preferabl|desirable|optional)\b", low):
        return "optional"
    return "unknown"


def _req(c: Clause, **kw) -> ExtractedRequirement:
    kw.setdefault("mandatory", _mandatory(c.text))
    return ExtractedRequirement(source_quote=c.text, source_page=c.page, **kw)


def _period(text: str) -> int | None:
    m = re.search(rf"(?:last|previous|preceding|past)\s+{_NUM}\s+(?:\(\w+\)\s+)?(?:financial|fiscal|completed)?\s*years?", text, re.I)
    return _n(m.group(1)) if m else None


def extract_requirements(doc: ParsedDocument) -> list[ExtractedRequirement]:
    reqs: list[ExtractedRequirement] = []
    seen: set[tuple] = set()

    def add(r: ExtractedRequirement, key: tuple) -> None:
        if key not in seen:
            seen.add(key)
            reqs.append(r)

    for c in clauses(doc):
        t, low = c.text, c.text.lower()

        # Financial: turnover
        if re.search(r"turnover", low) and (money := parse_inr(t)):
            agg = "each" if re.search(r"\beach\b|every year|in each", low) else ("average" if "average" in low else "any")
            period = _period(t)
            notes = None if period else "Measurement period not stated in this clause."
            add(
                _req(
                    c,
                    req_type="financial",
                    title=f"{'Average annual' if agg == 'average' else 'Annual'} turnover ≥ {money[1]}"
                    + (f" over last {period} financial years" if period else ""),
                    metric="average_annual_turnover" if agg == "average" else "annual_turnover",
                    operator=">=",
                    threshold_value=str(money[0]),
                    threshold_unit="INR",
                    period_years=period,
                    aggregation=agg,
                    confidence=0.9 if period else 0.7,
                    ambiguity_notes=notes,
                ),
                ("turnover",),
            )
            continue
        if re.search(r"net\s*worth", low) and re.search(r"positive|not be negative|non-?negative", low):
            add(_req(c, req_type="financial", title="Positive net worth", metric="net_worth", operator=">", threshold_value="0", threshold_unit="INR", period_years=_period(t), aggregation="each", confidence=0.8), ("networth",))
            continue

        # Experience: similar works
        m = re.search(rf"{_NUM}\s+similar\s+(?:completed\s+)?works?", low)
        if ("similar work" in low or "similar nature" in low) and (amounts := find_all_inr(t)):
            count = _n(m.group(1)) if m else 1
            period = _period(t)
            add(
                _req(
                    c,
                    req_type="experience",
                    title=f"{count} similar completed work(s) each ≥ {amounts[0][1]}" + (f" in last {period} years" if period else ""),
                    metric="similar_work_value",
                    operator=">=",
                    threshold_value=str(amounts[0][0]),
                    threshold_unit="INR",
                    period_years=period,
                    aggregation="count",
                    count_required=count,
                    keywords=_work_keywords(t),
                    confidence=0.8 if period else 0.65,
                    ambiguity_notes="Definition of 'similar work' requires interpretation; confirm the project scope matches."
                    ,
                ),
                ("similar_work",),
            )
            continue
        m = re.search(rf"(?:minimum|at least|not less than)\s+(?:of\s+)?{_NUM}\s*(?:\(\w+\)\s*)?years?\s+(?:of\s+)?(?:relevant\s+|experience|existence|operation|in business)", low) or re.search(
            rf"{_NUM}\s*(?:\(\w+\)\s*)?years?\s+of\s+(?:relevant\s+)?experience", low
        )
        if m and "experience" in low or (m and re.search(r"in (operation|existence|business)", low)):
            add(
                _req(c, req_type="experience", title=f"Minimum {_n(m.group(1))} years of relevant experience", metric="years_in_business", operator=">=", threshold_value=str(_n(m.group(1))), threshold_unit="years", confidence=0.8,
                     ambiguity_notes="'Relevant experience' is evaluated from establishment year and dated project records."),
                ("years_experience",),
            )
            continue

        # Certifications
        for iso in re.finditer(r"\bISO(?:/IEC)?\s*(\d{4,5})(?::\s*\d{4})?", t):
            std = f"ISO {iso.group(1)}"
            if re.search(r"certif|valid|accredit|registered", low):
                add(
                    _req(c, req_type="certification", title=f"Valid {std} certification", metric="certificate_valid", operator="exists", standard=std, confidence=0.9 if "valid" in low else 0.75),
                    ("cert", std),
                )
        if re.search(r"\bISO\b", t):
            continue

        # Registrations
        if re.search(r"\bgst(in)?\b.*registration|registration.*\bgst\b|valid gst", low):
            add(_req(c, req_type="registration", title="Valid GST registration", metric="registration", operator="exists", standard="GST", document_kind="gst_certificate", confidence=0.85), ("reg", "gst"))
        if re.search(r"\bpan\b", low) and "card" in low or "permanent account number" in low:
            add(_req(c, req_type="registration", title="PAN registration", metric="registration", operator="exists", standard="PAN", confidence=0.75), ("reg", "pan"))

        # EMD with explicit conditional exemption modelling.
        is_exemption = bool(re.search(r"msme|micro|small|udyam|nsic|startup", low) and re.search(r"exempt", low))
        existing_emd = next((r for r in reqs if r.req_type == "emd"), None)
        if is_exemption and existing_emd is not None:
            if t not in existing_emd.conditions:
                existing_emd.conditions.append(t)
            existing_emd.mandatory = "conditional"
            existing_emd.ambiguity_notes = EMD_NOTE
            continue
        if re.search(r"\bemd\b|earnest money", low):
            money = parse_inr(t)
            conditions = [t] if is_exemption else []
            add(
                _req(
                    c,
                    req_type="emd",
                    title=f"Earnest Money Deposit {money[1] if money else '(amount in clause)'}",
                    metric="emd",
                    operator="exists",
                    threshold_value=str(money[0]) if money else None,
                    threshold_unit="INR" if money else None,
                    conditions=conditions,
                    mandatory="conditional" if conditions else _mandatory(t),
                    confidence=0.8 if money else 0.6,
                    ambiguity_notes=EMD_NOTE if conditions else None,
                ),
                ("emd",),
            )
            continue
        if re.search(r"(msme|micro and small|udyam).{0,80}exempt|exempt.{0,80}(msme|micro and small|udyam)", low):
            for r in reqs:
                if r.req_type == "emd" and t not in r.conditions:
                    r.conditions.append(t)
                    r.mandatory = "conditional"
            continue

        # OEM / manufacturer authorisation
        if re.search(r"\b(oem|manufacturer'?s?)\b.{0,40}authori[sz]ation|\bmaf\b", low):
            add(_req(c, req_type="oem_authorization", title="OEM / Manufacturer authorization letter", metric="document", operator="exists", document_kind="authorization_letter", confidence=0.8), ("oem",))
            continue

        # Technical standards
        stds = [m.group(1) for m in re.finditer(r"\b(IS\s*\d{3,5}(?:\s*\(Part\s*\d+\))?|IEC\s*\d{4,5}(?:-\d+)?)\b", t)]
        if stds and re.search(r"conform|comply|complian|as per|accordance", low):
            joined = " and ".join(dict.fromkeys(stds))
            add(_req(c, req_type="technical", title=f"Products conform to {joined}", metric="standard_compliance", operator="exists", standard="; ".join(dict.fromkeys(stds)), document_kind="technical_datasheet", confidence=0.7,
                     ambiguity_notes="Compliance must be demonstrated by datasheet/type-test reports; text matching alone is not proof."),
                ("std", joined))
            continue

        # Geography
        g = re.search(r"(?:registered office|office|establishment|presence)\s+in\s+(?:the\s+)?(?:state of\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)", t)
        if g and re.search(r"shall|must|should|required", low):
            add(_req(c, req_type="geographic", title=f"Office/presence in {g.group(1)}", metric="state_presence", operator="in", standard=g.group(1), confidence=0.65), ("geo", g.group(1)))
            continue

        # Consortium / JV
        if re.search(r"consortium|joint venture|\bjv\b", low):
            allowed = not re.search(r"not (be )?(allowed|permitted|accepted)", low)
            add(_req(c, mandatory="mandatory" if not allowed else _mandatory(t), req_type="consortium", title="Consortium / JV bids " + ("permitted with conditions" if allowed else "not permitted"), metric="consortium_allowed", operator="==", threshold_value="1" if allowed else "0", confidence=0.75), ("jv",))
            continue

        # Blacklisting declaration
        if re.search(r"blacklist|debarred", low) and re.search(r"declaration|undertaking|affidavit|not been", low):
            add(_req(c, req_type="document", title="Declaration of not being blacklisted/debarred", metric="document", operator="exists", document_kind="legal_compliance", confidence=0.75), ("doc", "blacklist"))
            continue

        # Required documents in submission checklist
        if re.search(r"audited (balance sheet|financial statement|accounts)", low):
            add(_req(c, req_type="document", title="Audited financial statements", metric="document", operator="exists", document_kind="financial_statement", period_years=_period(t), confidence=0.75), ("doc", "fin"))
        if re.search(r"(product )?(catalogue|catalog|datasheet|data sheet)", low) and re.search(r"submit|enclose|attach|furnish|provide", low):
            add(_req(c, req_type="document", title="Product catalogue / technical datasheets", metric="document", operator="exists", document_kind="product_catalogue", confidence=0.7), ("doc", "catalogue"))
        if re.search(r"(completion|performance|experience) certificates?", low) and re.search(r"submit|enclose|attach|furnish|support", low):
            add(_req(c, req_type="document", title="Work completion / performance certificates", metric="document", operator="exists", document_kind="completion_certificate", confidence=0.7), ("doc", "completion"))
    return reqs


def _work_keywords(text: str) -> list[str]:
    m = re.search(r"similar works?\s*(?:means|shall mean|i\.e\.|:|-|\()\s*([^.;)]{5,160})", text, re.I)
    source = m.group(1) if m else text
    words = re.findall(r"[a-z]{4,}", source.lower())
    stop = {"similar", "works", "work", "completed", "value", "last", "years", "each", "least", "should", "have", "bidder", "with", "during", "shall", "mean", "means", "nature", "than", "less", "lakh", "crore"}
    return sorted({w for w in words if w not in stop})[:10]


_FIELD_LABELS: dict[str, str] = {
    "reference_number": r"(?:tender|bid|nit|rfp|e-?tender)\s*(?:reference|ref\.?|id|no\.?|number)\s*(?:no\.?)?",
    "title": r"name of (?:the )?work|(?:tender|work) (?:title|description)|subject",
    "procuring_organization": r"(?:procuring|tendering|issuing)\s+(?:entity|organi[sz]ation|authority)|organi[sz]ation name|department",
    "estimated_value": r"estimated (?:cost|value)|tender value|approximate value",
    "publication_date": r"date of (?:publication|issue)|(?:publication|published|publishing|issue) date",
    "prebid_meeting_at": r"pre-?bid meeting",
    "clarification_deadline": r"(?:last date|deadline) for (?:seeking )?(?:clarification|queries)",
    "submission_deadline": r"(?:last|closing|due) date (?:and time )?(?:for|of) (?:online )?(?:bid )?submission|bid submission end date|submission deadline",
    "bid_opening_at": r"(?:technical )?bid opening",
    "emd_amount": r"\bemd\b|earnest money",
    "tender_fee": r"tender (?:document )?fee|cost of tender document",
    "performance_security_text": r"performance (?:security|bank guarantee)",
    "location_state": r"(?:place|location) of (?:work|delivery)|delivery location",
}


_ANYWHERE = {"performance_security_text", "emd_amount"}


def extract_fields(doc: ParsedDocument) -> TenderFields:
    fields = TenderFields()
    lines: list[tuple[int, str]] = []
    for p in doc.pages:
        for ln in p.text.splitlines():
            if ln.strip():
                lines.append((p.page_no, re.sub(r"\s+", " ", ln.strip())))
    for name, label in _FIELD_LABELS.items():
        rx = re.compile(rf"^(?:\d+[.)]\s*)?(?:{label})(?:\s*\([^)]*\))?\s*(?:[:\-–|]\s*(.*)|\s*:?)$", re.I)
        anywhere = re.compile(rf"(?:{label})", re.I) if name in _ANYWHERE else None
        for i, (page, ln) in enumerate(lines):
            m = rx.search(ln)
            if not m and anywhere is not None and anywhere.search(ln):
                value = ln
            elif not m:
                continue
            else:
                value = (m.group(1) or "").strip(" :-–|")
            if not value and i + 1 < len(lines):
                value = lines[i + 1][1]
            if not value:
                continue
            parsed: str | None = value[:500]
            if name in ("estimated_value", "emd_amount", "tender_fee"):
                money = parse_inr(value)
                parsed = str(money[0]) if money else None
                if name == "emd_amount":
                    fields.emd_text = ln
                    if parsed is None:
                        continue
            elif name == "publication_date":
                d = parse_date(value)
                parsed = d.isoformat() if d else None
            elif name in ("prebid_meeting_at", "clarification_deadline", "submission_deadline", "bid_opening_at"):
                dt = parse_datetime_ist(value)
                parsed = dt.isoformat() if dt else None
            if parsed and getattr(fields, name) is None:
                setattr(fields, name, parsed)
                fields.citations[name] = FieldCitation(page=page, text=ln[:400])
                break
    if fields.title is None and lines:
        # First substantial heading-like line as a labelled low-confidence fallback.
        for page, ln in lines[:15]:
            if 15 < len(ln) < 200 and not _is_heading(ln) and not re.search(r"^(page|government of|tender document)\b", ln, re.I):
                fields.title = ln
                fields.citations["title"] = FieldCitation(page=page, text=ln + " [inferred: first heading]")
                break
    if fields.procuring_organization is None:
        org_rx = re.compile(r"\b(corporation|board|limited|ltd|department|authority|municipal|ministry|council|company)\b", re.I)
        for page, ln in [x for x in lines if x[0] == 1][:8]:
            if org_rx.search(ln) and not _is_heading(ln) and len(ln) < 160 and ":" not in ln:
                fields.procuring_organization = ln
                fields.citations["procuring_organization"] = FieldCitation(page=page, text=ln + " [inferred: header line]")
                break
    risk_rx = re.compile(r"liquidated damages|penalty|unlimited liability|indemnif|forfeit|termination for convenience|warranty of \d+ years|retention money", re.I)
    for page, ln in lines:
        if risk_rx.search(ln) and len(fields.risk_clauses) < 12:
            fields.risk_clauses.append(f"p.{page}: {ln[:300]}")
    return fields


def amount(value: str | None) -> Decimal | None:
    try:
        return Decimal(value) if value not in (None, "") else None
    except Exception:
        return None
