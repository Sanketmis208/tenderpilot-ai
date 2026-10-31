"""Reproducible evaluation: extraction precision/recall & critical-field accuracy (incl. page citations
for multi-page evidence), abstention on injection/unrelated text, rule-engine correctness, amendment
change detection, retrieval precision/hit@1/MRR, agent completion rate, latency and model cost.

Run: `uv run tenderpilot eval` (with an LLM provider configured it also evaluates the LLM extractor and
reports its token usage and cost). Writes var/eval_report.json.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.ai.evaluations.dataset import (
    AMENDMENT_CASES,
    EXTRACTION_CASES,
    RETRIEVAL_CASES,
    RETRIEVAL_CORPUS,
    RULE_CASES,
)
from app.db.tenancy import system_scoped
from app.extraction.parsers.types import ParsedBlock, ParsedDocument, ParsedPage
from app.extraction.tender_rules import extract_requirements
from app.services.eligibility import rules

CRITICAL = ("req_type", "metric", "threshold_value", "period_years", "mandatory", "standard", "source_page")


def _doc(text: str | list[str]) -> ParsedDocument:
    pages = [text] if isinstance(text, str) else text
    return ParsedDocument([ParsedPage(i, t, [ParsedBlock(t, [0, 0, 100, 20])]) for i, t in enumerate(pages, start=1)], "eval")


def _same(a, b) -> bool:
    """Numbers compare numerically ('20000000' == '20000000.00'); strings case-insensitively."""
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except Exception:
        return str(a).strip().lower() == str(b).strip().lower()


def _match(pred, gold) -> tuple[bool, int, int]:
    if pred.req_type != gold["req_type"]:
        return False, 0, 0
    total = ok = 0
    for k, v in gold.items():
        if k not in CRITICAL:
            continue
        total += 1
        ok += _same(getattr(pred, k), v)
    return True, ok, total


def eval_extraction(extractor=extract_requirements) -> dict:
    tp = fp = fn = field_ok = field_total = cite_ok = cite_total = 0
    per_case = []
    t0 = time.monotonic()
    for case in EXTRACTION_CASES:
        doc = _doc(case.get("pages") or case["text"])
        full = "\n".join(case.get("pages") or [case["text"]])
        preds = extractor(doc)
        matched = set()
        for g in case["gold"]:
            hit = next(((i, p) for i, p in enumerate(preds) if i not in matched and _match(p, g)[0]), None)
            if hit:
                matched.add(hit[0])
                tp += 1
                _, ok, tot = _match(hit[1], g)
                field_ok, field_total = field_ok + ok, field_total + tot
            else:
                fn += 1
        fp += len(preds) - len(matched)
        for p in preds:
            cite_total += 1
            cite_ok += p.source_quote.strip()[:40] in full
        per_case.append({"id": case["id"], "predicted": len(preds), "gold": len(case["gold"])})
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    abstain = [c for c in per_case if c["id"] in ("unrelated", "injection")]
    return {"precision": round(prec, 3), "recall": round(rec, 3), "critical_field_accuracy": round(field_ok / field_total, 3) if field_total else None,
            "citation_correctness": round(cite_ok / cite_total, 3) if cite_total else None,
            "abstention_ok": all(c["predicted"] == 0 for c in abstain), "unsupported_claim_rate": round(fp / max(tp + fp, 1), 3),
            "latency_ms_total": int((time.monotonic() - t0) * 1000), "cases": per_case}


def _fact(kind, values, confirmed=False):
    return rules.Fact(kind, uuid.uuid4(), values, None, None, 1, "", None, 0.9, confirmed, "eval")


def eval_rules() -> dict:
    ref = date(2026, 12, 15)
    results = []
    for c in RULE_CASES:
        if c["kind"] == "turnover":
            fins = [_fact("financial", {"fiscal_year": fy, "metric": "turnover", "amount": Decimal(a)}) for fy, amts in c["years"].items() for a in amts]
            req = rules.ReqView(uuid.uuid4(), "E", "financial", "t", "average_annual_turnover", ">=", Decimal(c["threshold"]), "INR", 3, "average", "mandatory", [], {},
                                "last three financial years (2022-23, 2023-24 and 2024-25)")
            got = rules.evaluate(req, rules.CompanyEvidence(profile={}, financials=fins), ref).status
        elif c["kind"] == "cert":
            vu = date.fromisoformat(c["valid_until"]) if c["valid_until"] else None
            cert = _fact("certification", {"cert_type": "ISO 9001", "valid_from": date(2020, 1, 1) if vu else None, "valid_until": vu})
            req = rules.ReqView(uuid.uuid4(), "E", "certification", "t", "certificate_valid", "exists", None, None, None, None, "mandatory", [], {"standard": "ISO 9001"}, "")
            got = rules.evaluate(req, rules.CompanyEvidence(profile={}, certifications=[cert]), ref).status
        else:
            reg = _fact("registration", {"kind": "udyam", "identifier": "UDYAM-XX-00-0000000", "attributes": {}})
            req = rules.ReqView(uuid.uuid4(), "E", "emd", "t", "emd", "exists", Decimal("100000"), "INR", None, None, "conditional", ["MSEs may be exempted subject to policy"], {}, "")
            got = rules.evaluate(req, rules.CompanyEvidence(profile={}, registrations=[reg]), ref).status
        results.append({"id": c["id"], "gold": c["gold"], "got": str(got), "ok": str(got) == c["gold"]})
    return {"accuracy": round(sum(r["ok"] for r in results) / len(results), 3), "cases": results}


def eval_amendments() -> dict:
    """Corrigendum change detection using the production identity/fingerprint logic."""
    from app.services.tenders.pipeline import _fingerprint, identity_key

    results = []
    for c in AMENDMENT_CASES:
        before = {identity_key(r): _fingerprint(r) for r in extract_requirements(_doc(c["original"]))}
        after = {identity_key(r): _fingerprint(r) for r in extract_requirements(_doc(c["corrigendum"]))}
        changed = {k for k in after if before.get(k) != after[k]}
        ok = set(c["changed"]) <= changed and not (set(c["unchanged"]) & changed) and set(c["unchanged"]) <= set(after)
        results.append({"id": c["id"], "detected_changes": sorted(changed), "ok": ok})
    return {"accuracy": round(sum(r["ok"] for r in results) / len(results), 3), "cases": results}


@system_scoped
def eval_retrieval(db) -> dict:
    """Hybrid retrieval over an isolated synthetic corpus in a throwaway organization (rolled back)."""
    from app.ai.retrieval.chunking import chunk_document
    from app.ai.retrieval.embeddings import get_embedder
    from app.ai.retrieval.search import hybrid_search
    from app.auth.context import TenantContext
    from app.db.models import Document, DocumentChunk, DocumentVersion, Organization

    org = Organization(name="Retrieval eval (synthetic, rolled back)", slug=f"eval-{uuid.uuid4().hex[:12]}")
    db.add(org)
    db.flush()
    emb = get_embedder()
    for item in RETRIEVAL_CORPUS:
        d = Document(organization_id=org.id, scope="company", title=item["title"], category=item["category"], is_synthetic=True)
        db.add(d)
        db.flush()
        v = DocumentVersion(organization_id=org.id, document_id=d.id, version_no=1, storage_key=f"eval/{d.id}", original_filename="eval.txt",
                            content_type="text/plain", size_bytes=len(item["text"]), sha256=uuid.uuid4().hex * 2)
        db.add(v)
        db.flush()
        d.current_version_id = v.id
        chunks = chunk_document(_doc(item["text"]))
        for ch, vec in zip(chunks, emb.embed([(c.section or "") + "\n" + c.text for c in chunks]), strict=True):
            db.add(DocumentChunk(organization_id=org.id, document_id=d.id, version_id=v.id, chunk_index=ch.index, page_no=ch.page_no, section=ch.section,
                                 kind=ch.kind, text=ch.text, bbox=ch.bbox, embedding=vec, embedding_model=emb.model_name))
    db.flush()
    ctx = TenantContext(user_id=uuid.UUID(int=0), organization_id=org.id, role="viewer", channel="eval")
    hit1 = mrr = prec = rec = 0.0
    cases = []
    t0 = time.monotonic()
    try:
        for c in RETRIEVAL_CASES:
            titles = [h.document_title for h in hybrid_search(db, ctx, c["query"], limit=3, log=False)]
            rel = set(c["relevant"])
            rank = next((i for i, t in enumerate(titles, start=1) if t in rel), None)
            hit1 += rank == 1
            mrr += 1 / rank if rank else 0
            prec += sum(t in rel for t in titles) / len(titles) if titles else 0  # standard P@3 (max 1/3 with one relevant doc)
            rec += len(rel & set(titles)) / len(rel)
            cases.append({"query": c["query"], "top": titles, "rank": rank})
    finally:
        db.rollback()
    n = len(RETRIEVAL_CASES)
    return {"hit_at_1": round(hit1 / n, 3), "mrr": round(mrr / n, 3), "precision_at_3": round(prec / n, 3), "recall_at_3": round(rec / n, 3), "embedder": emb.model_name,
            "latency_ms_total": int((time.monotonic() - t0) * 1000), "cases": cases}


@system_scoped
def eval_agent_runs(db) -> dict:
    """Agent completion rate from persisted runs (completed or correctly paused for a human vs failed)."""
    from sqlalchemy import func, select

    from app.db.models import AgentRun

    rows = db.execute(select(AgentRun.kind, AgentRun.status, func.count()).group_by(AgentRun.kind, AgentRun.status)).all()
    out: dict = {}
    for kind, status, n in rows:
        out.setdefault(kind, {})[status] = n
    for by in out.values():
        finished = sum(v for k, v in by.items() if k in ("completed", "interrupted", "failed"))
        by["completion_rate"] = round((by.get("completed", 0) + by.get("interrupted", 0)) / finished, 3) if finished else None
    return out


def main() -> dict:
    report: dict = {"extraction_rules": eval_extraction(), "eligibility_rules": eval_rules(), "amendments": eval_amendments()}
    try:
        from app.db.session import session_scope

        with session_scope() as db:
            report["retrieval"] = eval_retrieval(db)
        with session_scope() as db:
            report["agent_runs"] = eval_agent_runs(db)
    except Exception as exc:  # the evaluation must still report what it could measure
        report["retrieval"] = report.get("retrieval") or f"skipped: database unavailable ({type(exc).__name__})"
        report.setdefault("agent_runs", "skipped: database unavailable")
    from app.ai.llm.client import RunBudget, get_llm

    llm = get_llm()
    if llm is not None:
        from app.extraction.tender_llm import verify_quotes
        from app.schemas.extraction import LLMRequirementBatch

        budget = RunBudget(max_steps=1000, max_tokens=5_000_000, max_cost_usd=5.0)

        def llm_extract(doc):
            from app.extraction.tender_llm import build_prompt

            out = llm.structured(LLMRequirementBatch, build_prompt(doc.full_text, with_fields=False), purpose="eval", budget=budget)
            return verify_quotes(out.requirements, doc)[0]

        report["extraction_llm"] = eval_extraction(llm_extract)
        report["extraction_llm"]["usage"] = {"provider": llm.provider, "tokens_in": budget.tokens_in, "tokens_out": budget.tokens_out,
                                             "cost_usd": round(budget.cost_usd, 5)}
    else:
        report["extraction_llm"] = "skipped: no LLM provider configured"
    out = Path("var/eval_report.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: {m: v for m, v in r.items() if m != "cases"} if isinstance(r, dict) else r for k, r in report.items()}, indent=2))
    return report


if __name__ == "__main__":
    main()
