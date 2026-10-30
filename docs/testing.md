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
* **Security** — see `docs/security.md`.
* **Browser e2e** (`tests/e2e`, marker `e2e`) — Playwright journey against a running server:
  `uv run playwright install chromium`, start the app (`make dev` + `make worker`, seeded), then `make e2e`.
