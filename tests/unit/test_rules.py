"""Deterministic eligibility rule engine: Decimal arithmetic, dates, conflicts, abstention."""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.db.models.eligibility import DecisionStatus as D
from app.services.eligibility import rules
from app.services.eligibility.rules import CompanyEvidence, Fact, ReqView

REF = date(2026, 12, 15)


def req(**kw) -> ReqView:
    base = dict(id=uuid.uuid4(), code="R-1", req_type="financial", title="t", metric="average_annual_turnover", operator=">=",
                threshold=Decimal("50000000"), unit="INR", period_years=3, aggregation="average", mandatory="mandatory", conditions=[],
                parameters={}, source_text="average annual turnover during the last three financial years (2022-23, 2023-24 and 2024-25)")
    base.update(kw)
    return ReqView(**base)


def fact(kind="financial", confirmed=False, confidence=0.85, label="doc", page=1, **values) -> Fact:
    return Fact(kind, uuid.uuid4(), values, uuid.uuid4(), uuid.uuid4(), page, "excerpt", None, confidence, confirmed, label)


def fin(fy, amount, **kw):
    return fact(fiscal_year=fy, metric="turnover", amount=Decimal(amount), audited=True, **kw)


def ev(**kw) -> CompanyEvidence:
    return CompanyEvidence(profile=kw.pop("profile", {}), **kw)


class TestTurnover:
    def test_average_satisfied_uses_exact_decimal(self):
        e = ev(financials=[fin("2022-23", "40000000.01"), fin("2023-24", "50000000"), fin("2024-25", "60000000")])
        r = rules.evaluate(req(), e, REF)
        assert r.status == D.VERIFIED_SATISFIED
        assert r.computation["computed"] == "50000000.00"

    def test_average_just_below_threshold_fails(self):
        e = ev(financials=[fin("2022-23", "49999999.99"), fin("2023-24", "50000000"), fin("2024-25", "50000000")])
        r = rules.evaluate(req(), e, REF)
        assert r.status == D.VERIFIED_NOT_SATISFIED
        assert "shortfall" in r.gaps[0].lower()

    def test_missing_year_is_insufficient_not_failed(self):
        e = ev(financials=[fin("2022-23", "90000000"), fin("2023-24", "90000000")])
        r = rules.evaluate(req(), e, REF)
        assert r.status == D.INSUFFICIENT_EVIDENCE
        assert any("2024-25" in g for g in r.gaps)

    def test_conflicting_sources_detected(self):
        e = ev(financials=[fin("2022-23", "42000000"), fin("2023-24", "51000000", label="audited"), fin("2023-24", "58000000", label="CA cert"), fin("2024-25", "60000000")])
        r = rules.evaluate(req(), e, REF)
        assert r.status == D.CONFLICTING_EVIDENCE
        assert r.conflicts and "2023-24" in r.conflicts[0]

    def test_confirmed_fact_resolves_conflict(self):
        e = ev(financials=[fin("2022-23", "42000000"), fin("2023-24", "51000000", confirmed=True), fin("2023-24", "58000000"), fin("2024-25", "60000000")])
        r = rules.evaluate(req(), e, REF)
        assert r.status == D.VERIFIED_SATISFIED  # (42+51+60)/3 = 51 Cr

    def test_each_year_aggregation(self):
        e = ev(financials=[fin("2022-23", "49000000"), fin("2023-24", "90000000"), fin("2024-25", "90000000")])
        r = rules.evaluate(req(aggregation="each"), e, REF)
        assert r.status == D.VERIFIED_NOT_SATISFIED

    def test_low_confidence_requires_review(self):
        e = ev(financials=[fin(fy, "90000000", confidence=0.3) for fy in ("2022-23", "2023-24", "2024-25")])
        assert rules.evaluate(req(), e, REF).status == D.NEEDS_HUMAN_REVIEW

    def test_period_derived_from_reference_date_when_not_explicit(self):
        r = req(source_text="average annual turnover in the last three financial years")
        assert sorted(rules.required_fiscal_years(r, REF)) == ["2023-24", "2024-25", "2025-26"]


class TestCertificates:
    def cert(self, frm, until, **kw):
        return fact(kind="certification", cert_type="ISO 9001", certificate_number="X", valid_from=frm, valid_until=until, **kw)

    def r(self):
        return req(req_type="certification", metric="certificate_valid", operator="exists", threshold=None, parameters={"standard": "ISO 9001"})

    def test_valid_on_reference_date(self):
        res = rules.evaluate(self.r(), ev(certifications=[self.cert(date(2026, 3, 20), date(2029, 3, 19))]), REF)
        assert res.status == D.VERIFIED_SATISFIED
        assert "not been externally verified" in res.explanation

    def test_expired_before_reference_date(self):
        res = rules.evaluate(self.r(), ev(certifications=[self.cert(date(2023, 3, 15), date(2026, 3, 14))]), REF)
        assert res.status == D.VERIFIED_NOT_SATISFIED
        assert "renewed" in res.gaps[0]

    def test_expiry_on_reference_date_is_still_valid(self):
        assert rules.evaluate(self.r(), ev(certifications=[self.cert(date(2024, 1, 1), REF)]), REF).status == D.VERIFIED_SATISFIED

    def test_missing_validity_needs_review(self):
        assert rules.evaluate(self.r(), ev(certifications=[self.cert(None, None)]), REF).status == D.NEEDS_HUMAN_REVIEW

    def test_no_certificate_is_insufficient(self):
        assert rules.evaluate(self.r(), ev(), REF).status == D.INSUFFICIENT_EVIDENCE

    def test_other_standard_ignored(self):
        c = fact(kind="certification", cert_type="ISO 14001", valid_from=date(2025, 1, 1), valid_until=date(2030, 1, 1))
        assert rules.evaluate(self.r(), ev(certifications=[c]), REF).status == D.INSUFFICIENT_EVIDENCE


