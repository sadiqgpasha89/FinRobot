# syntax=docker/dockerfile:1.7
# One image runs the API, the UI and the CLI: `finrobot serve`, `finrobot ui`, `finrobot ingest ...`.

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

# ---- build: resolve and install locked dependencies into a venv, then the project itself --------------------------------------
FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

# ---- runtime: only what is needed to run ------------------------------------------------------------------------------------
FROM base AS runtime
# WeasyPrint renders the report PDF through Pango; tini reaps children and forwards signals; curl serves the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b fonts-dejavu-core tini curl \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --create-home --home-dir /home/finrobot finrobot \
    && mkdir -p /data/output /data/cache && chown -R finrobot:finrobot /data
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    FINROBOT_ENV=production FINROBOT_LOG_JSON=1 FINROBOT_HOST=0.0.0.0 FINROBOT_UI_HOST=0.0.0.0 \
    FINROBOT_OUTPUT_DIR=/data/output FINROBOT_CACHE_DIR=/data/cache \
    MPLCONFIGDIR=/tmp/mpl XDG_CACHE_HOME=/tmp/.cache   # writable under a read-only root filesystem
USER finrobot
WORKDIR /home/finrobot
VOLUME ["/data"]
EXPOSE 8010 8510
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=4 CMD curl -fsS http://127.0.0.1:8010/health || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["finrobot", "serve", "--port", "8010"]
