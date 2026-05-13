import { useEffect, useState } from "react";
import { Card, Table, Typography } from "antd";
import { getAudit } from "../api/client";

const { Title } = Typography;

export default function Audit() {
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  useEffect(() => {
    getAudit().then((r) => setRows(r.entries)).catch(() => setRows([]));
  }, []);
  const columns = ["id", "username", "action", "cost_level", "estimated_rows", "actual_rows", "created_at"].map(
    (c) => ({ title: c, dataIndex: c, key: c })
  );
  return (
    <div>
      <Title level={3}>Audit Log</Title>
      <Card>
        <Table
          size="small"
          dataSource={rows.map((r, i) => ({ key: i, ...r }))}
          columns={columns}
          pagination={{ pageSize: 50 }}
        />
      </Card>
    </div>
  );
}
