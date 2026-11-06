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
    external_id: str | None = None  # feed guid / OCDS ocid / email-derived id, used for de-duplication
    source_platform: str | None = None  # portal the item came from (e.g. "GeM"), when the source can tell


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
            f"{name} has no official public API, and TenderPilot does not scrape portals, bypass CAPTCHAs or log in on your "
            "behalf. To receive its tenders automatically, forward the portal's own email alerts (Settings → Tender feeds & "
            "email alerts) or subscribe to a licensed data provider's feed."
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


class FeedFileSource:
    """A downloaded feed file in any supported structured format (auto-detected)."""

    key, name, supported = "feed_file", "Feed file (OCDS, RSS/Atom, JSON, CSV)", True
    status_note = "Upload an export from a licensed provider, an open-data OCDS release package, or an RSS/Atom file."
    accept = ".json,.xml,.rss,.atom,.csv"

    def fetch(self, payload: bytes | None = None) -> list[SourceTender]:
        from app.integrations.feeds import parse_feed

        if not payload:
            raise ValidationFailed("Feed file required.")
        return parse_feed(payload)


class EmailAlertSource:
    """A portal alert email saved as .eml (or its text pasted into a .txt file)."""

    key, name, supported = "email_alert", "Portal alert email (.eml)", True
    status_note = "Upload a CPPP, GeM or state-portal alert email. Each tender reference in it becomes a discovered tender."
    accept = ".eml,.txt,.html"

    def fetch(self, payload: bytes | None = None) -> list[SourceTender]:
        from app.integrations.email_alerts import parse_alert_email

        if not payload:
            raise ValidationFailed("Email file required.")
        return parse_alert_email(payload).tenders


SOURCES: dict[str, TenderSource] = {
    "csv_feed": CsvFeedSource(),
    "feed_file": FeedFileSource(),
    "email_alert": EmailAlertSource(),
    "cppp": UnsupportedPortal("cppp", "Central Public Procurement Portal (CPPP)"),
    "gem": UnsupportedPortal("gem", "Government e-Marketplace (GeM)"),
    "state_portal": UnsupportedPortal("state_portal", "State e-procurement portals"),
    "enterprise_feed": UnsupportedPortal("enterprise_feed", "Enterprise procurement feed (partner integration)"),
}
