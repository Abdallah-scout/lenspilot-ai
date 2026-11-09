"""Agent execution state and its legal transitions. Owner: Abdallah.

State is a plain Pydantic model persisted as JSON after every operation, so
a run survives Streamlit reruns and process restarts, and Yasmine's eval can
load it without importing the UI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from agent.config import Limits


class AgentStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    STOPPED_LIMIT = "STOPPED_LIMIT"


TERMINAL = {AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED, AgentStatus.STOPPED_LIMIT}

ALLOWED_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
    AgentStatus.IDLE: {AgentStatus.RUNNING, AgentStatus.CANCELLED},
    AgentStatus.RUNNING: {AgentStatus.WAITING_FOR_APPROVAL} | TERMINAL,
    # Approve -> RUNNING; reject/cancel -> CANCELLED; failed publish -> FAILED;
    # step budget exhausted at approval time -> STOPPED_LIMIT.
    AgentStatus.WAITING_FOR_APPROVAL: {AgentStatus.RUNNING} | TERMINAL - {AgentStatus.COMPLETED},
    **{s: set() for s in TERMINAL},  # terminal states can never be left (no silent restart)
}


class InvalidTransition(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ToolCallRecord(BaseModel):
    step: int
    tool: str
    origin: str  # "model" | "orchestrator"
    arguments: dict
    status: str  # "success" | "error" | "blocked"
    error_code: str | None = None
    error_message: str | None = None
    retry_number: int = 0
    duration_ms: float = 0.0
    injection_flags: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=utcnow)


class Proposal(BaseModel):
    proposal_id: str
    proposal_hash: str
    collection: str
    destination: str
    items: list[dict]  # {photo_id, caption, title}
    tool_use_id: str  # the model's prepare_publication call awaiting its result


class ApprovalRecord(BaseModel):
    proposal_hash: str
    decision: str  # "approved" | "rejected" | "ignored_duplicate" | "mismatch"
    approver: str = "human"
    reason: str | None = None
    timestamp: str = Field(default_factory=utcnow)


class AgentState(BaseModel):
    run_id: str
    request: str
    provider: str
    model: str
    is_mock_provider: bool
    status: AgentStatus = AgentStatus.IDLE
    limits: Limits = Field(default_factory=Limits)

    steps: int = 0  # tool-call attempts (the counted unit)
    llm_calls: int = 0
    active_seconds: float = 0.0
    input_tokens: int | None = None  # None = unavailable (e.g. mock), never fake 0
    output_tokens: int | None = None

    messages: list[dict] = Field(default_factory=list)  # Anthropic message format
    tool_history: list[ToolCallRecord] = Field(default_factory=list)
    failure_counts: dict[str, int] = Field(default_factory=dict)  # call-key -> failures
    nonretryable_keys: list[str] = Field(default_factory=list)
    succeeded_keys: list[str] = Field(default_factory=list)

    pending_approval: Proposal | None = None
    pending_tool_results: list[dict] = Field(default_factory=list)
    approval_history: list[ApprovalRecord] = Field(default_factory=list)
    publications: list[dict] = Field(default_factory=list)

    started_at: str = Field(default_factory=utcnow)
    ended_at: str | None = None
    final_result: str | None = None
    stop_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def transition(self, new: AgentStatus) -> None:
        if new not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransition(f"{self.status.value} -> {new.value} is not allowed")
        self.status = new
        if new in TERMINAL:
            self.ended_at = utcnow()

    def add_tokens(self, input_tokens: int | None, output_tokens: int | None) -> None:
        if input_tokens is not None:
            self.input_tokens = (self.input_tokens or 0) + input_tokens
        if output_tokens is not None:
            self.output_tokens = (self.output_tokens or 0) + output_tokens
