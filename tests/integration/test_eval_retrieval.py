"""Retrieval and agent-run evaluation gates (need the database)."""

from __future__ import annotations


def test_retrieval_eval_gate_and_rollback(db):
    from sqlalchemy import func, select

    from app.ai.evaluations.run import eval_retrieval
    from app.db.models import Organization

    before = db.scalar(select(func.count()).select_from(Organization))
    r = eval_retrieval(db)
    assert r["hit_at_1"] >= 0.8 and r["mrr"] >= 0.85 and r["recall_at_3"] >= 0.9
    assert db.scalar(select(func.count()).select_from(Organization)) == before  # throwaway org rolled back


def test_agent_run_report(seeded, db):
    from app.ai.evaluations.run import eval_agent_runs

    out = eval_agent_runs(db)
    assert all(v["completion_rate"] is None or 0 <= v["completion_rate"] <= 1 for v in out.values())
