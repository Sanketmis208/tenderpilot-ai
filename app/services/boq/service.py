"""Bill of Quantities analysis.

The procuring entity's workbook is preserved byte-for-byte as the original document version.
Internal estimates (scenarios) are stored separately. Exports write rates only into cells the
template leaves editable and never replace formulas; the export is labelled as an internal copy
that must still be validated against the portal's submission rules.
"""

from __future__ import annotations

import io
import re
import uuid
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import openpyxl
from openpyxl.utils import get_column_letter
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.permissions import Perm
from app.core.errors import NotFound, ValidationFailed
from app.db.models import BoqImport, BoqItem, BoqScenario, Document, DocumentScope, DocumentVersion, TenderDocument
from app.services import audit
from app.services.documents import service as documents
from app.services.tenders.service import get_tender

HEADER_PATTERNS = {
    "item_code": r"^(sl\.?\s*no|s\.?\s*no|item\s*(no|code)|sr\.?\s*no)",
    "description": r"(description|particulars|item of work|name of item)",
    "unit": r"^(unit|uom|units)\b",
    "quantity": r"^(qty|quantity|quantities)",
    "rate": r"(rate|unit price|price per)",
    "amount": r"(amount|total price|value)",
}
Q2 = Decimal("0.01")


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _find_header(ws) -> tuple[int, dict[str, int]] | None:
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30)):
        found: dict[str, int] = {}
        for cell in row:
            if not isinstance(cell.value, str):
                continue
            text = cell.value.strip().lower()
            for key, pat in HEADER_PATTERNS.items():
                if key not in found and re.search(pat, text):
                    found[key] = cell.column
                    break
        if {"description", "quantity"} <= set(found) and ("rate" in found or "amount" in found):
            return row[0].row, found
    return None


def analyse_workbook(data: bytes) -> dict[str, Any]:
    wb_f = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    candidates = []
    for ws in wb_f.worksheets:
        hdr = _find_header(ws)
        if hdr:
            candidates.append((ws, hdr))
    if not candidates:
        raise ValidationFailed("No BOQ sheet found (expected columns like Description, Unit, Quantity, Rate/Amount).")
    ws, (header_row, cols) = max(candidates, key=lambda c: c[0].max_row)
    protected = bool(ws.protection.sheet)
    items, issues = [], []
    seen: dict[str, list[int]] = defaultdict(list)
    units_by_desc: dict[str, set[str]] = defaultdict(set)
    for r in range(header_row + 1, ws.max_row + 1):
        get = lambda k: ws.cell(r, cols[k]).value if k in cols else None  # noqa: E731
        desc = get("description")
        if desc is None or str(desc).strip() == "":
            continue
        desc = str(desc).strip()
        unit = (str(get("unit")).strip() if get("unit") not in (None, "") else None)
        qty_raw = get("quantity")
        rate_cell = ws.cell(r, cols["rate"]) if "rate" in cols else None
        amt_cell = ws.cell(r, cols["amount"]) if "amount" in cols else None
        amount_formula = amt_cell.value if amt_cell is not None and isinstance(amt_cell.value, str) and amt_cell.value.startswith("=") else None
        low = desc.lower()
        is_subtotal = bool(re.search(r"\b(sub-?total|grand total|total)\b", low)) and not unit
        is_tax = bool(re.search(r"\bgst\b|\btax\b|\bcess\b", low)) and not unit
        is_heading = not unit and qty_raw in (None, "") and not is_subtotal and not is_tax
        item_issues: list[str] = []
        qty = _dec(qty_raw) if not (isinstance(qty_raw, str) and qty_raw.startswith("=")) else None
        if not (is_heading or is_subtotal or is_tax):
            if qty is None:
                item_issues.append("missing_quantity")
            if not unit:
                item_issues.append("missing_unit")
            if rate_cell is not None and rate_cell.value in (None, ""):
                item_issues.append("missing_rate")
            norm = re.sub(r"\s+", " ", low)
            seen[norm].append(r)
            if unit:
                units_by_desc[norm].add(unit.lower())
        locked = bool(protected and rate_cell is not None and rate_cell.protection.locked)
        items.append({
            "row_no": r, "item_code": str(get("item_code")).strip() if get("item_code") not in (None, "") else None, "description": desc,
            "unit": unit, "quantity": qty, "quoted_rate": _dec(rate_cell.value) if rate_cell is not None and not str(rate_cell.value or "").startswith("=") else None,
            "rate_cell": f"{get_column_letter(cols['rate'])}{r}" if "rate" in cols else None,
            "amount_cell": f"{get_column_letter(cols['amount'])}{r}" if "amount" in cols else None,
            "amount_formula": amount_formula, "rate_cell_locked": locked, "is_heading": is_heading, "is_subtotal": is_subtotal or is_tax, "issues": item_issues,
        })
    for norm, rows in seen.items():
        if len(rows) > 1:
            units = units_by_desc[norm]
            kind = "unit_mismatch" if len(units) > 1 else "duplicate_line_item"
            for it in items:
                if it["row_no"] in rows:
                    it["issues"].append(kind)
            issues.append({"type": kind, "rows": rows, "description": norm[:120], "units": sorted(units)})
    for it in items:
        for i in it["issues"]:
            if i in ("missing_quantity", "missing_unit", "missing_rate"):
                issues.append({"type": i, "rows": [it["row_no"]], "description": it["description"][:120]})
    line_items = [i for i in items if not (i["is_heading"] or i["is_subtotal"])]
    summary = {
        "line_items": len(line_items), "headings": sum(i["is_heading"] for i in items), "totals_rows": sum(i["is_subtotal"] for i in items),
        "formula_cells": sum(1 for i in items if i["amount_formula"]), "sheet_protected": protected,
        "editable_rate_cells": sum(1 for i in line_items if i["rate_cell"] and not i["rate_cell_locked"]),
        "missing_rates": sum("missing_rate" in i["issues"] for i in line_items), "issue_count": len(issues),
    }
    return {"sheet": ws.title, "header_row": header_row, "columns": {k: get_column_letter(v) for k, v in cols.items()}, "items": items, "issues": issues, "summary": summary}


