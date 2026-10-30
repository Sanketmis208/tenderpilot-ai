# Implementation & verification report

Status as of 30 Oct 2026. This document is the progress record required by the brief; it states what was actually
exercised and what was not.

## Verification performed

| Check | Result |
|---|---|
| Unit + integration + security + evaluation tests (`pytest -m "not e2e"`) | **131 passed** locally (PostgreSQL 18 + pgvector 0.8.2) and on GitHub Actions (pgvector/pg16) |
| Browser e2e (Python Playwright, Chromium, live server + Celery worker) | **4 passed** — full user journey, bad-password path, auth redirect, cross-tenant URL |
| End-to-end §34 scenario (API-level, `tests/integration/test_demo_scenario.py`) | passed — see docs/testing.md for the steps |
| Lint (ruff) / type check (mypy on core, services, schemas, auth) | clean |
| Alembic migrations on an empty database | verified every test run and in CI |
| Docker image build | succeeded in CI (`docker compose up` itself not run locally — Docker is not installed on the dev machine) |
| Terraform (`infra/aws`) | `terraform validate` passes; **never planned or applied** against an AWS account |
| MCP over streamable HTTP | exercised with curl and tests (401 without token, tenant-scoped results with token, DNS-rebinding rejection) |
| Manual UI review (in-app browser, 1440×900) | landing, login, dashboard, tender detail, eligibility report, evidence graph, document viewer with highlight |

Bugs found and fixed by the tests (worth knowing): turnover average rounded *before* threshold comparison (could pass
₹4,99,99,999.997 against ₹5 Cr); heading detection was case-sensitive so chunks inherited page banners; BOQ items not
flushed before follow-up queries; older assessment runs only superseded at finalisation; DB sessions returned local
time zones; Celery prefork crash on macOS (local dev now uses the threads pool); local Redis DB 0 was shared with
another project (now DB 5).

## Module status

| Module | Status |
|---|---|
| A. Auth & multi-tenancy | Done: registration, login/logout, Argon2, sessions (TTL + idle), email verification & reset (token flows; email via SMTP when configured), orgs, switching, invitations (expiry/revoke), six roles, API tokens with scopes, suspension, deletion with purge job |
| B. Onboarding & vault | Done: 4-step wizard, uploads (PDF/DOCX/XLSX/CSV/TXT/ZIP), classification with confidence, fact extraction with provenance, field/fact confirmation, immutable versions, expiry warnings |
| 4. Ingestion | Done: upload, manual entry, SSRF-safe URL import, CSV feed. CPPP/GeM/state portals deliberately **unsupported** (no authorised API) and labelled so |
| 5–6. Document & requirement intelligence | Done. LLM extractor exercised with **Groq** (`openai/gpt-oss-120b`): rules + LLM merge, citation verification, evaluation in docs/ai.md. Anthropic backend implemented but not exercised (no key). Textract adapter implemented, **not exercised** (no AWS credentials); scanned pages are flagged, not silently skipped |
| 7–8. Evidence graph & rule engine | Done, the most thoroughly tested part |
| 9. Matching | Done (transparent weighted scoring; lexical hashing embeddings unless an embeddings endpoint is configured) |
| 10. LangGraph | Done: tender-analysis workflow with Postgres checkpoints, interrupt/resume, budgets, cancellation; copilot graph |
| 11. MCP | Done: 18 tools, HTTP + stdio + in-process; connectors are boundaries only (local-folder demo works) |
| 12–13. RAG & copilot | Done; LLM mode exercised with Groq (tool calls over MCP, grounded answers, filtered citations); rule mode labelled. Streaming is shown as tool-progress updates, not token streaming |
| 14–16. Bids, generation, BOQ | Done: tasks/comments/drafts/approvals with optimistic locking and four-eyes rule; DOCX/XLSX/PDF/ZIP exports verified to open; BOQ analysis, Decimal scenarios, formula-preserving export |
| 17. Corrigenda | Done: versioned requirements, field and clause diffs, impact tasks, approval reopening policy, auto re-assessment |
| 18. Notifications | Done: in-app, preferences, dedup, scheduled reminders, honest email status. SMTP delivery not exercised (no provider) |
| 19. Analytics | Done from persisted data; empty states for fresh orgs |
| 20. SaaS admin | Plans/entitlements/quotas/usage counters/billing history/plan-change requests. **No payment processor** — requests await an admin, nothing is reported as paid |
| 21. UI | All 12 navigation areas + landing/auth/onboarding/viewer/search. Responsive drawer; not formally audited for WCAG |
| 22–26. Data, API, storage, workers, security | Done; see architecture, API and security docs |
| 32. Deployment | Dockerfile, Compose stack, Terraform, CI + manual deploy workflow — deployment itself not performed |

## Credentials / resources required to go further

Anthropic API key (LLM extraction, copilot, evaluation of the LLM path) · AWS account (S3, Textract, ECS, RDS) and a
domain + ACM certificate · SMTP provider · optional embeddings endpoint · a payment processor contract.

## Known limitations

* Hindi: detected and flagged for review; rule extraction is English-only.
* Rule-based extraction covers common NIT/RFP clause shapes; unusual phrasing needs the LLM extractor or manual entry.
* `hashing-v1` embeddings are lexical, not semantic.
* No antivirus engine, no Postgres RLS, no MFA/SSO (see docs/security.md).
* NiceGUI keeps per-connection state in the web process: horizontal scaling needs sticky sessions (configured on the ALB).
* Evaluation dataset is small and author-written (docs/ai.md).

## Next actions

1. Run `tenderpilot eval` on real (anonymised) tenders and compare rules vs Groq vs Claude.
2. Grow the evaluation set with real tenders and reviewer-labelled answers.
3. Add ClamAV/GuardDuty malware scanning and Postgres RLS.
4. `terraform plan` in a staging account; run the Deploy workflow; add error tracking and alarms.
