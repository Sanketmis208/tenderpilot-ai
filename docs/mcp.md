# MCP integration

TenderPilot ships an MCP server built on the official Python SDK v2 (`mcp.server.mcpserver.MCPServer`).

## Transports

| Transport | Endpoint | Identity |
|---|---|---|
| Streamable HTTP | `https://<host>/mcp/` (stateless, JSON responses) | `Authorization: Bearer tpk_…` API token, rejected at the ASGI guard if invalid and **re-verified on every tool call** |
| stdio | `uv run tenderpilot mcp-stdio` | `TENDERPILOT_API_TOKEN` env var, resolved once at start-up |
| In-process | used by the copilot | server-set `TenantContext` context variable |

Example client config (stdio):

```json
{"mcpServers": {"tenderpilot": {"command": "uv", "args": ["--directory", "/path/to/tenderpilot-ai", "run", "tenderpilot", "mcp-stdio"],
  "env": {"TENDERPILOT_API_TOKEN": "tpk_…"}}}}
```

Create a token: Settings → API tokens, or `uv run tenderpilot create-token you@example.com --org "Your Org"`.

## Tools

Read: `search_company_documents`, `get_company_profile`, `get_tender_details`, `get_tender_requirements`,
`search_supporting_evidence`, `get_document_metadata`, `get_certificate_validity`, `run_eligibility_rules` (dry run, not
persisted), `get_bid_workspace`, `list_missing_documents`, `get_catalogue_products`, `list_relevant_past_projects`,
`list_upcoming_deadlines`, `compare_tenders`.

Mutations (require an `mcp:write` token scope, or explicit user approval in the copilot): `create_remediation_task`,
`attach_evidence_to_requirement`, `save_draft_response`, `request_human_review`.

## Security model (`app/mcp/tools/registry.py`)

For every call: identity present → organization active → role permission → mutation grant → strict argument schema
(`extra="forbid"`, so an `organization_id` argument is rejected) → per-user rate limit → execution with the context's
organization → output minimisation (`untrusted_text` wrapping, truncation) → `tool_execution_logs` row written in an
independent transaction (so denied calls are recorded) → audit event for mutations. DNS-rebinding protection rejects
unexpected `Host`/`Origin` headers.

## External connectors

`app/integrations/connectors.py` defines the connector boundary. Only `local_folder` (a clearly synthetic demo
repository via `TP_CONNECTOR_LOCAL_FOLDER`) works; Google Drive, ERP and DMS connectors report *not configured* and
never return fabricated results.
