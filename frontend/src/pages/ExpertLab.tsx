import { useEffect, useMemo, useState } from "react";
import { Alert, Card, Select, Typography, Space, Button, Spin, Tag } from "antd";
import { api, runAnalysis } from "../api/client";
import ChartRenderer from "../components/ChartRenderer";
import AssumptionsPanel from "../components/AssumptionsPanel";
import { palette } from "../theme";

const { Title, Paragraph, Text } = Typography;

interface Fn { name: string; doc: string }

interface CatColumn { name: string; type?: string; pii?: string }
interface CatTable  { name: string; columns?: CatColumn[] }

// Sensible fallbacks if /catalog hasn't loaded yet or the deployment hasn't
// onboarded a registry. These match the test-stack so dev keeps working with
// no extra config.
const FALLBACK_ENTITIES = ["mv_individuals_summary", "mv_district_aggregates"];
const FALLBACK_NUMERIC = ["pmt_score", "monthly_income", "estimated_age", "coping_strategies_index"];
const FALLBACK_CATEGORICAL = [
  "gender", "region_code", "zone_code", "woreda_code", "age_bracket",
  "primary_livelihood", "employment_status", "disability_status",
  "displacement_status", "pastoralist_classification", "headship_type",
];

// Postgres types that the analytics endpoints can compute numeric stats on.
const NUMERIC_PG_TYPES = new Set([
  "int", "integer", "bigint", "smallint",
  "float", "double precision", "real",
  "numeric", "decimal",
]);
// Anything string-ish / enum-shaped — categorical for group-by / crosstab.
const CATEGORICAL_PG_TYPES = new Set([
  "string", "text", "varchar", "character varying", "char", "enum",
  "bool", "boolean",
]);

interface Prefill {
  function: string;
  params: Record<string, unknown>;
  question?: string;
}

