import { useEffect, useState } from "react";
import { Card, Select, Typography, Space, Button, Spin } from "antd";
import { api, runAnalysis } from "../api/client";
import ChartRenderer from "../components/ChartRenderer";

const { Title, Paragraph } = Typography;

interface Fn { name: string; doc: string }

export default function ExpertLab() {
  const [fns, setFns] = useState<Fn[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [entity, setEntity] = useState<string>("mv_individuals_summary");
  const [column, setColumn] = useState<string>("pmt_score");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ result: Record<string, unknown>; viz_spec: Record<string, unknown> } | null>(null);

  useEffect(() => {
    api.get<{ functions: Fn[] }>("/analytics/functions").then((r) => setFns(r.data.functions));
  }, []);

  const run = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      const r = await runAnalysis(selected, { entity, column });
      setResult({ result: r.result, viz_spec: r.viz_spec });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <Title level={3}>Expert Lab</Title>
      <Paragraph>Pick a registered statistical function and run it on aggregated, PII-stripped data.</Paragraph>

      <Card>
        <Space wrap>
          <Select
            placeholder="Analysis function"
            style={{ width: 240 }}
            options={fns.map((f) => ({ value: f.name, label: f.name }))}
            onChange={setSelected}
          />
          <Select
            value={entity}
            onChange={setEntity}
            style={{ width: 240 }}
            options={[{ value: "mv_individuals_summary", label: "mv_individuals_summary" }]}
          />
          <Select
            value={column}
            onChange={setColumn}
            style={{ width: 200 }}
            options={[
              { value: "pmt_score", label: "pmt_score" },
              { value: "monthly_income", label: "monthly_income" },
            ]}
          />
          <Button type="primary" onClick={run} disabled={!selected}>
            Run
          </Button>
        </Space>
      </Card>

      {busy && <Spin style={{ marginTop: 24 }} />}
      {result && (
        <Card style={{ marginTop: 16 }} title="Result">
          <ChartRenderer vizSpec={result.viz_spec} data={result.result} />
        </Card>
      )}
    </div>
  );
}
