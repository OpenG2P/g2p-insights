"""Pydantic schemas for /ask and /ask/clarify endpoints."""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

from ..services.cost_gate_service import CostReport
from ..services.llm_service import Clarification, DirectAnswer, ExpertRedirect, VizSpec
from ..services.pii_service import PiiRedaction


# ---------- Requests ---------------------------------------------------------

class AskRequest(BaseModel):
    """Initial natural-language question (turn 1)."""

    question: str
    session_id: Optional[str] = None
    user: Optional[str] = None
    auto_run_yellow: bool = False


class ClarifyRequest(BaseModel):
    """Subsequent turn — user has answered a clarification."""

    session_id: str
    user: Optional[str] = None
    auto_run_yellow: bool = False
    # The user's response. Either a chosen option id or free text — both supported.
    option_id: Optional[str] = None
    text: Optional[str] = None


# ---------- Responses --------------------------------------------------------

ResponseStatus = Literal[
    "ok",                # query executed, rows returned
    "needs_clarification",
    "needs_confirmation",  # YELLOW cost; user must confirm
    "rejected",            # RED cost — won't run
    "expert_redirect",     # question needs statistical analysis; use Expert Lab
    "direct_answer",       # LLM answered a meta-question directly (no SQL)
]


class AskResponse(BaseModel):
    status: ResponseStatus
    session_id: str

    # Clarification (present only when status == "needs_clarification")
    clarification: Optional[Clarification] = None

    # Expert-lab redirect (present only when status == "expert_redirect").
    # The frontend renders a card with a button that deep-links to the
    # Expert Lab page with the function + params pre-filled.
    expert_redirect: Optional[ExpertRedirect] = None

    # Direct answer to a meta-question about the system (present only when
    # status == "direct_answer"). No SQL, no rows — just text.
    direct_answer: Optional[DirectAnswer] = None

    # Always present once intent is committed — the user can inspect the SQL
    # at any time. We DO NOT block on user approval; the cost gate decides
    # whether to run automatically.
    sql: Optional[str] = None

    cost: Optional[CostReport] = None
    rows: List[dict] = Field(default_factory=list)
    viz_spec: Optional[VizSpec] = None
    assumptions: List[str] = Field(default_factory=list)
    pii_redactions: List[PiiRedaction] = Field(default_factory=list)
    k_suppressions: int = 0

    # Echo of the conversation so far — useful for the UI to render the thread
    # without having to keep state itself.
    messages: List[dict] = Field(default_factory=list)


# ---------- Confirmation (re-run a YELLOW query the user approves) -----------

class ConfirmRunRequest(BaseModel):
    session_id: str
    user: Optional[str] = None


# ---------- Misc -------------------------------------------------------------

class AssumptionsOnlyResponse(BaseModel):
    """Helper response shape used by the dashboard preview endpoint."""

    status: str
    sql: Optional[str] = None
    assumptions: List[str] = Field(default_factory=list)
    extra: Optional[Any] = None
