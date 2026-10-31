"""Mandatory end-to-end scenario (spec §34) exercised through the REST API with real persistence.

VoltEdge imports the MSGIC LT-panel tender → extraction → assessment (paused for human review) →
review → bid workspace → drafts → four-eyes approval → export package → replacement certificate →
re-run (history preserved) → corrigendum → cross-tenant isolation.
Steps depend on each other, so they run in order within one module-scoped state.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest

from tests.conftest import login

SAMPLE = Path(__file__).resolve().parents[2] / "sample_data"
S: dict = {}

pytestmark = pytest.mark.usefixtures("seeded")


def _utc(iso: str) -> str:
    from datetime import UTC, datetime

    return datetime.fromisoformat(iso).astimezone(UTC).isoformat()


@pytest.fixture(scope="module")
def bm():
    return login(None, "bids@voltedge.example")


@pytest.fixture(scope="module")
def rev():
    return login(None, "review@voltedge.example")


def test_01_tender_stored_and_extracted(bm, seeded):
    c, h = bm
    tid = str(seeded.tenders["lt_panels"].id)
    S["tid"] = tid
    t = c.get(f"/api/v1/tenders/{tid}").json()
    assert t["reference_number"] == "MSGIC/PROC/2026/LT-118"
    assert t["status"] == "ready_for_review"
    assert _utc(t["submission_deadline"]) == "2026-12-15T09:30:00+00:00"  # 15:00 IST
    reqs = c.get(f"/api/v1/tenders/{tid}/requirements").json()
    assert len(reqs) >= 5
    types = {r["req_type"] for r in reqs}
    assert {"financial", "experience", "certification", "technical", "document"} <= types
    for r in reqs:
        assert r["source_page"] and r["source_text"] and r["source_document_version_id"]
    docs = c.get("/api/v1/documents?scope=tender").json()
    assert any(d["category"] == "boq" for d in docs["items"])


def test_02_assessment_runs_and_pauses_for_review(bm):
    c, h = bm
    r = c.post(f"/api/v1/tenders/{S['tid']}/assessments", headers=h)
    assert r.status_code == 202, r.text
    aid = r.json()["id"]
    S["a1"] = aid
    rep = c.get(f"/api/v1/assessments/{aid}").json()
    a = rep["assessment"]
    assert a["status"] == "awaiting_review"
    assert a["overall_status"] == "not_eligible"
    by_title = {it["requirement"]["title"]: it["decision"] for it in rep["items"]}
    turnover = next(d for t, d in by_title.items() if "turnover" in t.lower())
    iso = next(d for t, d in by_title.items() if "ISO 9001" in t)
    emd = next(d for t, d in by_title.items() if "Earnest" in t)
    assert turnover["status"] == "conflicting_evidence"  # audited 5.10 Cr vs CA 5.80 Cr for FY 2023-24
    assert any("2024-25" in g for g in turnover["gaps"])  # missing financial year
    assert iso["status"] == "verified_not_satisfied" and "expired" in iso["explanation"]
    assert emd["status"] == "needs_human_review"  # conditional MSME exemption never auto-granted
    assert any("ISO 9001" in m for m in rep["missing_documents"])
    iso_item = next(it for it in rep["items"] if "ISO 9001" in it["requirement"]["title"])
    assert iso_item["links"] and iso_item["links"][0]["page_no"] == 1 and iso_item["links"][0]["document_version_id"]
    graph = c.get(f"/api/v1/assessments/{aid}/graph").json()
    assert any(n["kind"] == "requirement" for n in graph["nodes"]) and graph["edges"]


def test_03_prompt_injection_in_tender_has_no_effect(bm):
    c, _ = bm
    rep = c.get(f"/api/v1/assessments/{S['a1']}").json()
    assert not any(it["decision"]["status"] == "verified_satisfied" and "turnover" in it["requirement"]["title"].lower() for it in rep["items"])
    assert all("ignore all previous instructions" not in it["requirement"]["source_text"].lower() for it in rep["items"])


def test_04_reviewer_resolves_and_resumes_workflow(rev, db):
    c, h = rev
    rep = c.get(f"/api/v1/assessments/{S['a1']}").json()
    viewer_c, viewer_h = login(None, "viewer@voltedge.example")
    d0 = rep["items"][0]["decision"]["id"]
    assert viewer_c.post(f"/api/v1/decisions/{d0}/review", json={"action": "accept"}, headers=viewer_h).status_code == 403
    for it in rep["items"]:
        d = it["decision"]
        if d["status"] in ("needs_human_review", "conflicting_evidence"):
            assert c.post(f"/api/v1/decisions/{d['id']}/review", json={"action": "annotate", "note": "Checked against originals"}, headers=h).status_code == 200
            assert c.post(f"/api/v1/decisions/{d['id']}/review", json={"action": "accept"}, headers=h).status_code == 200
        for conf in it["conflicts"]:
            assert c.post(f"/api/v1/conflicts/{conf['id']}/resolve", json={"note": "Audited statement is authoritative"}, headers=h).status_code == 204
    bad = c.post(f"/api/v1/decisions/{rep['items'][0]['decision']['id']}/review", json={"action": "override", "new_status": "verified_satisfied"}, headers=h)
    assert bad.status_code == 422  # override requires justification
    r = c.post(f"/api/v1/assessments/{S['a1']}/complete-review", json={"note": "Reviewed"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    from app.db.models import AgentRun, EligibilityAssessment
    from app.db.tenancy import system_scope

    with system_scope():  # test inspection across the API boundary
        a = db.get(EligibilityAssessment, uuid.UUID(S["a1"]))
        run = db.get(AgentRun, a.agent_run_id)
    assert run.status == "completed"
    assert [h_["node"] for h_ in run.history][-2:] == ["human_review", "remediation"]


def test_05_workspace_with_remediation_tasks(bm):
    c, h = bm
    r = c.post(f"/api/v1/tenders/{S['tid']}/workspace", headers=h)
    assert r.status_code == 201
    S["ws"] = r.json()["id"]
    tasks = c.get(f"/api/v1/workspaces/{S['ws']}/tasks").json()
    titles = " | ".join(t["title"] for t in tasks)
    assert "ISO 9001" in titles and "2024-25" in titles
    again = c.post(f"/api/v1/tenders/{S['tid']}/workspace", headers=h)
    assert again.json()["id"] == S["ws"]
    assert len(c.get(f"/api/v1/workspaces/{S['ws']}/tasks").json()) == len(tasks)  # idempotent remediation
    t = tasks[0]
    ok = c.patch(f"/api/v1/tasks/{t['id']}", json={"changes": {"status": "in_progress"}, "lock_version": t["lock_version"]}, headers=h)
    assert ok.status_code == 200
    stale = c.patch(f"/api/v1/tasks/{t['id']}", json={"changes": {"status": "done"}, "lock_version": t["lock_version"]}, headers=h)
    assert stale.status_code == 409  # optimistic locking


def test_06_drafts_are_grounded(bm):
    c, h = bm
    drafts = c.post(f"/api/v1/workspaces/{S['ws']}/drafts/generate", headers=h).json()
    tech = next(d for d in drafts if d["kind"] == "technical_response")
    body = "\n".join(s["body"] for s in tech["sections"])
    assert "[Source:" in body and "[TO BE COMPLETED:" in body
    assert "27AABCV1234F1Z5" in body  # GSTIN from the uploaded certificate
    assert "QMS/IN/2023/88412" not in body.split("5. Certifications")[-1].split("6.")[0]  # expired cert not claimed as valid
    assert "sources disagree" in body  # conflicting turnover not stated as fact
    S["draft"] = tech


def test_07_four_eyes_approval(bm, rev):
    c, h = bm
    rc, rh = rev
    assert c.post(f"/api/v1/workspaces/{S['ws']}/approval", json={"action": "approve"}, headers=h).status_code == 409
    assert c.post(f"/api/v1/workspaces/{S['ws']}/approval", json={"action": "submit_for_review"}, headers=h).status_code == 200
    edit = c.put(f"/api/v1/drafts/{S['draft']['id']}", json={"sections": S["draft"]["sections"], "lock_version": S["draft"]["lock_version"]}, headers=h)
    assert edit.status_code == 409  # cannot edit while under review
    self_approve = c.post(f"/api/v1/workspaces/{S['ws']}/approval", json={"action": "approve"}, headers=h)
    assert self_approve.status_code == 403  # author cannot approve own draft
    assert rc.post(f"/api/v1/workspaces/{S['ws']}/approval", json={"action": "request_changes"}, headers=rh).status_code == 422  # note required
    assert rc.post(f"/api/v1/workspaces/{S['ws']}/approval", json={"action": "approve", "note": "OK"}, headers=rh).json()["status"] == "approved"
    assert rc.post(f"/api/v1/workspaces/{S['ws']}/approval", json={"action": "mark_ready"}, headers=rh).json()["status"] == "ready_for_external_submission"
    hist = c.get(f"/api/v1/workspaces/{S['ws']}/approvals").json()
    assert list(dict.fromkeys(a["action"] for a in hist)) == ["submit_for_review", "approve", "mark_ready"]  # one row per draft
    assert c.get(f"/api/v1/tenders/{S['tid']}").json()["status"] == "ready_for_external_submission"


def test_08_export_package_files_open(bm, db):
    c, h = bm
    arts = c.post(f"/api/v1/workspaces/{S['ws']}/export", headers=h).json()
    kinds = {a["kind"] for a in arts}
    assert {"technical_response", "compliance_matrix", "document_checklist", "evidence_report", "risk_report", "submission_package"} <= kinds
    from app.db.models import Document
    from app.db.tenancy import system_scope

    files = {}
    for a in arts:
        with system_scope():
            ver = db.get(Document, uuid.UUID(a["document_id"])).current_version_id
        url = c.get(f"/api/v1/document-versions/{ver}/download").json()["url"]
        resp = c.get(url)
        assert resp.status_code == 200
        files[a["kind"]] = resp.content
    import docx
    import openpyxl

    d = docx.Document(io.BytesIO(files["technical_response"]))
    assert any("Compliance Statement" in p.text for p in d.paragraphs)
    wb = openpyxl.load_workbook(io.BytesIO(files["compliance_matrix"]))
    assert wb.active.max_row >= 15
    assert files["evidence_report"][:4] == b"%PDF" and files["risk_report"][:4] == b"%PDF"
    zf = zipfile.ZipFile(io.BytesIO(files["submission_package"]))
    manifest = json.loads(zf.read("manifest.json"))
    import hashlib

    for f in manifest["files"]:
        assert hashlib.sha256(zf.read(f["path"])).hexdigest() == f["sha256"]
    assert any(p.startswith("02_supporting/") for p in zf.namelist())
    assert "does not submit bids" in manifest["notice"]
    log = c.get("/api/v1/audit?limit=500", headers=h)
    assert log.status_code == 403  # bid managers cannot read the org audit log
    owner_c, _ = login(None, "owner@voltedge.example")
    actions = {e["action"] for e in owner_c.get("/api/v1/audit?limit=500").json()}
    assert {"bid.approve", "bid.package_exported", "eligibility.decision_accept", "document.uploaded"} <= actions


def test_09_replacement_certificate_new_version_preserves_history(bm, db):
    c, h = bm
    docs = c.get("/api/v1/documents?category=iso_certificate").json()["items"]
    iso = next(d for d in docs if "2023" in d["title"])
    old_version = iso["current_version_id"]
    data = (SAMPLE / "voltedge/iso_9001_certificate_2026.pdf").read_bytes()
    up = c.post("/api/v1/documents", files={"file": ("iso_9001_certificate_2026.pdf", data, "application/pdf")}, data={"document_id": iso["id"]}, headers=h)
    assert up.status_code == 201, up.text
    assert up.json()[0]["version"]["version_no"] == 2
    job = c.get(f"/api/v1/jobs/{up.json()[0]['job_id']}").json()
    assert job["status"] == "succeeded"
    # reopen bid so a re-run is meaningful, then re-assess
    owner_c, oh = login(None, "owner@voltedge.example")
    assert owner_c.post(f"/api/v1/workspaces/{S['ws']}/approval", json={"action": "reopen"}, headers=oh).status_code == 200
    a2 = c.post(f"/api/v1/tenders/{S['tid']}/assessments", headers=h).json()["id"]
    S["a2"] = a2
    rep2 = c.get(f"/api/v1/assessments/{a2}").json()
    iso2 = next(it for it in rep2["items"] if "ISO 9001" in it["requirement"]["title"])
    assert iso2["decision"]["status"] == "verified_satisfied"
    assert iso2["links"][0]["document_version_id"] != old_version
    rep1 = c.get(f"/api/v1/assessments/{S['a1']}").json()
    iso1 = next(it for it in rep1["items"] if "ISO 9001" in it["requirement"]["title"])
    assert iso1["decision"]["status"] == "verified_not_satisfied"  # history not rewritten
    assert iso1["links"][0]["document_version_id"] == old_version
    assert rep1["assessment"]["status"] == "superseded"
    diff = c.get(f"/api/v1/assessments/{S['a1']}/compare/{a2}").json()
    assert any(d["before"] == "verified_not_satisfied" and d["after"] == "verified_satisfied" for d in diff)


def test_10_corrigendum_versioning_and_impact(bm, db):
    c, h = bm
    data = (SAMPLE / "tenders/msgic_lt_panels_corrigendum_1.pdf").read_bytes()
    r = c.post(f"/api/v1/tenders/{S['tid']}/documents", files={"file": ("corrigendum_1.pdf", data, "application/pdf")}, data={"role": "corrigendum"}, headers=h)
    assert r.status_code == 201, r.text
    am = c.get(f"/api/v1/tenders/{S['tid']}/amendments").json()
    assert am and am[0]["status"] == "applied" and am[0]["to_version"] == 2
    assert any(fc["field"] == "submission_deadline" and _utc(fc["to"]) == "2026-12-22T09:30:00+00:00" for fc in am[0]["field_changes"])
    rc = next(x for x in am[0]["requirement_changes"] if "turnover" in x["title"].lower())
    assert rc["change"] == "modified" and rc["before"]["threshold"] == "50000000.00" and rc["after"]["threshold"] == "45000000.00"
    t = c.get(f"/api/v1/tenders/{S['tid']}").json()
    assert t["current_version_no"] == 2 and _utc(t["submission_deadline"]) == "2026-12-22T09:30:00+00:00"
    current = c.get(f"/api/v1/tenders/{S['tid']}/requirements").json()
    allv = c.get(f"/api/v1/tenders/{S['tid']}/requirements?include_superseded=true").json()
    assert len(allv) == len(current) + 1
    assert sum(1 for x in current if "turnover" in x["title"].lower()) == 1
    tasks = c.get(f"/api/v1/workspaces/{S['ws']}/tasks").json()
    assert any(t_["source"] == "amendment" for t_ in tasks)
    runs = c.get(f"/api/v1/tenders/{S['tid']}/assessments").json()
    assert runs[0]["trigger"] == "amendment" and runs[0]["tender_version_no"] == 2


def test_11_other_organization_cannot_access(seeded):
    kc, kh = login(None, "owner@kaveri.example")
    tid, aid, ws = S["tid"], S["a1"], S["ws"]
    for url in (f"/api/v1/tenders/{tid}", f"/api/v1/tenders/{tid}/requirements", f"/api/v1/assessments/{aid}", f"/api/v1/assessments/{aid}/graph",
                f"/api/v1/workspaces/{ws}", f"/api/v1/workspaces/{ws}/tasks"):
        assert kc.get(url).status_code == 404, url
    vc, _ = login(None, "bids@voltedge.example")
    vdoc = vc.get("/api/v1/documents").json()["items"][0]
    assert kc.get(f"/api/v1/documents/{vdoc['id']}").status_code == 404
    assert kc.get(f"/api/v1/document-versions/{vdoc['current_version_id']}/download").status_code == 404
    assert kc.get(f"/api/v1/document-versions/{vdoc['current_version_id']}/pages/1/image").status_code == 404
    assert kc.post(f"/api/v1/tenders/{tid}/assessments", headers=kh).status_code == 404
    titles = [d["title"] for d in kc.get("/api/v1/documents").json()["items"]]
    assert titles and not any("voltedge" in t.lower() or "udyam" in t for t in titles)
    assert not any(t["id"] == tid for t in kc.get("/api/v1/tenders").json()["items"])
