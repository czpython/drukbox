# syntax=docker/dockerfile:1.7

FROM python:3.11-slim AS python-runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
ENV UV_LINK_MODE=copy

# API bind address. Override with UVICORN_HOST=127.0.0.1 for loopback-only.
ENV UVICORN_HOST=0.0.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --all-extras --no-install-project

COPY src/ ./src/
COPY alembic.ini ./alembic.ini
COPY alembic/ ./alembic/

RUN uv sync --frozen --no-dev --all-extras

RUN useradd --system --no-create-home --uid 1001 appuser

USER appuser

EXPOSE 8780

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8780/healthz', timeout=2)"

# No --host flag — it would override UVICORN_HOST.
CMD [".venv/bin/uvicorn", "api.app:app", "--port", "8780"]
