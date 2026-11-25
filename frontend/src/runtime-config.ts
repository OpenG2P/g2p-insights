/**
 * Deployment settings, read at RUNTIME rather than baked into the bundle.
 *
 * WHY THIS EXISTS
 * ---------------
 * `import.meta.env.VITE_*` is substituted by Vite at BUILD time. A value that is
 * not supplied as a build argument is folded to `undefined`, and the surrounding
 * branch is often eliminated entirely — so setting the variable as a container
 * environment variable does nothing at all, and the code that would have used it
 * is not even present to debug.
 *
 * That is not a hypothetical. The published image was built with no build args,
 * so:
 *   - VITE_SUPERSET_URL fell back to http://localhost:8088 and every embedded
 *     dashboard tried to load from the viewer's own machine;
 *   - VITE_EVIDENCE_URL had no ARG at all, so the Maps tab compiled down to its
 *     "Maps not configured" placeholder with the iframe removed.
 * Both looked like deployment mistakes. Neither could be fixed by deployment.
 *
 * So the container writes /config.js from its environment at start-up, index.html
 * loads it before the app, and this module reads it. The Helm chart can then
 * configure the frontend the same way it configures everything else, and the same
 * image serves every environment.
 *
 * The build-time value is kept as the fallback so `npm run dev` and any existing
 * build-arg pipeline keep working unchanged.
 */

declare global {
  interface Window {
    __INSIGHT_CONFIG__?: Record<string, string>;
  }
}

function setting(key: string, buildTime: string | undefined, fallback = ""): string {
  const runtime = window.__INSIGHT_CONFIG__?.[key];
  // A placeholder that was never substituted must not win over the build-time
  // value — the entrypoint writes literal ${VAR} when a variable is unset.
  if (runtime && !runtime.startsWith("${")) return runtime;
  return buildTime || fallback;
}

/** Base URL for the Insight API. Same origin by default. */
export const API_BASE_URL = setting(
  "apiBaseUrl",
  import.meta.env.VITE_API_BASE_URL,
  "/v1/insight",
);

/** Browser-facing Superset origin. The embed SDK loads its iframe from here. */
export const SUPERSET_URL = setting("supersetUrl", import.meta.env.VITE_SUPERSET_URL);

/**
 * Where the Evidence maps site is served. A path on this origin, not a hostname:
 * one product, one URL. Deliberately NOT `/maps` — the shell's own router owns
 * that for the Maps tab.
 */
export const EVIDENCE_URL = setting("evidenceUrl", import.meta.env.VITE_EVIDENCE_URL);

export {};
