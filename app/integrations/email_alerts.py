"""Turn tender-alert emails (CPPP, GeM, state e-procurement portals, aggregators) into discovered tenders.

The organization subscribes to the portal's own alerts with its own account and forwards them to TenderPilot, either
as uploaded ``.eml`` files or through its private inbound address. Nothing logs in to a portal. Alert layouts vary and
are not documented, so parsing is heuristic:

* tender references are found by pattern: GeM bid numbers (``GEM/2026/B/1234567``), NIC GePNIC tender IDs
  (``2026_MES_123456_1``, used by CPPP and most state portals), and ``Tender ID:`` / ``Bid No:`` labels;
* each reference's surrounding block (an HTML table row, or a blank-line separated paragraph) gives its title,
  organisation, closing date, value and link through common labels;
* anything that cannot be read is left empty for the user to fill in. Nothing is guessed.

Email content is untrusted. Only plain text fields are kept, with lengths capped, and only http(s) links are stored.
"""

from __future__ import annotations

import email
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from app.core.errors import ValidationFailed
from app.integrations.feeds import _clip, _url, parse_amount, parse_when
from app.integrations.sources import SourceTender

MAX_EMAIL_BYTES = 5 * 1024 * 1024
MAX_TENDERS_PER_EMAIL = 200

GEM_REF = r"GEM/\d{4}/[BR]/\d{5,}"
NIC_REF = r"\d{4}_[A-Z][A-Z0-9]{1,15}_\d{3,9}_\d{1,3}"
REF_RE = re.compile(rf"(?<![\w/])({GEM_REF}|{NIC_REF})(?![\w/])")
LABELLED_REF_RE = re.compile(
    r"(?:tender\s*(?:id|ref(?:erence)?\.?\s*(?:no\.?|number)?)|bid\s*(?:no\.?|number)|nit\s*no\.?|reference\s*no\.?)\s*[:#\-]\s*"
    r"([A-Za-z0-9][A-Za-z0-9/_.\-]{4,80})",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s<>\"')\]|]+", re.I)

LABELS: dict[str, tuple[str, ...]] = {
    "title": ("tender title", "title", "item", "items", "name of work", "work description", "bid title", "description of work", "product"),
    "procuring_organization": ("organisation chain", "organization chain", "organisation name", "organization name", "organisation",
                               "organization", "ministry", "department", "buyer", "office"),
    "submission_deadline": ("bid submission end date", "bid end date", "closing date", "submission end date", "last date of submission",
                            "last date", "end date", "due date"),
    "estimated_value": ("tender value", "estimated cost", "estimated value", "ecv", "value"),
    "location_state": ("location", "state", "consignee state"),
    "category": ("category", "product category", "tender category"),
}

PORTALS = (
    ("GeM", ("gem.gov.in", "gemportal")),
    ("CPPP", ("eprocure.gov.in",)),
    ("Defence eProc", ("defproc.gov.in",)),
    ("State e-procurement", ("nprocure", "eproc", "etender", "mahatenders", "tntenders", "wbtenders", "tender.gov.in", "eproc.rajasthan")),
)


@dataclass
class AlertEmail:
    sender: str
    subject: str
    message_id: str
    received_at: datetime | None
    portal: str | None
    tenders: list[SourceTender] = field(default_factory=list)


class _TextExtractor(HTMLParser):
    """HTML to text that keeps table rows as single ' | '-separated lines and links as text."""

    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.lower().startswith(("http://", "https://")):
                self.out.append(f" {href} ")
        if tag == "table":
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("tr", "table", "p", "div"):
            self.out.append("\n")
        if tag == "table":
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    p.close()
    return "".join(p.out)


def _body_text(msg: EmailMessage) -> str:
    plain = html = None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
        if ctype == "text/html" and html is None:
            html = content
        elif ctype == "text/plain" and plain is None:
            plain = content
        elif ctype == "message/rfc822" and isinstance(content, EmailMessage):  # forwarded as attachment
            inner = _body_text(content)
            plain = (plain or "") + "\n" + inner
    # HTML alerts usually carry the table; the plain part is often a stub.
    if html and (not plain or len(REF_RE.findall(html_to_text(html))) > len(REF_RE.findall(plain))):
        return html_to_text(html)
    return plain or ""


def _detect_portal(sender: str, text: str) -> str | None:
    hay = (sender + " " + " ".join(URL_RE.findall(text))).lower()
    for name, needles in PORTALS:
        if any(n in hay for n in needles):
            return name
    if re.search(GEM_REF, text):
        return "GeM"
    return None


def _label_value(block: str, labels: tuple[str, ...]) -> str | None:
    for lab in labels:
        m = re.search(rf"(?:^|[\n|])\s*{re.escape(lab)}\s*[:\-–]\s*([^\n|]{{2,500}})", block, re.I)
        if m:
            return m.group(1).strip()
    return None


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.split("|") if c.strip()]


