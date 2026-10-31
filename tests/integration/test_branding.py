"""Organization branding and standard template sections in generated documents."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("seeded")


def test_branding_validation_and_permissions(seeded, db):
    from app.core.errors import PermissionDenied, ValidationFailed
    from app.services.organizations import org_service

    with pytest.raises(ValidationFailed):
        org_service.update_document_template(db, seeded.contexts["owner@kaveri.example"], branding_in={"primary_color": "red;"}, standard_sections=[])
    with pytest.raises(PermissionDenied):
        org_service.update_document_template(db, seeded.contexts["bids@voltedge.example"], branding_in={}, standard_sections=[])
    db.rollback()


def test_branded_docx_xlsx_pdf_and_standard_sections(seeded, db):
    import docx
    import openpyxl

    from app.db.models import BidDraft, Organization, Tender
    from app.services.bids import generation
    from app.services.bids import service as bids
    from app.services.organizations import org_service

    ctx = seeded.contexts["owner@kaveri.example"]
    org = org_service.update_document_template(
        db, ctx, branding_in={"letterhead_name": "Kaveri Infra (synthetic)", "address_line": "Bengaluru", "footer_text": "CONFIDENTIAL — Kaveri",
                              "primary_color": "#0A7E5C"},
        standard_sections=[{"heading": "Quality Policy", "body": "Our quality policy text, approved by the board."}, {"heading": "", "body": "ignored"}])
    db.flush()
    assert org.settings["template"]["standard_sections"] == [{"heading": "Quality Policy", "body": "Our quality policy text, approved by the board."}]

    ws = bids.create_workspace(db, ctx, seeded.tenders["kaveri_wtp"].id)
    secs = generation._technical_sections(generation._context(db, ctx, ws))
    std = next(s for s in secs if s.get("origin") == "org_template")
    assert std["heading"].endswith("Quality Policy") and std["body"].startswith("Our quality policy")

    tender = db.get(Tender, ws.tender_id)
    draft = BidDraft(kind="technical_response", title="Technical Bid Response", version_no=1, sections=secs, status="draft")
    d = docx.Document(io.BytesIO(generation.render_docx(draft, tender, db.get(Organization, ctx.organization_id))))
    header = d.sections[0].header.paragraphs[0]
    assert header.runs[0].text == "Kaveri Infra (synthetic)" and str(header.runs[0].font.color.rgb) == "0A7E5C"
    assert "CONFIDENTIAL" in d.sections[0].footer.paragraphs[0].text
    assert any(p.text.endswith("Quality Policy") for p in d.paragraphs)

    rep = {"assessment": SimpleNamespace(run_no=1, overall_status="needs_review", readiness_score=50, summary={}), "items": []}
    wb = openpyxl.load_workbook(io.BytesIO(generation.render_compliance_xlsx(rep, tender, org)))
    sheet = wb.active
    assert sheet["A1"].value.startswith("Kaveri Infra") and sheet["A4"].fill.fgColor.rgb.endswith("0A7E5C")
    pdf = generation.render_pdf_report("Requirement-to-Evidence Report", rep, tender, org=org)
    assert pdf.startswith(b"%PDF")
    db.rollback()
