"""Management CLI: `uv run tenderpilot <command>`."""

from __future__ import annotations

import argparse
import asyncio


def main() -> None:
    p = argparse.ArgumentParser(prog="tenderpilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="Load the synthetic demo organizations, documents and tenders")
    sub.add_parser("generate-samples", help="Regenerate the synthetic sample documents")
    mk = sub.add_parser("create-admin", help="Promote a user to platform administrator")
    mk.add_argument("email")
    tok = sub.add_parser("create-token", help="Create an API token for a user in an organization")
    tok.add_argument("email")
    tok.add_argument("--org", required=True)
    tok.add_argument("--write", action="store_true")
    sub.add_parser("mcp-stdio", help="Run the MCP server over stdio (auth via TENDERPILOT_API_TOKEN)")
    sub.add_parser("eval", help="Run the AI/extraction evaluation suite")
    a = p.parse_args()
    if a.cmd == "seed":
        from app.db.session import session_scope
        from app.services.demo.seed import DEMO_PASSWORD, seed

        with session_scope() as db:
            r = seed(db)
        print("Seeded organizations:", ", ".join(o.name for o in r.orgs.values()))
        print("Demo users (password from TP_DEMO_PASSWORD, default shown in .env.example):")
        for email, c in r.contexts.items():
            print(f"  {email:30s} {c.role}")
        _ = DEMO_PASSWORD
    elif a.cmd == "generate-samples":
        import runpy

        runpy.run_path("scripts/generate_sample_docs.py", run_name="__main__")
    elif a.cmd == "create-admin":
        from sqlalchemy import select

        from app.db.models import User
        from app.db.session import session_scope

        with session_scope() as db:
            u = db.scalar(select(User).where(User.email == a.email.lower()))
            if not u:
                raise SystemExit("No such user")
            u.is_platform_admin = True
        print("ok")
    elif a.cmd == "create-token":
        from sqlalchemy import select

        from app.auth.context import TenantContext
        from app.db.models import Organization, OrganizationMember, User
        from app.db.session import session_scope
        from app.services.organizations.auth_service import create_api_token

        with session_scope() as db:
            u = db.scalar(select(User).where(User.email == a.email.lower()))
            org = db.scalar(select(Organization).where(Organization.name.ilike(f"%{a.org}%")))
            m = db.scalar(select(OrganizationMember).where(OrganizationMember.user_id == u.id, OrganizationMember.organization_id == org.id)) if u and org else None
            if not m:
                raise SystemExit("User is not a member of that organization")
            _, raw = create_api_token(db, TenantContext(u.id, org.id, m.role), "cli", ["mcp:write", "*"] if a.write else [])
        print(raw)
    elif a.cmd == "mcp-stdio":
        from app.mcp.server.stdio import run_stdio

        asyncio.run(run_stdio())
    elif a.cmd == "eval":
        from app.ai.evaluations.run import main as run_eval

        run_eval()


if __name__ == "__main__":
    main()
