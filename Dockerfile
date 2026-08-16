FROM python:3.12-slim-trixie
LABEL io.modelcontextprotocol.server.name="io.github.D4Vinci/Scrapling"
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --compile-bytecode

COPY tiktok_scrapling.py ./

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    apt-get update && \
    uv run playwright install-deps chromium && \
    uv run playwright install chromium && \
    uv sync --compile-bytecode && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

ENV TARGET_DOMAIN=api24-normal-useast1a.tiktokv.com \
    TARGET_SCHEME=https \
    API_PATH=/ \
    MODE=adaptive \
    STEALTHY=1 \
    DYNAMIC=0 \
    RUN_INTERVAL=5 \
    MAX_RETRIES=3 \
    BACKOFF=2 \
    TIMEOUT=15 \
    PROXY_ROTATE=0 \
    PROXY_CHECK_URL=https://httpbin.org/ip \
    LOG_LEVEL=INFO

# Expose port for the status server / MCP HTTP transport
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD pgrep -f tiktok_scrapling.py || exit 1

ENTRYPOINT ["uv", "run"]
CMD ["python", "tiktok_scrapling.py"]
