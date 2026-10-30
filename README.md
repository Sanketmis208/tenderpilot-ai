# TenderPilot AI

**Discover Smarter. Qualify Faster. Bid with Confidence.**

TenderPilot AI is a Python-first B2B SaaS platform for Indian suppliers and contractors. A company uploads its
qualification documents once; TenderPilot reads each tender, extracts every eligibility condition with its source
page, checks it against the company's own evidence using a deterministic rule engine, and prepares an auditable,
reviewable bid package for **external** submission through the official portal.

The core differentiator is the **Requirements-to-Evidence Graph**: every requirement links to its tender clause
(document, page, verbatim text, bbox), its structured interpretation, the exact company document *version*, page and
passage supporting or contradicting it, a decision state, and its review history.

> TenderPilot does not submit bids, log in to procurement portals, bypass CAPTCHAs, apply digital signatures, or
> verify registrations with government registries. Generated content is not legal advice.

## Stack

Python 3.12 · uv · FastAPI · NiceGUI 3 · Pydantic v2 · SQLAlchemy 2 + Alembic · PostgreSQL + pgvector · Celery + Redis ·
LangGraph (Postgres checkpoints) · official MCP Python SDK v2 · boto3 (S3, Textract) · PyMuPDF / pdfplumber /
python-docx / openpyxl / reportlab · structlog · pytest · Playwright · Docker · Terraform (AWS).

## Quick start (local, no cloud accounts needed)

Prerequisites: PostgreSQL 16+ with the `vector` extension available, Redis, and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env          # then set TP_SECRET_KEY
createdb tenderpilot && psql -d tenderpilot -c "create extension vector"
uv run alembic upgrade head
uv run tenderpilot seed       # synthetic demo orgs, documents and tenders
make worker                   # terminal 1: Celery worker
make dev                      # terminal 2: http://localhost:8080
```

Or with Docker: `docker compose up --build`, then `docker compose run --rm web tenderpilot seed`
(stack: Postgres+pgvector, Redis, MinIO, Mailpit, web, worker, beat).

Demo sign-in (synthetic users, password = `TP_DEMO_PASSWORD`, default in `.env.example`):

| User | Organization | Role |
|---|---|---|
| `owner@voltedge.example` | VoltEdge Industries (demo) | Owner |
| `bids@voltedge.example` | VoltEdge Industries (demo) | Bid Manager |
| `review@voltedge.example` | VoltEdge Industries (demo) | Reviewer |
| `docs@voltedge.example` | VoltEdge Industries (demo) | Contributor |
| `viewer@voltedge.example` | VoltEdge Industries (demo) | Viewer |
| `owner@kaveri.example` | Kaveri Infraprojects (demo) | Owner |

## Operating modes

* **demo** (default): when no LLM key is configured, extraction and the copilot use deterministic, *labelled*
  rule-based logic; embeddings use an offline lexical hashing embedder (`hashing-v1`, not semantic).
* **integrated**: real providers are expected; a missing provider surfaces as a service-state error instead of a
  silent fallback. Set `TP_ANTHROPIC_API_KEY` (Claude), `TP_EMBEDDING_PROVIDER=openai_compatible`, `TP_TEXTRACT_ENABLED`,
  `TP_STORAGE_BACKEND=s3`, `TP_SMTP_HOST`.

## Common commands

```bash
make test        # unit + integration + security + evaluation gates (needs local Postgres)
make lint        # ruff
make typecheck   # mypy
make eval        # AI/extraction evaluation report → var/eval_report.json
make e2e         # Playwright browser journey (server must be running)
```

## Documentation

| | |
|---|---|
| [Architecture](docs/architecture.md) | modules, diagrams, data flow, design decisions |
| [Database schema](docs/database.md) | entities and tenancy model |
| [API](docs/api.md) | REST usage, auth, async jobs, errors (OpenAPI at `/api/docs`) |
| [AI workflows](docs/ai.md) | extraction, rule engine, LangGraph, RAG, copilot, evaluation |
| [MCP integration](docs/mcp.md) | tools, auth, connecting clients |
| [Security](docs/security.md) | controls, threat model, limitations |
| [Deployment & AWS](docs/deployment.md) | Terraform, ECS, secrets, operations |
| [Testing](docs/testing.md) | suites and how to run them |
| [User guide](docs/user-guide.md) | end-user walkthrough |
| [Implementation report](docs/implementation-report.md) | honest status, verification and limitations |
