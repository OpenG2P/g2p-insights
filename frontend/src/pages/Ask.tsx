import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Input,
  Row,
  Space,
  Tag,
  Typography,
} from "antd";
import { useNavigate } from "react-router-dom";
import { ExperimentOutlined } from "@ant-design/icons";
import { ask, clarify, confirm, type AskResponse, type ClarifyOption, type ConvMessage } from "../api/client";
import AssumptionsPanel from "../components/AssumptionsPanel";
import AutoChart from "../components/AutoChart";
import CostWarning from "../components/CostWarning";
import SqlPanel from "../components/SqlPanel";
import { palette } from "../theme";

const { Title, Text } = Typography;
const { TextArea } = Input;

export default function Ask() {
  const navigate = useNavigate();
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

  // -- main chat column (questions, answers, charts, errors) -----------------
  const mainColumn = (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      {/* Conversation thread */}
      {resp?.messages && resp.messages.length > 0 && (
        <Card>
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
              {resp && <Button onClick={reset} disabled={loading}>New question</Button>}
            </Space>
          </Space>
        </Card>
      ) : null}

      {/* Clarification picker */}
      {resp?.status === "needs_clarification" && resp.clarification && (
        <Card style={{ borderLeft: `4px solid ${palette.orange}` }}>
          <Space direction="vertical" style={{ width: "100%" }} size="middle">
            <Text strong>{resp.clarification.question}</Text>
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

      {err && <Alert type="error" message={err} />}

      {/* Rejected / needs_confirmation banners stay in the main column —
          they're decisions the user needs to act on, not background metadata. */}
      {resp?.status === "rejected" && (
        <Alert
          type="error"
          showIcon
          message="Query rejected by the cost gate"
          description={resp.cost?.bottleneck || "Try a narrower question."}
        />
      )}
      {resp?.status === "needs_confirmation" && (
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

      {/* Expert-lab redirect — LLM detected a statistical question that
          our aggregation-only Intent can't express. */}
      {resp?.status === "expert_redirect" && resp.expert_redirect && (
        <Alert
          type="info"
          showIcon
          message="This question needs statistical analysis"
          description={resp.expert_redirect.explanation}
          action={
            <Button
              type="primary"
              icon={<ExperimentOutlined />}
              onClick={() => {
                sessionStorage.setItem(
                  "insight.expertPrefill",
                  JSON.stringify({
                    function: resp.expert_redirect!.function,
                    params: resp.expert_redirect!.suggested_params,
                    question: resp.messages?.find((m) => m.role === "user")?.content ?? "",
                  }),
                );
                navigate("/expert");
              }}
            >
              Run in Expert Lab
            </Button>
          }
        />
      )}

      {/* Result chart + table */}
      {resp?.status === "ok" && resp.rows.length > 0 ? (
        <Card title={resp.viz_spec?.title || "Result"}>
          {resp.viz_spec?.user_requested && resp.viz_spec?.applies === false && (
            <Alert
              style={{ marginBottom: 12 }}
              type="info"
              showIcon
              message={`A ${resp.viz_spec.type} chart doesn't quite fit this data — showing it as a table instead.`}
              description={resp.viz_spec.applies_reason || undefined}
            />
          )}
          {resp.viz_spec?.applies !== false && (
            <AutoChart vizSpec={resp.viz_spec} rows={resp.rows} />
          )}
          {resp.viz_spec?.applies === false ? (
            <AutoChart vizSpec={{ type: "table" } as never} rows={resp.rows} />
          ) : !isSingleMetric(resp) ? (
            <Collapse
              size="small"
              style={{ marginTop: 12 }}
              items={[{
                key: "rows",
                label: `Data (${resp.rows.length} row${resp.rows.length === 1 ? "" : "s"})`,
                children: <AutoChart vizSpec={{ type: "table" } as never} rows={resp.rows} />,
              }]}
            />
          ) : null}
        </Card>
      ) : resp?.status === "ok" ? (
        <Alert type="info" message="No rows returned." />
      ) : null}

      {resp && resp.k_suppressions > 0 && (
        <Alert
          type="warning"
          message={`${resp.k_suppressions} group(s) suppressed by k-anonymity policy.`}
        />
      )}
    </Space>
  );

  // -- right-side metadata panel ---------------------------------------------
  const showSidebar = resp && resp.status !== "needs_clarification" &&
    (resp.cost || resp.sql || resp.assumptions?.length || resp.pii_redactions?.length);

  const sidebar = showSidebar ? (
    <Space direction="vertical" style={{ width: "100%", position: "sticky", top: 16 }} size="middle">
      <Text strong style={{ fontFamily: '"Roboto Slab", serif', fontSize: 13, color: palette.black }}>
        Query details
      </Text>
      {resp!.cost && <CostWarning cost={resp!.cost} />}
      <AssumptionsPanel assumptions={resp!.assumptions} />
      <SqlPanel sql={resp!.sql} defaultOpen={false} />
      {resp!.pii_redactions?.length > 0 && (
        <Card size="small">
          <Text strong style={{ fontSize: 12 }}>PII redacted:</Text>
          <div style={{ marginTop: 6 }}>
            {resp!.pii_redactions.map((p, i) => (
              <Tag key={i} style={{ marginBottom: 4 }}>{p.entity_type}</Tag>
            ))}
          </div>
        </Card>
      )}
    </Space>
  ) : null;

  return (
    <div>
      <Title level={3}>Ask</Title>
      <Row gutter={16}>
        <Col xs={24} lg={showSidebar ? 16 : 24}>
          {mainColumn}
        </Col>
        {showSidebar && (
          <Col xs={24} lg={8}>
            {sidebar}
          </Col>
        )}
      </Row>
    </div>
  );
}

// -------- helpers --------------------------------------------------------

function isSingleMetric(resp: AskResponse): boolean {
  const t = (resp.viz_spec?.type || "").toLowerCase();
  return t === "metric_card" || (resp.rows.length === 1 && Object.keys(resp.rows[0] || {}).length === 1);
}

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
