# Security considerations

## Controls implemented (and tested in `tests/security`)

| Area | Control |
|---|---|
| Passwords | Argon2id, strength policy, lockout after 8 failures (15 min), constant-ish timing for unknown users, identical error messages (no enumeration) |
| Sessions | Random tokens stored as keyed HMAC hashes; HttpOnly + SameSite=Lax cookie (`Secure` via `TP_COOKIE_SECURE`); absolute TTL and idle timeout; all sessions revoked on password reset |
| CSRF | Per-session token required on cookie-authenticated mutations (`X-CSRF-Token`); HTML auth forms check Origin/Referer |
| AuthZ | Code-defined RBAC (6 roles); server-side checks in every service; no role escalation (cannot grant above own role, last owner protected); four-eyes rule on approvals; API-token scopes |
| Tenancy | Org taken only from the verified context; foreign IDs 404; retrieval, MCP tools, copilot and downloads scoped and tested cross-tenant. **Postgres row-level security** underneath: the app runs as the non-bypassing `tenderpilot_app` role with per-transaction `app.org_id`, so a query that forgets its organization filter still only sees (and can only write) the bound tenant's rows; unscoped code sees nothing (`tests/security/test_rls.py`) |
| Uploads | Size limits and quotas, content sniffing (not client MIME), legacy/macro/JS-PDF rejection, ZIP traversal/absolute path/symlink/bomb/nesting protection |
| Malware | ClamAV (clamd INSTREAM) when `TP_CLAMAV_HOST` is set: infected files rejected, scanner outage fails closed (`TP_CLAMAV_FAIL_CLOSED`); every upload's audit record states `clean` / `not_scanned`; `/readyz` reports the engine state. ClamAV runs in the Compose stack |
| Downloads | Tenant-prefixed randomized keys; S3 presigned (5 min) or HMAC-signed expiring local URLs; download issuance audited |
| SSRF | URL import: http(s) only, default ports, no credentials, every resolved IP checked (private, loopback, link-local, metadata, CGNAT), connection pinned to the vetted IP, manual redirect re-validation, size cap, optional allowlist |
| Web | Security headers (nosniff, frame DENY, referrer policy, HSTS when secure, CSP on API), rate limiting (per IP, auth, copilot, tools), request IDs |
| Secrets | Env/Secrets Manager only; integration credentials AES-256-GCM encrypted; log redaction of token/password/secret keys |
| AI | Prompt-injection guard + untrusted-data tagging, verbatim citation verification, tool allowlist with strict schemas, mutations need approval/scope, per-run step/token/cost budgets, org AI quota |
| Audit | Append-only `audit_events` for auth, membership, documents, reviews, approvals, exports, tool mutations |
| Retention | Org deletion request → 30-day grace → purge job deletes rows (cascade) and stored objects; retrieval query logs keep only a SHA-256 of each query (raw text opt-in) and are purged after `TP_RETRIEVAL_LOG_RETENTION_DAYS` |
| Agents | Bid preparation and tender analysis pause (`interrupt`) before consequential steps; resume re-checks the resuming user's current permissions; a paused run cannot act for a member who has since been removed |

## Known limitations / follow-ups

* **Antivirus is optional** — without `TP_CLAMAV_HOST` uploads are only checked heuristically (and are recorded as
  `not_scanned`). Enable ClamAV (included in Compose; on AWS run it as a sidecar/service or use GuardDuty Malware
  Protection for S3) before accepting untrusted public uploads.
* **RLS bypass scope** — identity resolution before a tenant is known (login, session/API-token lookup, invitations),
  scheduled maintenance, seeding/CLI and platform-admin actions run in an explicit `system_scope()` (grep for
  `system_scoped` / `system_scope`). These blocks are small and reviewed; the bypass is never inherited by a session.
  RLS protects against application query bugs, not against SQL injection that could set `app.rls_bypass` itself (all
  SQL is parameterised; there is no dynamic SQL from user input).
* Raw engine connections that bypass the ORM `Session` (only the `/readyz` `SELECT 1` today) and LangGraph's checkpoint
  connection run as the owner; checkpoint tables hold no organization column.
* **NiceGUI pages** rely on the session cookie for page access and run actions server-side over the websocket; the CSRF
  token protects the REST API. NiceGUI requires `unsafe-eval`, so no strict CSP is applied to UI pages.
* Email verification is optional (`TP_EMAIL_VERIFICATION_REQUIRED`); enable it in production.
* No MFA/SSO yet.
* India's DPDP Act obligations (consent notices, data principal rights, breach notification, cross-border transfer) need
  legal review; provisions have staggered commencement dates and are not encoded as product logic here. Disclose to
  customers which data is sent to external model providers (Anthropic) and embedding/OCR providers.