def import_boq(db: Session, ctx: TenantContext, tender_id: uuid.UUID, document_version_id: uuid.UUID | None = None) -> BoqImport:
    ctx.require(Perm.BIDS_WRITE)
    tender = get_tender(db, ctx, tender_id)
    if document_version_id is None:
        document_version_id = db.scalar(
            select(Document.current_version_id).join(TenderDocument, TenderDocument.document_id == Document.id)
            .where(TenderDocument.tender_id == tender.id, TenderDocument.role == "boq", Document.deleted_at.is_(None))
            .order_by(TenderDocument.created_at.desc())
        )
        if not document_version_id:
            raise ValidationFailed("No BOQ workbook attached to this tender.")
    data, ver = documents.read_bytes(db, ctx, document_version_id)
    if not ver.content_type.endswith("spreadsheetml.sheet"):
        raise ValidationFailed("BOQ analysis requires an XLSX workbook.")
    result = analyse_workbook(data)
    imp = db.scalar(select(BoqImport).where(BoqImport.document_version_id == ver.id, BoqImport.organization_id == ctx.organization_id))
    if imp:
        db.execute(delete(BoqItem).where(BoqItem.boq_import_id == imp.id))
    else:
        from app.db.models import BidWorkspace

        ws = db.scalar(select(BidWorkspace).where(BidWorkspace.tender_id == tender.id))
        imp = BoqImport(organization_id=ctx.organization_id, tender_id=tender.id, workspace_id=ws.id if ws else None, document_version_id=ver.id,
                        sheet_name=result["sheet"], header_row=result["header_row"])
        db.add(imp)
    imp.column_map, imp.issues, imp.summary = result["columns"], result["issues"], result["summary"]
    imp.item_count = result["summary"]["line_items"]
    db.flush()
    for it in result["items"]:
        db.add(BoqItem(organization_id=ctx.organization_id, boq_import_id=imp.id, **it))
    audit.record(db, "boq.imported", ctx=ctx, subject_type="boq_import", subject_id=imp.id, data=result["summary"])
    return imp


def get_import(db: Session, ctx: TenantContext, import_id: uuid.UUID) -> BoqImport:
    ctx.require(Perm.BIDS_READ)
    imp = db.scalar(select(BoqImport).where(BoqImport.id == import_id, BoqImport.organization_id == ctx.organization_id))
    if not imp:
        raise NotFound("BOQ import not found.")
    return imp


def items(db: Session, ctx: TenantContext, import_id: uuid.UUID) -> list[BoqItem]:
    get_import(db, ctx, import_id)
    return list(db.scalars(select(BoqItem).where(BoqItem.boq_import_id == import_id).order_by(BoqItem.row_no)))


def compute_totals(line_items: list[BoqItem], rates: dict[str, Decimal], markup_pct: Decimal, gst_pct: Decimal) -> dict[str, Any]:
    subtotal = Decimal(0)
    priced = unpriced = 0
    lines = {}
    for it in line_items:
        if it.is_heading or it.is_subtotal:
            continue
        rate = rates.get(str(it.id), it.quoted_rate)
        if rate is None or it.quantity is None:
            unpriced += 1
            continue
        amt = (it.quantity * rate * (1 + markup_pct / 100)).quantize(Q2, rounding=ROUND_HALF_UP)
        lines[str(it.id)] = str(amt)
        subtotal += amt
        priced += 1
    gst = (subtotal * gst_pct / 100).quantize(Q2, rounding=ROUND_HALF_UP)
    return {"subtotal": str(subtotal), "gst": str(gst), "grand_total": str(subtotal + gst), "priced_items": priced, "unpriced_items": unpriced,
            "markup_pct": str(markup_pct), "gst_pct": str(gst_pct), "lines": lines, "complete": unpriced == 0}


