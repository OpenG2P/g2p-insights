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
# These are the names pydantic-settings actually reads: env_prefix is
# "insight_", so unprefixed DB_HOST / DB_NAME / CATALOG_PATH were silently
# ignored and the app fell back to its own defaults. CONFIG_PATH is gone
# entirely — nothing has ever read it.
ENV INSIGHT_DB_HOSTNAME=localhost \
    INSIGHT_DB_PORT=5432 \
    INSIGHT_DB_DBNAME=insight \
    INSIGHT_DB_USERNAME=postgres \
    INSIGHT_CATALOG_PATH=/app/catalog \
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

# Install the AI/PII extras (spaCy, Presidio, sentence-transformers,
# statsmodels) at build time? On by default — the published image is the
# full-fidelity one (heavier — pulls torch, ~2-3 GB). The shared CI workflow
# passes no build args, so the default is what ships.
# Turn off for a quick local image (~1 minute, small) with graceful fallbacks:
#   * PII      -> regex-only (no spaCy NER)
#   * schema   -> keyword retrieval (no embeddings)
#   * analytics-> functions needing statsmodels are unavailable
#   docker build --build-arg INSTALL_AI=false ...   (test-stack passes false
#   unless you run `make up-ai`)
ARG INSTALL_AI=true

COPY --from=builder /build/dist/*.whl /tmp/
# With INSTALL_AI=true we install the `[ai]` extra AND pre-download the spaCy
# model so no runtime download is needed (works air-gapped). The heavy install
# is a separate layer so the base install still caches.
RUN if [ "$INSTALL_AI" = "true" ]; then \
        pip install --no-cache-dir "/tmp/$(ls /tmp)[ai]" && \
        python -m spacy download en_core_web_lg ; \
    else \
        pip install --no-cache-dir /tmp/*.whl ; \
    fi && \
    rm -f /tmp/*.whl

# No config/ is copied: settings come from INSIGHT_* environment variables
# only. A config/default.yaml used to be copied here and read by nothing.
COPY --chown=appuser:appuser catalog/     /app/catalog/
# The app's own schema — insight.audit_log / chat_session / saved_chart. Alembic
# was already installed and the migration already written, but the migrations
# themselves were never copied in and nothing ever ran them, so the schema did
# not exist in ANY deployment: the Audit tab and the Ask tab's session store
# both failed on every call with a bare "Unknown Error".
COPY --chown=appuser:appuser migrations/  /app/migrations/
COPY --chown=appuser:appuser docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/v1/insight/health')"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD []
