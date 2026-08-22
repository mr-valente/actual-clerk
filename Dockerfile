# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.12-slim-bookworm AS runtime

ARG ACTUAL_CLERK_VERSION=0.1.0

LABEL org.opencontainers.image.title="Actual Clerk" \
      org.opencontainers.image.description="A budgeting assistant sidecar for Actual Budget" \
      org.opencontainers.image.version="${ACTUAL_CLERK_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    ACTUAL_CLERK_VERSION=${ACTUAL_CLERK_VERSION} \
    CLERK_DATA_DIR=/app/data \
    CLERK_HOST=0.0.0.0 \
    CLERK_PORT=8080 \
    PATH=/app/.venv/bin:$PATH

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    && mkdir -p /app/data

EXPOSE 8080
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('CLERK_PORT','8080')+'/api/health', timeout=3)"

ENTRYPOINT ["actual-clerk"]
