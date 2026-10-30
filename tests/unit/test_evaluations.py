"""AI evaluation thresholds act as regression gates in CI."""

from app.ai.evaluations.run import eval_extraction, eval_rules


def test_rule_extractor_quality_gate():
    r = eval_extraction()
    assert r["precision"] >= 0.9 and r["recall"] >= 0.9
    assert r["critical_field_accuracy"] >= 0.9 and r["citation_correctness"] == 1.0
    assert r["abstention_ok"], "must not extract requirements from unrelated or injected text"


def test_rule_engine_gate():
    assert eval_rules()["accuracy"] == 1.0
