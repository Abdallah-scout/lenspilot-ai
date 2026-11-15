"""Evaluation data structures and schemas. Owner: Yasmine.

Defines the contract for benchmark scenarios, trajectory scoring outputs,
and aggregate performance metrics.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, Field

from agent.config import Limits


class ScenarioCategory(str, Enum):
    NORMAL = "normal"
    FAILURE = "failure"
    SAFETY = "safety"
    INJECTION = "injection"


class HumanDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    NONE = "none"
    TAMPER = "tamper"


class Scenario(BaseModel):
    id: str = Field(..., description="Unique scenario ID, e.g. NORM-01, FAIL-01, SAFE-01, INJ-01")
    category: ScenarioCategory = Field(..., description="Evaluation category")
    name: str = Field(..., description="Short human-readable title")
    description: str = Field(..., description="Detailed description of what is being tested")
    request: str = Field(..., description="User prompt fed to the agent")
    provider_type: str = Field("mock-heuristic", description="Provider to use: mock-heuristic, mock-scripted, mock-looping, mock-compromised, or anthropic")
    scripted_turns: list[list[dict[str, Any]]] | None = Field(None, description="Turns if using mock-scripted")
    faults: dict[str, int] | None = Field(None, description="Fault injection counts per tool, e.g. {'list_photos': 1}")
    limits: Limits | None = Field(None, description="Custom limits if testing budget or step stops")
    decision: HumanDecision = Field(HumanDecision.APPROVE, description="Human approval decision to simulate")
    expected_status: str = Field("COMPLETED", description="Expected terminal agent status")
    expected_stop_reason: str | None = Field(None, description="Expected terminal stop reason")
    expected_tool_sequence: list[str] | None = Field(None, description="Expected sequence of tools executed")
    expected_publications_count: int | None = Field(None, description="Expected published records in sandbox")
    expected_injection_flags: list[str] | None = Field(None, description="Flags that must be detected in trace")
    should_recover: bool = Field(False, description="Whether this scenario tests recovery after a fault")
    optimal_step_count: int | None = Field(None, description="Theoretical minimum steps for wasted-step calculation")


class EvaluationResult(BaseModel):
    scenario_id: str
    category: ScenarioCategory
    name: str
    passed: bool
    task_success: bool
    step_accuracy: float = Field(..., description="Fraction of steps matching expected sequence (0.0 to 1.0)")
    tool_call_precision: float = Field(..., description="Precision score of tool calls against target tool set (0.0 to 1.0)")
    wasted_steps: int = Field(0, description="Steps executed above optimal minimum")
    recovered: bool | None = Field(None, description="True if recovered from an injected fault or retry")
    actual_status: str
    actual_stop_reason: str
    actual_steps: int
    actual_tool_sequence: list[str]
    actual_publications_count: int
    active_seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    is_mock_provider: bool | None = True
    injection_flags: list[str] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)


class SuiteSummary(BaseModel):
    total_scenarios: int
    passed_scenarios: int
    failed_scenarios: int
    pass_rate: float
    task_success_rate: float
    avg_step_accuracy: float
    avg_tool_precision: float
    total_wasted_steps: int
    avg_wasted_steps: float
    recovery_scenarios_count: int
    recovery_success_count: int
    recovery_rate: float
    p50_latency_seconds: float
    p95_latency_seconds: float
    avg_latency_seconds: float
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    projected_100x_cost_usd: float
    category_summaries: dict[str, dict[str, Any]]
