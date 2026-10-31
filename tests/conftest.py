"""Test configuration: dedicated PostgreSQL database, fresh migrations, eager jobs, temp storage."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Must be set before any app module reads settings.
os.environ.setdefault("TP_DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://localhost/tenderpilot_test"))
os.environ["TP_CELERY_EAGER"] = "true"
os.environ["TP_REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
os.environ["TP_LOCAL_STORAGE_PATH"] = tempfile.mkdtemp(prefix="tp-test-storage-")
os.environ["TP_STORAGE_BACKEND"] = "local"
os.environ["TP_ENVIRONMENT"] = "test"
os.environ["TP_SECRET_KEY"] = "test-secret-key-0123456789-abcdefghijklmnop"
os.environ["TP_RATE_LIMIT_PER_MINUTE"] = "100000"
os.environ["TP_AUTH_RATE_LIMIT_PER_MINUTE"] = "100000"
# Tests never call paid providers, even if a developer's .env contains keys (env vars override .env).
os.environ["TP_ANTHROPIC_API_KEY"] = ""
os.environ["TP_GROQ_API_KEY"] = ""

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _reset_database() -> None:
    from alembic import command
    from alembic.config import Config

    url = os.environ["TP_DATABASE_URL"]
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def database():
    if not (ROOT / "sample_data" / "tenders" / "msgic_lt_panels_nit.pdf").exists():
        import runpy

        runpy.run_path(str(ROOT / "scripts" / "generate_sample_docs.py"), run_name="__main__")
    _reset_database()
    import app.workers.tasks as tasks

    tasks._load_handlers()
    yield


@pytest.fixture(scope="session")
def seeded(database):
    """The full synthetic demo (two organizations, documents, three tenders), processed."""
    from app.db.session import session_scope
    from app.services.demo.seed import seed

    with session_scope() as db:
        return seed(db)


@pytest.fixture(autouse=True)
def _fresh_tenant_scope():
    """Row-level-security scope is per execution context; never let one test inherit another's tenant."""
    from app.db import tenancy

    tenancy.reset()
    yield
    tenancy.reset()


@pytest.fixture
def db():
    from app.db.session import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture(scope="session")
def client(database):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def login(client, email: str, password: str = "Demo@Pass2026"):
    """Fresh cookie-authenticated client session; returns (client_cookies, csrf headers)."""
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c, {"X-CSRF-Token": r.json()["csrf_token"]}
