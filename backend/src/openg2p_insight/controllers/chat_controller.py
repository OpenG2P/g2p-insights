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
    ExpertRedirect,
    Intent,
    LlmService,
    PlanResult,
)
from ..services.pii_service import PiiService, SanitizedQuery
from ..services.query_executor_service import QueryExecutorService
from ..services.schema_service import SchemaService
from ..services.sql_planner_service import SqlPlannerService

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


class ChatController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["ask"]

        self._pii = PiiService.get_component()
        self._schema = SchemaService.get_component()
        self._llm = LlmService.get_component()
        self._planner = SqlPlannerService.get_component()
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
            history = [
                ConvMessage(role=m["role"], content=m["content"])
                for m in prior_messages
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]

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

            history = [
                ConvMessage(role=m["role"], content=m["content"])
                for m in (session.messages or [])
                if m.get("role") in ("user", "assistant")
            ]
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
                    detail="No cached intent on this session — nothing to confirm.",
                )
            if session.status not in ("ready", "executed", "rejected"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Session not in a confirmable state (status={session.status!r})",
                )

            intent = Intent.model_validate(session.last_intent)
            sanitized = SanitizedQuery(
                text=session.original_question or "",
                redactions=[],
                placeholder_map=session.placeholder_map or {},
            )
            sql = self._planner.build(intent, sanitized.placeholder_map)
            cost = await self._cost.assess(sql)
            sql_text = str(sql)

            if cost.level is CostLevel.RED:
                session.status = "rejected"
                await self._persist(session)
                return self._build_response(
                    "rejected", session, sql_text, cost, intent, sanitized,
                    rows=[], k_suppressions=0,
                )

            result = await self._exec.run(sql, group_by=intent.group_by)
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
                "ok", session, sql_text, cost, intent, sanitized,
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

        intent: Intent = plan
        try:
            sql = self._planner.build(intent, sanitized.placeholder_map)
        except ValueError as exc:
            # The planner rejected the intent — surface a clarification
            # rather than corrupt data.
            _logger.warning("Planner rejected intent: %s", exc)
            session.status = "clarifying"
            await self._persist(session)
            return AskResponse(
                status="needs_clarification",
                session_id=session.id,
                clarification=Clarification(
                    question=f"I couldn't safely build a SQL for that intent ({exc}). Could you rephrase?",
                    context="planner_validation_error",
                ),
                assumptions=intent.assumptions,
                pii_redactions=sanitized.redactions,
                messages=session.messages or [],
            )

        cost = await self._cost.assess(sql)
        session.last_intent = intent.model_dump()
        sql_text = str(sql)

        if cost.level is CostLevel.RED:
            session.status = "rejected"
            await self._persist(session)
            return self._build_response(
                "rejected", session, sql_text, cost, intent, sanitized,
                rows=[], k_suppressions=0,
            )

        if cost.level is CostLevel.YELLOW and not auto_run_yellow:
            session.status = "ready"
            await self._persist(session)
            return self._build_response(
                "needs_confirmation", session, sql_text, cost, intent, sanitized,
                rows=[], k_suppressions=0,
            )

        result = await self._exec.run(sql, group_by=intent.group_by)

        # Append an assistant turn summarising what was computed so the LLM
        # has context for follow-ups like "and of men?". Short — the goal is
        # to anchor the entity / metric / filters / rough result, not to
        # mirror the entire data frame.
        session.messages = (session.messages or []) + [
            {
                "role": "assistant",
                "kind": "result_summary",
                "content": _summarise_result(intent, sql_text, result.rows),
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
            "ok", session, sql_text, cost, intent, sanitized,
            rows=result.rows, k_suppressions=result.k_suppressions,
        )

    def _build_response(
        self,
        status: str,
        session: ChatSession,
        sql_text: str,
        cost: CostReport,
        intent: Intent,
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
            viz_spec=intent.viz,
            assumptions=intent.assumptions,
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


def _summarise_result(intent: Intent, sql_text: str, rows: list[dict]) -> str:
    """Build a short assistant-side message that anchors the LLM for follow-ups.
    Includes the entity, metric, and a one-line preview of the result so the
    next user message can be interpreted relative to it."""
    metric = intent.metric
    metric_col = intent.metric_column or ""
    metric_label = f"{metric}({metric_col})" if metric_col else metric
    filt_parts = []
    for f in intent.filters:
        filt_parts.append(f"{f.col} {f.op} {f.val!r}")
    filt = "; filters: " + ", ".join(filt_parts) if filt_parts else ""
    grouped = f"; grouped by {', '.join(intent.group_by)}" if intent.group_by else ""

    if not rows:
        head = "(no rows)"
    elif len(rows) == 1:
        head = ", ".join(f"{k}={v}" for k, v in list(rows[0].items())[:4])
    else:
        first = rows[0]
        head = (
            ", ".join(f"{k}={v}" for k, v in list(first.items())[:3])
            + f" … ({len(rows)} rows)"
        )

    return (
        f"Computed {metric_label} from {intent.entity}{grouped}{filt}. "
        f"Result: {head}."
    )
