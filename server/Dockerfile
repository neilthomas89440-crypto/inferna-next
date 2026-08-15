FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

# curl for the compose healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY inferna_server ./inferna_server

EXPOSE 8000 9091

CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn inferna_server.main:app --host 0.0.0.0 --port 8000"]
