import axios from "axios";
import { API_BASE_URL } from "../runtime-config";

export const api = axios.create({
  baseURL: API_BASE_URL,
  headers: { "Content-Type": "application/json" },
});

// ---------------- Ask types --------------------------------------------------

export type AskStatus =
  | "ok"
  | "needs_clarification"
  | "needs_confirmation"
  | "rejected"
  | "expert_redirect"
  | "direct_answer";

export interface ExpertRedirect {
  kind: "expert_redirect";
  function: string;
  suggested_params: Record<string, unknown>;
  explanation: string;
}

export interface DirectAnswer {
  kind: "direct_answer";
  answer: string;
  source: "catalog" | "system_info" | "other";
}

export interface ClarifyOption { id: string; label: string; description?: string }

export interface Clarification {
  kind: "clarification";
  question: string;
  context: string;
  options: ClarifyOption[];
  free_text_allowed?: boolean;
}

export interface VizSpec {
  type: string;
  x?: string;
  y?: string;
  series?: string;
  title?: string;
  extra?: Record<string, unknown>;
  /** TRUE only when the user explicitly named this viz type. */
  user_requested?: boolean;
  /** Whether this viz type fits the data the query will return. */
  applies?: boolean;
  /** Populated when applies=false — one polite sentence + suggestion. */
  applies_reason?: string | null;
}

export interface Cost {
  level: "green" | "yellow" | "red";
  estimated_rows?: number;
  estimated_cost?: number;
  bottleneck?: string;
  suggestion?: string;
  static_issues?: string[];
}

export interface PiiRedaction {
  placeholder: string;
  entity_type: string;
  start: number;
  end: number;
}

export interface ConvMessage {
  role: "user" | "assistant";
  content: string;
  kind?: string;
  options?: ClarifyOption[];
  context?: string;
}

export interface AskResponse {
  status: AskStatus;
  session_id: string;
  clarification?: Clarification | null;
  expert_redirect?: ExpertRedirect | null;
  direct_answer?: DirectAnswer | null;
  sql?: string | null;
  cost?: Cost | null;
  rows: Record<string, unknown>[];
  viz_spec?: VizSpec | null;
  assumptions: string[];
  pii_redactions: PiiRedaction[];
  k_suppressions: number;
  messages: ConvMessage[];
}

// ---------------- Ask calls --------------------------------------------------

export async function ask(payload: {
  question: string;
  session_id?: string;
  user?: string;
  auto_run_yellow?: boolean;
}): Promise<AskResponse> {
  const { data } = await api.post<AskResponse>("/ask", payload);
  return data;
}

export async function clarify(payload: {
  session_id: string;
  option_id?: string;
  text?: string;
  user?: string;
  auto_run_yellow?: boolean;
}): Promise<AskResponse> {
  const { data } = await api.post<AskResponse>("/ask/clarify", payload);
  return data;
}

export async function confirm(payload: {
  session_id: string;
  user?: string;
}): Promise<AskResponse> {
  const { data } = await api.post<AskResponse>("/ask/confirm", payload);
  return data;
}

// ---------------- Embedded dashboards ----------------------------------------

export interface DashboardListing {
  id: number;
  uuid: string;
  title: string;
  slug?: string | null;
  status: string;
  embedded_uuid: string | null;   // null until embedding is enabled
  changed_on?: string | null;
}

export async function listDashboards() {
  const { data } = await api.get<{ dashboards: DashboardListing[]; error?: string }>(
    "/dashboards"
  );
  return data;
}

export async function enableEmbedding(dashboardId: number) {
  const { data } = await api.post<{ embedded_uuid: string }>(
    `/dashboards/${dashboardId}/enable-embedding`,
  );
  return data;
}

export async function fetchEmbedToken(embeddedUuid: string) {
  const { data } = await api.post<{ token: string; embedded_uuid: string }>(
    `/dashboards/${embeddedUuid}/embed-token`,
  );
  return data;
}

// ---------------- Geo --------------------------------------------------------

export interface GeoLevel { level_id: string; level_mnemonic: string; parent_level_id?: string }
export interface GeoValue {
  level_value_id: string;
  level_id: string;
  level_value_mnemonic: string;
  parent_level_value_id?: string;
}

export async function getGeoLevels(parent_level_id?: string) {
  const { data } = await api.get<{ levels: GeoLevel[] }>("/geo/levels", {
    params: parent_level_id ? { parent_level_id } : {},
  });
  return data.levels;
}

export async function getGeoValues(level_id: string, parent_level_value_id?: string) {
  const { data } = await api.get<{ values: GeoValue[] }>("/geo/values", {
    params: { level_id, ...(parent_level_value_id ? { parent_level_value_id } : {}) },
  });
  return data.values;
}

// ---------------- Other ------------------------------------------------------

export async function runAnalysis(fn: string, body: unknown) {
  const { data } = await api.post(`/analytics/${fn}`, body);
  return data as {
    status: string;
    result: Record<string, unknown>;
    viz_spec: Record<string, unknown>;
    assumptions: string[];
  };
}

export async function getCatalog() {
  const { data } = await api.get("/catalog");
  return data as Record<string, unknown>;
}

export async function getAudit(limit = 100) {
  const { data } = await api.get("/audit", { params: { limit } });
  return data as { entries: Record<string, unknown>[] };
}

// ------------------------------------------------------------------- drill
// Hierarchical drill-down — the one drill mechanic Superset lacks. Nothing
// here knows a country, a level name, or a depth: all of that comes from
// Master Data Service via the backend.

export interface DrillLevel {
  level_id: string;
  level_mnemonic: string;
  parent_level_id?: string | null;
  is_leaf: boolean;
  /** False when the catalog has no column mapping for this level. */
  is_queryable: boolean;
}

export interface DrillPathItem {
  level_id: string;
  level_value_id: string;
  label?: string | null;
}

export interface DrillRow {
  code: string;
  label: string;
  value: number | null;
  drillable: boolean;
}

export interface DrillResponse {
  level: DrillLevel;
  next_level?: DrillLevel | null;
  breadcrumb: DrillPathItem[];
  rows: DrillRow[];
  sql: string;
  k_suppressions: number;
  measure_label: string;
}

export interface DrillMeasure {
  agg?: string;
  column?: string | null;
  label?: string | null;
}

export async function getDrillHierarchy() {
  const { data } = await api.get<{ levels: DrillLevel[] }>("/drill/hierarchy");
  return data.levels;
}

export async function drill(payload: {
  table: string;
  measure?: DrillMeasure;
  level_id?: string;
  path?: DrillPathItem[];
  limit?: number;
}) {
  const { data } = await api.post<DrillResponse>("/drill", payload);
  return data;
}

// ---------------------------------------------------------------- features

export interface Features {
  /** False when this deployment has AI switched off (INSIGHT_LLM__ENABLED=false). */
  ai_enabled: boolean;
  /** False when no Superset URL is configured. */
  dashboards_enabled: boolean;
  /** False when no Evidence.dev URL is configured (Maps tab). */
  maps_enabled: boolean;
  version?: string;
}

/**
 * Capability flags used by the app shell to decide which tabs to render.
 * Fails safe: if the call errors we assume features are ON so the UI is never
 * silently crippled by a transient backend blip.
 */
export async function getFeatures(): Promise<Features> {
  try {
    const { data } = await api.get<Features>("/features");
    return data;
  } catch {
    return { ai_enabled: true, dashboards_enabled: true, maps_enabled: true };
  }
}
