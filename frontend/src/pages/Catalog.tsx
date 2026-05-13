import { useEffect, useState } from "react";
import { Card, Typography } from "antd";
import { getCatalog } from "../api/client";

const { Title } = Typography;

export default function Catalog() {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    getCatalog().then(setData).catch(() => setData(null));
  }, []);
  return (
    <div>
      <Title level={3}>Schema Catalog</Title>
      <Card>
        <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>
          {data ? JSON.stringify(data, null, 2) : "Loading…"}
        </pre>
      </Card>
    </div>
  );
}
