FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.32" \
        "pydantic>=2.9" \
        "pydantic-settings>=2.5" \
        "asyncpg>=0.30" \
        "httpx>=0.27" \
        "tiktoken>=0.8" \
        "openai>=1.54" \
        "tenacity>=9.0" \
        "python-json-logger>=2.0" \
        "orjson>=3.10" \
        "pytest>=8.3" \
        "pytest-asyncio>=0.24" \
        "anyio>=4.6"

COPY src/ ./src/
COPY tests/ ./tests/
COPY fixtures/ ./fixtures/

ENV PYTHONPATH=/app/src
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["uvicorn", "memory_service.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
