"""Normalisation, parsing, classification, requirement extraction, chunking."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.ai.retrieval.chunking import chunk_document
from app.extraction.classify import classify
from app.extraction.company_facts import extract_company_facts
from app.extraction.normalization import values as v
from app.extraction.parsers.formats import parse
from app.extraction.tender_rules import extract_fields, extract_requirements

SAMPLE = Path(__file__).resolve().parents[2] / "sample_data"


@pytest.mark.parametrize("text,expected", [
    ("Rs. 5 crore", Decimal("50000000.00")), ("₹ 4.50 Crores", Decimal("45000000.00")), ("INR 85 lakh", Decimal("8500000.00")),
    ("Rs. 5,00,000", Decimal("500000.00")), ("1.5 cr", Decimal("15000000.00")), ("Rupees 12,345.67", Decimal("12345.67")),
])
def test_parse_inr(text, expected):
    assert v.parse_inr(text)[0] == expected


def test_parse_inr_none():
    assert v.parse_inr("no money here") is None


def test_format_inr_indian_grouping():
    assert v.format_inr(Decimal("50000000")) == "₹5,00,00,000.00"
    assert v.format_inr(Decimal("1234.5")) == "₹1,234.50"


def test_dates_are_day_first():
    assert v.parse_date("Valid until: 05/06/2026") == date(2026, 6, 5)
    assert v.parse_date("Issued 3rd March, 2025") == date(2025, 3, 3)


def test_deadline_ist_with_time():
    dt = v.parse_datetime_ist("15/12/2026 15:00 hrs")
    assert dt.isoformat() == "2026-12-15T15:00:00+05:30"


def test_deadline_without_time_is_end_of_day_ist():
    assert v.parse_datetime_ist("15/12/2026").hour == 23


@pytest.mark.parametrize("text,fy", [("FY 2023-24", "2023-24"), ("2023-2024", "2023-24"), ("2023/24", "2023-24"), ("2023-25", None)])
def test_fiscal_year(text, fy):
    assert v.normalise_fiscal_year(text) == fy


def test_previous_fiscal_years():
    assert v.previous_fiscal_years(date(2026, 12, 15), 3) == ["2025-26", "2024-25", "2023-24"]
    assert v.fiscal_year_of(date(2026, 3, 31)) == "2025-26"


def test_language_detection():
    assert v.detect_language("This is English text")[0] == "en"
    assert v.detect_language("यह एक हिंदी वाक्य है जो परीक्षण के लिए")[0] == "hi"


def _pdf(path):
    return parse(path.read_bytes(), "application/pdf")


def test_pdf_parse_keeps_pages_and_bbox():
    doc = _pdf(SAMPLE / "tenders" / "msgic_lt_panels_nit.pdf")
    assert len(doc.pages) == 4
    assert all(b.bbox and len(b.bbox) == 4 for b in doc.pages[1].blocks)


@pytest.mark.parametrize("name,category", [
    ("voltedge/iso_9001_certificate_2023.pdf", "iso_certificate"), ("voltedge/gst_registration_certificate.pdf", "gst_certificate"),
    ("voltedge/udyam_registration.pdf", "msme_registration"), ("voltedge/audited_financial_statements.pdf", "financial_statement"),
    ("voltedge/completion_certificate_msedcl.pdf", "completion_certificate"), ("tenders/msgic_lt_panels_nit.pdf", "tender_notice"),
    ("tenders/msgic_lt_panels_corrigendum_1.pdf", "corrigendum"),
])
def test_classification(name, category):
    doc = _pdf(SAMPLE / name)
    assert classify(doc.full_text, name).category == category


def test_iso_facts_and_expiry():
    doc = _pdf(SAMPLE / "voltedge/iso_9001_certificate_2023.pdf")
    fx = extract_company_facts(doc, "iso_certificate")
    cert = next(f for f in fx.facts if f.kind == "certification")
    assert cert.values["cert_type"] == "ISO 9001"
    assert cert.values["valid_until"] == date(2026, 3, 14)
    assert cert.page == 1 and "14/03/2026" in cert.source_text


def test_turnover_facts():
    fx = extract_company_facts(_pdf(SAMPLE / "voltedge/audited_financial_statements.pdf"), "financial_statement")
    got = {f.values["fiscal_year"]: f.values["amount"] for f in fx.facts if f.kind == "financial"}
    assert got == {"2022-23": Decimal("42000000.00"), "2023-24": Decimal("51000000.00")}


def test_tender_requirements_extracted_with_sources():
    doc = _pdf(SAMPLE / "tenders/msgic_lt_panels_nit.pdf")
    reqs = extract_requirements(doc)
    by = {(r.req_type, r.metric): r for r in reqs}
    t = by[("financial", "average_annual_turnover")]
    assert t.threshold_value == "50000000.00" and t.period_years == 3 and t.source_page == 2
    assert by[("experience", "similar_work_value")].count_required == 2
    assert any(r.req_type == "certification" and r.standard == "ISO 9001" for r in reqs)
    emd = next(r for r in reqs if r.req_type == "emd")
    assert emd.mandatory == "conditional" and emd.conditions, "MSME exemption must stay conditional"
    assert len(reqs) >= 10


def test_prompt_injection_text_is_not_a_requirement():
    doc = _pdf(SAMPLE / "tenders/msgic_lt_panels_nit.pdf")
    assert not any("ignore all previous instructions" in r.source_quote.lower() for r in extract_requirements(doc))


def test_tender_fields():
    f = extract_fields(_pdf(SAMPLE / "tenders/msgic_lt_panels_nit.pdf"))
    assert f.reference_number == "MSGIC/PROC/2026/LT-118"
    assert f.submission_deadline == "2026-12-15T15:00:00+05:30"
    assert f.estimated_value == "37500000.00" and f.emd_amount == "500000.00"
    assert f.citations["submission_deadline"].page == 1


def test_missing_values_are_not_fabricated():
    f = extract_fields(_pdf(SAMPLE / "tenders/kuwwb_wtp_nit.pdf"))
    assert f.emd_amount is None and f.prebid_meeting_at is None


def test_chunking_keeps_sections_and_pages():
    chunks = chunk_document(_pdf(SAMPLE / "tenders/msgic_lt_panels_nit.pdf"))
    turnover = next(c for c in chunks if "average annual turnover" in c.text)
    assert turnover.page_no == 2 and "ELIGIBILITY" in (turnover.section or "")
    assert "Rs. 5 crore" in turnover.text  # financial clause not split


def test_docx_and_xlsx_parsers(tmp_path):
    import docx

    d = docx.Document()
    d.add_heading("Eligibility", 1)
    d.add_paragraph("The bidder shall have a valid ISO 9001 certification.")
    p = tmp_path / "t.docx"
    d.save(p)
    parsed = parse(p.read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert "ISO 9001" in parsed.full_text
    xl = parse((SAMPLE / "tenders/msgic_lt_panels_boq.xlsx").read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert any("Power Control Centre" in pg.text for pg in xl.pages)
