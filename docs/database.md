# Database schema

PostgreSQL 16+ with `vector` (pgvector) and `pg_trgm`. Migrations: `uv run alembic upgrade head` (works on an empty
database; verified in CI and the test suite, which rebuilds the schema from scratch each run).

## Tenancy model

Every tenant-owned table has an indexed, non-null `organization_id` FK (`TenantMixin`) with `ON DELETE CASCADE`.
Services always filter by the `organization_id` of the server-resolved `TenantContext`; IDs from clients are only
ever looked up *within* that organization, so a foreign ID returns 404. Row-level security is not enabled in this
release (see Security → limitations); isolation is enforced in the service layer and covered by tests.

## Entities

| Area | Tables |
|---|---|
| Identity | `users`, `organizations`, `organization_members` (role), `invitations`, `user_sessions` (hashed token, CSRF token, active org), `auth_tokens` (verify/reset), `api_tokens` (hashed, scoped) |
| Company facts | `company_profiles`, `company_certifications`, `company_financial_records`, `company_projects`, `company_products`, `company_registrations` — each with source version/page/text/bbox, confidence, status (`extracted`/`confirmed`/`rejected`/`superseded`) |
| Documents | `documents`, `document_versions` (immutable, sha256, processing state, quality notes, validity dates), `document_pages` (text, blocks with bbox, tables), `document_chunks` (`vector(384)` HNSW cosine index + generated `tsvector` GIN index), `document_fields` (confirmable metadata), `processing_jobs` |
| Tenders | `tenders` (optimistic `lock_version`), `tender_versions` (snapshots), `tender_documents` (role), `tender_requirements` (versioned, `is_current`, `superseded_by_id`, fingerprint), `tender_amendments` (field/requirement diffs, impact), `tender_status_events`, `tender_matches`, `saved_searches` |
| Evidence graph | `eligibility_assessments` (run number, reference date, evidence snapshot), `eligibility_decisions` (`status` + immutable `machine_status`), `requirement_evidence_links` (edge with excerpt/page/bbox/relation/origin), `evidence_conflicts`, `review_events` |
| Bids | `bid_workspaces`, `bid_tasks` (dedup keys for idempotent system tasks, history, lock version), `bid_comments`, `bid_drafts` (versioned sections with citations), `bid_approvals`, `bid_artifacts` (generated files + evidence snapshot), `boq_imports`, `boq_items`, `boq_scenarios` |
| AI & MCP | `agent_runs` (thread id, steps, tokens, cost, history, interrupt payload), `ai_usage_records`, `tool_execution_logs`, `copilot_conversations`, `copilot_messages`, `integration_connections` (encrypted credentials). LangGraph checkpoints live in the library's own `checkpoints*` tables. |
| Operations | `notifications` (dedup per user, email delivery status), `notification_preferences`, `audit_events`, `plans`, `subscriptions`, `billing_events`, `usage_counters` |

## Conventions

UUID primary keys; timestamps are `timestamptz` and sessions run with `timezone=UTC`; money is `NUMERIC(20,2)`
(BOQ quantities/rates `NUMERIC(20,4)`) mapped to Python `Decimal`; JSONB only for genuinely variable structures
(computation traces, snapshots, settings).
