import { Layout, Space, Typography } from "antd";
import { palette } from "../theme";

const { Header } = Layout;
const { Text } = Typography;

export default function TitleBar() {
  return (
    <Header
      style={{
        background: palette.black,
        display: "flex",
        alignItems: "center",
        padding: "0 24px",
        boxShadow: "0 2px 4px rgba(0,0,0,0.2)",
      }}
    >
      <img
        src="/logos/g2p-insight-dark.svg"
        alt="G2P Insight"
        style={{ height: 36, marginRight: 16 }}
      />
      <div style={{ flex: 1 }} />
      <Space size="middle">
        <Text style={{ color: palette.white, fontFamily: '"Roboto Slab", serif' }}>
          OpenG2P Insight
        </Text>
      </Space>
    </Header>
  );
}
