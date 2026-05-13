import { Card, Empty, Typography } from "antd";

const { Title, Paragraph } = Typography;

const SUPERSET_URL = import.meta.env.VITE_SUPERSET_URL;

export default function Dashboards() {
  return (
    <div>
      <Title level={3}>Dashboards</Title>
      <Paragraph>
        Standard dashboards are served by Apache Superset
        {SUPERSET_URL ? <> at <a href={SUPERSET_URL}>{SUPERSET_URL}</a></> : null}
        . Embedded views land here once the dashboard registry is wired up.
      </Paragraph>
      <Card>
        <Empty description="No dashboards embedded yet" />
      </Card>
    </div>
  );
}
