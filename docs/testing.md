# Testing

```bash
make test                         # everything except browser e2e
uv run pytest tests/unit          # pure logic: rules, extraction, normalisation, security units, BOQ, eval gates
uv run pytest tests/integration   # DB + jobs + storage + MCP + copilot + full §34 scenario
uv run pytest tests/security      # cross-tenant, CSRF, roles, uploads, SSRF, sessions, MCP auth
uv run pytest --cov=app           # coverage
```

The suite needs PostgreSQL with pgvector (default `postgresql+psycopg://localhost/tenderpilot_test`, override with
`TEST_DATABASE_URL`) and optionally Redis (`TEST_REDIS_URL`; rate limiting falls back to in-process). The schema is
dropped and rebuilt via Alembic at session start, jobs run eagerly, storage goes to a temp directory, and no LLM key is
used, so tests need no paid services.

## What is covered

* **End-to-end scenario** (`tests/integration/test_demo_scenario.py`) — the VoltEdge journey from the specification
  through the REST API: extraction with sources → assessment paused for review → conflict/expiry/missing-evidence
  detection → reviewer actions and workflow resume → remediation tasks (idempotent) → grounded drafts → four-eyes
  approval → export package (DOCX/XLSX/PDF/ZIP opened and hash-verified) → replacement certificate as a new version →
  re-run with history preserved → corrigendum diff and impact → cross-tenant denial.
* **Bid-preparation agent** (`test_bid_preparation.py`) — interrupt, idempotent re-start, keep-draft and submit resumes,
  cross-tenant denial, unsupported-claim detector.
* **RAG operations** (`test_retrieval_ops.py`, `test_eval_retrieval.py`) — reranker modes, hashed query log and
  retention purge, incremental reindex, retrieval quality gate (hit@1, MRR, recall@3) on an isolated corpus.
* **MCP tools & collaboration** (`test_mcp_tools_and_collaboration.py`) — every read tool, mutation gating by
  approval/scope and role, argument validation (no caller-supplied org ids), task locking/history/assignment rules.
* **Streaming** (`tests/unit/test_llm_streaming.py`) — SSE reassembly of text, tool-call fragments and usage.
* **Security** — see `docs/security.md`; also `tests/security/test_antivirus.py` (clamd protocol against a fake clamd,
  EICAR rejection, fail-closed) and `tests/security/test_url_fetch.py` (DNS pinning, redirect-to-internal blocking).
* **Browser e2e** (`tests/e2e`, marker `e2e`) — Playwright journey against a running server:
  `uv run playwright install chromium`, start the app (`make dev` + `make worker`, seeded), then `make e2e`.