class TestExperience:
    def proj(self, value, done, title="Supply and commissioning of LT distribution panels"):
        return fact(kind="project", title=title, description=title, contract_value=Decimal(value) if value else None, completion_date=done, start_date=None)

    def sim(self, **kw):
        return req(req_type="experience", metric="similar_work_value", threshold=Decimal("15000000"), period_years=5, aggregation="count",
                   parameters={"count_required": 2, "keywords": ["distribution", "panels", "commissioning"]}, **kw)

    def test_two_qualifying_projects(self):
        e = ev(projects=[self.proj("18000000", date(2022, 11, 30)), self.proj("24000000", date(2024, 6, 20))])
        assert rules.evaluate(self.sim(), e, REF).status == D.VERIFIED_SATISFIED

    def test_value_below_threshold_excluded(self):
        e = ev(projects=[self.proj("14999999", date(2023, 1, 1)), self.proj("24000000", date(2024, 6, 20))])
        r = rules.evaluate(self.sim(), e, REF)
        assert r.status == D.INSUFFICIENT_EVIDENCE
        assert r.computation["projects"][0]["excluded_because"]

    def test_outside_window_excluded(self):
        e = ev(projects=[self.proj("18000000", date(2019, 1, 1)), self.proj("24000000", date(2024, 6, 20))])
        assert rules.evaluate(self.sim(), e, REF).status != D.VERIFIED_SATISFIED

    def test_weak_similarity_needs_review(self):
        e = ev(projects=[self.proj("18000000", date(2023, 1, 1), "Road resurfacing"), self.proj("24000000", date(2024, 6, 20), "Road resurfacing")])
        assert rules.evaluate(self.sim(), e, REF).status == D.NEEDS_HUMAN_REVIEW

    def test_years_in_business_from_incorporation(self):
        r = req(req_type="experience", metric="years_in_business", threshold=Decimal(3), parameters={})
        cin = fact(kind="registration", kind_=None, **{"kind": "cin", "issued_on": date(2014, 6, 12)}) if False else Fact(
            "registration", uuid.uuid4(), {"kind": "cin", "issued_on": date(2014, 6, 12)}, None, None, 1, "x", None, 0.9, False, "coi")
        assert rules.evaluate(r, ev(registrations=[cin]), REF).status == D.VERIFIED_SATISFIED

    def test_years_from_profile_only_needs_review(self):
        r = req(req_type="experience", metric="years_in_business", threshold=Decimal(3), parameters={})
        assert rules.evaluate(r, ev(profile={"year_established": 2010}), REF).status == D.NEEDS_HUMAN_REVIEW


class TestConditionalAndMisc:
    def test_msme_emd_exemption_is_never_auto_granted(self):
        r = req(req_type="emd", metric="emd", threshold=Decimal("500000"), mandatory="conditional",
                conditions=["Micro and Small Enterprises ... may be exempted ... subject to submission of a valid Udyam certificate"])
        udyam = Fact("registration", uuid.uuid4(), {"kind": "udyam", "identifier": "UDYAM-MH-26-0012345", "attributes": {"msme_category": "small"}}, None, None, 1, "", None, 0.9, False, "udyam")
        res = rules.evaluate(r, ev(registrations=[udyam]), REF)
        assert res.status == D.NEEDS_HUMAN_REVIEW
        assert "conditional" in res.explanation.lower()

    def test_rejected_requirement_not_applicable(self):
        assert rules.evaluate(req(review_status="rejected"), ev(), REF).status == D.NOT_APPLICABLE

    def test_unknown_requirement_needs_review(self):
        assert rules.evaluate(req(req_type="personnel", metric=None), ev(), REF).status == D.NEEDS_HUMAN_REVIEW

    def test_standard_mention_is_not_proof(self):
        r = req(req_type="technical", metric="standard_compliance", parameters={"standard": "IEC 61439; IS 8623"})
        hit = Fact("chunk", uuid.uuid4(), {}, uuid.uuid4(), uuid.uuid4(), 1, "IEC 61439", None, 0.6, False, "catalogue")
        res = rules.evaluate(r, ev(text_hits={"IEC 61439": [hit], "IS 8623": []}), REF)
        assert res.status == D.NEEDS_HUMAN_REVIEW and any("IS 8623" in g for g in res.gaps)


class TestSummary:
    def test_failed_mandatory_caps_readiness_and_status(self):
        score, overall, _ = rules.summarise([(D.VERIFIED_SATISFIED, "mandatory", "low")] * 9 + [(D.VERIFIED_NOT_SATISFIED, "mandatory", "high")])
        assert overall == "not_eligible" and score <= 40

    def test_never_eligible_with_unresolved_mandatory(self):
        _, overall, _ = rules.summarise([(D.VERIFIED_SATISFIED, "mandatory", "low"), (D.NEEDS_HUMAN_REVIEW, "mandatory", "medium")])
        assert overall == "needs_review"

    def test_eligible_only_when_all_mandatory_verified(self):
        _, overall, _ = rules.summarise([(D.VERIFIED_SATISFIED, "mandatory", "low"), (D.NOT_APPLICABLE, "mandatory", "low"), (D.NEEDS_HUMAN_REVIEW, "optional", "low")])
        assert overall == "eligible_per_policy"


@pytest.mark.parametrize("op,v,t,expected", [(">=", "5", "5", True), (">", "5", "5", False), ("<=", "4", "5", True), ("==", "5.00", "5", True)])
def test_compare(op, v, t, expected):
    assert rules._compare(op, Decimal(v), Decimal(t)) is expected
