"""Evidence-grounded draft generation and file exports (DOCX / XLSX / PDF / ZIP).

Every factual statement in a generated draft is taken from authorised company records or the
latest eligibility assessment and carries a citation. Anything not on record is emitted as an
explicit [TO BE COMPLETED: …] placeholder — never invented.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.permissions import Perm
from app.core.errors import ValidationFailed
from app.db.models import (
    BidArtifact,
    BidDraft,
    BidWorkspace,
    CompanyProfile,
    DecisionStatus,
    DocumentScope,
    Organization,
    Tender,
)
from app.extraction.normalization.values import IST, format_inr
from app.services import audit
from app.services.bids.service import get_workspace
from app.services.documents import service as documents
from app.services.eligibility import service as elig
from app.services.evidence.gather import load_company_evidence

PH = "[TO BE COMPLETED: {}]"
STATUS_TEXT = {
    DecisionStatus.VERIFIED_SATISFIED: "Complied — supported by attached evidence",
    DecisionStatus.VERIFIED_NOT_SATISFIED: "NOT MET on current records",
    DecisionStatus.INSUFFICIENT_EVIDENCE: "Evidence pending",
    DecisionStatus.CONFLICTING_EVIDENCE: "Evidence conflict — under internal review",
    DecisionStatus.NEEDS_HUMAN_REVIEW: "Subject to internal review",
    DecisionStatus.NOT_APPLICABLE: "Not applicable",
}


def _cite(label: str | None, page: int | None) -> str:
    return f"[Source: {label or 'record'}{f', p.{page}' if page else ''}]"


def _context(db: Session, ctx: TenantContext, ws: BidWorkspace) -> dict[str, Any]:
    tender = db.get(Tender, ws.tender_id)
    prof = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == ctx.organization_id))
    org = db.get(Organization, ctx.organization_id)
    a = elig.latest_assessment(db, ctx, ws.tender_id)
    rep = elig.report(db, ctx, a.id) if a else None
    ev, snap = load_company_evidence(db, ctx.organization_id)
    return {"tender": tender, "profile": prof, "org": org, "assessment": a, "report": rep, "evidence": ev, "snapshot": snap}


def _technical_sections(c: dict[str, Any]) -> list[dict[str, Any]]:
    t, p, ev, rep = c["tender"], c["profile"], c["evidence"], c["report"]
    secs: list[dict[str, Any]] = []
    placeholders: list[str] = []

    def val(v, what):
        if v in (None, "", []):
            placeholders.append(what)
            return PH.format(what)
        return str(v)

    secs.append({"heading": "1. Bidder Information", "body": "\n".join([
        f"Legal name: {val(p.legal_name if p else None, 'legal name')}",
        f"GSTIN: {val(p.gstin if p else None, 'GSTIN')}",
        f"PAN: {val(p.pan if p else None, 'PAN')}",
        f"CIN: {val(p.cin if p else None, 'CIN')}",
        f"Udyam registration: {val(p.udyam_number if p else None, 'Udyam number (if applicable)')}",
        f"Registered address: {val(p.address if p else None, 'registered address')}",
        f"Authorised signatory: {PH.format('name and designation of authorised signatory')}",
    ]), "citations": [], "placeholders": list(placeholders)})
    scope = t.description or t.title
    secs.append({"heading": "2. Understanding of Scope", "body":
                 f"We have examined tender {t.reference_number or ''} — “{t.title}” — issued by {t.procuring_organization or PH.format('procuring entity')}, "
                 f"including all annexures and corrigenda up to version {t.current_version_no}. Scope as stated in the tender: {scope}.\n"
                 f"{PH.format('bidder’s methodology, delivery schedule and quality plan (to be written by the bid team)')}",
                 "citations": [], "placeholders": ["methodology"]})
    lines, cites = [], []
    for it in (rep["items"] if rep else []):
        d, r = it["decision"], it["requirement"]
        support = [lk for lk in it["links"] if lk.relation == "supports" and lk.origin == "rule"]
        src = "; ".join(_cite(lk.label, lk.page_no) for lk in support[:3])
        lines.append(f"{r.code} — {r.title}: {STATUS_TEXT.get(d.status, d.status)}. {src}")
        cites += [{"label": lk.label, "page": lk.page_no, "document_version_id": str(lk.document_version_id)} for lk in support[:3]]
    secs.append({"heading": "3. Compliance Statement", "body": "\n".join(lines) if lines else PH.format("run an eligibility assessment first"),
                 "citations": cites, "placeholders": [] if lines else ["compliance statement"]})
    proj_lines, proj_cites = [], []
    for f in ev.projects:
        v = f.values
        proj_lines.append(
            f"• {v.get('title')} — client: {v.get('client_name') or PH.format('client')}; value: {format_inr(v['contract_value']) if v.get('contract_value') else PH.format('contract value')}; "
            f"completed: {v['completion_date']:%b %Y}" if v.get("completion_date") else f"• {v.get('title')} — completion date {PH.format('completion date')}")
        proj_lines[-1] += " " + _cite(f.label, f.page)
        proj_cites.append({"label": f.label, "page": f.page, "document_version_id": str(f.version_id)})
    secs.append({"heading": "4. Relevant Experience", "body": "\n".join(proj_lines) or PH.format("upload work completion certificates"), "citations": proj_cites, "placeholders": []})
    ref = c["assessment"].reference_date.date() if c["assessment"] else datetime.now(IST).date()
    cert_lines = []
    for f in ev.certifications:
        vu = f.values.get("valid_until")
        if vu and vu >= ref:
            cert_lines.append(f"• {f.values['cert_type']} — certificate {f.values.get('certificate_number') or '—'}, valid until {vu:%d %b %Y} (as stated on the certificate) {_cite(f.label, f.page)}")
    secs.append({"heading": "5. Certifications", "body": "\n".join(cert_lines) or PH.format("valid certifications — none valid on file for the reference date"), "citations": [], "placeholders": []})
    fin_lines = []
    by_year: dict[str, list] = {}
    for f in ev.financials:
        by_year.setdefault(f.values["fiscal_year"], []).append(f)
    for fy in sorted(by_year):
        facts = by_year[fy]
        amounts = {str(f.values["amount"]) for f in facts}
        if len(amounts) > 1:
            fin_lines.append(f"• FY {fy}: {PH.format('turnover — sources disagree; confirm the authoritative figure')}")
        else:
            fin_lines.append(f"• FY {fy}: turnover {format_inr(facts[0].values['amount'])} {_cite(facts[0].label, facts[0].page)}")
    secs.append({"heading": "6. Financial Capability", "body": "\n".join(fin_lines) or PH.format("audited turnover figures"), "citations": [], "placeholders": []})
    prods = [f"• {f.values['name']} — model {f.values.get('model_number') or '—'} {_cite(f.label, f.page)}" for f in ev.products]
    secs.append({"heading": "7. Products Offered", "body": "\n".join(prods) or PH.format("offered products and models"), "citations": [], "placeholders": []})
    secs.append({"heading": "8. Declarations", "body": "\n".join([
        PH.format("declaration of not being blacklisted/debarred, signed on letterhead"),
        PH.format("signature, seal and date of authorised signatory"),
        "Note: this document was prepared with TenderPilot AI from the bidder's own records. It is not legal advice and must be reviewed before submission.",
    ]), "citations": [], "placeholders": ["declarations", "signature"]})
    return secs


def _capability_sections(c: dict[str, Any]) -> list[dict[str, Any]]:
    p, ev = c["profile"], c["evidence"]
    return [
        {"heading": "Company Overview", "body": f"{p.legal_name if p else PH.format('legal name')} — {p.industry or PH.format('industry')}. "
                                              f"Established: {p.year_established or PH.format('year established')}. Operating in: {', '.join(p.operational_states or []) or PH.format('states')}.",
         "citations": [], "placeholders": []},
        {"heading": "Capabilities", "body": "\n".join(f"• {x}" for x in (p.capabilities or [])) or PH.format("capabilities"), "citations": [], "placeholders": []},
        {"heading": "Track Record", "body": f"{len(ev.projects)} completed project(s) documented; {len(ev.certifications)} certification record(s) on file.", "citations": [], "placeholders": []},
    ]


def generate_drafts(db: Session, ctx: TenantContext, workspace_id: uuid.UUID) -> list[BidDraft]:
    ctx.require(Perm.BIDS_WRITE)
    ws = get_workspace(db, ctx, workspace_id)
    if ws.status in ("internal_review", "approved", "ready_for_external_submission"):
        raise ValidationFailed("Drafts cannot be regenerated while the bid is under review or approved.")
    c = _context(db, ctx, ws)
    out = []
    for kind, title, sections in (("technical_response", "Technical Bid Response", _technical_sections(c)),
                                  ("capability_summary", "Company Capability Summary", _capability_sections(c))):
        prev = db.scalar(select(BidDraft).where(BidDraft.workspace_id == ws.id, BidDraft.kind == kind, BidDraft.is_current.is_(True)))
        if prev:
            prev.is_current = False
        d = BidDraft(organization_id=ctx.organization_id, workspace_id=ws.id, kind=kind, title=title, version_no=(prev.version_no + 1) if prev else 1,
                     sections=sections, generator="template",
                     evidence_snapshot={**c["snapshot"], "assessment_id": str(c["assessment"].id) if c["assessment"] else None,
                                        "generated_at": datetime.now(IST).isoformat()}, created_by_id=ctx.user_id)
        db.add(d)
        out.append(d)
    db.flush()
    audit.record(db, "bid.drafts_generated", ctx=ctx, subject_type="bid_workspace", subject_id=ws.id, data={"drafts": [str(d.id) for d in out]})
    return out


# ---------------- file renderers ----------------

def render_docx(draft: BidDraft, tender: Tender, org: Organization) -> bytes:
    import docx
    from docx.shared import Pt, RGBColor

    doc = docx.Document()
    style = doc.styles["Normal"]
    style.font.name, style.font.size = "Calibri", Pt(10.5)
    brand = (org.settings or {}).get("letterhead_name") or org.name
    hdr = doc.sections[0].header.paragraphs[0]
    hdr.text = f"{brand} — {draft.title} (v{draft.version_no})"
    doc.add_heading(draft.title, 0)
    doc.add_paragraph(f"Tender: {tender.title}\nReference: {tender.reference_number or '—'}\nStatus: {draft.status.replace('_', ' ').upper()}")
    for s in draft.sections:
        doc.add_heading(s["heading"], level=1)
        for line in s["body"].split("\n"):
            para = doc.add_paragraph()
            for part in re.split(r"(\[TO BE COMPLETED:[^\]]*\]|\[Source:[^\]]*\])", line):
                if not part:
                    continue
                run = para.add_run(part)
                if part.startswith("[TO BE COMPLETED"):
                    run.bold, run.font.color.rgb = True, RGBColor(0xC0, 0x39, 0x2B)
                elif part.startswith("[Source:"):
                    run.italic, run.font.size, run.font.color.rgb = True, Pt(8.5), RGBColor(0x5B, 0x6B, 0x7F)
    if draft.status not in ("approved", "ready_for_external_submission"):
        doc.add_paragraph().add_run("DRAFT — not approved for submission").bold = True
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def render_compliance_xlsx(rep: dict[str, Any], tender: Tender) -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Compliance Matrix"
    ws.append([f"Compliance matrix — {tender.title}"])
    ws.append([f"Reference: {tender.reference_number or '—'}  |  Assessment run #{rep['assessment'].run_no}  |  Generated {datetime.now(IST):%d %b %Y %H:%M} IST"])
    ws.append([])
    headers = ["Code", "Type", "Requirement", "Mandatory", "Tender clause (verbatim)", "Tender page", "Status", "Machine status", "Explanation", "Evidence", "Gaps", "Reviewer note"]
    ws.append(headers)
    for c in ws[4]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F2A44")
    fills = {"verified_satisfied": "D5F5E3", "verified_not_satisfied": "FADBD8", "insufficient_evidence": "FCF3CF", "conflicting_evidence": "F5CBA7", "needs_human_review": "D6EAF8"}
    for it in rep["items"]:
        d, r = it["decision"], it["requirement"]
        ev = "; ".join(f"{lk.label} p.{lk.page_no or '-'} ({lk.relation})" for lk in it["links"] if lk.origin == "rule")
        ws.append([r.code, r.req_type, r.title, r.mandatory, r.source_text, r.source_page, d.status, d.machine_status, d.explanation, ev, "\n".join(d.gaps), d.reviewer_note or ""])
        if d.status in fills:
            ws.cell(ws.max_row, 7).fill = PatternFill("solid", fgColor=fills[d.status])
    for col, w in zip("ABCDEFGHIJKL", (8, 14, 40, 12, 60, 8, 22, 22, 60, 45, 40, 30), strict=True):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=5):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A5"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_checklist_xlsx(rep: dict[str, Any], tender: Tender, doc_rows: list[tuple]) -> bytes:
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Document Checklist"
    ws.append(["Supporting-document checklist", tender.title])
    ws.append(["Requirement", "Required document", "Status", "File in package"])
    for c in ws[2]:
        c.font = Font(bold=True)
    for it in rep["items"]:
        d, r = it["decision"], it["requirement"]
        files = sorted({lk.label for lk in it["links"] if lk.relation in ("supports", "partial") and lk.origin == "rule" and lk.label})
        ws.append([r.code, r.title, "Available" if d.status == DecisionStatus.VERIFIED_SATISFIED else ("Missing" if d.status == DecisionStatus.INSUFFICIENT_EVIDENCE else "Review"),
                   ", ".join(files) or "—"])
    ws.column_dimensions["B"].width, ws.column_dimensions["D"].width = 60, 60
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_pdf_report(title: str, rep: dict[str, Any], tender: Tender, *, risk: bool = False) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=title, leftMargin=36, rightMargin=36, topMargin=40, bottomMargin=40)
    a = rep["assessment"]
    story = [Paragraph(title, styles["Title"]), Paragraph(f"{tender.title} — {tender.reference_number or ''}", styles["Normal"]),
             Paragraph(f"Assessment run #{a.run_no} · overall: <b>{(a.overall_status or '').replace('_', ' ')}</b> · readiness {a.readiness_score}/100 "
                       f"(explanatory; mandatory conditions govern)", styles["Normal"]), Spacer(1, 10)]
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    if risk:
        rows = [["Severity", "Kind", "Detail"]] + [[r["severity"], r["kind"].replace("_", " "), Paragraph(esc(r["text"])[:600], styles["BodyText"])]
                                                     for r in (a.summary or {}).get("risks", [])]
        widths = [60, 110, 350]
    else:
        rows = [["Req.", "Status", "Finding & evidence"]]
        for it in rep["items"]:
            d, r = it["decision"], it["requirement"]
            evs = "<br/>".join(f"• {esc(lk.label)} p.{lk.page_no or '-'} ({lk.relation}): <i>{esc((lk.excerpt or '')[:160])}</i>" for lk in it["links"][:4])
            rows.append([r.code, d.status.replace("_", " "), Paragraph(f"<b>{esc(r.title)}</b><br/>Clause (p.{r.source_page}): <i>{esc(r.source_text[:300])}</i><br/>{esc(d.explanation)}<br/>{evs}", styles["BodyText"])])
        widths = [40, 90, 390]
    tbl = Table(rows, colWidths=widths, repeatRows=1)
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2A44")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                             ("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C8D0DC")),
                             ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    story += [tbl, Spacer(1, 12), Paragraph("Generated by TenderPilot AI. Document-stated facts are not externally verified; this is not legal advice.", styles["Italic"])]
    doc.build(story)
    return buf.getvalue()


def _store(db: Session, ctx: TenantContext, ws: BidWorkspace, kind: str, fmt: str, data: bytes, filename: str, *, draft: BidDraft | None,
           assessment_id: uuid.UUID | None, snapshot: dict) -> BidArtifact:
    res = documents.upload(db, ctx, data, filename, scope=DocumentScope.GENERATED, title=filename.rsplit(".", 1)[0].replace("_", " "),
                           category="generated", source="generated", process=False, expand_archives=False)[0]
    art = BidArtifact(organization_id=ctx.organization_id, workspace_id=ws.id, kind=kind, format=fmt, document_id=res.document.id,
                      draft_id=draft.id if draft else None, assessment_id=assessment_id, evidence_snapshot=snapshot, created_by_id=ctx.user_id)
    db.add(art)
    db.flush()
    return art


def export_package(db: Session, ctx: TenantContext, workspace_id: uuid.UUID) -> list[BidArtifact]:
    """Render every artifact, then bundle them with the cited supporting documents into a ZIP."""
    ctx.require(Perm.BIDS_WRITE)
    ws = get_workspace(db, ctx, workspace_id)
    c = _context(db, ctx, ws)
    if not c["report"]:
        raise ValidationFailed("Run an eligibility assessment before exporting.")
    tender, org, rep = c["tender"], c["org"], c["report"]
    from app.services.bids.service import drafts as list_drafts

    current = {d.kind: d for d in list_drafts(db, ctx, ws.id)}
    if "technical_response" not in current:
        raise ValidationFailed("Generate drafts before exporting.")
    stamp = datetime.now(IST).strftime("%Y%m%d-%H%M")
    snap = {**c["snapshot"], "assessment_id": str(rep["assessment"].id)}
    aid = rep["assessment"].id
    files: list[tuple[str, bytes, BidArtifact]] = []
    for kind, fmt, name, data, draft in [
        ("technical_response", "docx", f"technical_bid_response_v{current['technical_response'].version_no}.docx", render_docx(current["technical_response"], tender, org), current["technical_response"]),
        ("capability_summary", "docx", "company_capability_summary.docx", render_docx(current["capability_summary"], tender, org) if "capability_summary" in current else b"", current.get("capability_summary")),
        ("compliance_matrix", "xlsx", "eligibility_compliance_matrix.xlsx", render_compliance_xlsx(rep, tender), None),
        ("document_checklist", "xlsx", "supporting_document_checklist.xlsx", render_checklist_xlsx(rep, tender, []), None),
        ("evidence_report", "pdf", "requirement_to_evidence_report.pdf", render_pdf_report("Requirement-to-Evidence Report", rep, tender), None),
        ("risk_report", "pdf", "internal_risk_report.pdf", render_pdf_report("Internal Risk Report (confidential)", rep, tender, risk=True), None),
    ]:
        if not data:
            continue
        files.append((name, data, _store(db, ctx, ws, kind, fmt, data, f"{stamp}_{name}", draft=draft, assessment_id=aid, snapshot=snap)))
    # Supporting documents actually cited as supporting evidence (exact versions used).
    supporting: dict[uuid.UUID, str] = {}
    for it in rep["items"]:
        for lk in it["links"]:
            if lk.origin == "rule" and lk.relation in ("supports", "partial") and lk.document_version_id:
                supporting[lk.document_version_id] = lk.label or "document"
    manifest = {"tender": {"title": tender.title, "reference": tender.reference_number, "version": tender.current_version_no},
                "bid_status": ws.status, "generated_at": datetime.now(IST).isoformat(), "assessment_id": str(aid),
                "notice": "Prepared for EXTERNAL submission by the bidder through the official portal. TenderPilot AI does not submit bids.",
                "files": []}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data, _ in files:
            zf.writestr(f"01_generated/{name}", data)
            manifest["files"].append({"path": f"01_generated/{name}", "sha256": hashlib.sha256(data).hexdigest()})
        for vid, label in supporting.items():
            data, ver = documents.read_bytes(db, ctx, vid)
            path = f"02_supporting/{ver.original_filename}"
            zf.writestr(path, data)
            manifest["files"].append({"path": path, "sha256": ver.sha256, "document_version_id": str(vid), "label": label})
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    pkg = _store(db, ctx, ws, "submission_package", "zip", buf.getvalue(), f"{stamp}_submission_package.zip", draft=None, assessment_id=aid, snapshot=snap)
    audit.record(db, "bid.package_exported", ctx=ctx, subject_type="bid_workspace", subject_id=ws.id,
                 data={"artifacts": [str(a.id) for _, _, a in files] + [str(pkg.id)], "bid_status": ws.status})
    return [a for _, _, a in files] + [pkg]
