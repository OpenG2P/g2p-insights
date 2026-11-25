/**
 * Hierarchical drill-down — the drill mechanic Apache Superset does not
 * provide (it ships cross-filter, drill-by and drill-to-detail).
 *
 * Deliberately country-agnostic: the level chain, its names and its depth all
 * come from Master Data Service via /drill/hierarchy. Nothing here mentions a
 * country, a level name, or assumes how deep the hierarchy goes.
 *
 * Renderer note: this uses a bar chart, which works against any hierarchy —
 * geographic or not (payment funnel, programme, time). The choropleth variant
 * additionally needs boundary polygons keyed by pcode; see
 * docs/master-data-service-changes.md.
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, Breadcrumb, Button, Card, Empty, Space, Spin, Tag, Typography } from "antd";
import { HomeOutlined, RollbackOutlined } from "@ant-design/icons";
import Plot from "react-plotly.js";
import {
  drill,
  getDrillHierarchy,
  type DrillLevel,
  type DrillMeasure,
  type DrillPathItem,
  type DrillResponse,
} from "../api/client";
import { palette } from "../theme";

const { Text } = Typography;

interface Props {
  /** Entity to aggregate — must be in the catalog allow-list. */
  table: string;
  measure?: DrillMeasure;
  /** Show the generated SQL, consistent with the Ask flow. */
  showSql?: boolean;
  height?: number;
}

/** Prettify a level mnemonic for display until MDS supplies display_name. */
function levelLabel(level?: DrillLevel | null): string {
  if (!level) return "";
  return level.level_mnemonic
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function DrillDown({ table, measure, showSql = true, height = 420 }: Props) {
  const [path, setPath] = useState<DrillPathItem[]>([]);
  const [data, setData] = useState<DrillResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load the level chain once. With one country per deployment the root level
  // holds a single node, so we start one level below it when we can — the
  // engine stays depth-agnostic, this is purely a UI default.
  const [startLevelId, setStartLevelId] = useState<string | undefined>();

  useEffect(() => {
    let cancelled = false;
    getDrillHierarchy()
      .then((levels) => {
        if (cancelled) return;
        const queryable = levels.filter((l) => l.is_queryable);
        const start = queryable.length > 1 ? queryable[1] : queryable[0];
        setStartLevelId(start?.level_id);
      })
      .catch((e) => !cancelled && setError(readError(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  // Depend on the measure's VALUE, not its identity: a caller passing an
  // inline object literal would otherwise recreate it every render and spin
  // the load effect forever.
  const measureKey = JSON.stringify(measure ?? null);

  const load = useCallback(
    async (nextPath: DrillPathItem[], levelId?: string) => {
      setLoading(true);
      setError(null);
      try {
        const res = await drill({
          table,
          measure,
          // Only pin the level explicitly at the top; deeper calls let the
          // backend derive "one below the deepest path item".
          level_id: nextPath.length === 0 ? levelId : undefined,
          path: nextPath,
        });
        setData(res);
      } catch (e) {
        setError(readError(e));
        setData(null);
      } finally {
        setLoading(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [table, measureKey],
  );

  useEffect(() => {
    if (!startLevelId) return;
    load(path, startLevelId);
  }, [startLevelId, path, load]);

  // Structural type rather than the global `Plotly` namespace — the project
  // has @types/react-plotly.js but not @types/plotly.js, so referencing that
  // namespace would break the build.
  const onBarClick = (evt: { points?: Array<{ pointIndex?: number }> }) => {
    const idx = evt?.points?.[0]?.pointIndex;
    if (idx == null || !data) return;
    const row = data.rows[idx];
    if (!row?.drillable) return;
    setPath([
      ...path,
      { level_id: data.level.level_id, level_value_id: row.code, label: row.label },
    ]);
  };

  const goTo = (index: number) => setPath(path.slice(0, index));

  if (error) {
    return (
      <Alert
        type="warning"
        showIcon
        message="Drill-down unavailable"
        description={error}
      />
    );
  }

  const rows = data?.rows ?? [];
  const atLeaf = !data?.next_level || !data.next_level.is_queryable;

  return (
    <Card
      size="small"
      title={
        <Space size="small" wrap>
          <Text strong>{levelLabel(data?.level)}</Text>
          {data?.measure_label && <Tag color="default">{data.measure_label}</Tag>}
          {atLeaf && <Tag>deepest available level</Tag>}
          {!!data?.k_suppressions && (
            <Tag color="orange">{data.k_suppressions} row(s) suppressed (k-anonymity)</Tag>
          )}
        </Space>
      }
      extra={
        path.length > 0 && (
          <Button size="small" icon={<RollbackOutlined />} onClick={() => goTo(path.length - 1)}>
            Back
          </Button>
        )
      }
    >
      <Breadcrumb
        style={{ marginBottom: 12 }}
        items={[
          {
            title: (
              <a onClick={() => goTo(0)}>
                <HomeOutlined /> All
              </a>
            ),
          },
          ...path.map((p, i) => ({
            title:
              i === path.length - 1 ? (
                <span>{p.label || p.level_value_id}</span>
              ) : (
                <a onClick={() => goTo(i + 1)}>{p.label || p.level_value_id}</a>
              ),
          })),
        ]}
      />

      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin />
        </div>
      ) : rows.length === 0 ? (
        <Empty description="No data at this level" />
      ) : (
        <>
          <Plot
            data={[
              {
                type: "bar",
                orientation: "h",
                x: rows.map((r) => r.value ?? 0),
                y: rows.map((r) => r.label),
                marker: { color: palette.yellow },
                hovertemplate: "%{y}<br>%{x:,}<extra></extra>",
              },
            ]}
            layout={{
              height,
              margin: { l: 160, r: 24, t: 8, b: 40 },
              // Same brand tokens the Superset theme uses, so charts across
              // the Dashboards and Expert Lab tabs read as one product.
              font: { family: "Roboto, system-ui, sans-serif", color: palette.black },
              paper_bgcolor: "transparent",
              plot_bgcolor: "transparent",
              yaxis: { automargin: true, autorange: "reversed" },
              xaxis: { title: { text: data?.measure_label || "" } },
            }}
            config={{ displayModeBar: false, responsive: true }}
            style={{ width: "100%" }}
            onClick={onBarClick}
          />
          {!atLeaf && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              Click a bar to drill into {levelLabel(data?.next_level).toLowerCase()}.
            </Text>
          )}
        </>
      )}

      {showSql && data?.sql && (
        <details style={{ marginTop: 12 }}>
          <summary style={{ cursor: "pointer", fontSize: 12, color: palette.blue }}>
            Show SQL
          </summary>
          <pre style={{ fontSize: 11, background: palette.lightGrey, padding: 8, overflowX: "auto" }}>
            {data.sql}
          </pre>
        </details>
      )}
    </Card>
  );
}

function readError(e: unknown): string {
  const anyE = e as { response?: { data?: { detail?: string } }; message?: string };
  return anyE?.response?.data?.detail || anyE?.message || "Unknown error";
}
