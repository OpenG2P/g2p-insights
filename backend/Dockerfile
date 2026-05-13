# =============================================================================
# OpenG2P Insight — Backend (multi-stage)
# =============================================================================
# Build:
#   docker build -t openg2p/g2p-insight-backend:dev \
#     --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) \
#     --build-arg BUILD_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ") .
# =============================================================================

# ---------- Stage 1: build wheel -----------------------------------------------
FROM python:3.13-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir build && \
    python -m build --wheel --outdir /build/dist

# ---------- Stage 2: runtime ---------------------------------------------------
FROM python:3.13-slim

ARG GIT_COMMIT=dev
ARG BUILD_TIME=dev
ENV GIT_COMMIT=${GIT_COMMIT} BUILD_TIME=${BUILD_TIME}

# spaCy English model is needed by Presidio + our PII service.
# Installed at build-time so air-gapped runtime has it pre-cached.
ENV DB_HOST=localhost \
    DB_PORT=5432 \
    DB_NAME=insight \
    DB_USER=postgres \
    CONFIG_PATH=/app/config/config.yaml \
    CATALOG_PATH=/app/catalog \
    UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8000 \
    UVICORN_WORKERS=1 \
    UVICORN_LOG_LEVEL=info

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && \
    python -m spacy download en_core_web_lg && \
    rm -f /tmp/*.whl

COPY --chown=appuser:appuser config/      /app/config/
COPY --chown=appuser:appuser catalog/     /app/catalog/
COPY --chown=appuser:appuser docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/v1/insight/health')"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD []
