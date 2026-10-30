.PHONY: install dev worker beat migrate seed samples test lint typecheck eval e2e up down

install:
	uv sync
dev:
	uv run uvicorn app.main:app --reload --port 8080
worker:
	uv run celery -A app.workers.celery_app worker -Q default,documents,ai,notifications --pool=threads -c 4 --loglevel=INFO
beat:
	uv run celery -A app.workers.celery_app beat --loglevel=INFO
migrate:
	uv run alembic upgrade head
seed:
	uv run tenderpilot seed
samples:
	uv run tenderpilot generate-samples
test:
	uv run pytest -m "not e2e"
lint:
	uv run ruff check app tests
typecheck:
	uv run mypy
eval:
	uv run tenderpilot eval
e2e:
	uv run pytest -m e2e tests/e2e
up:
	docker compose up --build
down:
	docker compose down
