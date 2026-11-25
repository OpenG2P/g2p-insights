import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Empty,
  List,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import {
  ArrowLeftOutlined,
  ExportOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  enableEmbedding,
  listDashboards,
  type DashboardListing,
} from "../api/client";
import SupersetEmbed from "../components/SupersetEmbed";
import { SUPERSET_URL } from "../runtime-config";

const { Text } = Typography;


export default function Dashboards() {
  const navigate = useNavigate();
  // Read the selected dashboard from the LOCATION rather than useParams: this
  // page is rendered outside <Routes> so it can stay mounted across tab
  // switches (see KeepAlive), and useParams only sees params of the <Route>
  // that rendered it — which, outside Routes, is none.
  const { pathname } = useLocation();
  const embeddedUuid = pathname.startsWith("/dashboards/")
    ? decodeURIComponent(pathname.slice("/dashboards/".length)).replace(/\/+$/, "") || undefined
    : undefined;

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
  const [dashboards, setDashboards] = useState<DashboardListing[] | null>(null);
  const [listErr, setListErr] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  // The embedded_uuid currently shown. Auto-set to the first embed-ready
  // dashboard once the list loads, so the user lands directly on a rendered
  // dashboard instead of a card they have to click.
  const [selectedUuid, setSelectedUuid] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const d = await listDashboards();
      setDashboards(d.dashboards);
      setListErr(d.error || null);
      const embedReady = d.dashboards.filter((x) => x.embedded_uuid);
      setSelectedUuid((prev) =>
        prev && embedReady.some((x) => x.embedded_uuid === prev)
          ? prev
          : embedReady[0]?.embedded_uuid ?? null,
      );
    } catch (e) {
      setListErr(String(e));
      setDashboards([]);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  // Enable embedding on a not-yet-embeddable dashboard, then show it directly.
  const enableThenShow = async (d: DashboardListing) => {
    setBusyId(d.id);
    try {
      const { embedded_uuid } = await enableEmbedding(d.id);
      await refresh();
      setSelectedUuid(embedded_uuid);
    } catch (e) {
      setListErr(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const embedReady = (dashboards || []).filter((d) => d.embedded_uuid);
  const notEnabled = (dashboards || []).filter((d) => !d.embedded_uuid);
  const selected = embedReady.find((d) => d.embedded_uuid === selectedUuid) || null;

  return (
    <div>
      {/* One slim toolbar, kept deliberately minimal so the dashboard itself
          starts as high up the page as possible:
            - no page heading — the left nav already marks the active tab, and
              the embedded dashboard carries its own title
            - no Refresh button — the embedded dashboard has Superset's own */}
      <Space
        style={{ width: "100%", justifyContent: "space-between", marginBottom: 8 }}
        wrap
      >
        {embedReady.length > 1 ? (
          <Select
            value={selectedUuid || undefined}
            style={{ minWidth: 240 }}
            size="small"
            onChange={setSelectedUuid}
            options={embedReady.map((d) => ({
              value: d.embedded_uuid as string,
              label: d.title,
            }))}
          />
        ) : (
          <span />
        )}
        <Space size={4}>
          {selected && SUPERSET_URL && (
            <a
              href={`${SUPERSET_URL}/superset/dashboard/${selected.id}/`}
              target="_blank"
              rel="noreferrer"
            >
              <Button type="link" size="small" icon={<ExportOutlined />}>
                Edit in Superset
              </Button>
            </a>
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
      ) : embedReady.length === 0 && notEnabled.length === 0 ? (
        <Card>
          <Empty
            description={
              <>
                No dashboards yet. Create one in{" "}
                <a href={SUPERSET_URL} target="_blank" rel="noreferrer">
                  Superset <ExportOutlined />
                </a>
                .
              </>
            }
          />
        </Card>
      ) : (
        <>
          {/* The embedded dashboard, rendered directly. Its title and refresh
              control come from Superset itself — see the toolbar above. */}
          {selected && (
            <Card bodyStyle={{ padding: 0 }}>
              <SupersetEmbed
                key={selected.embedded_uuid}
                embeddedUuid={selected.embedded_uuid as string}
              />
            </Card>
          )}

          {/* Dashboards that still need embedding enabled in Superset. */}
          {notEnabled.length > 0 && (
            <Card
              size="small"
              title="Not yet embeddable"
              style={{ marginTop: 16 }}
            >
              <List
                size="small"
                dataSource={notEnabled}
                renderItem={(d) => (
                  <List.Item
                    actions={[
                      <Button
                        key="enable"
                        type="link"
                        icon={<ThunderboltOutlined />}
                        loading={busyId === d.id}
                        onClick={() => enableThenShow(d)}
                      >
                        Enable embedding
                      </Button>,
                    ]}
                  >
                    <Space size={8}>
                      <Text>{d.title}</Text>
                      <Tag>Embed not enabled</Tag>
                    </Space>
                  </List.Item>
                )}
              />
            </Card>
          )}
        </>
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
