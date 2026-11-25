import { useEffect, useState } from "react";
import { Layout, Menu } from "antd";
import {
  DashboardOutlined,
  GlobalOutlined,
  MessageOutlined,
  ExperimentOutlined,
  DatabaseOutlined,
  AuditOutlined,
} from "@ant-design/icons";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import TitleBar from "./components/TitleBar";
import Dashboards from "./pages/Dashboards";
import Maps from "./pages/Maps";
import Ask from "./pages/Ask";
import ExpertLab from "./pages/ExpertLab";
import Catalog from "./pages/Catalog";
import Audit from "./pages/Audit";
import { getFeatures, type Features } from "./api/client";
import KeepAlive from "./components/KeepAlive";

const { Sider, Content } = Layout;

export default function App() {
  const location = useLocation();
  // Assume everything is on until the backend says otherwise, so the UI never
  // flickers features away on a slow or failed call.
  const [features, setFeatures] = useState<Features>({
    ai_enabled: true,
    dashboards_enabled: true,
    maps_enabled: true,
  });

  useEffect(() => {
    let cancelled = false;
    getFeatures().then((f) => {
      if (!cancelled) setFeatures(f);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Tabs are filtered by deployment capability. Each surface only appears when
  // it's actually wired up:
  //   Dashboards -> Superset URL set    Maps -> Evidence URL set    Ask -> AI on
  const navItems = [
    features.dashboards_enabled && {
      key: "/dashboards",
      icon: <DashboardOutlined />,
      label: <Link to="/dashboards">Dashboards</Link>,
    },
    features.maps_enabled && {
      key: "/maps",
      icon: <GlobalOutlined />,
      label: <Link to="/maps">Maps</Link>,
    },
    features.ai_enabled && {
      key: "/ask",
      icon: <MessageOutlined />,
      label: <Link to="/ask">Ask</Link>,
    },
    { key: "/expert", icon: <ExperimentOutlined />, label: <Link to="/expert">Expert Lab</Link> },
    { key: "/catalog", icon: <DatabaseOutlined />, label: <Link to="/catalog">Catalog</Link> },
    { key: "/audit", icon: <AuditOutlined />, label: <Link to="/audit">Audit</Link> },
  ].filter(Boolean) as { key: string; icon: JSX.Element; label: JSX.Element }[];

  const fallbackPath = navItems[0]?.key ?? "/expert";
  const selectedKey =
    navItems.find((it) => location.pathname.startsWith(it.key))?.key || fallbackPath;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <TitleBar />
      <Layout>
        {/* Kept narrow — the labels are short, and every pixel here is one the
            embedded dashboards and maps don't get. */}
        <Sider width={168} theme="light">
          <Menu mode="inline" selectedKeys={[selectedKey]} items={navItems} style={{ borderRight: 0 }} />
        </Sider>
        <Content style={{ padding: "16px 16px 24px" }}>
          {/* The two iframe tabs live OUTSIDE <Routes> so routing cannot unmount
              them. Re-creating an iframe means Superset re-authenticates and
              re-renders every chart, and Evidence re-downloads its parquet —
              losing any filter or drill-down the user had set. KeepAlive mounts
              each on first visit and then only toggles visibility. */}
          {features.dashboards_enabled && (
            <KeepAlive active={location.pathname.startsWith("/dashboards")}>
              <Dashboards />
            </KeepAlive>
          )}
          {features.maps_enabled && (
            <KeepAlive active={location.pathname === "/maps"}>
              <Maps />
            </KeepAlive>
          )}
          <Routes>
            <Route path="/" element={<Navigate to={fallbackPath} replace />} />
            {/* Still routed, so a direct hit lands somewhere valid — the
                elements above do the rendering. */}
            {features.dashboards_enabled && (
              <>
                <Route path="/dashboards" element={null} />
                <Route path="/dashboards/*" element={null} />
              </>
            )}
            {features.maps_enabled && <Route path="/maps" element={null} />}
            {/* Route removed entirely when AI is off, so a bookmarked /ask
                redirects rather than rendering a dead page. */}
            {features.ai_enabled && <Route path="/ask" element={<Ask />} />}
            <Route path="/expert" element={<ExpertLab />} />
            <Route path="/catalog" element={<Catalog />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="*" element={<Navigate to={fallbackPath} replace />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}
