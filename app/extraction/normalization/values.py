"""Normalisation of Indian procurement values: rupee amounts (lakh/crore), dates, fiscal years, language."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser

IST = ZoneInfo("Asia/Kolkata")

_UNITS = {
    "crore": Decimal("10000000"),
    "crores": Decimal("10000000"),
    "cr": Decimal("10000000"),
    "lakh": Decimal("100000"),
    "lakhs": Decimal("100000"),
    "lac": Decimal("100000"),
    "lacs": Decimal("100000"),
    "million": Decimal("1000000"),
    "mn": Decimal("1000000"),
    "thousand": Decimal("1000"),
    "k": Decimal("1000"),
}

MONEY_RE = re.compile(
    r"(?:₹|rs\.?|inr|rupees)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(crores?|cr\.?|lakhs?|lacs?|lac|million|mn|thousand)?"
    r"|([0-9][0-9,]*(?:\.[0-9]+)?)\s*(crores?|cr\.?|lakhs?|lacs?|lac)\b",
    re.IGNORECASE,
)


def parse_decimal(raw: str | float | int | None) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    cleaned = raw.replace(",", "").replace("₹", "").strip()
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def parse_inr(text: str) -> tuple[Decimal, str] | None:
    """Return (amount in rupees, matched text) for the first rupee amount in text."""
    m = MONEY_RE.search(text)
    if not m:
        return None
    number = m.group(1) or m.group(3)
    unit = (m.group(2) or m.group(4) or "").lower().rstrip(".")
    value = parse_decimal(number)
    if value is None:
        return None
    if unit:
        value *= _UNITS.get(unit, Decimal(1))
    return value.quantize(Decimal("0.01")), m.group(0)


def find_all_inr(text: str) -> list[tuple[Decimal, str, int]]:
    out = []
    for m in MONEY_RE.finditer(text):
        sub = parse_inr(m.group(0))
        if sub:
            out.append((sub[0], m.group(0), m.start()))
    return out


def format_inr(value: Decimal | None) -> str:
    """Indian digit grouping: ₹5,00,00,000.00"""
    if value is None:
        return "—"
    q = value.quantize(Decimal("0.01"))
    sign = "-" if q < 0 else ""
    whole, frac = f"{abs(q):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return f"{sign}₹{whole}.{frac}"


def format_inr_short(value: Decimal | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= Decimal("10000000"):
        return f"₹{(value / Decimal('10000000')).quantize(Decimal('0.01'))} Cr"
    if abs(value) >= Decimal("100000"):
        return f"₹{(value / Decimal('100000')).quantize(Decimal('0.01'))} L"
    return format_inr(value)


DATE_RE = re.compile(
    r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4}"
    r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b"
)
TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm|hrs|hours|AM|PM)?\b")


def parse_date(text: str) -> date | None:
    """Indian documents are day-first: 05/06/2026 is 5 June 2026."""
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        d = dateparser.parse(re.sub(r"(\d)(st|nd|rd|th)", r"\1", m.group(1)), dayfirst=True, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    if not (1990 <= d.year <= 2100):
        return None
    return d.date()


def find_all_dates(text: str) -> list[tuple[date, str, int]]:
    out = []
    for m in DATE_RE.finditer(text):
        d = parse_date(m.group(0))
        if d:
            out.append((d, m.group(0), m.start()))
    return out


def parse_datetime_ist(text: str) -> datetime | None:
    """Parse a deadline expressed in IST and return an aware UTC-comparable datetime."""
    m = DATE_RE.search(text)
    d = parse_date(text)
    if not d or not m:
        return None
    t = time(23, 59)  # a date without a time is treated as end of day IST
    tm = TIME_RE.search(text[m.end() : m.end() + 40])
    if tm:
        hh, mm, ampm = int(tm.group(1)), int(tm.group(2)), (tm.group(3) or "").lower()
        if ampm == "pm" and hh < 12:
            hh += 12
        if ampm == "am" and hh == 12:
            hh = 0
        if 0 <= hh < 24 and 0 <= mm < 60:
            t = time(hh, mm)
    return datetime.combine(d, t, tzinfo=IST)


FY_RE = re.compile(r"\b(?:FY\s*)?(20\d{2})\s*[-–/]\s*(\d{2,4})\b", re.IGNORECASE)


def normalise_fiscal_year(text: str) -> str | None:
    """'FY 2023-24', '2023-2024', '2023/24' -> '2023-24' (Indian April–March fiscal year)."""
    m = FY_RE.search(text)
    if not m:
        return None
    start = int(m.group(1))
    end = m.group(2)
    end_two = int(end[-2:])
    if (start + 1) % 100 != end_two:
        return None
    return f"{start}-{end_two:02d}"


def fiscal_year_of(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def previous_fiscal_years(reference: date, count: int) -> list[str]:
    """The `count` completed fiscal years before the fiscal year containing `reference`."""
    current_start = reference.year if reference.month >= 4 else reference.year - 1
    return [f"{y}-{(y + 1) % 100:02d}" for y in range(current_start - count, current_start)][::-1]


def detect_language(text: str) -> tuple[str, float]:
    """Script-based detection: Devanagari share decides hi / en / mixed."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "unknown", 0.0
    deva = sum(1 for c in letters if "ऀ" <= c <= "ॿ")
    share = deva / len(letters)
    if share > 0.6:
        return "hi", round(share, 2)
    if share > 0.1:
        return "mixed", round(1 - abs(0.5 - share), 2)
    return "en", round(1 - share, 2)
