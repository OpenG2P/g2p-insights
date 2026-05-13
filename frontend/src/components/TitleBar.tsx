import { Avatar, Layout, Space, Typography } from "antd";
import { UserOutlined } from "@ant-design/icons";
import { palette } from "../theme";
import { useUser } from "../auth";

const { Header } = Layout;
const { Text } = Typography;

export default function TitleBar() {
  const user = useUser();
  const displayName = user.name && user.name !== "anonymous" ? user.name : "User";

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
      <Space size="small" align="center">
        <Avatar size="small" icon={<UserOutlined />} style={{ background: palette.yellow, color: palette.black }} />
        <Text style={{ color: palette.white, fontFamily: '"Roboto", sans-serif' }}>
          {displayName}
        </Text>
      </Space>
    </Header>
  );
}
