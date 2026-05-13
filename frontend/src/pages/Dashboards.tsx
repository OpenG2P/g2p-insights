import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  Space,
  Table,
  Typography,
  Tag,
} from "antd";
import {
  PlusOutlined,
  SaveOutlined,
  ExportOutlined,
} from "@ant-design/icons";
import {
  previewChart,
  saveChart,
  type ChartPreviewResponse,
} from "../api/client";
import AssumptionsPanel from "../components/AssumptionsPanel";
import CostWarning from "../components/CostWarning";
import SqlPanel from "../components/SqlPanel";
import { palette } from "../theme";

const { Title, Text } = Typography;
const { TextArea } = Input;

const SUPERSET_URL = import.meta.env.VITE_SUPERSET_URL;

export default function Dashboards() {
  const [authoring, setAuthoring] = useState(false);
  const [question, setQuestion] = useState("");
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [preview, setPreview] = useState<ChartPreviewResponse | null>(null);
  const [saved, setSaved] = useState<{ chart_id?: number; name: string } | null>(null);

  const reset = () => {
    setAuthoring(false);
    setQuestion("");
    setName("");
    setPreview(null);
    setSaved(null);
    setErr(null);
  };

  const doPreview = async () => {
    setLoading(true);
    setErr(null);
    setSaved(null);
    try {
      const r = await previewChart(question);
      setPreview(r);
      if (!name && r.viz_spec?.title) setName(String(r.viz_spec.title));
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  };

  const doSave = async () => {
    if (!preview?.form_data) return;
    setLoading(true);
    setErr(null);
    try {
      const dataset = guessDataset(preview);
      const r = await saveChart({
        name: name || (preview.viz_spec?.title as string) || "Untitled chart",
        dataset,
        form_data: preview.form_data,
      });
      setSaved({ chart_id: r.chart_id ?? undefined, name: name || "chart" });
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <Space style={{ width: "100%", justifyContent: "space-between" }}>
        <Title level={3} style={{ marginBottom: 0 }}>Dashboards</Title>
        {!authoring && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setAuthoring(true)}>
            Add chart from natural language
          </Button>
        )}
      </Space>

      {!authoring && !saved && (
        <Card style={{ marginTop: 16 }}>
          <Space direction="vertical">
            <Text>
              Charts and dashboards are managed in Apache Superset.
              {SUPERSET_URL ? (
                <>
                  {" "}Open it at{" "}
                  <a href={SUPERSET_URL} target="_blank" rel="noreferrer">
                    {SUPERSET_URL} <ExportOutlined />
                  </a>
                </>
              ) : null}
              .
            </Text>
            <Text type="secondary">
              To create a new chart without leaving Insight, click <b>Add chart from natural language</b>.
              Describe what you want; Insight will plan the SQL, show a preview, then push it to Superset.
            </Text>
          </Space>
        </Card>
      )}

      {authoring && (
        <Card style={{ marginTop: 16 }}>
          <Space direction="vertical" style={{ width: "100%" }} size="middle">
            <Text strong>Describe the chart you want</Text>
            <TextArea
              rows={3}
              placeholder="e.g. Pie chart of gender distribution.  Or: Bar chart of average PMT score by region."
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              disabled={loading}
            />
            <Input
              placeholder="Chart name (optional — defaults to the LLM-suggested title)"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={loading}
            />
            <Space>
              <Button
                type="primary"
                loading={loading && !preview}
                disabled={!question.trim()}
                onClick={doPreview}
              >
                Preview
              </Button>
              <Button onClick={reset} disabled={loading}>Cancel</Button>
            </Space>
          </Space>
        </Card>
      )}

      {err && <Alert style={{ marginTop: 16 }} type="error" message={err} />}

      {preview && (
        <Space direction="vertical" style={{ width: "100%", marginTop: 16 }} size="middle">
          {preview.cost && <CostWarning cost={preview.cost} />}
          <SqlPanel sql={preview.sql} defaultOpen={false} />
          {preview.viz_spec && (
            <Tag style={{ background: palette.lightGrey }}>
              Suggested viz: <b>{preview.viz_spec.type}</b>
              {preview.viz_spec.title ? ` — ${preview.viz_spec.title}` : ""}
            </Tag>
          )}
          <AssumptionsPanel assumptions={preview.assumptions} />
          {preview.preview_rows?.length ? (
            <Card title="Preview data (first rows)">
              <Table
                size="small"
                dataSource={preview.preview_rows.map((r, i) => ({ key: i, ...r }))}
                columns={Object.keys(preview.preview_rows[0]).map((c) => ({
                  title: c, dataIndex: c, key: c,
                }))}
                pagination={{ pageSize: 10 }}
              />
            </Card>
          ) : null}
          {!saved ? (
            <Space>
              <Button
                type="primary"
                icon={<SaveOutlined />}
                loading={loading}
                onClick={doSave}
              >
                Save to Superset
              </Button>
              <Button onClick={() => setPreview(null)} disabled={loading}>
                Refine question
              </Button>
            </Space>
          ) : null}
        </Space>
      )}

      {saved && (
        <Alert
          style={{ marginTop: 16 }}
          type="success"
          showIcon
          message={`Chart saved to Superset (id=${saved.chart_id})`}
          description={
            <>
              <div>{`"${saved.name}"`} is now available in your Superset workspace.</div>
              {SUPERSET_URL && saved.chart_id ? (
                <a
                  href={`${SUPERSET_URL}/explore/?slice_id=${saved.chart_id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open in Superset <ExportOutlined />
                </a>
              ) : null}
            </>
          }
          action={
            <Button size="small" type="primary" onClick={reset}>
              Add another
            </Button>
          }
        />
      )}
    </div>
  );
}

/** Try to find the table the Intent referenced — for the MVP we pull it out
 *  of the `sql` string (FROM <name>). */
function guessDataset(preview: ChartPreviewResponse): string {
  if (preview.sql) {
    const m = preview.sql.match(/\bfrom\s+([a-zA-Z0-9_.]+)/i);
    if (m) return m[1].split(".").pop()!;
  }
  return "mv_individuals_summary";
}
