# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
WORKDIR /app

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

FROM base AS runtime
RUN useradd --create-home --uid 10001 tenderpilot
COPY --from=deps /app/.venv /app/.venv
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini pyproject.toml ./
COPY scripts ./scripts
COPY sample_data ./sample_data
ENV PATH="/app/.venv/bin:$PATH" TP_LOCAL_STORAGE_PATH=/app/var/storage
RUN mkdir -p /app/var && chown -R tenderpilot /app/var
USER tenderpilot
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"
# Web/API process. Workers override the command (see docker-compose.yml / ECS task definitions).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
