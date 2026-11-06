"""Parsers for structured tender feeds: OCDS (Open Contracting Data Standard), RSS 2.0 / Atom, generic JSON and CSV.

These are the authorised alternatives to portal scraping. Typical sources are a licensed data provider's API or export,
an open-data publication (e.g. OCDS releases on data.gov.in) or a partner system. Feeds are untrusted input. Every
value is length-capped, only http(s) links are kept, XML is parsed with defusedxml and any DTD is rejected (no entity
expansion), and at most ``MAX_ITEMS`` items are read per payload.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element, ParseError

from dateutil import parser as dateparser
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

from app.core.errors import ValidationFailed
from app.extraction.normalization.values import IST, parse_datetime_ist, parse_inr
from app.integrations.sources import SourceTender

MAX_ITEMS = 1000
FORMATS = ("auto", "ocds", "rss", "json", "csv")

# Field names accepted by the generic JSON / CSV formats (case-insensitive); a feed's field_map can add aliases.
DEFAULT_FIELDS: dict[str, tuple[str, ...]] = {
    "title": ("title", "tender_title", "name", "bid_title", "work_description", "name_of_work"),
    "reference_number": ("reference_number", "reference", "ref_no", "tender_id", "bid_number", "bid_no", "tender_ref_no"),
    "procuring_organization": ("procuring_organization", "organisation", "organization", "buyer", "department", "ministry"),
    "source_url": ("source_url", "url", "link", "tender_url"),
    "estimated_value": ("estimated_value", "value", "tender_value", "estimated_cost", "ecv"),
    "location_state": ("location_state", "state", "location"),
    "submission_deadline": ("submission_deadline", "closing_date", "bid_end_date", "deadline", "last_date"),
    "category": ("category", "product_category", "type"),
    "description": ("description", "summary", "details"),
    "external_id": ("external_id", "id", "guid", "uid"),
    "source_platform": ("source_platform", "portal", "platform", "source"),
}


def _clip(v: Any, n: int) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s[:n] or None


def _url(v: Any) -> str | None:
    s = _clip(v, 1000)
    if not s:
        return None
    parts = urlsplit(s)
    return s if parts.scheme in ("http", "https") and parts.hostname else None


def parse_when(v: Any) -> datetime | None:
    """ISO 8601 (with or without offset), RFC 822, or Indian day-first text. Naive times are taken as IST."""
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=IST)
    s = str(v).strip()[:80]
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=IST)
    except ValueError:
        pass
    got = parse_datetime_ist(s)
    if got:
        return got
    try:
        d = dateparser.parse(s, dayfirst=True, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    if not (1990 <= d.year <= 2100):
        return None
    return d if d.tzinfo else d.replace(tzinfo=IST)


def parse_amount(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float, Decimal)):
        try:
            return Decimal(str(v)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
    s = str(v).strip()[:100]
    money = parse_inr(s if re.search(r"₹|rs\.?|inr|rupees|crore|lakh|lac", s, re.I) else "Rs " + s)
    return money[0] if money else None


def detect_format(payload: bytes) -> str:
    head = payload.lstrip()[:2000].lower()
    if head.startswith((b"{", b"[")):
        try:
            data = json.loads(payload.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            raise ValidationFailed("The feed looks like JSON but could not be parsed.")
        if isinstance(data, dict) and ("releases" in data or "records" in data or "ocid" in data):
            return "ocds"
        return "json"
    if head.startswith(b"<") or head.startswith(b"\xef\xbb\xbf<"):
        return "rss"
    return "csv"


def parse_feed(payload: bytes, fmt: str = "auto", field_map: dict[str, list[str]] | None = None) -> list[SourceTender]:
    if not payload:
        raise ValidationFailed("The feed is empty.")
    if fmt not in FORMATS:
        raise ValidationFailed(f"Unknown feed format. Use one of: {', '.join(FORMATS)}.")
    if fmt == "auto":
        fmt = detect_format(payload)
    if fmt == "ocds":
        return parse_ocds(_json(payload))
    if fmt == "rss":
        return parse_rss(payload)
    if fmt == "json":
        return parse_json_items(_json(payload), field_map)
    return parse_csv(payload, field_map)


def _json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise ValidationFailed("The feed is not valid JSON.")


# ---------------------------------------------------------------- OCDS


def _ocds_releases(data: Any) -> Iterable[dict[str, Any]]:
    if isinstance(data, list):
        yield from (r for r in data if isinstance(r, dict))
    elif isinstance(data, dict):
        if isinstance(data.get("releases"), list):
            yield from (r for r in data["releases"] if isinstance(r, dict))
        elif isinstance(data.get("records"), list):
            for rec in data["records"]:
                if not isinstance(rec, dict):
                    continue
                if isinstance(rec.get("compiledRelease"), dict):
                    yield rec["compiledRelease"]
                elif isinstance(rec.get("releases"), list) and rec["releases"] and isinstance(rec["releases"][-1], dict):
                    yield rec["releases"][-1]
        elif "ocid" in data:
            yield data


def parse_ocds(data: Any) -> list[SourceTender]:
    """OCDS release/record packages. Only releases with a tender section become tenders; awards-only releases are skipped.
    When several releases share an ocid, the last one wins (OCDS releases are published in order)."""
    by_ocid: dict[str, SourceTender] = {}
    for rel in _ocds_releases(data):
        t = rel.get("tender") if isinstance(rel.get("tender"), dict) else None
        if not t:
            continue
        title = _clip(t.get("title") or t.get("description"), 500)
        if not title:
            continue
        value = t.get("value") if isinstance(t.get("value"), dict) else {}
        currency = (value.get("currency") or "INR").upper()
        period = t.get("tenderPeriod") if isinstance(t.get("tenderPeriod"), dict) else {}
        buyer = rel.get("buyer") if isinstance(rel.get("buyer"), dict) else {}
        pe = t.get("procuringEntity") if isinstance(t.get("procuringEntity"), dict) else {}
        region = None
        for party in rel.get("parties") or []:
            if isinstance(party, dict) and party.get("id") in (pe.get("id"), buyer.get("id")):
                addr = party.get("address") if isinstance(party.get("address"), dict) else {}
                region = addr.get("region") or addr.get("locality")
                break
        docs = [d for d in (t.get("documents") or []) if isinstance(d, dict)]
        link = next((u for u in (_url(d.get("url")) for d in docs) if u), None)
        cat = t.get("mainProcurementCategory") or next(
            (c.get("description") for it in (t.get("items") or []) if isinstance(it, dict)
             for c in [it.get("classification")] if isinstance(c, dict) and c.get("description")), None)
        ocid = _clip(rel.get("ocid"), 200)
        st = SourceTender(
            title=title,
            reference_number=_clip(t.get("id"), 200) if t.get("id") not in (None, ocid) else None,
            procuring_organization=_clip(pe.get("name") or buyer.get("name"), 300),
            source_url=link,
            estimated_value=parse_amount(value.get("amount")) if currency == "INR" else None,
            location_state=_clip(region, 80),
            submission_deadline=parse_when(period.get("endDate")),
            category=_clip(cat, 120),
            description=_clip(t.get("description"), 4000) if t.get("description") != t.get("title") else None,
            external_id=ocid or _clip(rel.get("id"), 200),
        )
        by_ocid[st.external_id or f"#{len(by_ocid)}"] = st
        if len(by_ocid) >= MAX_ITEMS:
            break
    return list(by_ocid.values())


# ---------------------------------------------------------------- RSS / Atom

_ATOM = "{http://www.w3.org/2005/Atom}"
_REF_RE = re.compile(r"(GEM/\d{4}/[BR]/\d{5,}|\b\d{4}_[A-Z0-9]{2,}_\d{3,}_\d{1,3}\b)")
_LABEL_DEADLINE = re.compile(r"(?:closing|bid\s*end|submission\s*end|last)\s*date[^:]*[:\-]\s*([^\n|<]{6,40})", re.I)
_LABEL_VALUE = re.compile(r"(?:tender\s*value|estimated\s*(?:cost|value)|ecv)[^:]*[:\-]\s*([^\n|<]{2,40})", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    import html

    return html.unescape(_TAG_RE.sub(" ", s or ""))


def parse_rss(payload: bytes) -> list[SourceTender]:
    try:
        root = safe_fromstring(payload, forbid_dtd=True)
    except DefusedXmlException:
        raise ValidationFailed("XML feeds with DOCTYPE or ENTITY declarations are not accepted.")
    except ParseError as e:
        raise ValidationFailed(f"The feed is not valid XML ({e}).")
    out: list[SourceTender] = []
    items = root.findall("./channel/item") or root.findall(f"{_ATOM}entry") or root.findall(".//item")
    for it in items[:MAX_ITEMS]:
        def tx(tag: str, it: Element = it) -> str:
            el = it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        title = _clip(_strip_html(tx("title") or tx(f"{_ATOM}title")), 500)
        if not title:
            continue
        link = tx("link")
        if not link:
            el = it.find(f"{_ATOM}link")
            link = el.get("href", "") if el is not None else ""
        desc = _strip_html(tx("description") or tx(f"{_ATOM}summary") or tx(f"{_ATOM}content"))
        blob = f"{title}\n{desc}"
        ref = _REF_RE.search(blob)
        dl = _LABEL_DEADLINE.search(desc)
        val = _LABEL_VALUE.search(desc)
        out.append(SourceTender(
            title=title,
            reference_number=ref.group(1) if ref else None,
            source_url=_url(link),
            estimated_value=parse_amount(val.group(1)) if val else None,
            submission_deadline=parse_when(dl.group(1)) if dl else None,
            category=_clip(tx("category"), 120),
            description=_clip(desc, 4000),
            external_id=_clip(tx("guid") or tx(f"{_ATOM}id") or link, 200),
        ))
    return out


# ---------------------------------------------------------------- generic JSON / CSV


def _resolver(field_map: dict[str, list[str]] | None):
    aliases = {k: [a.lower() for a in v] for k, v in DEFAULT_FIELDS.items()}
    for k, extra in (field_map or {}).items():
        if k in aliases:
            aliases[k] = [str(a).lower() for a in extra] + aliases[k]

    def get(row: dict[str, Any], key: str) -> Any:
        for a in aliases[key]:
            if row.get(a) not in (None, ""):
                return row[a]
        return None

    return get


def _row_to_tender(row: dict[str, Any], get) -> SourceTender | None:
    title = _clip(get(row, "title"), 500)
    if not title:
        return None
    return SourceTender(
        title=title,
        reference_number=_clip(get(row, "reference_number"), 200),
        procuring_organization=_clip(get(row, "procuring_organization"), 300),
        source_url=_url(get(row, "source_url")),
        estimated_value=parse_amount(get(row, "estimated_value")),
        location_state=_clip(get(row, "location_state"), 80),
        submission_deadline=parse_when(get(row, "submission_deadline")),
        category=_clip(get(row, "category"), 120),
        description=_clip(get(row, "description"), 4000),
        external_id=_clip(get(row, "external_id"), 200),
        source_platform=_clip(get(row, "source_platform"), 80),
    )


def parse_json_items(data: Any, field_map: dict[str, list[str]] | None = None) -> list[SourceTender]:
    if isinstance(data, dict):
        data = next((v for k, v in data.items() if k.lower() in ("items", "tenders", "data", "results", "bids") and isinstance(v, list)), None)
    if not isinstance(data, list):
        raise ValidationFailed("JSON feeds must be a list of tenders, or an object with an 'items'/'tenders'/'data' list.")
    get = _resolver(field_map)
    out = []
    for raw in data[:MAX_ITEMS]:
        if isinstance(raw, dict) and (t := _row_to_tender({str(k).lower(): v for k, v in raw.items()}, get)):
            out.append(t)
    return out


def parse_csv(payload: bytes, field_map: dict[str, list[str]] | None = None) -> list[SourceTender]:
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig", errors="replace")))
    if not reader.fieldnames:
        raise ValidationFailed("The CSV has no header row.")
    get = _resolver(field_map)
    out = []
    for i, raw in enumerate(reader):
        if i >= MAX_ITEMS:
            break
        row = {k.lower().strip(): (v or "").strip() for k, v in raw.items() if k}
        if t := _row_to_tender(row, get):
            out.append(t)
    if not out and "title" not in [f.lower().strip() for f in reader.fieldnames]:
        raise ValidationFailed("CSV must include a 'title' column (or a column mapped to it).")
    return out
