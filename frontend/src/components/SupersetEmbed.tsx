/**
 * Embed a Superset dashboard inside our app using the official
 * @superset-ui/embedded-sdk. The SDK injects a managed iframe and refreshes
 * the guest token on its own schedule.
 *
 * What this component does NOT do:
 *  - Enable embedding on the dashboard. The dashboard's `embedded_uuid` must
 *    already exist; if it doesn't, render the EnableEmbedHint instead.
 *  - Pass row-level security rules. Deferred to when Keycloak lands; for now
 *    every guest token sees everything in the dashboard.
 */

import { useEffect, useRef } from "react";
import { embedDashboard } from "@superset-ui/embedded-sdk";
import { fetchEmbedToken } from "../api/client";

interface Props {
  embeddedUuid: string;
  height?: number | string;
  hideTitle?: boolean;
  hideTab?: boolean;
  hideChartControls?: boolean;
}

const SUPERSET_URL = import.meta.env.VITE_SUPERSET_URL || "http://localhost:8088";

export default function SupersetEmbed({
  embeddedUuid,
  height = "calc(100vh - 200px)",
  hideTitle = false,
  hideTab = false,
  hideChartControls = false,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current || !embeddedUuid) return;
    let cancelled = false;

    (async () => {
      try {
        await embedDashboard({
          id: embeddedUuid,
          supersetDomain: SUPERSET_URL,
          mountPoint: ref.current!,
          fetchGuestToken: async () => {
            const { token } = await fetchEmbedToken(embeddedUuid);
            return token;
          },
          dashboardUiConfig: {
            hideTitle,
            hideTab,
            hideChartControls,
            filters: { expanded: true },
          },
        });
        if (cancelled) return;
      } catch (err) {
        // The SDK throws if Superset rejects the guest token or the embed
        // isn't enabled — render a hint in place of the iframe.
        if (!ref.current) return;
        ref.current.innerHTML = "";
        const msg = document.createElement("div");
        msg.style.padding = "16px";
        msg.style.background = "#fff8e6";
        msg.style.borderRadius = "4px";
        msg.textContent =
          "Could not load embedded dashboard. Either embedding is not yet " +
          "enabled on this dashboard, or the guest token request failed. " +
          "See backend logs for details.";
        ref.current.appendChild(msg);
        console.error("[SupersetEmbed] failed:", err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [embeddedUuid, hideTitle, hideTab, hideChartControls]);

  // The SDK injects an <iframe> inside our mount div but its default size is
  // tiny. Force it to fill the container via a scoped class.
  const containerHeight = typeof height === "number" ? `${height}px` : height;
  return (
    <>
      <style>{`
        .superset-embed-mount iframe {
          width: 100% !important;
          height: 100% !important;
          min-height: ${containerHeight};
          border: 0;
          display: block;
        }
      `}</style>
      <div
        ref={ref}
        className="superset-embed-mount"
        style={{ width: "100%", height: containerHeight }}
      />
    </>
  );
}