def save_scenario(db: Session, ctx: TenantContext, import_id: uuid.UUID, name: str, rates: dict[str, str], markup_pct: str = "0", gst_pct: str = "18",
                  scenario_id: uuid.UUID | None = None) -> BoqScenario:
    ctx.require(Perm.BIDS_WRITE)
    its = items(db, ctx, import_id)
    valid_ids = {str(i.id) for i in its}
    parsed: dict[str, Decimal] = {}
    for k, v in rates.items():
        if k not in valid_ids:
            raise ValidationFailed("Rate refers to an unknown BOQ item.")
        if v in (None, ""):
            continue
        d = _dec(v)
        if d is None or d < 0:
            raise ValidationFailed(f"Invalid rate '{v}'.")
        parsed[k] = d
    mk, gst = _dec(markup_pct) or Decimal(0), _dec(gst_pct) if _dec(gst_pct) is not None else Decimal(18)
    totals = compute_totals(its, parsed, mk, gst)
    sc = db.scalar(select(BoqScenario).where(BoqScenario.id == scenario_id, BoqScenario.organization_id == ctx.organization_id)) if scenario_id else None
    if sc is None:
        sc = BoqScenario(organization_id=ctx.organization_id, boq_import_id=import_id, name=name[:120] or "Scenario", created_by_id=ctx.user_id)
        db.add(sc)
    sc.rates, sc.markup_pct, sc.gst_pct, sc.totals = {k: str(v) for k, v in parsed.items()}, mk, gst, totals
    db.flush()
    audit.record(db, "boq.scenario_saved", ctx=ctx, subject_type="boq_scenario", subject_id=sc.id, data={"grand_total": totals["grand_total"]})
    return sc


def scenarios(db: Session, ctx: TenantContext, import_id: uuid.UUID) -> list[BoqScenario]:
    get_import(db, ctx, import_id)
    return list(db.scalars(select(BoqScenario).where(BoqScenario.boq_import_id == import_id).order_by(BoqScenario.created_at)))


def export_priced_copy(db: Session, ctx: TenantContext, scenario_id: uuid.UUID, *, allow_locked: bool = False) -> DocumentVersion:
    """Write scenario rates into a COPY of the original workbook. Formulas and locked cells untouched."""
    ctx.require(Perm.BIDS_WRITE)
    sc = db.scalar(select(BoqScenario).where(BoqScenario.id == scenario_id, BoqScenario.organization_id == ctx.organization_id))
    if not sc:
        raise NotFound("Scenario not found.")
    imp = get_import(db, ctx, sc.boq_import_id)
    data, ver = documents.read_bytes(db, ctx, imp.document_version_id)
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    ws = wb[imp.sheet_name]
    skipped = []
    by_id = {str(i.id): i for i in items(db, ctx, imp.id)}
    for item_id, rate in sc.rates.items():
        it = by_id.get(item_id)
        if not it or not it.rate_cell:
            continue
        cell = ws[it.rate_cell]
        if isinstance(cell.value, str) and cell.value.startswith("="):
            skipped.append(f"{it.rate_cell} (formula)")
            continue
        if it.rate_cell_locked and not allow_locked:
            skipped.append(f"{it.rate_cell} (locked by template)")
            continue
        cell.value = float(Decimal(rate))  # Excel stores numbers as doubles; source of truth stays Decimal in DB
    note_ws = wb.create_sheet("TenderPilot_Notes")
    note_ws["A1"] = "Internal priced copy generated by TenderPilot AI."
    note_ws["A2"] = "Validate against the tender's prescribed format and portal rules before any submission."
    note_ws["A3"] = f"Scenario: {sc.name} | markup {sc.markup_pct}% | GST {sc.gst_pct}% | internal grand total ₹{sc.totals.get('grand_total')}"
    note_ws["A4"] = f"Cells skipped: {', '.join(skipped) or 'none'}"
    buf = io.BytesIO()
    wb.save(buf)
    res = documents.upload(db, ctx, buf.getvalue(), f"BOQ_priced_copy_{sc.name}.xlsx", scope=DocumentScope.GENERATED, category="generated",
                           source="generated", process=False)[0]
    audit.record(db, "boq.exported", ctx=ctx, subject_type="boq_scenario", subject_id=sc.id, data={"skipped": skipped})
    return res.version
