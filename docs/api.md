# REST API (`/api/v1`)

Interactive OpenAPI docs: `http://localhost:8080/api/docs` (schema at `/api/v1/openapi.json`).

## Authentication

* **Browser session** — `POST /api/v1/auth/login {email, password}` sets an HttpOnly `tp_session` cookie and returns a
  `csrf_token`. Every non-GET request authenticated by cookie must send `X-CSRF-Token: <csrf_token>`.
* **API token** — create one in Settings → API tokens (or `POST /api/v1/api-tokens`), then send
  `Authorization: Bearer tpk_…`. Tokens are bound to one user and organization; optional scopes narrow the role.

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/tenders?sort=deadline
```

## Asynchronous work

Long operations never hold a request open. Endpoints return immediately with a resource and/or `job_id`; poll:

```
GET /api/v1/jobs/{job_id}     → {status: queued|running|retrying|succeeded|failed|cancelled, progress, stage, error}
POST /api/v1/jobs/{job_id}/cancel
```

Document uploads (`POST /documents`, `POST /tenders/{id}/documents`) return `job_id`s; `POST /tenders/{id}/assessments`
returns `202` with the assessment (status `running` → `awaiting_review`/`completed`).

## Errors

All errors share one shape and carry the request correlation id (also in the `X-Request-ID` header):

```json
{"error": {"code": "permission_denied", "message": "Your role does not allow 'bids:approve'.", "details": null, "request_id": "…"}}
```

Codes: `unauthenticated` 401, `permission_denied`/`csrf_failed` 403, `not_found` 404, `conflict`/`invalid_transition` 409
(including optimistic-lock conflicts — send the `lock_version` you read), `validation_failed` 422, `quota_exceeded` 402,
`rate_limited` 429, `service_unavailable` 503.

## Endpoint groups

| Group | Main endpoints |
|---|---|
| Auth & orgs | `/auth/register`, `/auth/login`, `/auth/logout`, `/auth/session`, `/auth/verify-email`, `/auth/password-reset/*`, `/organizations`, `/organizations/{id}/switch`, `/members`, `/members/invitations`, `/invitations/accept`, `/api-tokens` |
| Company & documents | `/company/profile`, `/documents` (multipart upload, list with filters/pagination), `/documents/uploads/initiate` + `/complete` (presigned S3), `/documents/{id}/versions`, `/document-versions/{id}/pages`, `/fields`, `/download`, `/pages/{n}/image`, `/documents/{id}/category`, `/document-fields/{id}/confirm` |
| Tenders | `/tenders` (create, list/filter/sort/paginate), `/tenders/{id}` (PATCH with `lock_version`), `/tenders/{id}/documents`, `/tenders/import-url`, `/tender-sources`, `/tender-sources/{key}/import` (`csv_feed`, `feed_file`, `email_alert`), `/tender-feeds` (list/create, `/{id}/poll`, `/{id}/pause`, `/{id}/resume`, DELETE), `/email-alerts` (`/enable` returns the inbound URL once, `/disable`, `/upload`), `POST /inbound/email/{token}` (unauthenticated webhook; raw RFC 822 body or multipart field `email`/`body-mime`/`message`), `/tenders/{id}/transition`, `/tenders/{id}/requirements`, `/requirements/{id}/review`, `/tenders/{id}/amendments`, `/opportunities` |
| Eligibility & evidence | `/tenders/{id}/assessments`, `/assessments/{id}` (full report), `/assessments/{id}/graph`, `/decisions/{id}/review`, `/conflicts/{id}/resolve`, `/assessments/{id}/complete-review` (resumes the paused workflow), `/assessments/{a}/compare/{b}` |
| Bids & BOQ | `/tenders/{id}/workspace`, `/workspaces`, `/workspaces/{id}/tasks`, `/tasks/{id}`, `/workspaces/{id}/comments`, `/workspaces/{id}/drafts/generate`, `/workspaces/{id}/preparation` (POST starts the bid-preparation agent, GET shows its latest run), `/preparation/{run_id}/decision` (`submit` | `keep_draft`), `/drafts/{id}`, `/workspaces/{id}/approval`, `/workspaces/{id}/export`, `/workspaces/{id}/artifacts`, `/workspaces/{id}/timeline`, `/tenders/{id}/boq`, `/boq/{id}/items`, `/boq/{id}/scenarios`, `/boq/scenarios/{id}/export` |
| Copilot | `/copilot/conversations`, `/copilot/conversations/{id}/messages`, `/copilot/conversations/{id}/ask`, `/copilot/messages/{id}/actions/{i}/approve` |
| Operations | `/notifications`, `/notifications/read`, `/notifications/preferences/{kind}`, `/analytics/dashboard`, `/analytics/ai-costs`, `/usage`, `/billing/plan-change`, `/billing/history`, `/audit`, `/organization/deletion`, `/admin/organizations/{id}/suspend`, `/admin/organizations/{id}/apply-plan-change` |

Health: `GET /healthz` (liveness), `GET /readyz` (database, Redis, storage, LLM, antivirus and OCR state).
