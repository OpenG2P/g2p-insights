/**
 * Maps tab — embeds an Evidence.dev static site (choropleths + hierarchical
 * zoom-drill) inside the Insight shell.
 *
 * Evidence is a separate build-time/static surface (near-live via a scheduled
 * rebuild). It owns the in-map zoom-drill that Superset can't do natively. We
 * embed it as an iframe, exactly like the Superset Dashboards tab.
 */
import { useEffect, useRef, useState } from "react";
import { Alert, Spin } from "antd";
import { EVIDENCE_URL } from "../runtime-config";


export default function Maps() {
  const [loading, setLoading] = useState(true);
  const [errored, setErrored] = useState(false);
  const ref = useRef<HTMLIFrameElement>(null);

  // Guard: if the iframe hasn't fired `load` within a few seconds, assume the
  // Evidence site is unreachable and show a helpful message instead of a
  // permanent spinner.
  useEffect(() => {
    const t = setTimeout(() => setLoading((l) => (l ? (setErrored(true), false) : l)), 8000);
    return () => clearTimeout(t);
  }, []);

  if (!EVIDENCE_URL) {
    return (
      <Alert
        type="info"
        showIcon
        message="Maps not configured"
        description="No Evidence.dev URL is set for this deployment (VITE_EVIDENCE_URL). The Maps tab embeds an Evidence site for choropleth and zoom-drill views."
      />
    );
  }

  return (
    <div style={{ position: "relative", height: "calc(100vh - 64px - 48px)" }}>
      {loading && (
        <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center" }}>
          <Spin tip="Loading maps…" />
        </div>
      )}
      {errored && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="Could not load the maps site"
          description={`The Evidence site at ${EVIDENCE_URL} did not respond. Is it running and reachable from the browser?`}
        />
      )}
      <iframe
        ref={ref}
        title="OpenG2P Insight — Maps (Evidence)"
        src={EVIDENCE_URL}
        onLoad={() => setLoading(false)}
        style={{ width: "100%", height: "100%", border: "none" }}
      />
    </div>
  );
}
