# syntax=docker/dockerfile:1.7

# Multi-stage build for qb-service. The builder stage uses uv to resolve and
# install the locked dependency set into a project-local virtualenv; the
# runtime stage copies that virtualenv into a minimal slim image and runs
# as a non-root user. The image listens on $PORT (default 8080) so Cloud
# Run can route to it.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.5.11

# ARG only expands in FROM lines, not in COPY --from (even under BuildKit) —
# alias the uv image as a stage so the version stays parameterized.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------- builder ----------
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Resolve dependencies first (no project source yet) so layer caching survives
# code-only and docs-only changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra gcp

# README.md is referenced by pyproject.toml; needed at project-install time.
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra gcp

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim AS runtime

RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --home-dir /home/app --create-home app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

USER app

EXPOSE 8080

# Cloud Run injects $PORT; honor it but default to 8080 for local runs.
CMD ["sh", "-c", "exec uvicorn qbsvc.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