export default function ExpertLab() {
  const [fns, setFns] = useState<Fn[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  // Catalog-driven entity list. Falls back to test-stack names when /catalog
  // hasn't loaded yet (initial render) or returned empty.
  const [catalogTables, setCatalogTables] = useState<CatTable[]>([]);
  const [entity, setEntity] = useState<string>(FALLBACK_ENTITIES[0]);

  // Per-function parameters. We flatten so the form is one set of fields;
  // individual functions consume what they need and ignore the rest.
  const [column, setColumn] = useState<string>("pmt_score");
  const [groupBy, setGroupBy] = useState<string>("gender");
  const [columns, setColumns] = useState<string[]>(["pmt_score", "monthly_income"]);
  const [row, setRow] = useState<string>("displacement_status");
  const [col, setCol] = useState<string>("gender");

  const [prefillFrom, setPrefillFrom] = useState<string | null>(null);

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<{
    result: Record<string, unknown>;
    viz_spec: Record<string, unknown>;
    assumptions: string[];
  } | null>(null);

  // Load analytics-function catalog.
  useEffect(() => {
    api.get<{ functions: Fn[] }>("/analytics/functions").then((r) => setFns(r.data.functions));
  }, []);

  // Load table+column catalog so the entity / column dropdowns reflect the
  // actually-onboarded registry instead of the test-stack defaults.
  useEffect(() => {
    api.get<{ tables?: CatTable[] }>("/catalog")
      .then((r) => {
        const tabs = r.data.tables || [];
        if (tabs.length === 0) return;
        setCatalogTables(tabs);
        // If current `entity` isn't in the new catalog, switch to the first one.
        if (!tabs.some((t) => t.name === entity)) {
          setEntity(tabs[0].name);
        }
      })
      .catch((e) => console.warn("Could not load /catalog — using fallback dropdown lists:", e));
    // intentional: run once at mount; entity changes are user-driven afterwards.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Derived lists for the entity / column dropdowns.
  const entityNames = useMemo<string[]>(
    () => (catalogTables.length ? catalogTables.map((t) => t.name) : FALLBACK_ENTITIES),
    [catalogTables],
  );
  const currentTableCols = useMemo<CatColumn[]>(
    () => catalogTables.find((t) => t.name === entity)?.columns || [],
    [catalogTables, entity],
  );
  const numericColumns = useMemo<string[]>(() => {
    if (!currentTableCols.length) return FALLBACK_NUMERIC;
    return currentTableCols
      .filter((c) => c.pii !== "pii" && c.type && NUMERIC_PG_TYPES.has(c.type.toLowerCase()))
      .map((c) => c.name);
  }, [currentTableCols]);
  const categoricalColumns = useMemo<string[]>(() => {
    if (!currentTableCols.length) return FALLBACK_CATEGORICAL;
    return currentTableCols
      .filter((c) => c.pii !== "pii" && c.type && CATEGORICAL_PG_TYPES.has(c.type.toLowerCase()))
      .map((c) => c.name);
  }, [currentTableCols]);

  // Read any pre-fill from sessionStorage (set by Ask when redirecting).
  useEffect(() => {
    const raw = sessionStorage.getItem("insight.expertPrefill");
    if (!raw) return;
    try {
      const pf: Prefill = JSON.parse(raw);
      setSelected(pf.function);
      const p = pf.params || {};
      if (typeof p.metric === "string") setColumn(p.metric);
      if (typeof p.column === "string") setColumn(p.column);
      if (typeof p.group_by === "string") setGroupBy(p.group_by);
      if (Array.isArray(p.columns)) setColumns(p.columns as string[]);
      if (typeof p.row === "string") setRow(p.row);
      if (typeof p.col === "string") setCol(p.col);
      if (pf.question) setPrefillFrom(pf.question);
    } catch (e) {
      console.warn("Bad prefill payload:", e);
    } finally {
      sessionStorage.removeItem("insight.expertPrefill");
    }
  }, []);

  const fnHelp = useMemo<Record<string, string>>(() => ({
    distribution: "Histogram + percentiles + fitted distributions for one numeric column.",
    correlation: "Correlation matrix over multiple numeric/categorical columns.",
    crosstab: "Contingency table + chi-square between two categorical columns.",
    group_compare: "Compare a numeric metric across a categorical group (t-test / Welch / ANOVA).",
  }), []);

  const buildBody = (): Record<string, unknown> => {
    switch (selected) {
      case "distribution":
        return { entity, column };
      case "correlation":
        return { entity, columns };
      case "crosstab":
        return { entity, params: { row, col } };
      case "group_compare":
        return { entity, params: { metric: column, group_by: groupBy } };
      default:
        return { entity };
    }
  };

  const run = async () => {
    if (!selected) return;
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      const r = await runAnalysis(selected, buildBody());
      setResult({ result: r.result, viz_spec: r.viz_spec, assumptions: r.assumptions });
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <Title level={3}>Expert Lab</Title>
      <Paragraph>Pick a registered statistical function and run it on PII-stripped data.</Paragraph>

      {prefillFrom && (
        <Alert
          style={{ marginBottom: 16 }}
          type="info"
          showIcon
          message="Pre-filled from Ask"
          description={
            <>
              Original question: <Text italic>"{prefillFrom}"</Text>
              <br />Function and parameters were chosen by the LLM. Adjust if needed, then Run.
            </>
          }
        />
      )}

      <Card>
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Space wrap>
            <Select
              placeholder="Analysis function"
              style={{ width: 240 }}
              value={selected || undefined}
              options={fns.map((f) => ({ value: f.name, label: f.name }))}
              onChange={(v) => { setSelected(v); setResult(null); }}
            />
            <Select
              value={entity}
              onChange={setEntity}
              style={{ width: 240 }}
              options={entityNames.map((e) => ({ value: e, label: e }))}
            />
          </Space>

          {selected && (
            <Text type="secondary">{fnHelp[selected] || ""}</Text>
          )}

          {/* Param fields shown only for the relevant function. */}
          {(selected === "distribution" || selected === "group_compare") && (
            <Space wrap>
              <Text>Metric column:</Text>
              <Select
                value={column}
                onChange={setColumn}
                style={{ width: 220 }}
                options={numericColumns.map((c) => ({ value: c, label: c }))}
              />
            </Space>
          )}

          {selected === "group_compare" && (
            <Space wrap>
              <Text>Group by:</Text>
              <Select
                value={groupBy}
                onChange={setGroupBy}
                style={{ width: 220 }}
                options={categoricalColumns.map((c) => ({ value: c, label: c }))}
              />
            </Space>
          )}

          {selected === "correlation" && (
            <Space wrap>
              <Text>Columns:</Text>
              <Select
                mode="multiple"
                value={columns}
                onChange={setColumns}
                style={{ minWidth: 360 }}
                options={[...numericColumns, ...categoricalColumns].map((c) => ({ value: c, label: c }))}
              />
            </Space>
          )}

          {selected === "crosstab" && (
            <Space wrap>
              <Text>Row:</Text>
              <Select value={row} onChange={setRow} style={{ width: 220 }}
                options={categoricalColumns.map((c) => ({ value: c, label: c }))} />
              <Text>Column:</Text>
              <Select value={col} onChange={setCol} style={{ width: 220 }}
                options={categoricalColumns.map((c) => ({ value: c, label: c }))} />
            </Space>
          )}

          <Button type="primary" onClick={run} disabled={!selected} loading={busy}>
            Run
          </Button>
        </Space>
      </Card>

      {busy && <Spin style={{ marginTop: 24 }} />}
      {err && <Alert style={{ marginTop: 16 }} type="error" message={err} />}

      {result && (
        <Space direction="vertical" style={{ width: "100%", marginTop: 16 }} size="middle">
          <Card title="Result">
            <ChartRenderer vizSpec={result.viz_spec} data={result.result} />
          </Card>
          {result.assumptions?.length > 0 && (
            <AssumptionsPanel assumptions={result.assumptions} />
          )}
          {selected === "group_compare" && <GroupCompareSummary data={result.result} />}
        </Space>
      )}
    </div>
  );
}

// ---------------- group_compare summary table ------------------------------

function GroupCompareSummary({ data }: { data: Record<string, unknown> }) {
  const groups = data.groups as Record<string, Record<string, number>> | undefined;
  const test = data.test as Record<string, unknown> | undefined;
  const d = data.effect_size_cohens_d as number | null | undefined;

  if (!groups) return null;
  const groupKeys = Object.keys(groups);
  const statKeys = ["n", "mean", "median", "std", "q1", "q3", "min", "max"];

  return (
    <Card title="Group statistics" size="small">
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
        <thead>
          <tr style={{ borderBottom: `1px solid ${palette.grey}` }}>
            <th style={{ textAlign: "left", padding: 4 }}>Group</th>
            {statKeys.map((k) => <th key={k} style={{ textAlign: "right", padding: 4 }}>{k}</th>)}
          </tr>
        </thead>
        <tbody>
          {groupKeys.map((gk) => (
            <tr key={gk} style={{ borderBottom: `1px solid ${palette.lightGrey}` }}>
              <td style={{ padding: 4 }}><Tag>{gk}</Tag></td>
              {statKeys.map((k) => (
                <td key={k} style={{ textAlign: "right", padding: 4 }}>
                  {typeof groups[gk][k] === "number" ? Number(groups[gk][k]).toFixed(2) : "—"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {test?.applicable ? (
        <div style={{ marginTop: 12 }}>
          <Text strong>{String(test.name)}: </Text>
          <Text>statistic = {Number(test.statistic).toFixed(3)}, p-value = {Number(test.p_value).toExponential(2)}</Text>
          {typeof d === "number" && !isNaN(d) && (
            <div><Text strong>Effect size (Cohen's d): </Text><Text>{d.toFixed(3)}</Text></div>
          )}
        </div>
      ) : null}
    </Card>
  );
}
