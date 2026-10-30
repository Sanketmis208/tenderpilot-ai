"""Deterministic eligibility rule engine.

Pure functions over plain dataclasses: no DB, no LLM. Each rule returns a RuleResult with the
values it compared, the evidence it used and the gaps it found. LLM agents may *explain* these
results but can never override them (EligibilityDecision.machine_status is immutable).

Status semantics:
- verified_satisfied / verified_not_satisfied: deterministic comparison of document-stated facts.
  "Verified" means verified against the uploaded evidence — not external authenticity checks.
- insufficient_evidence: required facts absent.
- conflicting_evidence: sources disagree; a human must resolve.
- needs_human_review: interpretation required (similarity, conditional exemptions, low confidence).
- not_applicable: condition does not apply to this bidder (or rejected by a reviewer).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from app.db.models.eligibility import DecisionStatus as D
from app.extraction.normalization.values import FY_RE, format_inr, normalise_fiscal_year, previous_fiscal_years


@dataclass(frozen=True)
class Policy:
    min_fact_confidence: float = 0.6
    similar_work_min_keyword_overlap: int = 2
    treat_profile_only_as_review: bool = True


@dataclass
class ReqView:
    id: uuid.UUID
    code: str
    req_type: str
    title: str
    metric: str | None
    operator: str | None
    threshold: Decimal | None
    unit: str | None
    period_years: int | None
    aggregation: str | None
    mandatory: str
    conditions: list[str]
    parameters: dict[str, Any]
    source_text: str
    review_status: str = "unreviewed"


@dataclass
class Fact:
    """A company fact as seen by the rule engine (from a company_* table row)."""

    kind: str
    id: uuid.UUID
    values: dict[str, Any]
    document_id: uuid.UUID | None
    version_id: uuid.UUID | None
    page: int | None
    excerpt: str | None
    bbox: list[float] | None
    confidence: float
    confirmed: bool
    label: str


@dataclass
class CompanyEvidence:
    profile: dict[str, Any]
    certifications: list[Fact] = field(default_factory=list)
    financials: list[Fact] = field(default_factory=list)
    projects: list[Fact] = field(default_factory=list)
    registrations: list[Fact] = field(default_factory=list)
    products: list[Fact] = field(default_factory=list)
    documents_by_category: dict[str, list[Fact]] = field(default_factory=dict)
    text_hits: dict[str, list[Fact]] = field(default_factory=dict)  # standard -> chunk facts mentioning it


@dataclass
class EvidenceUse:
    fact: Fact
    relation: str = "supports"  # supports | contradicts | partial | related


@dataclass
class RuleResult:
    status: str
    rule_id: str
    explanation: str
    computation: dict[str, Any] = field(default_factory=dict)
    evidence: list[EvidenceUse] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    risk: str = "medium"
    evidence_confidence: float = 0.0
    conflicts: list[str] = field(default_factory=list)


def _conf(facts: list[Fact]) -> float:
    return round(min((1.0 if f.confirmed else f.confidence) for f in facts), 2) if facts else 0.0


def _low_conf(facts: list[Fact], policy: Policy) -> list[Fact]:
    return [f for f in facts if not f.confirmed and f.confidence < policy.min_fact_confidence]


def _compare(op: str | None, value: Decimal, threshold: Decimal) -> bool:
    return {
        ">=": value >= threshold,
        ">": value > threshold,
        "<=": value <= threshold,
        "<": value < threshold,
        "==": value == threshold,
    }.get(op or ">=", value >= threshold)


def required_fiscal_years(req: ReqView, reference: date) -> list[str]:
    explicit = []
    for m in FY_RE.finditer(req.source_text):
        fy = normalise_fiscal_year(m.group(0))
        if fy and fy not in explicit:
            explicit.append(fy)
    n = req.period_years or 3
    if len(explicit) >= n:
        return explicit[:n]
    return previous_fiscal_years(reference, n)


def rule_turnover(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy) -> RuleResult:
    if req.threshold is None:
        return RuleResult(D.NEEDS_HUMAN_REVIEW, "financial.turnover", "Turnover threshold could not be read from the clause.", risk="high")
    years = required_fiscal_years(req, reference)
    by_year: dict[str, list[Fact]] = defaultdict(list)
    for f in ev.financials:
        if f.values.get("metric") == "turnover" and f.values.get("fiscal_year") in years:
            by_year[f.values["fiscal_year"]].append(f)
    used: list[EvidenceUse] = []
    conflicts: list[str] = []
    chosen: dict[str, Decimal] = {}
    gaps = []
    for fy in years:
        facts = by_year.get(fy, [])
        if not facts:
            gaps.append(f"No turnover evidence for FY {fy}. Upload audited statements or a CA certificate covering FY {fy}.")
            continue
        confirmed = [f for f in facts if f.confirmed]
        pool = confirmed or facts
        amounts = {Decimal(str(f.values["amount"])) for f in pool}
        if len(amounts) > 1:
            listing = "; ".join(f"{format_inr(Decimal(str(f.values['amount'])))} in {f.label} (p.{f.page})" for f in pool)
            conflicts.append(f"FY {fy} turnover differs between sources: {listing}.")
            used += [EvidenceUse(f, "contradicts") for f in pool]
            continue
        chosen[fy] = amounts.pop()
        used += [EvidenceUse(f, "supports") for f in pool]
    comp: dict[str, Any] = {
        "required_years": years,
        "values": {k: str(v) for k, v in chosen.items()},
        "threshold": str(req.threshold),
        "operator": req.operator or ">=",
        "aggregation": req.aggregation or "average",
    }
    if conflicts:
        return RuleResult(D.CONFLICTING_EVIDENCE, "financial.turnover", " ".join(conflicts) + " Confirm the correct figure before relying on this requirement.",
                          comp, used, gaps + ["Resolve conflicting turnover figures (confirm the authoritative document)."], "high", _conf([u.fact for u in used]), conflicts)
    if gaps:
        partial = ", ".join(f"FY {k}: {format_inr(v)}" for k, v in chosen.items()) or "none"
        return RuleResult(D.INSUFFICIENT_EVIDENCE, "financial.turnover",
                          f"Turnover evidence found for {len(chosen)} of {len(years)} required years ({partial}). The average cannot be computed without every year.",
                          comp, used, gaps, "high", _conf([u.fact for u in used]))
    values = list(chosen.values())
    if (req.aggregation or "average") == "each":
        ok = all(_compare(req.operator, v, req.threshold) for v in values)
        metric_value = min(values)
        desc = f"lowest yearly turnover {format_inr(metric_value)}"
    else:
        exact = sum(values, Decimal(0)) / Decimal(len(values))
        ok = _compare(req.operator, exact, req.threshold)  # compare unrounded; rounding is for display only
        metric_value = exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not ok and metric_value >= req.threshold:
            metric_value = exact.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        desc = f"average turnover {format_inr(metric_value)} over FY {', '.join(years)}"
    comp["computed"] = str(metric_value)
    low = _low_conf([u.fact for u in used], policy)
    if low:
        return RuleResult(D.NEEDS_HUMAN_REVIEW, "financial.turnover", f"Computed {desc}, but some figures were extracted with low confidence; confirm them.",
                          comp, used, [f"Confirm turnover figure in {f.label}" for f in low], "medium", _conf([u.fact for u in used]))
    return RuleResult(
        D.VERIFIED_SATISFIED if ok else D.VERIFIED_NOT_SATISFIED,
        "financial.turnover",
        f"{desc[0].upper() + desc[1:]} {'meets' if ok else 'is below'} the required {format_inr(req.threshold)} ({req.operator or '>='}).",
        comp, used, [] if ok else [f"Turnover shortfall of {format_inr(max(req.threshold - metric_value, Decimal('0.01')))} against the threshold."],
        "low" if ok else "high", _conf([u.fact for u in used]),
    )


def rule_certificate(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy) -> RuleResult:
    std = (req.parameters.get("standard") or "").upper().replace("  ", " ")
    certs = [f for f in ev.certifications if (f.values.get("cert_type") or "").upper() == std]
    comp: dict[str, Any] = {"standard": std, "reference_date": reference.isoformat()}
    note = " Validity is as stated on the document; issuer authenticity has not been externally verified."
    if not certs:
        return RuleResult(D.INSUFFICIENT_EVIDENCE, "certification.validity", f"No {std} certificate found in the document vault.", comp, [],
                          [f"Upload a valid {std} certificate."], "high")
    valid, expired, undated = [], [], []
    for f in certs:
        vf, vu = f.values.get("valid_from"), f.values.get("valid_until")
        if vu is None:
            undated.append(f)
        elif (vf is None or vf <= reference) and reference <= vu:
            valid.append(f)
        elif vu < reference:
            expired.append(f)
        else:
            undated.append(f)  # not yet valid on reference date
    if valid:
        best = max(valid, key=lambda f: f.values["valid_until"])
        comp.update({"certificate_number": best.values.get("certificate_number"), "valid_until": best.values["valid_until"].isoformat()})
        low = _low_conf([best], policy)
        status = D.NEEDS_HUMAN_REVIEW if low else D.VERIFIED_SATISFIED
        return RuleResult(status, "certification.validity",
                          f"{std} certificate {best.values.get('certificate_number') or ''} is valid until {best.values['valid_until']:%d %b %Y}, covering the reference date {reference:%d %b %Y}.{note}",
                          comp, [EvidenceUse(best)] + [EvidenceUse(f, "related") for f in expired], [], "low", _conf([best]))
    if expired:
        latest = max(expired, key=lambda f: f.values["valid_until"])
        comp.update({"expired_on": latest.values["valid_until"].isoformat()})
        return RuleResult(D.VERIFIED_NOT_SATISFIED, "certification.validity",
                          f"The only {std} certificate on file expired on {latest.values['valid_until']:%d %b %Y}, before the reference date {reference:%d %b %Y}.{note}",
                          comp, [EvidenceUse(latest, "contradicts")], [f"Upload the renewed {std} certificate (current one expired {latest.values['valid_until']:%d %b %Y})."], "high", _conf([latest]))
    return RuleResult(D.NEEDS_HUMAN_REVIEW, "certification.validity", f"A {std} certificate exists but its validity period could not be determined.{note}",
                      comp, [EvidenceUse(f, "partial") for f in undated], ["Confirm the certificate validity dates."], "medium", _conf(undated))


def _years_between(start: date, end: date) -> Decimal:
    days = Decimal((end - start).days)
    return (days / Decimal("365.25")).quantize(Decimal("0.01"))


def rule_years_in_business(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy) -> RuleResult:
    need = req.threshold or Decimal(0)
    cin = [f for f in ev.registrations if f.values.get("kind") == "cin" and f.values.get("issued_on")]
    projects = [f for f in ev.projects if f.values.get("start_date") or f.values.get("completion_date")]
    comp: dict[str, Any] = {"required_years": str(need), "reference_date": reference.isoformat()}
    if cin:
        inc = min(f.values["issued_on"] for f in cin)
        yrs = _years_between(inc, reference)
        comp.update({"established": inc.isoformat(), "years": str(yrs)})
        ok = yrs >= need
        earliest = min((f.values.get("start_date") or f.values.get("completion_date") for f in projects), default=None)
        expl = f"Incorporated {inc:%d %b %Y} — {yrs} years before the reference date."
        if earliest:
            expl += f" Earliest dated project activity on file: {earliest:%b %Y}."
        status = D.VERIFIED_SATISFIED if ok else D.VERIFIED_NOT_SATISFIED
        if ok and req.parameters.get("identity", "").startswith("experience") and not projects:
            status = D.NEEDS_HUMAN_REVIEW
            expl += " Years since incorporation do not by themselves prove *relevant* experience; no dated project records found."
        return RuleResult(status, "experience.years", expl, comp, [EvidenceUse(f) for f in cin] + [EvidenceUse(f, "related") for f in projects[:3]],
                          [] if ok else [f"Requires {need} years; company has {yrs}."], "low" if ok else "high", _conf(cin))
    year = ev.profile.get("year_established")
    if year:
        yrs = Decimal(reference.year - int(year))
        comp.update({"established_year": year, "years": str(yrs), "source": "company profile (user-entered)"})
        return RuleResult(D.NEEDS_HUMAN_REVIEW, "experience.years",
                          f"Company profile states establishment in {year} (~{yrs} years), but no incorporation document was found to support it.",
                          comp, [], ["Upload the certificate of incorporation to evidence years of operation."], "medium", 0.4)
    return RuleResult(D.INSUFFICIENT_EVIDENCE, "experience.years", "No evidence of establishment date or years of operation.", comp, [],
                      ["Upload the certificate of incorporation."], "high")


_WORD = re.compile(r"[a-z]{3,}")


def rule_similar_work(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy) -> RuleResult:
    if req.threshold is None:
        return RuleResult(D.NEEDS_HUMAN_REVIEW, "experience.similar_work", "Work value threshold could not be read from the clause.", risk="high")
    count_needed = int(req.parameters.get("count_required") or 1)
    years = req.period_years
    window_start = date(reference.year - years, reference.month, min(reference.day, 28)) if years else None
    keywords = set(req.parameters.get("keywords") or [])
    qualifying, near, gaps = [], [], []
    rows = []
    for f in ev.projects:
        v = f.values.get("contract_value")
        done = f.values.get("completion_date")
        text = f"{f.values.get('title', '')} {f.values.get('description', '')}".lower()
        overlap = sorted(keywords & set(_WORD.findall(text))) if keywords else []
        reasons = []
        if v is None:
            reasons.append("value not stated")
        elif Decimal(str(v)) < req.threshold:
            reasons.append(f"value {format_inr(Decimal(str(v)))} below {format_inr(req.threshold)}")
        if done is None:
            reasons.append("completion date not stated")
        elif window_start and done < window_start:
            reasons.append(f"completed {done:%b %Y}, outside the {years}-year window")
        rows.append({"project": f.values.get("title"), "value": str(v) if v is not None else None,
                     "completed": done.isoformat() if done else None, "keyword_overlap": overlap, "excluded_because": reasons})
        if not reasons:
            qualifying.append((f, overlap))
        else:
            near.append(f)
    comp = {"count_required": count_needed, "threshold": str(req.threshold), "window_years": years,
            "window_start": window_start.isoformat() if window_start else None, "keywords": sorted(keywords), "projects": rows}
    used = [EvidenceUse(f) for f, _ in qualifying] + [EvidenceUse(f, "related") for f in near[:3]]
    if len(qualifying) < count_needed:
        gaps.append(f"Found {len(qualifying)} qualifying completed work(s); {count_needed} required.")
        if near:
            gaps.append("Some projects were excluded (see computation) — upload certificates stating value and completion date.")
        status = D.INSUFFICIENT_EVIDENCE if not ev.projects or near else D.VERIFIED_NOT_SATISFIED
        return RuleResult(status, "experience.similar_work",
                          f"{len(qualifying)} of {count_needed} required similar works meet the value and period criteria.", comp, used, gaps, "high",
                          _conf([f for f, _ in qualifying]) if qualifying else 0.0)
    weak = [f for f, ov in qualifying if keywords and len(ov) < policy.similar_work_min_keyword_overlap]
    if weak or not keywords:
        return RuleResult(D.NEEDS_HUMAN_REVIEW, "experience.similar_work",
                          f"{len(qualifying)} completed work(s) meet the value and period criteria; whether they are 'similar work' as defined by the tender needs confirmation.",
                          comp, used, ["Confirm that the listed projects match the tender's definition of similar work."], "medium", _conf([f for f, _ in qualifying]))
    return RuleResult(D.VERIFIED_SATISFIED, "experience.similar_work",
                      f"{len(qualifying)} completed work(s) meet value ≥ {format_inr(req.threshold)}"
                      + (f" within {years} years" if years else "") + " and match the similar-work scope keywords.",
                      comp, used, [], "low", _conf([f for f, _ in qualifying]))


REG_KIND = {"GST": "gstin", "PAN": "pan", "UDYAM": "udyam", "MSME": "udyam", "CIN": "cin"}


def rule_registration(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy) -> RuleResult:
    std = (req.parameters.get("standard") or "").upper()
    kind = REG_KIND.get(std, std.lower())
    regs = [f for f in ev.registrations if f.values.get("kind") == kind]
    comp = {"registration": std, "kind": kind}
    if not regs:
        if kind == "pan" and (gst := [f for f in ev.registrations if f.values.get("kind") == "gstin"]):
            return RuleResult(D.NEEDS_HUMAN_REVIEW, "registration.present", "PAN is embedded in the GSTIN on file; a PAN card copy may still be required.",
                              comp, [EvidenceUse(f, "partial") for f in gst], ["Upload a copy of the PAN card if the tender requires it."], "low", _conf(gst))
        return RuleResult(D.INSUFFICIENT_EVIDENCE, "registration.present", f"No {std} registration document found.", comp, [], [f"Upload the {std} registration certificate."], "high")
    f = regs[0]
    comp["identifier"] = f.values.get("identifier")
    return RuleResult(D.VERIFIED_SATISFIED, "registration.present",
                      f"{std} registration {f.values.get('identifier')} is stated in {f.label}. Registration status has not been checked against the government registry.",
                      comp, [EvidenceUse(f)], [], "low", _conf([f]))


def rule_emd(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy) -> RuleResult:
    amount = req.threshold
    comp: dict[str, Any] = {"emd_amount": str(amount) if amount is not None else None, "conditions": req.conditions}
    udyam = [f for f in ev.registrations if f.values.get("kind") == "udyam"]
    msme_condition = any(re.search(r"msme|micro|small|udyam|nse|nsic", c, re.I) for c in req.conditions)
    if msme_condition and udyam:
        cat = (udyam[0].values.get("attributes") or {}).get("msme_category") or ev.profile.get("msme_category")
        comp["msme_category"] = cat
        return RuleResult(D.NEEDS_HUMAN_REVIEW, "emd.exemption",
                          f"The tender allows a conditional EMD exemption for registered MSEs. A Udyam registration ({udyam[0].values.get('identifier')}, category: {cat or 'not stated'}) is on file. "
                          "Whether the exemption applies depends on the stated conditions (e.g. registration for the tendered items) — confirm before relying on it.",
                          comp, [EvidenceUse(f, "partial") for f in udyam],
                          ["Confirm EMD exemption eligibility against the tender conditions, or arrange the EMD" + (f" of {format_inr(amount)}" if amount else "") + "."], "medium", _conf(udyam))
    return RuleResult(D.NEEDS_HUMAN_REVIEW, "emd.arrange",
                      "EMD is a submission obligation rather than a qualification" + (f": arrange {format_inr(amount)} before submission." if amount else "."),
                      comp, [], ["Arrange the EMD instrument (bank guarantee / online payment)."], "medium", 0.0)


DOC_RULE_CATEGORIES = {
    "financial_statement": ["financial_statement"],
    "product_catalogue": ["product_catalogue", "technical_datasheet"],
    "technical_datasheet": ["technical_datasheet", "product_catalogue"],
    "completion_certificate": ["completion_certificate", "experience_certificate"],
    "authorization_letter": ["authorization_letter", "oem_certificate"],
    "gst_certificate": ["gst_certificate"],
    "legal_compliance": ["legal_compliance"],
}
BID_SPECIFIC = {"legal_compliance"}


def rule_document(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy) -> RuleResult:
    kind = req.parameters.get("document_kind") or "other"
    cats = DOC_RULE_CATEGORIES.get(kind, [kind])
    docs = [f for c in cats for f in ev.documents_by_category.get(c, [])]
    comp: dict[str, Any] = {"document_kind": kind, "matched_categories": cats, "found": len(docs)}
    if kind == "financial_statement" and req.period_years:
        years = required_fiscal_years(req, reference)
        covered = sorted({f.values.get("fiscal_year") for f in ev.financials if f.values.get("audited")} & set(years))
        comp.update({"required_years": years, "audited_years_found": covered})
        missing = [y for y in years if y not in covered]
        if missing:
            return RuleResult(D.INSUFFICIENT_EVIDENCE, "document.presence",
                              f"Audited statements found for {', '.join(covered) or 'no'} year(s); missing FY {', '.join(missing)}.",
                              comp, [EvidenceUse(f, "partial") for f in docs], [f"Upload audited financial statements for FY {y}." for y in missing], "high", _conf(docs))
    if not docs:
        if kind in BID_SPECIFIC:
            return RuleResult(D.INSUFFICIENT_EVIDENCE, "document.bid_specific",
                              "This declaration is bid-specific and must be prepared on letterhead for this tender.", comp, [],
                              [f"Prepare and sign: {req.title}."], "medium")
        return RuleResult(D.INSUFFICIENT_EVIDENCE, "document.presence", f"No document of type '{kind.replace('_', ' ')}' found in the vault.", comp, [],
                          [f"Upload: {req.title}."], "high")
    return RuleResult(D.VERIFIED_SATISFIED, "document.presence", f"{len(docs)} supporting document(s) of type '{kind.replace('_', ' ')}' on file.",
                      comp, [EvidenceUse(f) for f in docs[:5]], [], "low", _conf(docs))


def rule_standard_compliance(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy) -> RuleResult:
    stds = [s.strip() for s in (req.parameters.get("standard") or "").split(";") if s.strip()]
    found, missing, used = [], [], []
    for s in stds:
        hits = ev.text_hits.get(s, [])
        (found if hits else missing).append(s)
        used += [EvidenceUse(h, "partial") for h in hits[:2]]
    comp = {"standards": stds, "mentioned_in_company_documents": found, "not_found": missing}
    if not found:
        return RuleResult(D.INSUFFICIENT_EVIDENCE, "technical.standard", f"No company document mentions {', '.join(stds)}.", comp, [],
                          [f"Upload datasheets / type-test reports showing compliance with {s}." for s in stds], "high")
    gaps = ["Type-test reports are required to demonstrate compliance; catalogue mentions are not proof."]
    gaps += [f"No evidence found for {s}." for s in missing]
    return RuleResult(D.NEEDS_HUMAN_REVIEW, "technical.standard",
                      f"Company documents mention {', '.join(found)}" + (f" but not {', '.join(missing)}" if missing else "") + ". A technical reviewer must confirm compliance.",
                      comp, used, gaps, "medium", _conf([u.fact for u in used]))


def rule_geographic(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy) -> RuleResult:
    place = (req.parameters.get("standard") or "").lower()
    states = [s.lower() for s in ([ev.profile.get("state")] + list(ev.profile.get("operational_states") or [])) if s]
    comp = {"required": place, "company_states": states}
    if any(place in s or s in place for s in states):
        return RuleResult(D.NEEDS_HUMAN_REVIEW, "geographic.presence", f"Company profile lists presence in {place.title()}; confirm with registration address proof.",
                          comp, [], ["Attach proof of address / registration in the required state."], "low", 0.5)
    return RuleResult(D.INSUFFICIENT_EVIDENCE, "geographic.presence", f"No evidence of presence in {place.title()}.", comp, [], [f"Provide proof of office in {place.title()}."], "high")


def rule_consortium(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy) -> RuleResult:
    permitted = req.threshold is not None and req.threshold > 0
    return RuleResult(D.NOT_APPLICABLE, "consortium.info",
                      "Consortium/JV bids are " + ("permitted under the stated conditions" if permitted else "not permitted") + "; a sole-bidder submission is assumed.",
                      {"consortium_permitted": permitted}, [], [], "low", 1.0)


def evaluate(req: ReqView, ev: CompanyEvidence, reference: date, policy: Policy | None = None) -> RuleResult:
    policy = policy or Policy()
    if req.review_status == "rejected":
        return RuleResult(D.NOT_APPLICABLE, "review.rejected", "This extracted requirement was rejected by a reviewer.", risk="low", evidence_confidence=1.0)
    m, t = req.metric or "", req.req_type
    if t == "financial" and m in ("average_annual_turnover", "annual_turnover"):
        return rule_turnover(req, ev, reference, policy)
    if t == "certification":
        return rule_certificate(req, ev, reference, policy)
    if t == "experience" and m == "years_in_business":
        return rule_years_in_business(req, ev, reference, policy)
    if t == "experience" and m == "similar_work_value":
        return rule_similar_work(req, ev, reference, policy)
    if t == "registration":
        return rule_registration(req, ev, reference, policy)
    if t == "emd":
        return rule_emd(req, ev, reference, policy)
    if t in ("document", "oem_authorization"):
        return rule_document(req, ev, reference, policy)
    if t == "technical" and m == "standard_compliance":
        return rule_standard_compliance(req, ev, reference, policy)
    if t == "geographic":
        return rule_geographic(req, ev, reference, policy)
    if t == "consortium":
        return rule_consortium(req, ev, reference, policy)
    return RuleResult(D.NEEDS_HUMAN_REVIEW, "generic.review", "No deterministic rule covers this requirement; a reviewer must assess it against the evidence.",
                      {}, [], ["Review this requirement manually."], "medium")


# ---- assessment summary ----

WEIGHT = {D.VERIFIED_SATISFIED: 1.0, D.NOT_APPLICABLE: 1.0, D.NEEDS_HUMAN_REVIEW: 0.5, D.CONFLICTING_EVIDENCE: 0.25,
          D.INSUFFICIENT_EVIDENCE: 0.25, D.VERIFIED_NOT_SATISFIED: 0.0}


def summarise(decisions: list[tuple[str, str, str]]) -> tuple[float, str, dict[str, Any]]:
    """decisions: [(status, mandatory, risk)] → (readiness score, overall status, counts).

    The score is explanatory only; the overall status is driven by mandatory conditions and can
    never read 'eligible' while any mandatory condition is unresolved or failed.
    """
    counts: dict[str, int] = defaultdict(int)
    mandatory_counts: dict[str, int] = defaultdict(int)
    total = got = 0.0
    for status, mandatory, _risk in decisions:
        counts[status] += 1
        w = 2.0 if mandatory in ("mandatory", "conditional") else 1.0
        total += w
        got += w * WEIGHT.get(status, 0)
        if mandatory in ("mandatory", "conditional", "unknown"):
            mandatory_counts[status] += 1
    score = round(100 * got / total, 1) if total else 0.0
    if mandatory_counts[D.VERIFIED_NOT_SATISFIED]:
        overall = "not_eligible"
    elif mandatory_counts[D.CONFLICTING_EVIDENCE] or mandatory_counts[D.INSUFFICIENT_EVIDENCE]:
        overall = "gaps_found"
    elif mandatory_counts[D.NEEDS_HUMAN_REVIEW]:
        overall = "needs_review"
    elif decisions:
        overall = "eligible_per_policy"
    else:
        overall = "no_requirements"
    if overall == "not_eligible":
        score = min(score, 40.0)  # a failed mandatory condition caps readiness
    return score, overall, {"by_status": dict(counts), "mandatory_by_status": dict(mandatory_counts), "total": len(decisions)}