def _from_row(ref: str, line: str, header: list[str] | None) -> dict[str, str | None]:
    """A table row: map cells by header names when the table had a header, otherwise by what each cell looks like."""
    cells = _cells(line)
    got: dict[str, str | None] = {}
    if header and len(header) == len(cells):
        for h, c in zip(header, cells, strict=True):
            hl = h.lower()
            for key, labels in LABELS.items():
                if key not in got and any(lab == hl or lab in hl for lab in labels):
                    got[key] = c
                    break
    rest = [c for c in cells if ref not in c and not URL_RE.fullmatch(c) and c not in got.values()]
    if "submission_deadline" not in got:
        got["submission_deadline"] = next((c for c in rest if parse_when(c) and re.search(r"\d{4}", c) and len(c) < 40), None)
    if "estimated_value" not in got:
        got["estimated_value"] = next((c for c in rest if re.search(r"₹|rs\.?\s*\d|inr|crore|lakh", c, re.I)), None)
    if "title" not in got:
        text_cells = [c for c in rest if c not in got.values() and not re.fullmatch(r"[\d\s.,/:\-]+", c)]
        got["title"] = max(text_cells, key=len) if text_cells else None
    return got


def _blocks(text: str) -> list[str]:
    return [b for b in re.split(r"\n\s*\n|\n[-=_*]{5,}\n", text) if b.strip()]


def extract_tenders(text: str, portal: str | None) -> list[SourceTender]:
    text = text.replace("\r\n", "\n").replace("||", " › ")  # CPPP writes organisation chains as "A||B"
    found: dict[str, SourceTender] = {}
    header: list[str] | None = None
    for block in _blocks(text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        refs_in_block = list(dict.fromkeys(REF_RE.findall(block)))
        labelled = LABELLED_REF_RE.search(block)
        if not refs_in_block and labelled:
            refs_in_block = [labelled.group(1).rstrip(".")]
        if not refs_in_block:
            # Possibly a table header ("Tender ID | Title | Closing Date ...").
            for ln in lines:
                cells = _cells(ln)
                if len(cells) >= 3 and any(re.search(r"tender|bid|title|closing|date", c, re.I) for c in cells):
                    header = cells
            continue
        table_rows = [ln for ln in lines if ln.count("|") >= 2]
        for ref in refs_in_block:
            row = next((ln for ln in table_rows if ref in ln), None)
            if row and (len(refs_in_block) > 1 or not _label_value(block, LABELS["title"])):
                # Find the header row directly above, if it is in this block.
                idx = lines.index(row)
                hdr = next((_cells(ln) for ln in reversed(lines[:idx]) if ln.count("|") >= 2 and not REF_RE.search(ln)), header)
                vals = _from_row(ref, row, hdr)
                scope = row
            else:
                vals = {k: _label_value(block, labs) for k, labs in LABELS.items()}
                scope = block
                if not vals.get("title"):
                    vals["title"] = next((ln.strip() for ln in lines if ref not in ln and ":" not in ln and not URL_RE.search(ln)
                                          and len(ln.strip()) > 10), None)
            links = [u for u in URL_RE.findall(scope) if _url(u)]
            link = next((u for u in links if any(n in u.lower() for _, ns in PORTALS for n in ns)), links[0] if links else None)
            title = _clip(vals.get("title"), 500) or f"{portal or 'Portal'} tender {ref}"
            if ref in found:
                continue
            found[ref] = SourceTender(
                title=title,
                reference_number=_clip(ref, 200),
                procuring_organization=_clip(vals.get("procuring_organization"), 300),
                source_url=_url(link),
                estimated_value=parse_amount(vals.get("estimated_value")) if vals.get("estimated_value") else None,
                location_state=_clip(vals.get("location_state"), 80),
                submission_deadline=parse_when(vals.get("submission_deadline")) if vals.get("submission_deadline") else None,
                category=_clip(vals.get("category"), 120),
                external_id=_clip(f"{(portal or 'email').lower()}:{ref}", 200),
                source_platform=portal,
            )
            if len(found) >= MAX_TENDERS_PER_EMAIL:
                return list(found.values())
    return list(found.values())


def parse_alert_email(raw: bytes) -> AlertEmail:
    if not raw or not raw.strip():
        raise ValidationFailed("The email is empty.")
    if len(raw) > MAX_EMAIL_BYTES:
        raise ValidationFailed("The email is larger than 5 MB.")
    msg = email.message_from_bytes(raw, policy=policy.default)
    has_headers = any(msg.get(h) for h in ("From", "Subject", "Date", "Message-ID"))
    if has_headers:
        text = _body_text(msg)  # type: ignore[arg-type]
        sender = parseaddr(str(msg.get("From", "")))[1].lower()
        subject = _clip(msg.get("Subject", ""), 300) or ""
        try:
            received = parsedate_to_datetime(str(msg["Date"])) if msg.get("Date") else None
        except (TypeError, ValueError):
            received = None
        message_id = _clip(msg.get("Message-ID"), 300) or ""
    else:  # pasted alert text, no headers
        decoded = raw.decode("utf-8", errors="replace")
        text = html_to_text(decoded) if re.search(r"<(table|tr|td|p|div|br)\b", decoded, re.I) else decoded
        sender, subject, received, message_id = "", "", None, ""
    if not message_id:
        message_id = "sha256:" + hashlib.sha256(raw).hexdigest()
    portal = _detect_portal(sender, text)
    return AlertEmail(sender=sender, subject=subject, message_id=message_id, received_at=received, portal=portal,
                      tenders=extract_tenders(text, portal))
