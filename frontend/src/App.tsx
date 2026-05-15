import { Layout, Menu } from "antd";
import {
  DashboardOutlined,
  MessageOutlined,
  ExperimentOutlined,
  DatabaseOutlined,
  AuditOutlined,
} from "@ant-design/icons";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import TitleBar from "./components/TitleBar";
import Dashboards from "./pages/Dashboards";
import Ask from "./pages/Ask";
import ExpertLab from "./pages/ExpertLab";
import Catalog from "./pages/Catalog";
import Audit from "./pages/Audit";

const { Sider, Content } = Layout;

const navItems = [
  { key: "/dashboards", icon: <DashboardOutlined />, label: <Link to="/dashboards">Dashboards</Link> },
  { key: "/ask",        icon: <MessageOutlined />,   label: <Link to="/ask">Ask</Link> },
  { key: "/expert",     icon: <ExperimentOutlined />, label: <Link to="/expert">Expert Lab</Link> },
  { key: "/catalog",    icon: <DatabaseOutlined />,  label: <Link to="/catalog">Catalog</Link> },
  { key: "/audit",      icon: <AuditOutlined />,     label: <Link to="/audit">Audit</Link> },
];

export default function App() {
  const location = useLocation();
  const selectedKey =
    navItems.find((it) => location.pathname.startsWith(it.key))?.key || "/dashboards";

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <TitleBar />
      <Layout>
        <Sider width={220} theme="light">
          <Menu mode="inline" selectedKeys={[selectedKey]} items={navItems} style={{ borderRight: 0 }} />
        </Sider>
        <Content style={{ padding: 24 }}>
          <Routes>
            <Route path="/" element={<Navigate to="/dashboards" replace />} />
            <Route path="/dashboards" element={<Dashboards />} />
            <Route path="/dashboards/:embeddedUuid" element={<Dashboards />} />
            <Route path="/ask"          element={<Ask />} />
            <Route path="/expert"       element={<ExpertLab />} />
            <Route path="/catalog"      element={<Catalog />} />
            <Route path="/audit"        element={<Audit />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}
