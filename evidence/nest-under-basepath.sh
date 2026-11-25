#!/bin/sh
# Move the built site one level down, into the directory its basePath names.
#
# WHY THIS IS NEEDED, AND WHY EVIDENCE_BUILD_DIR IS NOT THE ANSWER
# ----------------------------------------------------------------
# evidence.config.yaml sets `deployment.basePath: /maps`, so every asset in the
# built site is referenced as /maps/_app/..., /maps/geo/... — but the FILES still
# land at the root of build/. Served as-is, index.html loads and then every asset
# it asks for 404s.
#
# EVIDENCE_BUILD_DIR looks like the fix and is not. It sets the SvelteKit
# adapter's `pages` target, resolved relative to .evidence/template/, while the
# CLI afterwards does an unconditional `fs.copySync(outDir, './build')`. Point it
# at an absolute path and the adapter writes there, the copy step then looks for
# .evidence/template/build, does not find it, and the build dies with a bare
# ENOENT naming a path that appears nowhere in our configuration.
#
# So let Evidence build exactly as it wants to, and move the result afterwards.
# The server then needs to know nothing about the prefix: a request for
# /maps/foo resolves to <root>/maps/foo on disk.
set -e

BUILD="${1:-/app/build}"
BASE="${2:-maps}"

[ -d "$BUILD" ] || { echo "[basepath] no $BUILD to nest" >&2; exit 0; }
# Already nested (a re-run, or a build that was interrupted after this step).
[ -d "$BUILD/$BASE" ] && [ ! -f "$BUILD/index.html" ] && {
  echo "[basepath] $BUILD already nested under $BASE/"; exit 0; }

TMP="${BUILD}.nest"
rm -rf "$TMP"
mkdir -p "$TMP/$BASE"
cp -a "$BUILD/." "$TMP/$BASE/"
rm -rf "$BUILD"
mv "$TMP" "$BUILD"
echo "[basepath] nested the site under $BASE/ — /$BASE/... now resolves on disk"
