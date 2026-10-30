"""Tender source adapters.

Only adapters backed by an authorised access path are marked supported. Public visibility of a
portal is not treated as permission for automated access: CPPP/GeM/state portal adapters are
declared but report `unsupported` until an official API or licensed feed is configured.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.core.errors import ServiceUnavailable, ValidationFailed
from app.extraction.normalization.values import parse_datetime_ist, parse_inr


@dataclass
class SourceTender:
    title: str
    reference_number: str | None = None
    procuring_organization: str | None = None
    source_url: str | None = None
    estimated_value: Decimal | None = None
    location_state: str | None = None
    submission_deadline: datetime | None = None
    category: str | None = None
    description: str | None = None
    keywords: list[str] = field(default_factory=list)


class TenderSource(Protocol):
    key: str
    name: str
    supported: bool
    status_note: str

    def fetch(self, payload: bytes | None = None) -> list[SourceTender]: ...


class UnsupportedPortal:
    supported = False

    def __init__(self, key: str, name: str):
        self.key, self.name = key, name
        self.status_note = (
            f"No authorised API or licensed feed is configured for {name}. TenderPilot does not scrape portals, "
            "bypass CAPTCHAs or log in on your behalf. Download the tender from the portal and upload it, or use a CSV feed."
        )

    def fetch(self, payload: bytes | None = None) -> list[SourceTender]:
        raise ServiceUnavailable(self.status_note)


class CsvFeedSource:
    """User- or partner-supplied CSV feed. Columns (case-insensitive): title, reference_number,
    procuring_organization, source_url, estimated_value, location_state, submission_deadline, category, description."""

    key, name, supported = "csv_feed", "CSV / structured feed", True
    status_note = "Upload a CSV export from an authorised feed or your own tracking sheet."

    def fetch(self, payload: bytes | None = None) -> list[SourceTender]:
        if not payload:
            raise ValidationFailed("CSV payload required.")
        text = payload.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "title" not in [f.lower().strip() for f in reader.fieldnames]:
            raise ValidationFailed("CSV must include a 'title' column.")
        out = []
        for i, raw in enumerate(reader):
            if i >= 1000:
                break
            row = {k.lower().strip(): (v or "").strip() for k, v in raw.items() if k}
            if not row.get("title"):
                continue
            money = parse_inr("Rs " + row["estimated_value"]) if row.get("estimated_value") else None
            out.append(
                SourceTender(
                    title=row["title"][:500],
                    reference_number=row.get("reference_number") or None,
                    procuring_organization=row.get("procuring_organization") or None,
                    source_url=row.get("source_url") or None,
                    estimated_value=money[0] if money else None,
                    location_state=row.get("location_state") or None,
                    submission_deadline=parse_datetime_ist(row["submission_deadline"]) if row.get("submission_deadline") else None,
                    category=row.get("category") or None,
                    description=row.get("description") or None,
                )
            )
        return out


SOURCES: dict[str, TenderSource] = {
    "csv_feed": CsvFeedSource(),
    "cppp": UnsupportedPortal("cppp", "Central Public Procurement Portal (CPPP)"),
    "gem": UnsupportedPortal("gem", "Government e-Marketplace (GeM)"),
    "state_portal": UnsupportedPortal("state_portal", "State e-procurement portals"),
    "enterprise_feed": UnsupportedPortal("enterprise_feed", "Enterprise procurement feed (partner integration)"),
}
