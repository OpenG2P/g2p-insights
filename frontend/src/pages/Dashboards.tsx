import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Input,
  List,
  Row,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  Table,
} from "antd";
import {
  ArrowLeftOutlined,
  ExportOutlined,
  PlusOutlined,
  SaveOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  enableEmbedding,
  listDashboards,
  previewChart,
  saveChart,
  type ChartPreviewResponse,
  type DashboardListing,
} from "../api/client";
import AssumptionsPanel from "../components/AssumptionsPanel";
import CostWarning from "../components/CostWarning";
import SqlPanel from "../components/SqlPanel";
import SupersetEmbed from "../components/SupersetEmbed";
import { palette } from "../theme";

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;

const SUPERSET_URL = import.meta.env.VITE_SUPERSET_URL;

export default function Dashboards() {
  const navigate = useNavigate();
  const { embeddedUuid } = useParams<{ embeddedUuid?: string }>();

  return embeddedUuid ? (
    <DashboardDetail
      embeddedUuid={embeddedUuid}
      onBack={() => navigate("/dashboards")}
    />
  ) : (
    <DashboardList />
  );
}

// ==================== List view ============================================

function DashboardList() {
  const navigate = useNavigate();
  const [authoring, setAuthoring] = useState(false);
  const [dashboards, setDashboards] = useState<DashboardListing[] | null>(null);
  const [listErr, setListErr] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const refresh = async () => {
    try {
      const d = await listDashboards();
      setDashboards(d.dashboards);
      setListErr(d.error || null);
    } catch (e) {
      setListErr(String(e));
      setDashboards([]);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  const enableThenOpen = async (d: DashboardListing) => {
    setBusyId(d.id);
    try {
      const { embedded_uuid } = await enableEmbedding(d.id);
      navigate(`/dashboards/${embedded_uuid}`);
    } catch (e) {
      setListErr(String(e));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div>
      <Space style={{ width: "100%", justifyContent: "space-between", marginBottom: 16 }}>
        <Title level={3} style={{ marginBottom: 0 }}>Dashboards</Title>
        <Space>
          <Button onClick={refresh}>Refresh</Button>
          {!authoring && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setAuthoring(true)}>
              Add chart from natural language
            </Button>
          )}
        </Space>
      </Space>

      {listErr && (
        <Alert
          style={{ marginBottom: 16 }}
          type="warning"
          showIcon
          message="Could not load dashboard list"
          description={listErr}
        />
      )}

      {dashboards === null ? (
        <Spin />
      ) : dashboards.length === 0 && !authoring ? (
        <Card>
          <Empty
            description={
              <>
                No dashboards yet. Create one in{" "}
                <a href={SUPERSET_URL} target="_blank" rel="noreferrer">
                  Superset <ExportOutlined />
                </a>{" "}
                or use the "Add chart from natural language" button above and save the chart to a new dashboard.
              </>
            }
          />
        </Card>
      ) : !authoring ? (
        <List
          grid={{ gutter: 16, xs: 1, sm: 2, md: 2, lg: 3, xl: 3, xxl: 4 }}
          dataSource={dashboards}
          renderItem={(d) => (
            <List.Item>
              <Card
                hoverable
                title={d.title}
                style={{ borderLeft: `4px solid ${d.embedded_uuid ? palette.yellow : palette.grey}` }}
                actions={
                  d.embedded_uuid ? [
                    <Button
                      key="open"
                      type="link"
                      onClick={() => navigate(`/dashboards/${d.embedded_uuid}`)}
                    >
                      Open here →
                    </Button>,
                  ] : [
                    <Tooltip key="enable" title="Enable embedding in Superset so this dashboard can render inside Insight.">
                      <Button
                        type="link"
                        icon={<ThunderboltOutlined />}
                        loading={busyId === d.id}
                        onClick={() => enableThenOpen(d)}
                      >
                        Enable embedding
                      </Button>
                    </Tooltip>,
                  ]
                }
              >
                <Space direction="vertical" size={4}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    Superset id: {d.id} · {d.changed_on || "—"}
                  </Text>
                  <div>
                    {d.embedded_uuid ? (
                      <Tag color={palette.yellow} style={{ color: palette.black }}>
                        Embed-ready
                      </Tag>
                    ) : (
                      <Tag>Embed not enabled</Tag>
                    )}
                  </div>
                  {SUPERSET_URL && (
                    <a
                      href={`${SUPERSET_URL}/superset/dashboard/${d.id}/`}
                      target="_blank"
                      rel="noreferrer"
                      style={{ fontSize: 12 }}
                    >
                      Edit in Superset <ExportOutlined />
                    </a>
                  )}
                </Space>
              </Card>
            </List.Item>
          )}
        />
      ) : null}

      {authoring && (
        <NlChartAuthor
          onClose={() => {
            setAuthoring(false);
            void refresh();
          }}
        />
      )}
    </div>
  );
}

// ==================== Detail view (embedded) ===============================

function DashboardDetail({ embeddedUuid, onBack }: { embeddedUuid: string; onBack: () => void }) {
  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>
          Back to dashboards
        </Button>
        {SUPERSET_URL && (
          <a
            href={`${SUPERSET_URL}/superset/dashboard/${embeddedUuid}/`}
            target="_blank"
            rel="noreferrer"
          >
            <Button type="link" icon={<ExportOutlined />}>
              Edit in Superset
            </Button>
          </a>
        )}
      </Space>
      <Card bodyStyle={{ padding: 0 }}>
        <SupersetEmbed embeddedUuid={embeddedUuid} />
      </Card>
    </div>
  );
}

