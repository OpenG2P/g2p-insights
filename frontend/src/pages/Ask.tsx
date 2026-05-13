import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { ask, clarify, confirm, type AskResponse, type ClarifyOption, type ConvMessage } from "../api/client";
import AssumptionsPanel from "../components/AssumptionsPanel";
import CostWarning from "../components/CostWarning";
import SqlPanel from "../components/SqlPanel";
import { palette } from "../theme";

const { Title, Text } = Typography;
const { TextArea } = Input;

export default function Ask() {
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [resp, setResp] = useState<AskResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [followUp, setFollowUp] = useState("");

  const submitNew = async () => {
    setLoading(true);
    setErr(null);
    setResp(null);
    try {
      const r = await ask({ question: q });
      setResp(r);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  };

  const submitClarify = async (payload: { option_id?: string; text?: string }) => {
    if (!resp) return;
    setLoading(true);
    setErr(null);
    try {
      const r = await clarify({ session_id: resp.session_id, ...payload });
      setResp(r);
      setFollowUp("");
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  };

  const reset = () => {
    setResp(null);
    setQ("");
    setFollowUp("");
    setErr(null);
  };

  return (
    <div>
      <Title level={3}>Ask</Title>

      {/* Conversation thread */}
      {resp?.messages && resp.messages.length > 0 && (
        <Card style={{ marginBottom: 16 }}>
          <Space direction="vertical" style={{ width: "100%" }} size="middle">
            {resp.messages.map((m, i) => (
              <Bubble key={i} message={m} />
            ))}
          </Space>
        </Card>
      )}

      {/* Input area */}
      {!resp || resp.status === "ok" || resp.status === "rejected" || resp.status === "needs_confirmation" ? (
        <Card>
          <Space direction="vertical" style={{ width: "100%" }}>
            <TextArea
              rows={3}
              placeholder="e.g. Women in Oromia with low income, grouped by woreda"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              disabled={loading}
            />
            <Space>
              <Button type="primary" loading={loading} onClick={submitNew} disabled={!q.trim()}>
                Ask
              </Button>
              {resp && (
                <Button onClick={reset} disabled={loading}>New question</Button>
              )}
            </Space>
          </Space>
        </Card>
      ) : null}

      {/* Clarification picker */}
      {resp?.status === "needs_clarification" && resp.clarification && (
        <Card style={{ marginTop: 16, borderLeft: `4px solid ${palette.orange}` }}>
          <Space direction="vertical" style={{ width: "100%" }} size="middle">
            <Text strong>
              {resp.clarification.question}
            </Text>
            {resp.clarification.context && (
              <Text type="secondary">Context: {resp.clarification.context}</Text>
            )}
            {resp.clarification.options?.length > 0 && (
              <Space wrap>
                {resp.clarification.options.map((opt: ClarifyOption) => (
                  <Button
                    key={opt.id}
                    onClick={() => submitClarify({ option_id: opt.id })}
                    title={opt.description}
                    disabled={loading}
                  >
                    {opt.label}
                  </Button>
                ))}
              </Space>
            )}
            {(resp.clarification.free_text_allowed ?? true) && (
              <Space.Compact style={{ width: "100%" }}>
                <Input
                  placeholder="Or type your own answer"
                  value={followUp}
                  onChange={(e) => setFollowUp(e.target.value)}
                  onPressEnter={() => followUp.trim() && submitClarify({ text: followUp })}
                  disabled={loading}
                />
                <Button
                  type="primary"
                  loading={loading}
                  onClick={() => submitClarify({ text: followUp })}
                  disabled={!followUp.trim()}
                >
                  Send
                </Button>
              </Space.Compact>
            )}
          </Space>
        </Card>
      )}

      {err && <Alert style={{ marginTop: 16 }} type="error" message={err} />}

      {/* Result area — visible whenever we have a committed intent */}
      {resp && resp.status !== "needs_clarification" && (
        <Space direction="vertical" style={{ width: "100%", marginTop: 16 }} size="middle">
          {resp.cost && <CostWarning cost={resp.cost} />}
          <SqlPanel sql={resp.sql} defaultOpen={resp.status !== "ok"} />
          <AssumptionsPanel assumptions={resp.assumptions} />
          {resp.pii_redactions?.length > 0 && (
            <div>
              <Text strong>PII redacted: </Text>
              {resp.pii_redactions.map((p, i) => (
                <Tag key={i}>{p.entity_type}</Tag>
              ))}
            </div>
          )}
          {resp.status === "rejected" && (
            <Alert
              type="error"
              showIcon
              message="Query rejected by the cost gate"
              description={resp.cost?.bottleneck || "Try a narrower question."}
            />
          )}
          {resp.status === "needs_confirmation" && (
            <Alert
              type="warning"
              showIcon
              message="This query is estimated to be expensive."
              description="Click below to run it anyway, or reformulate the question."
              action={
                <Button
                  type="primary"
                  loading={loading}
                  onClick={async () => {
                    setLoading(true);
                    try {
                      setResp(await confirm({ session_id: resp.session_id }));
                    } catch (e) {
                      setErr(String(e));
                    } finally {
                      setLoading(false);
                    }
                  }}
                >
                  Run anyway
                </Button>
              }
            />
          )}
          {resp.status === "ok" && resp.rows.length > 0 ? (
            <Card title={resp.viz_spec?.title || "Result"}>
              <Table
                size="small"
                dataSource={resp.rows.map((r, i) => ({ key: i, ...r }))}
                columns={Object.keys(resp.rows[0]).map((c) => ({
                  title: c, dataIndex: c, key: c,
                }))}
                pagination={{ pageSize: 20 }}
              />
            </Card>
          ) : resp.status === "ok" ? (
            <Alert type="info" message="No rows returned." />
          ) : null}
          {resp.k_suppressions > 0 && (
            <Alert
              type="warning"
              message={`${resp.k_suppressions} group(s) suppressed by k-anonymity policy.`}
            />
          )}
        </Space>
      )}
    </div>
  );
}

// -------- helper ---------------------------------------------------------

function Bubble({ message }: { message: ConvMessage }) {
  const isUser = message.role === "user";
  return (
    <div style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start" }}>
      <div
        style={{
          maxWidth: "78%",
          padding: "8px 12px",
          borderRadius: 8,
          background: isUser ? palette.yellow : palette.lightGrey,
          color: palette.black,
          fontFamily: '"Roboto", sans-serif',
        }}
      >
        <div style={{ fontSize: 11, opacity: 0.6, marginBottom: 2 }}>
          {isUser ? "You" : "Insight"}
          {message.kind === "clarification" ? " · clarifying" : ""}
        </div>
        <div style={{ whiteSpace: "pre-wrap" }}>{message.content}</div>
      </div>
    </div>
  );
}
