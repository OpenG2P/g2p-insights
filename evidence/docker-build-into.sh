#!/bin/sh
# Re-extract Evidence's sources from the live database and publish the built
# site into a shared volume.
#
# Runs as a Job after the registry finishes seeding, and again on a schedule.
# The serving pod reads the same volume, so a refresh needs no restart and no
# new image: `serve` reads from disk per request.
#
# Writes to a staging directory and swaps it in at the end. A build takes a
# while, and copying file-by-file into the live directory would serve a site
# whose pages and parquet disagree for the duration.
set -e

OUT="${EVIDENCE_BUILD_OUT:-/out}"
STAGE="${OUT}/.staging"
LIVE="${OUT}/site"

# Give every page the platform's look — the two-column layout, the brand bar and
# the type. Runs AFTER the registry's content overlay, so it themes whatever the
# registry supplied, and replaces any inline <style> a page still carries.
python3 /app/apply_theme.py

# Take the boundaries from Master Data, not from whatever was baked in.
#
# The image is built with a DEFAULT pack (XKM), because at build time nobody
# knows which country the deployment will carry. Master Data does know — it was
# seeded from the pack and recorded a boundary URL on every geo row — so syncing
# here makes the map follow the environment instead of the image. Without it a
# stock image drew Kamuntu over Ethiopian data and, because the choropleth joins
# on P-code, produced an EMPTY map with nothing in any log.
#
# Non-fatal: if Master Data is unreachable or carries no boundary URLs, the
# baked-in pack is still there and the build proceeds with it. A map of the wrong
# country is bad, but no map at all is worse, and the message says which happened.
if [ "${MAPS_SYNC_FROM_MDS:-true}" = "true" ]; then
  echo "[maps-build] syncing boundaries from Master Data"
  if ! python3 /app/sync_pack.py --from-master-data; then
    echo "[maps-build] could not sync from Master Data — building with the"
    echo "[maps-build] boundaries baked into this image, which may be a"
    echo "[maps-build] DIFFERENT COUNTRY and will then render an empty map."
  fi
fi

echo "[maps-build] extracting sources from ${EVIDENCE_SOURCE__registry__database}@${EVIDENCE_SOURCE__registry__host}"
npm run sources
echo "[maps-build] compiling"
# Clear the previous output first. The image ships a build made from the DEFAULT
# pack, and Evidence copies static/ over that directory rather than replacing it,
# so a pack with fewer levels left the deeper file behind — a 4-level Ethiopia
# published Kamuntu's level4.geojson, half a megabyte of another country's
# geometry served to browsers alongside the right one.
rm -rf /app/build
npm run build
# The site is served under /evidence, so it has to LIVE under maps/ in whatever the
# server treats as its root — see nest-under-basepath.sh for why this is a move
# after the fact rather than an EVIDENCE_BUILD_DIR.
sh /app/nest-under-basepath.sh /app/build evidence

rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -a /app/build/. "$STAGE"/

# Swap: the old site stays served until the new one is complete on disk.
if [ -d "$LIVE" ]; then
  rm -rf "${OUT}/.previous"
  mv "$LIVE" "${OUT}/.previous"
fi
mv "$STAGE" "$LIVE"
rm -rf "${OUT}/.previous"

echo "[maps-build] published $(find "$LIVE" -type f | wc -l) files to ${LIVE}"
