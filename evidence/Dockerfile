# =============================================================================
# OpenG2P Insights — Maps (Evidence static site)
# =============================================================================
# Evidence has three stages and only the first needs a database:
#
#   1. source extraction   runs sources/nsr/*.sql on Postgres -> parquet
#   2. compile             Vite/SvelteKit -> a static bundle
#   3. query               DuckDB-WASM, in the browser, against that parquet
#
# So Postgres is a BUILD-time dependency, never a runtime one. The shipped image
# serves static files with no credentials and no egress, and refreshing the
# figures is a rebuild rather than a restart.
#
# Two ways to build:
#
#   Baked data — pass the registry DB so stage 1 runs:
#     DOCKER_BUILDKIT=1 docker build --build-arg BAKE_DATA=true \
#       --build-arg EVIDENCE_SOURCE__registry__host=commons-postgresql \
#       --build-arg EVIDENCE_SOURCE__registry__database=nsr \
#       --build-arg EVIDENCE_SOURCE__registry__user=postgres \
#       --secret id=pgpass,env=PGPASSWORD -t openg2p-insights-evidence .
#
#   Data-free (the default, and what CI publishes) — skips stage 1. The site
#   builds and serves; every query is empty until someone rebuilds against a real
#   database. That is deliberate: a published image must not carry any
#   deployment's registry data.
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

FROM node:20-bookworm-slim
WORKDIR /app
RUN npm install -g serve@14
COPY --from=build /app/build ./build
USER 1000
EXPOSE 3000
# -s = SPA fallback, which Evidence's client-side routing needs.
CMD ["serve", "-s", "build", "-l", "3000"]
