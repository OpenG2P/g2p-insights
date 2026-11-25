# =============================================================================
# OpenG2P Insights — Maps builder (openg2p-insights-evidence-builder)
# =============================================================================
# Same `build` stage as Dockerfile, finished as a re-extract Job image rather
# than the static server. It is a separate file, not a `--target`, because the
# shared OpenG2P build workflow builds each image from one Dockerfile's final
# stage. The build stage between the markers is duplicated from Dockerfile and
# the CI workflow fails if the two copies differ — edit both together.
# =============================================================================
# >>> shared build stage — keep byte-identical with Dockerfile.builder (CI checks)
FROM node:20-bookworm-slim AS build
WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

# Which country pack's boundaries the map draws. Must match what the
# environment's Master Data Service was seeded with (geoSeed.countryPack in the
# openg2p-master-data chart) — the choropleth joins on P-code, so a mismatch
# renders an empty map rather than an error.
# The pack baked in as a FALLBACK only. The country a deployment carries is
# chosen at install time in Master Data, which the build cannot know, so
# build-into re-syncs the boundaries from Master Data before every extract and
# this default is what remains if that is unreachable. Do NOT rely on it to
# select a country — set the country on the master-data chart instead.
ARG COUNTRY_PACK=XKM
ARG OPENG2P_DATA_REPO=https://github.com/openg2p/openg2p-data.git
ARG OPENG2P_DATA_BRANCH=develop

# Boundaries and the licence/attribution block come from the pack, not from
# GeoJSON copied in by hand — the hand copies had already drifted from the pack
# and were missing a ring-validity fix.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git init -q /tmp/openg2p-data \
    && git -C /tmp/openg2p-data remote add origin "$OPENG2P_DATA_REPO" \
    && git -C /tmp/openg2p-data fetch -q --depth 1 origin "$OPENG2P_DATA_BRANCH" \
    && git -C /tmp/openg2p-data checkout -q FETCH_HEAD \
    && python3 sync_pack.py --pack "/tmp/openg2p-data/packs/${COUNTRY_PACK}" \
    && rm -rf /tmp/openg2p-data

ARG BAKE_DATA=false
ARG EVIDENCE_SOURCE__registry__host
ARG EVIDENCE_SOURCE__registry__port=5432
ARG EVIDENCE_SOURCE__registry__database
ARG EVIDENCE_SOURCE__registry__user

# The password arrives as a BuildKit secret, never an ARG: an ARG is recorded in
# the image history, which would ship the registry credential with the image.
RUN --mount=type=secret,id=pgpass,required=false \
    if [ "$BAKE_DATA" = "true" ]; then \
      export EVIDENCE_SOURCE__registry__password="$(cat /run/secrets/pgpass 2>/dev/null || true)"; \
      npm run sources; \
    else \
      echo "[maps] BAKE_DATA is not true — building with no data; queries will be empty."; \
    fi \
    && python3 apply_theme.py \
    && npm run build \
    && sh nest-under-basepath.sh /app/build evidence
# <<< shared build stage

# ---------------------------------------------------------------------------
# Builder image. Same `build` stage, published on its own so the cluster can
# re-extract from a live database on a schedule.
#
# It exists because the runtime image below CANNOT rebuild itself: it carries
# /app/build and nothing else — no sources, no node_modules — which is what
# keeps it small and credential-free. Refreshing the figures needs the project
# back, so that is shipped separately and run as a Job rather than bloating
# every serving pod with a toolchain it never uses.
# ---------------------------------------------------------------------------
FROM build AS builder
WORKDIR /app
# Where the Job writes the built site; the serving pod mounts the same volume.
ENV EVIDENCE_BUILD_OUT=/out
COPY docker-build-into.sh /usr/local/bin/build-into
USER 0
# psycopg2 for the RUN-time boundary sync: build-into re-reads the pack from
# Master Data before every extract, so the map follows the country the
# environment was seeded with rather than the one baked in at build time.
# Only the builder needs it — the serving image never talks to a database.
RUN chmod +x /usr/local/bin/build-into \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3-psycopg2 python3-boto3 \
    && rm -rf /var/lib/apt/lists/*
CMD ["/usr/local/bin/build-into"]
