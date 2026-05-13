import { Alert } from "antd";

export default function AssumptionsPanel({ assumptions }: { assumptions: string[] }) {
  if (!assumptions?.length) return null;
  return (
    <Alert
      type="info"
      showIcon
      message="Assumptions"
      description={
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          {assumptions.map((a, i) => (
            <li key={i}>{a}</li>
          ))}
        </ul>
      }
    />
  );
}
