/**
 * Pick a Plotly chart based on the LLM's `viz_spec` and the row shape returned
 * by /ask. Falls back to a plain table when the type doesn't fit the data.
 *
 * Supports: metric_card, bar, pie, donut, line, histogram, table.
 * The pipeline always strips the internal `_k_count` column before this
 * component sees the data, so we render whatever's left.
 */
import { Statistic, Table } from "antd";
import Plot from "react-plotly.js";
import type { VizSpec } from "../api/client";
import { palette } from "../theme";

interface Props {
  vizSpec?: VizSpec | null;
  rows: Record<string, unknown>[];
}

export default function AutoChart({ vizSpec, rows }: Props) {
  if (!rows?.length) return null;

  const type = (vizSpec?.type || "").toLowerCase();
  const cols = Object.keys(rows[0]);
  const title = vizSpec?.title || undefined;

  // ── single metric value → big-number card ────────────────────────────────
  if (type === "metric_card" || (rows.length === 1 && cols.length === 1)) {
    const onlyCol = cols[0];
    const val = rows[0][onlyCol];
    const num = toNumber(val);
    return num !== null ? (
      <Statistic
        title={title || onlyCol}
        value={num}
        precision={precisionFor(num)}
        valueStyle={{ color: palette.black, fontFamily: '"Roboto Slab", serif', fontSize: 36 }}
      />
    ) : (
      <Statistic
        title={title || onlyCol}
        value={String(val)}
        valueStyle={{ color: palette.black, fontFamily: '"Roboto Slab", serif', fontSize: 36 }}
      />
    );
  }

  // ── pie / donut ──────────────────────────────────────────────────────────
  if ((type === "pie" || type === "donut") && cols.length >= 2) {
    const labelCol = vizSpec?.x || cols[0];
    const valueCol = vizSpec?.y || guessNumericCol(rows[0], [labelCol]) || cols[1];
    return (
      <Plot
        data={[
          {
            type: "pie",
            labels: rows.map((r) => String(r[labelCol])),
            values: rows.map((r) => Number(r[valueCol] ?? 0)),
            hole: type === "donut" ? 0.45 : 0,
            textinfo: "label+percent",
          },
        ]}
        layout={{ title: title ? { text: title } : undefined, margin: { t: title ? 40 : 12 }, autosize: true }}
        style={{ width: "100%", height: 380 }}
        useResizeHandler
      />
    );
  }

  // ── bar (grouped or stacked) ─────────────────────────────────────────────
  if (type === "bar" && cols.length >= 2) {
    const xCol = vizSpec?.x || cols[0];
    const yCol = vizSpec?.y || guessNumericCol(rows[0], [xCol]) || cols[1];
    return (
      <Plot
        data={[
          {
            type: "bar",
            x: rows.map((r) => String(r[xCol])),
            y: rows.map((r) => Number(r[yCol] ?? 0)),
            marker: { color: palette.yellow, line: { color: palette.black, width: 1 } },
          },
        ]}
        layout={{
          title: title ? { text: title } : undefined,
          xaxis: { title: { text: xCol } },
          yaxis: { title: { text: yCol } },
          margin: { t: title ? 40 : 12 },
          autosize: true,
        }}
        style={{ width: "100%", height: 400 }}
        useResizeHandler
      />
    );
  }

  // ── line (assume x is a date / ordered axis) ─────────────────────────────
  if (type === "line" && cols.length >= 2) {
    const xCol = vizSpec?.x || cols[0];
    const yCol = vizSpec?.y || guessNumericCol(rows[0], [xCol]) || cols[1];
    return (
      <Plot
        data={[
          {
            type: "scatter",
            mode: "lines+markers",
            x: rows.map((r) => r[xCol] as string | number),
            y: rows.map((r) => Number(r[yCol] ?? 0)),
            line: { color: palette.orange },
          },
        ]}
        layout={{
          title: title ? { text: title } : undefined,
          xaxis: { title: { text: xCol } },
          yaxis: { title: { text: yCol } },
          margin: { t: title ? 40 : 12 },
          autosize: true,
        }}
        style={{ width: "100%", height: 400 }}
        useResizeHandler
      />
    );
  }

  // ── histogram (numeric column, render as bar over bins) ──────────────────
  if (type === "histogram" && cols.length >= 1) {
    const col = vizSpec?.x || cols[0];
    return (
      <Plot
        data={[
          {
            type: "histogram",
            x: rows.map((r) => Number(r[col] ?? 0)),
            marker: { color: palette.yellow },
          },
        ]}
        layout={{
          title: title ? { text: title } : undefined,
          xaxis: { title: { text: col } },
          yaxis: { title: { text: "frequency" } },
          autosize: true,
        }}
        style={{ width: "100%", height: 400 }}
        useResizeHandler
      />
    );
  }

  // ── fallback: data table ─────────────────────────────────────────────────
  return (
    <Table
      size="small"
      dataSource={rows.map((r, i) => ({ key: i, ...r }))}
      columns={cols.map((c) => ({
        title: c,
        dataIndex: c,
        key: c,
        render: (v: unknown) => formatCell(v),
      }))}
      pagination={{ pageSize: 20 }}
    />
  );
}

function guessNumericCol(sample: Record<string, unknown>, exclude: string[]): string | undefined {
  for (const [k, v] of Object.entries(sample)) {
    if (exclude.includes(k)) continue;
    if (typeof v === "number") return k;
  }
  return undefined;
}

/**
 * Coerce a value to a finite number, or null if it isn't representable as one.
 * Postgres NUMERIC columns serialise as strings ("4.6962598425196850") so we
 * need to recognise numeric strings as numbers for rendering purposes.
 */
function toNumber(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "boolean") return null;
  if (typeof v === "string") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** How many decimals to show for a given number. */
function precisionFor(n: number): number {
  if (!Number.isFinite(n)) return 0;
  if (Number.isInteger(n)) return 0;
  return Math.abs(n) >= 1 ? 2 : 4;
}

/** Render a table cell. Numbers get rounded; everything else passes through. */
function formatCell(v: unknown): string {
  const num = toNumber(v);
  if (num === null) return v === null || v === undefined ? "" : String(v);
  const p = precisionFor(num);
  return p === 0 ? num.toLocaleString() : num.toFixed(p);
}
