import { Alert } from "antd";
import { palette } from "../theme";

interface Props {
  cost: { level: "green" | "yellow" | "red"; estimated_rows?: number; bottleneck?: string };
}

const COLORS: Record<string, string> = {
  green: palette.blue,
  yellow: palette.orange,
  red: palette.magenta,
};

const TYPES: Record<string, "success" | "warning" | "error"> = {
  green: "success",
  yellow: "warning",
  red: "error",
};

export default function CostWarning({ cost }: Props) {
  return (
    <Alert
      style={{ borderLeft: `4px solid ${COLORS[cost.level] || palette.grey}` }}
      type={TYPES[cost.level] || "info"}
      showIcon
      message={`Cost: ${cost.level.toUpperCase()} — estimated ${cost.estimated_rows ?? "?"} rows`}
      description={cost.bottleneck ? `Bottleneck: ${cost.bottleneck}` : undefined}
    />
  );
}
