"""row-level security: non-owner app role + tenant isolation policies on every organization-scoped table

Revision ID: b7a1c2d3e4f5
Revises: b2deb3125ea0
Create Date: 2026-10-31 11:00:00

The application connects as the schema owner but switches each transaction to ``tenderpilot_app`` (no ownership, no
BYPASSRLS), so these policies apply to it. Migrations keep running as the owner and are not filtered (tables are not
FORCE'd), which keeps data migrations possible on managed Postgres where the owner is not a superuser.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b7a1c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = "b2deb3125ea0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE = "tenderpilot_app"
ALLOW = (
    "current_setting('app.rls_bypass', true) = 'on' "
    "OR {col} = NULLIF(current_setting('app.org_id', true), '')::uuid"
)


def _tenant_tables(conn) -> list[str]:
    rows = conn.exec_driver_sql(
        "SELECT table_name FROM information_schema.columns c JOIN information_schema.tables t USING (table_schema, table_name) "
        "WHERE c.table_schema = 'public' AND c.column_name = 'organization_id' AND t.table_type = 'BASE TABLE' ORDER BY 1"
    ).all()
    return [r[0] for r in rows]


def upgrade() -> None:
    conn = op.get_bind()
    op.execute(f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
            CREATE ROLE {ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
          END IF;
        END $$;
    """)
    # The migrating (= connecting) user must be a member to SET ROLE (needed on RDS, where it is not a superuser).
    op.execute(f"GRANT {ROLE} TO CURRENT_USER")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {ROLE}")
    for table in _tenant_tables(conn):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        expr = ALLOW.format(col="organization_id")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} TO {ROLE} USING ({expr}) WITH CHECK ({expr})")
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON organizations")
    expr = ALLOW.format(col="id")
    op.execute(f"CREATE POLICY tenant_isolation ON organizations TO {ROLE} USING ({expr}) WITH CHECK ({expr})")


def downgrade() -> None:
    conn = op.get_bind()
    for table in [*_tenant_tables(conn), "organizations"]:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
