# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12-python3.12-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.12-slim-trixie

RUN groupadd --system fcast && useradd --system --gid fcast --home /app fcast \
    && mkdir -p /data && chown fcast:fcast /data

COPY --from=builder --chown=fcast:fcast /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FCAST_DB_PATH=/data/fcast.db

USER fcast
WORKDIR /app
VOLUME ["/data"]

ENTRYPOINT ["fcast"]
EXPOSE 8000
CMD ["serve"]
