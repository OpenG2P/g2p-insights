# =============================================================================
# OpenG2P Insight — Frontend (multi-stage: Vite build → nginx)
# =============================================================================

# ---------- Stage 1: build -----------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /build
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY tsconfig.json vite.config.ts index.html ./
COPY src/    src/
COPY public/ public/

# Build-time defaults only. The real values arrive at RUNTIME via
# docker-entrypoint.sh — see src/runtime-config.ts. Passing them here is
# optional and only affects the fallback baked into the bundle.
ARG VITE_API_BASE_URL=/v1/insight
ARG VITE_SUPERSET_URL=
ARG VITE_EVIDENCE_URL=
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_SUPERSET_URL=${VITE_SUPERSET_URL} \
    VITE_EVIDENCE_URL=${VITE_EVIDENCE_URL}

RUN npm run build

# ---------- Stage 2: runtime ---------------------------------------------------
FROM nginx:1.27-alpine

COPY --from=builder /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint-openg2p.sh
RUN chmod +x /docker-entrypoint-openg2p.sh

EXPOSE 8080

ENTRYPOINT ["/docker-entrypoint-openg2p.sh"]
CMD ["nginx", "-g", "daemon off;"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:8080/ > /dev/null || exit 1