// ==================== NL-author flow (unchanged behaviour, scoped to a card) ====

function NlChartAuthor({ onClose }: { onClose: () => void }) {
  const [question, setQuestion] = useState("");
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [preview, setPreview] = useState<ChartPreviewResponse | null>(null);
  const [saved, setSaved] = useState<{ chart_id?: number; name: string } | null>(null);

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

  const showSidebar = preview && (preview.cost || preview.sql || preview.assumptions?.length);

  return (
    <Row gutter={16}>
      <Col xs={24} lg={showSidebar ? 16 : 24}>
        <Card title="Add chart from natural language">
          <Space direction="vertical" style={{ width: "100%" }} size="middle">
            <TextArea
              rows={3}
              placeholder="e.g. Pie chart of gender distribution. Or: Bar chart of average PMT score by region."
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              disabled={loading}
            />
            <Input
              placeholder="Chart name (optional)"
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
              <Button onClick={onClose} disabled={loading}>Close</Button>
            </Space>
            {err && <Alert type="error" message={err} />}
            {preview?.viz_spec && (
              <Tag style={{ background: palette.lightGrey }}>
                Suggested viz: <b>{preview.viz_spec.type}</b>
                {preview.viz_spec.title ? ` — ${preview.viz_spec.title}` : ""}
              </Tag>
            )}
            {preview?.preview_rows?.length ? (
              <Table
                size="small"
                dataSource={preview.preview_rows.map((r, i) => ({ key: i, ...r }))}
                columns={Object.keys(preview.preview_rows[0]).map((c) => ({
                  title: c, dataIndex: c, key: c,
                }))}
                pagination={{ pageSize: 10 }}
              />
            ) : null}
            {preview && !saved && (
              <Space>
                <Button type="primary" icon={<SaveOutlined />} loading={loading} onClick={doSave}>
                  Save to Superset
                </Button>
                <Button onClick={() => setPreview(null)} disabled={loading}>
                  Refine question
                </Button>
              </Space>
            )}
            {saved && (
              <Alert
                type="success"
                showIcon
                message={`Chart saved to Superset (id=${saved.chart_id})`}
                description={
                  <>
                    <div>"{saved.name}" is now available. Attach it to a dashboard in Superset, then enable embedding to view it inside Insight.</div>
                    {SUPERSET_URL && saved.chart_id && (
                      <a
                        href={`${SUPERSET_URL}/explore/?slice_id=${saved.chart_id}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Open in Superset <ExportOutlined />
                      </a>
                    )}
                  </>
                }
                action={
                  <Button size="small" type="primary" onClick={() => setSaved(null)}>
                    Add another
                  </Button>
                }
              />
            )}
          </Space>
        </Card>
      </Col>
      {showSidebar && (
        <Col xs={24} lg={8}>
          <Space direction="vertical" style={{ width: "100%", position: "sticky", top: 16 }} size="middle">
            <Text strong style={{ fontFamily: '"Roboto Slab", serif', fontSize: 13, color: palette.black }}>
              Chart details
            </Text>
            {preview!.cost && <CostWarning cost={preview!.cost} />}
            <AssumptionsPanel assumptions={preview!.assumptions} />
            <SqlPanel sql={preview!.sql} defaultOpen={false} />
          </Space>
        </Col>
      )}
    </Row>
  );
}

function guessDataset(preview: ChartPreviewResponse): string {
  if (preview.sql) {
    const m = preview.sql.match(/\bfrom\s+([a-zA-Z0-9_.]+)/i);
    if (m) return m[1].split(".").pop()!;
  }
  return "mv_individuals_summary";
}
