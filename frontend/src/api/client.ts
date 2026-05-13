import axios from "axios";

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "/v1/insight",
  headers: { "Content-Type": "application/json" },
});

// ---------------- Ask types --------------------------------------------------

export type AskStatus =
  | "ok"
  | "needs_clarification"
  | "needs_confirmation"
  | "rejected";

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

// ---------------- Dashboard chart authoring ----------------------------------

export interface ChartPreviewResponse {
  status: "preview" | "saved" | "error" | string;
  chart_id?: number | null;
  cost?: Cost | null;
  form_data?: Record<string, unknown> | null;
  viz_spec?: VizSpec | null;
  preview_rows: Record<string, unknown>[];
  assumptions: string[];
  sql?: string | null;
}

export async function previewChart(question: string, dashboard_id?: number) {
  const { data } = await api.post<ChartPreviewResponse>("/dashboards/charts/preview", {
    question,
    ...(dashboard_id !== undefined ? { dashboard_id } : {}),
  });
  return data;
}

export async function saveChart(payload: {
  name: string;
  dataset: string;
  form_data: Record<string, unknown>;
  dashboard_id?: number;
}) {
  const { data } = await api.post<ChartPreviewResponse>("/dashboards/charts/save", payload);
  return data;
}

export async function getCatalog() {
  const { data } = await api.get("/catalog");
  return data as Record<string, unknown>;
}

export async function getAudit(limit = 100) {
  const { data } = await api.get("/audit", { params: { limit } });
  return data as { entries: Record<string, unknown>[] };
}
