import Plot from "react-plotly.js";

interface Props {
  vizSpec: Record<string, unknown>;
  data: Record<string, unknown>;
}

export default function ChartRenderer({ vizSpec, data }: Props) {
  const type = (vizSpec.type as string) || "table";

  if (type === "histogram" && data.histogram) {
    const h = data.histogram as { counts: number[]; edges: number[] };
    const xs = h.edges.slice(0, -1).map((e, i) => (e + h.edges[i + 1]) / 2);
    return (
      <Plot
        data={[{ x: xs, y: h.counts, type: "bar" }]}
        layout={{
          xaxis: { title: { text: (vizSpec.x_label as string) || "value" } },
          yaxis: { title: { text: (vizSpec.y_label as string) || "frequency" } },
          margin: { t: 24 },
          autosize: true,
        }}
        style={{ width: "100%", height: 420 }}
        useResizeHandler
      />
    );
  }

  if (type === "boxplot" && data.raw_values_for_plot) {
    const groups = data.raw_values_for_plot as Record<string, number[]>;
    return (
      <Plot
        data={Object.entries(groups).map(([name, values]) => ({
          type: "box",
          name,
          y: values,
          boxpoints: "outliers" as const,
        }))}
        layout={{
          xaxis: { title: { text: (vizSpec.x_label as string) || "group" } },
          yaxis: { title: { text: (vizSpec.y_label as string) || "value" } },
          showlegend: false,
          margin: { t: 24 },
          autosize: true,
        }}
        style={{ width: "100%", height: 420 }}
        useResizeHandler
      />
    );
  }

  if (type === "heatmap" && data.matrix) {
    const matrix = data.matrix as Record<string, Record<string, number>>;
    const labels = Object.keys(matrix);
    const z = labels.map((r) => labels.map((c) => matrix[r][c] ?? 0));
    return (
      <Plot
        data={[{ x: labels, y: labels, z, type: "heatmap" }]}
        layout={{ margin: { t: 24, l: 120 }, autosize: true }}
        style={{ width: "100%", height: 520 }}
        useResizeHandler
      />
    );
  }

  return <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>{JSON.stringify(data, null, 2)}</pre>;
}
