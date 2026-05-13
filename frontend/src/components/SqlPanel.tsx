import { Collapse, Typography } from "antd";

const { Text } = Typography;

interface Props {
  sql?: string | null;
  defaultOpen?: boolean;
}

export default function SqlPanel({ sql, defaultOpen = false }: Props) {
  if (!sql) return null;
  return (
    <Collapse
      size="small"
      defaultActiveKey={defaultOpen ? ["sql"] : []}
      items={[
        {
          key: "sql",
          label: <Text strong>Generated SQL (review-only — no approval required)</Text>,
          children: (
            <pre
              style={{
                margin: 0,
                padding: 12,
                background: "#0613271a",
                fontFamily: '"Roboto Mono", monospace',
                fontSize: 12,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {sql}
            </pre>
          ),
        },
      ]}
    />
  );
}
