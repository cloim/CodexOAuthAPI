# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.9.25 AS uv

FROM python:3.13-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul \
    CODEX_OAUTH_API_STATE_ROOT=/state \
    CODEX_OAUTH_API_HOST=0.0.0.0
COPY --from=builder /app/.venv /app/.venv
RUN useradd --create-home --uid 10001 codex \
    && mkdir -p /state \
    && chown -R codex:codex /state
WORKDIR /state
USER codex
VOLUME ["/state"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
ENTRYPOINT ["codex-oauth-api"]
CMD ["serve", "--debug"]
