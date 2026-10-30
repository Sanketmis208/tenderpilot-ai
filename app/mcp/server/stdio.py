"""MCP over stdio for local agent clients (e.g. Claude Desktop/Code). Identity comes from TENDERPILOT_API_TOKEN."""

from __future__ import annotations

import os
import sys

from app.db.session import session_scope
from app.mcp.server.app import channel_var, current_identity, mcp_server
from app.services.organizations.auth_service import resolve_api_token


async def run_stdio() -> None:
    token = os.environ.get("TENDERPILOT_API_TOKEN", "")
    with session_scope() as db:
        ctx = resolve_api_token(db, token, channel="mcp_stdio")
    if ctx is None:
        print("TENDERPILOT_API_TOKEN is missing or invalid.", file=sys.stderr)
        raise SystemExit(2)
    current_identity.set(ctx)
    channel_var.set("mcp_stdio")
    await mcp_server.run_stdio_async()
