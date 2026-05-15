"""
Natural-language query endpoints — orchestrates Flow A from the README.

The LLM may ask the user a clarifying question; the controller stores the
session state and resumes from where it left off when the user answers.

The final SQL is ALWAYS surfaced in the response so the user can review it.
We do NOT block on user approval — the cost gate decides whether to run.

Endpoints:
  POST /ask            — turn 1 (new question)
  POST /ask/clarify    — turn N (user has answered a clarification)
  POST /ask/confirm    — re-run a YELLOW-classified query the user has approved
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import HTTPException
from openg2p_fastapi_common.context import dbengine
from openg2p_fastapi_common.controller import BaseController
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models.chat_session import ChatSession
from ..schemas.chat import (
    AskRequest,
    AskResponse,
    ClarifyRequest,
    ConfirmRunRequest,
)
from ..services.audit_service import AuditService
from ..services.cost_gate_service import CostGateService, CostLevel, CostReport
from ..services.llm_service import (
    Clarification,
    ConvMessage,
    DirectAnswer,
    ExpertRedirect,
    LlmService,
    PlanResult,
    SqlPlan,
)
from ..services.pii_service import PiiService, SanitizedQuery
from ..services.query_executor_service import QueryExecutorService
from ..services.schema_service import SchemaService
from ..services.sql_validator_service import SqlValidationError, SqlValidatorService

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


class ChatController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["ask"]

        self._pii = PiiService.get_component()
        self._schema = SchemaService.get_component()
        self._llm = LlmService.get_component()
        self._validator = SqlValidatorService.get_component()
        self._cost = CostGateService.get_component()
        self._exec = QueryExecutorService.get_component()
        self._audit = AuditService.get_component()

        @self.router.post("/ask", response_model=AskResponse)
        async def ask(req: AskRequest) -> AskResponse:
            session_id = req.session_id or uuid.uuid4().hex
            session = await self._load_or_create_session(
                session_id,
                username=req.user or "anonymous",
                original_question=req.question,
            )

            sanitized = self._pii.sanitize(req.question, session_id=session_id)
            session.placeholder_map = sanitized.placeholder_map or None

            # Carry the conversation forward: prior user / assistant turns go
            # to the LLM as history so it can interpret follow-ups like
            # "and of men?" as refinements of the previous question rather
            # than fresh queries.
            prior_messages = session.messages or []
            history = _to_history(prior_messages)

            # Append the current user question to the persisted thread.
            session.messages = prior_messages + [
                {"role": "user", "content": req.question}
            ]

            schemas = await self._schema.retrieve_relevant(sanitized.text)
            plan = await self._llm.plan(sanitized.text, schemas, history=history)
            return await self._handle_plan(session, sanitized, plan, req.auto_run_yellow)

        @self.router.post("/ask/clarify", response_model=AskResponse)
        async def clarify(req: ClarifyRequest) -> AskResponse:
            if not req.option_id and not req.text:
                raise HTTPException(status_code=400, detail="Either option_id or text is required")

            session = await self._load_session(req.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if session.status != "clarifying":
                raise HTTPException(
                    status_code=409,
                    detail=f"Session is not awaiting clarification (status={session.status!r})",
                )

            user_answer = req.text or req.option_id or ""
            session.messages = (session.messages or []) + [
                {"role": "user", "content": user_answer}
            ]

            history = _to_history(session.messages or [])
            original_question = session.original_question or ""
            sanitized = self._pii.sanitize(original_question, session_id=req.session_id)

            schemas = await self._schema.retrieve_relevant(sanitized.text)
            plan = await self._llm.plan(sanitized.text, schemas, history=history)
            return await self._handle_plan(session, sanitized, plan, req.auto_run_yellow)

        @self.router.post("/ask/confirm", response_model=AskResponse)
        async def confirm(req: ConfirmRunRequest) -> AskResponse:
            """Re-run the SQL that was previously classified YELLOW and shown
            to the user for approval. Reuses `session.last_intent` exactly —
            does NOT re-call the LLM, so the result is deterministic given the
            same DB state."""
            session = await self._load_session(req.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if not session.last_intent:
                raise HTTPException(
                    status_code=409,
                    detail="No cached plan on this session — nothing to confirm.",
                )
            if session.status not in ("ready", "executed", "rejected"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Session not in a confirmable state (status={session.status!r})",
                )

            plan = SqlPlan.model_validate(session.last_intent)
            sanitized = SanitizedQuery(
                text=session.original_question or "",
                redactions=[],
                placeholder_map=session.placeholder_map or {},
            )
            try:
                validated = self._validator.validate(plan.sql, sanitized.placeholder_map)
            except SqlValidationError as exc:
                raise HTTPException(status_code=409, detail=f"Cached SQL no longer valid: {exc}")

            cost = await self._cost.assess(validated)
            sql_text = validated.sql

            if cost.level is CostLevel.RED:
                session.status = "rejected"
                await self._persist(session)
                return self._build_response(
                    "rejected", session, sql_text, cost, plan, sanitized,
                    rows=[], k_suppressions=0,
                )

            result = await self._exec.run(validated)
            session.status = "executed"
            await self._persist(session)
            await self._audit.record_query(
                username=session.username,
                question=session.original_question or "",
                sql=sql_text,
                cost=cost,
                rows=len(result.rows),
            )
            return self._build_response(
                "ok", session, sql_text, cost, plan, sanitized,
                rows=result.rows, k_suppressions=result.k_suppressions,
            )

    # ------------------------------------------------------------------ helpers

    async def _handle_plan(
        self,
        session: ChatSession,
        sanitized: SanitizedQuery,
        plan: PlanResult,
        auto_run_yellow: bool,
    ) -> AskResponse:
        if isinstance(plan, Clarification):
            session.messages = (session.messages or []) + [
                {
                    "role": "assistant",
                    "content": plan.question,
                    "kind": "clarification",
                    "options": [o.model_dump() for o in plan.options],
                    "context": plan.context,
                }
            ]
            session.status = "clarifying"
            await self._persist(session)
            return AskResponse(
                status="needs_clarification",
                session_id=session.id,
                clarification=plan,
                pii_redactions=sanitized.redactions,
                messages=session.messages,
            )

        if isinstance(plan, DirectAnswer):
            session.messages = (session.messages or []) + [
                {
                    "role": "assistant",
                    "content": plan.answer,
                    "kind": "direct_answer",
                    "source": plan.source,
                }
            ]
            # Treat as a fully-answered turn so subsequent follow-ups still
            # have a coherent thread.
            session.status = "executed"
            await self._persist(session)
            return AskResponse(
                status="direct_answer",
                session_id=session.id,
                direct_answer=plan,
                pii_redactions=sanitized.redactions,
                messages=session.messages,
            )

        if isinstance(plan, ExpertRedirect):
            session.messages = (session.messages or []) + [
                {
                    "role": "assistant",
                    "content": plan.explanation or
                        "This question needs statistical analysis — please run it in Expert Lab.",
                    "kind": "expert_redirect",
                    "function": plan.function,
                    "suggested_params": plan.suggested_params,
                }
            ]
            session.status = "expert_redirect"
            await self._persist(session)
            return AskResponse(
                status="expert_redirect",
                session_id=session.id,
                expert_redirect=plan,
                pii_redactions=sanitized.redactions,
                messages=session.messages,
            )

        sql_plan: SqlPlan = plan
        try:
            validated = self._validator.validate(sql_plan.sql, sanitized.placeholder_map)
        except SqlValidationError as exc:
            # The validator rejected the LLM-emitted SQL — surface a
            # clarification rather than corrupt data. We also feed the error
            # back to the conversation so a follow-up turn (or the user) can
            # course-correct.
            _logger.warning("Validator rejected SQL: %s\nSQL was: %s", exc, sql_plan.sql)
            session.messages = (session.messages or []) + [
                {
                    "role": "assistant",
                    "kind": "validator_error",
                    "content": f"Generated SQL was rejected by the safety validator: {exc}",
                    "sql": sql_plan.sql,
                }
            ]
            session.status = "clarifying"
            await self._persist(session)
            return AskResponse(
                status="needs_clarification",
                session_id=session.id,
                sql=sql_plan.sql,
                clarification=Clarification(
                    question=(
                        f"I couldn't safely run that query ({exc}). "
                        "Could you rephrase or be more specific?"
                    ),
                    context="sql_validation_error",
                ),
                assumptions=sql_plan.assumptions,
                pii_redactions=sanitized.redactions,
                messages=session.messages,
            )

        cost = await self._cost.assess(validated)
        session.last_intent = sql_plan.model_dump()
        sql_text = validated.sql

        if cost.level is CostLevel.RED:
            session.status = "rejected"
            await self._persist(session)
            return self._build_response(
                "rejected", session, sql_text, cost, sql_plan, sanitized,
                rows=[], k_suppressions=0,
            )

        if cost.level is CostLevel.YELLOW and not auto_run_yellow:
            session.status = "ready"
            await self._persist(session)
            return self._build_response(
                "needs_confirmation", session, sql_text, cost, sql_plan, sanitized,
                rows=[], k_suppressions=0,
            )

        result = await self._exec.run(validated)

        # Append an assistant turn summarising what was computed so the LLM
        # has context for follow-ups like "and of men?". Short — the goal is
        # to anchor the SQL + rough result, not to mirror the entire frame.
        session.messages = (session.messages or []) + [
            {
                "role": "assistant",
                "kind": "result_summary",
                "content": _summarise_result(result.rows),
                # `sql` is for LLM context on the next turn, not for chat
                # display — the right-side SQL panel renders it visually.
                "sql": sql_text,
            }
        ]
        session.status = "executed"
        await self._persist(session)
        await self._audit.record_query(
            username=session.username,
            question=session.original_question or "",
            sql=sql_text,
            cost=cost,
            rows=len(result.rows),
        )
        return self._build_response(
            "ok", session, sql_text, cost, sql_plan, sanitized,
            rows=result.rows, k_suppressions=result.k_suppressions,
        )

    def _build_response(
        self,
        status: str,
        session: ChatSession,
        sql_text: str,
        cost: CostReport,
        plan: SqlPlan,
        sanitized: SanitizedQuery,
        rows: list[dict],
        k_suppressions: int,
    ) -> AskResponse:
        return AskResponse(
            status=status,  # type: ignore[arg-type]
            session_id=session.id,
            sql=sql_text,
            cost=cost,
            rows=rows,
            viz_spec=plan.viz,
            assumptions=plan.assumptions,
            pii_redactions=sanitized.redactions,
            k_suppressions=k_suppressions,
            messages=session.messages or [],
        )

    # ------------------------------------------------------------- session I/O

    async def _load_or_create_session(
        self, session_id: str, username: str, original_question: str
    ) -> ChatSession:
        session = await self._load_session(session_id)
        if session is not None:
            return session
        return ChatSession(
            id=session_id,
            username=username,
            original_question=original_question,
            messages=[],
            status="clarifying",
        )

    async def _load_session(self, session_id: str) -> Optional[ChatSession]:
        engine = dbengine.get()
        async with AsyncSession(engine) as db:
            result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
            return result.scalar_one_or_none()

    async def _persist(self, session: ChatSession) -> None:
        engine = dbengine.get()
        async with AsyncSession(engine) as db:
            await db.merge(session)
            await db.commit()


def _summarise_result(rows: list[dict]) -> str:
    """Brief result preview shown in the chat thread. The SQL itself is
    rendered in the right-side SQL panel, so we don't repeat it here."""
    if not rows:
        return "Result: (no rows)."
    if len(rows) == 1:
        head = ", ".join(f"{k}={_fmt_value(v)}" for k, v in list(rows[0].items())[:4])
    else:
        first = rows[0]
        head = (
            ", ".join(f"{k}={_fmt_value(v)}" for k, v in list(first.items())[:3])
            + f" … ({len(rows)} rows)"
        )
    return f"Result: {head}."


def _to_history(messages: list[dict]) -> list[ConvMessage]:
    """Turn the persisted message log into LLM-facing history.

    The chat-visible `content` is intentionally brief (no SQL — the SQL panel
    already shows that). For LLM context we prepend the prior SQL stored on
    the message, so elliptical follow-ups like "and the second highest?"
    can be interpreted against the previous query."""
    out: list[ConvMessage] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content") or ""
        sql = m.get("sql")
        if sql:
            sql_oneline = " ".join(str(sql).split())
            content = f"Previous SQL: {sql_oneline}\n{content}".strip()
        if content:
            out.append(ConvMessage(role=role, content=content))
    return out


def _fmt_value(v):
    """Round floats / Decimals to a sensible precision for display + LLM
    context. Postgres NUMERIC arithmetic preserves arbitrary precision —
    AVG of integers easily comes back as 4.6962598425196850, which is
    unreadable. Counts stay whole."""
    import decimal
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, (float, decimal.Decimal)):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return v
        if f == int(f):
            return int(f)
        # 2 decimals for normal-sized numbers; 4 decimals for very small fractions.
        return f"{f:.2f}" if abs(f) >= 1 else f"{f:.4f}"
    return v
