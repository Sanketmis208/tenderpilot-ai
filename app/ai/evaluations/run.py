"""Reproducible evaluation: extraction precision/recall & critical-field accuracy, abstention on
injection/unrelated text, rule-engine correctness, citation correctness and latency.

Run: `uv run tenderpilot eval`  (add TP_ANTHROPIC_API_KEY to also evaluate the LLM extractor).
Writes var/eval_report.json.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.ai.evaluations.dataset import EXTRACTION_CASES, RULE_CASES
from app.extraction.parsers.types import ParsedBlock, ParsedDocument, ParsedPage
from app.extraction.tender_rules import extract_requirements
from app.services.eligibility import rules

CRITICAL = ("req_type", "metric", "threshold_value", "period_years", "mandatory", "standard")


def _doc(text: str) -> ParsedDocument:
    return ParsedDocument([ParsedPage(1, text, [ParsedBlock(text, [0, 0, 100, 20])])], "eval")


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
        doc = _doc(case["text"])
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
            cite_ok += p.source_quote.strip()[:40] in case["text"]
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


def main() -> dict:
    report = {"extraction_rules": eval_extraction(), "eligibility_rules": eval_rules()}
    from app.ai.llm.client import get_llm

    llm = get_llm()
    if llm is not None:
        from app.extraction.tender_llm import verify_quotes
        from app.schemas.extraction import LLMRequirementBatch

        def llm_extract(doc):
            from app.extraction.tender_llm import build_prompt

            out = llm.structured(LLMRequirementBatch, build_prompt(doc.full_text, with_fields=False), purpose="eval")
            return verify_quotes(out.requirements, doc)[0]

        report["extraction_llm"] = eval_extraction(llm_extract)
    else:
        report["extraction_llm"] = "skipped: no LLM provider configured"
    out = Path("var/eval_report.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: {m: v for m, v in r.items() if m != "cases"} if isinstance(r, dict) else r for k, r in report.items()}, indent=2))
    return report


if __name__ == "__main__":
    main()
