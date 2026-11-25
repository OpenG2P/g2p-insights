import { useEffect, useRef, type ReactNode } from "react";

/**
 * Keep a tab's contents mounted after it is first visited.
 *
 * WHY
 * ---
 * The Dashboards and Maps tabs are iframes: an embedded Superset dashboard and
 * the Evidence maps site. Routing unmounts them on every tab switch, so coming
 * back re-created the iframe from scratch — Superset re-authenticated and
 * re-rendered every chart, Evidence re-downloaded its parquet and rebuilt the
 * DuckDB session, and whatever the user had done in either (a filter, a drill
 * into a woreda) was gone. For an iframe that is seconds of waiting to arrive
 * back where you already were.
 *
 * So render them once and toggle VISIBILITY instead. The iframe keeps its
 * document, its session and its state; switching tabs costs nothing.
 *
 * Mounted lazily — nothing loads until the tab is opened the first time, so an
 * install where nobody opens Maps never pays for it. Once mounted it stays.
 *
 * `display: none` rather than unmounting does mean the hidden tab keeps its
 * memory and any timers it started. For two iframes that is the trade we want;
 * do not reach for this on a page that polls.
 */
export default function KeepAlive({
  active,
  children,
}: {
  active: boolean;
  children: ReactNode;
}) {
  const everActive = useRef(active);
  useEffect(() => {
    if (active) everActive.current = true;
  }, [active]);

  if (!active && !everActive.current) return null;

  return (
    <div style={{ display: active ? "block" : "none", height: "100%" }}>
      {children}
    </div>
  );
}
