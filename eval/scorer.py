"""Trajectory scorer for agent evaluation. Owner: Yasmine.

Computes step accuracy, tool-call precision, wasted steps, recovery success,
and task completion metrics according to the CS496 grading rubric.
"""

from __future__ import annotations

from typing import Any
from eval.models import EvaluationResult, Scenario


class TrajectoryScorer:
    """Evaluates agent trajectory and metrics against scenario expectations."""

    @staticmethod
    def score(scenario: Scenario, trace_summary: dict[str, Any], publications_count: int) -> EvaluationResult:
        actual_status = trace_summary.get("status") or "UNKNOWN"
        actual_stop_reason = trace_summary.get("stop_reason") or "UNKNOWN"
        actual_tools = trace_summary.get("tool_sequence", [])
        actual_steps = trace_summary.get("steps", len(actual_tools))
        retries = trace_summary.get("retries", 0)
        active_seconds = float(trace_summary.get("active_seconds") or 0.0)
        is_mock_val = trace_summary.get("is_mock_provider")
        is_mock = is_mock_val if is_mock_val is not None else True
        injection_flags = trace_summary.get("injection_flags", [])

        failure_reasons: list[str] = []

        # 1. Status verification
        if actual_status != scenario.expected_status:
            failure_reasons.append(
                f"Status mismatch: expected {scenario.expected_status!r}, got {actual_status!r}"
            )

        # 2. Stop reason verification
        if scenario.expected_stop_reason and actual_stop_reason != scenario.expected_stop_reason:
            failure_reasons.append(
                f"Stop reason mismatch: expected {scenario.expected_stop_reason!r}, got {actual_stop_reason!r}"
            )

        # 3. Publication count verification
        if scenario.expected_publications_count is not None:
            if publications_count != scenario.expected_publications_count:
                failure_reasons.append(
                    f"Publication count mismatch: expected {scenario.expected_publications_count}, got {publications_count}"
                )

        # 4. Injection flags verification
        if scenario.expected_injection_flags:
            missing_flags = [f for f in scenario.expected_injection_flags if f not in injection_flags]
            if missing_flags:
                failure_reasons.append(f"Missing expected injection flags: {missing_flags}")

        # 5. Recovery check
        had_fault_or_retry = retries > 0 or bool(scenario.faults)
        recovered: bool | None = None
        if had_fault_or_retry:
            recovered = (actual_status == scenario.expected_status) and (len(failure_reasons) == 0)
            if scenario.should_recover and not recovered:
                failure_reasons.append("Agent failed to recover from transient fault or retry")

        # 6. Step Accuracy calculation
        step_accuracy = TrajectoryScorer.calculate_step_accuracy(
            actual_sequence=actual_tools,
            expected_sequence=scenario.expected_tool_sequence,
        )

        # 7. Tool Precision calculation
        tool_precision = TrajectoryScorer.calculate_tool_precision(
            actual_sequence=actual_tools,
            expected_sequence=scenario.expected_tool_sequence,
        )

        # 8. Wasted Steps calculation
        optimal = scenario.optimal_step_count
        if optimal is not None:
            wasted_steps = max(0, actual_steps - optimal)
        else:
            wasted_steps = 0

        task_success = len(failure_reasons) == 0
        passed = task_success and (step_accuracy >= 0.70)

        return EvaluationResult(
            scenario_id=scenario.id,
            category=scenario.category,
            name=scenario.name,
            passed=passed,
            task_success=task_success,
            step_accuracy=step_accuracy,
            tool_call_precision=tool_precision,
            wasted_steps=wasted_steps,
            recovered=recovered,
            actual_status=actual_status,
            actual_stop_reason=actual_stop_reason,
            actual_steps=actual_steps,
            actual_tool_sequence=actual_tools,
            actual_publications_count=publications_count,
            active_seconds=active_seconds,
            input_tokens=trace_summary.get("input_tokens"),
            output_tokens=trace_summary.get("output_tokens"),
            cost_usd=trace_summary.get("cost_usd"),
            is_mock_provider=is_mock,
            injection_flags=injection_flags,
            failure_reasons=failure_reasons,
        )

    @staticmethod
    def calculate_step_accuracy(actual_sequence: list[str], expected_sequence: list[str] | None) -> float:
        """Measure positional correctness of tool calls."""
        if expected_sequence is None:
            return 1.0
        if not expected_sequence and not actual_sequence:
            return 1.0
        if not expected_sequence or not actual_sequence:
            return 0.0

        matches = 0
        min_len = min(len(actual_sequence), len(expected_sequence))
        for i in range(min_len):
            if actual_sequence[i] == expected_sequence[i]:
                matches += 1

        denom = max(len(actual_sequence), len(expected_sequence))
        return round(matches / denom, 4)

    @staticmethod
    def calculate_tool_precision(actual_sequence: list[str], expected_sequence: list[str] | None) -> float:
        """Measure what fraction of executed tools were relevant."""
        if expected_sequence is None:
            return 1.0
        if not actual_sequence:
            return 1.0 if not expected_sequence else 0.0

        expected_set = set(expected_sequence)
        valid_calls = sum(1 for tool in actual_sequence if tool in expected_set)
        return round(valid_calls / len(actual_sequence), 4)
