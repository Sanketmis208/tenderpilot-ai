"""Feed parsers (OCDS, RSS/Atom, JSON, CSV) and portal alert-email parsing. No database."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.errors import ValidationFailed
from app.extraction.normalization.values import IST
from app.integrations import email_alerts, feeds

FX = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"


def test_ocds_latest_release_wins_and_awards_are_skipped():
    items = feeds.parse_feed((FX / "ocds_release_package.json").read_bytes())
    assert len(items) == 1
    t = items[0]
    assert t.external_id == "ocds-synth-0001"
    assert t.reference_number == "2026_DEMOA_111111_1"
    assert t.title.startswith("Supply of 33kV")
    assert t.estimated_value == Decimal("18500000.00")
    # The amendment release (later in the package) moved the deadline.
    assert t.submission_deadline == datetime(2026, 12, 12, 15, 0, tzinfo=IST)
    assert t.procuring_organization == "Demo State Power Distribution Co."


def test_ocds_record_package_and_foreign_currency():
    data = {"records": [{"ocid": "x-1", "compiledRelease": {"ocid": "x-1", "tender": {"title": "Pumps", "value": {"amount": 5, "currency": "USD"}}}}]}
    [t] = feeds.parse_ocds(data)
    assert t.title == "Pumps" and t.estimated_value is None  # never converted or assumed to be rupees


def test_rss_extracts_reference_deadline_value_and_guid():
    items = feeds.parse_feed((FX / "provider_feed.rss").read_bytes())
    assert [i.external_id for i in items] == ["provider-item-9100001", "provider-item-42"]
    gem = items[0]
    assert gem.reference_number == "GEM/2026/B/9100001"
    assert gem.estimated_value == Decimal("42000000.00")
    assert gem.submission_deadline == datetime(2026, 12, 10, 15, 0, tzinfo=IST)
    assert "<p>" not in (gem.description or "")
    assert items[1].reference_number is None
    assert items[1].submission_deadline and items[1].submission_deadline.date().isoformat() == "2026-12-15"


def test_atom_feed():
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Cable laying works</title>
    <id>urn:x:1</id><link href="https://example.org/t/1"/><summary>Tender ID: 2026_ABC_123456_1</summary></entry></feed>"""
    [t] = feeds.parse_feed(atom)
    assert t.source_url == "https://example.org/t/1" and t.reference_number == "2026_ABC_123456_1" and t.external_id == "urn:x:1"


def test_xml_entities_and_dtds_are_rejected():
    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><rss><channel><item><title>&b;</title></item></channel></rss>'
    with pytest.raises(ValidationFailed, match="DOCTYPE"):
        feeds.parse_feed(bomb)
    with pytest.raises(ValidationFailed):
        feeds.parse_feed(b"<rss><channel><item>")


def test_json_list_with_field_map_and_unsafe_links():
    payload = b'{"data": [{"bid_title": "Transformer oil", "Bid_No": "B-1", "link": "javascript:alert(1)", "value": "12.5 lakh"},' \
              b' {"no_title": true}]}'
    [t] = feeds.parse_feed(payload, "json", {"title": ["bid_title"]})
    assert t.title == "Transformer oil" and t.reference_number == "B-1"
    assert t.source_url is None  # only http(s) links are kept
    assert t.estimated_value == Decimal("1250000.00")


def test_csv_aliases_and_missing_title_column():
    [t] = feeds.parse_feed(b"Tender_Title,Tender_ID,Closing_Date,State\nLED fittings,REF-9,2026-12-01T17:00:00,Kerala\n", "csv")
    assert (t.title, t.reference_number, t.location_state) == ("LED fittings", "REF-9", "Kerala")
    assert t.submission_deadline == datetime(2026, 12, 1, 17, 0, tzinfo=IST)
    with pytest.raises(ValidationFailed):
        feeds.parse_feed(b"foo,bar\n1,2\n", "csv")


def test_item_cap_and_length_caps():
    rows = "title\n" + "\n".join(f"T{i}" for i in range(feeds.MAX_ITEMS + 50))
    assert len(feeds.parse_feed(rows.encode(), "csv")) == feeds.MAX_ITEMS
    [t] = feeds.parse_feed(("title\n" + "x" * 5000).encode(), "csv")
    assert len(t.title) == 500


def test_unknown_format_and_empty():
    with pytest.raises(ValidationFailed):
        feeds.parse_feed(b"x", "xlsx")
    with pytest.raises(ValidationFailed):
        feeds.parse_feed(b"")


# ---------------------------------------------------------------- email alerts


def test_gem_html_alert_table():
    a = email_alerts.parse_alert_email((FX / "gem_alert.eml").read_bytes())
    assert a.portal == "GeM" and a.sender == "alerts@gem.gov.in"
    assert a.message_id == "<gem-alert-20261105-001@gem.gov.in>"
    assert [t.reference_number for t in a.tenders] == ["GEM/2026/B/9000001", "GEM/2026/B/9000002"]
    first = a.tenders[0]
    assert first.title == "LT Distribution Panel 415V with ACB"
    assert first.procuring_organization == "Ministry of Power, Demo Discom Ltd"
    assert first.source_url == "https://bidplus.gemportal.in/showbidDocument/9000001"
    assert first.submission_deadline == datetime(2026, 11, 25, 15, 0, tzinfo=IST)
    assert first.source_platform == "GeM"


def test_cppp_plain_text_alert_blocks():
    a = email_alerts.parse_alert_email((FX / "cppp_alert.eml").read_bytes())
    assert a.portal == "CPPP"
    one, two = a.tenders
    assert one.reference_number == "2026_DEMO_123456_1"
    assert one.title == "Supply and installation of 11kV VCB panels at Demo Substation"
    assert one.procuring_organization == "Demo Power Transmission Corporation › Projects Wing"
    assert one.estimated_value == Decimal("25000000.00")
    assert one.location_state == "Maharashtra"
    assert one.submission_deadline == datetime(2026, 11, 30, 15, 0, tzinfo=IST)
    assert two.reference_number == "2026_DEMOW_654321_2" and two.estimated_value is None and two.source_url is None


def test_pasted_alert_without_headers_and_no_guessing():
    a = email_alerts.parse_alert_email(b"Bid No: GEM/2026/B/9000003\n\nThanks")
    [t] = a.tenders
    assert t.reference_number == "GEM/2026/B/9000003"
    assert t.title == "GeM tender GEM/2026/B/9000003"  # placeholder title, not an invented one
    assert t.submission_deadline is None and t.estimated_value is None
    assert a.message_id.startswith("sha256:")


def test_email_without_tenders_and_limits():
    assert email_alerts.parse_alert_email(b"From: a@b.example\nSubject: hi\n\nNothing here").tenders == []
    with pytest.raises(ValidationFailed):
        email_alerts.parse_alert_email(b"   ")
    with pytest.raises(ValidationFailed):
        email_alerts.parse_alert_email(b"x" * (email_alerts.MAX_EMAIL_BYTES + 1))


def test_html_script_content_is_dropped():
    raw = (b"From: x@gem.gov.in\nSubject: s\nContent-Type: text/html\n\n<script>GEM/2026/B/1111111</script>"
           b"<p>Bid No: GEM/2026/B/2222222</p><p>Items: Cable drums</p>")
    a = email_alerts.parse_alert_email(raw)
    assert [t.reference_number for t in a.tenders] == ["GEM/2026/B/2222222"]
