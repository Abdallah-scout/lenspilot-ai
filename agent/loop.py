"""The LensPilot agent loop: plan -> act -> observe -> repeat, with a human checkpoint.

Owner: Abdallah.

Design rule: THE MODEL CHOOSES ACTIONS; THIS CODE CONTROLS PERMISSIONS,
VALIDATION, STATE TRANSITIONS AND STOPPING.

One ``step`` = one model call + execution of the tool calls it requested.
The *counted* unit for MAX_AGENT_STEPS is a tool-call attempt: every attempt
(success, failure, blocked, retry, invalid) increments ``state.steps`` and
the check ``steps >= max_steps`` happens BEFORE each attempt, so the count
can never exceed the limit.

Human approval is a persisted state, not a waiting loop: when the model's
``prepare_publication`` succeeds the run moves to WAITING_FOR_APPROVAL and
``step`` becomes a no-op. ``approve``/``reject`` resume it. The proposal's
tool_use stays unanswered until the human decides; the decision and the
publish result become that tool_result, so the model observes them.

All public methods load state from disk, act under a per-run lock and save,
so Streamlit reruns or double clicks cannot duplicate work.

The order of the steps is a LangGraph graph (agent/graph.py); the methods
below are what its nodes execute.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from typing import Callable

from langgraph.errors import GraphRecursionError

from agent.config import Limits, estimate_cost_usd
from agent import observability
from agent.graph import RUN_GRAPH, TURN_GRAPH, TURN_RECURSION_LIMIT, RunContext, TurnContext
from agent.prompts import SYSTEM_PROMPT
from agent.providers import ModelProvider, ProviderError
from agent.registry import ToolKind, ToolRegistry
from agent.state import AgentState, AgentStatus, ApprovalRecord, Proposal, ToolCallRecord
from agent.store import RunStore
from agent.tracing import Tracer, redact
from security import scan_for_injection
from tools.schemas import ToolContext, ToolResult, compute_proposal_hash

MAX_REQUEST_CHARS = 2000
# Errors after which continuing would be unsafe or pointless.
FATAL_CODES = {"sandbox_unavailable", "corrupted_data", "internal_error", "write_failed", "path_forbidden"}
# Calls refused by a control (as opposed to a tool that ran and failed).
BLOCKED_CODES = {"unknown_tool", "tool_not_allowed", "policy_denied", "duplicate_call", "not_retryable",
                 "one_proposal_at_a_time", "retry_limit_exceeded", "invalid_arguments"}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _run_lock(run_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(run_id, threading.Lock())


def _call_key(name: str, args: dict) -> str:
    return name + ":" + hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _tool_result_block(tool_use_id: str, payload: dict, is_error: bool) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(payload), "is_error": is_error}


class ApprovalMismatch(Exception):
    """The proposal the human saw is not the one pending (stale or altered)."""


class Agent:
    def __init__(self, *, provider: ModelProvider, registry: ToolRegistry, store: RunStore,
                 sandbox, approvals, quality, limits: Limits | None = None):
        self.provider = provider
        self.registry = registry
        self.store = store
        self.sandbox = sandbox
        self.approvals = approvals
        self.quality = quality
        self.limits = limits or Limits()

    # ------------------------------------------------------------------ public API
    def start(self, request: str, limits: Limits | None = None) -> AgentState:
        request = (request or "").strip()
        if not request:
            raise ValueError("Request is empty.")
        if len(request) > MAX_REQUEST_CHARS:
            raise ValueError(f"Request exceeds {MAX_REQUEST_CHARS} characters.")
        st = AgentState(
            run_id=f"run_{secrets.token_hex(6)}", request=request, provider=self.provider.name,
            model=self.provider.model, is_mock_provider=self.provider.is_mock, limits=limits or self.limits,
            messages=[{"role": "user", "content": request}],
        )
        st.transition(AgentStatus.RUNNING)
        self._tracer(st).emit("run_started", request=request, provider=st.provider, model=st.model,
                              is_mock_provider=st.is_mock_provider, limits=st.limits.model_dump())
        self.store.save(st)
        return st

    def step(self, run_id: str) -> AgentState:
        """Advance a RUNNING run by one model turn. No-op in any other status."""
        with _run_lock(run_id):
            st = self.store.load(run_id)
            if st.status != AgentStatus.RUNNING:
                return st  # waiting/terminal runs are never advanced implicitly
            if st.provider != self.provider.name:
                raise ValueError(f"Run {run_id} belongs to provider {st.provider!r}.")
            t0 = time.perf_counter()
            try:
                self._invoke_turn(st, "step")
            finally:
                st.active_seconds = round(st.active_seconds + time.perf_counter() - t0, 3)
                self._commit(st, was_terminal=False)
            return st

    def run(self, run_id: str, on_step: Callable[[AgentState], None] | None = None) -> AgentState:
        """Step until the run pauses or stops. Hard-bounded independently of the state checks."""
        st = self.store.load(run_id)
        # One graph step = one turn, so recursion_limit bounds the run even if every other check failed.
        try:
            RUN_GRAPH.invoke({"status": st.status.value}, context=RunContext(self, run_id, on_step),
                             config=self._graph_config(st, st.limits.max_llm_calls + 1))
        except GraphRecursionError:
            pass
        return self.store.load(run_id)

    def approve(self, run_id: str, proposal_hash: str) -> AgentState:
        with _run_lock(run_id):
            st = self.store.load(run_id)
            if not self._check_pending(st, proposal_hash):
                return st
            st.approval_history.append(ApprovalRecord(proposal_hash=proposal_hash, decision="approved"))
            self._tracer(st).emit("approval_decision", decision="approved", proposal_hash=proposal_hash)
            t0 = time.perf_counter()
            self._invoke_turn(st, "publish")
            st.active_seconds = round(st.active_seconds + time.perf_counter() - t0, 3)
            self._commit(st, was_terminal=False)
            return st

    def reject(self, run_id: str, proposal_hash: str, reason: str = "") -> AgentState:
        with _run_lock(run_id):
            st = self.store.load(run_id)
            if not self._check_pending(st, proposal_hash):
                return st
            st.approval_history.append(ApprovalRecord(proposal_hash=proposal_hash, decision="rejected",
                                                      reason=reason or None))
            self._tracer(st).emit("approval_decision", decision="rejected", proposal_hash=proposal_hash,
                                  reason=reason)
            st.pending_approval, st.pending_tool_results = None, []
            self._finish(st, AgentStatus.CANCELLED, "approval_rejected")
            st.final_result = "Publication rejected by the photographer. Nothing was written."
            self._commit(st, was_terminal=False)
            return st

    def cancel(self, run_id: str) -> AgentState:
        with _run_lock(run_id):
            st = self.store.load(run_id)
            if st.is_terminal:
                return st
            st.pending_approval, st.pending_tool_results = None, []
            self._finish(st, AgentStatus.CANCELLED, "user_cancelled")
            self._commit(st, was_terminal=False)
            return st

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _graph_config(st: AgentState, recursion_limit: int) -> dict:
        # Metadata reaches LangSmith when tracing is on: identifiers only, never content.
        return {"recursion_limit": recursion_limit,
                "metadata": {"session_id": st.run_id, "provider": st.provider, "model": st.model}}

    def _invoke_turn(self, st: AgentState, action: str) -> None:
        TURN_GRAPH.invoke({"action": action}, context=TurnContext(self, st),
                          config=self._graph_config(st, TURN_RECURSION_LIMIT))

    # ---- graph nodes (called by agent/graph.py, in this order)
    def _check_budget(self, st: AgentState) -> None:
        limit = self._budget_exhausted(st)
        if limit:
            self._tracer(st).emit("limit_enforced", limit=limit, steps=st.steps, llm_calls=st.llm_calls)
            self._finish(st, AgentStatus.STOPPED_LIMIT, limit)

    def _call_model(self, st: AgentState) -> list[dict]:
        """One model call. Returns the tool calls it requested ([] if the run ended)."""
        try:
            with observability.span("llm", "llm", ls_provider=st.provider, ls_model_name=st.model) as run:
                turn = self.provider.next_turn(SYSTEM_PROMPT, st.messages, self.registry.model_tool_definitions())
                if turn.input_tokens is not None and turn.output_tokens is not None:
                    observability.annotate(run, usage_metadata={
                        "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens,
                        "total_tokens": turn.input_tokens + turn.output_tokens})
        except ProviderError as exc:
            st.llm_calls += 1
            self._tracer(st).emit("llm_call", llm_call=st.llm_calls, ok=False, error_code=exc.code,
                                  error_message=exc.message)
            self._finish(st, AgentStatus.FAILED, exc.code)
            return []

        st.llm_calls += 1
        st.add_tokens(turn.input_tokens, turn.output_tokens)
        self._tracer(st).emit("llm_call", llm_call=st.llm_calls, ok=True, model=turn.model,
                              stop_reason=turn.stop_reason, input_tokens=turn.input_tokens,
                              output_tokens=turn.output_tokens, latency_ms=turn.latency_ms,
                              text=turn.text[:1000], tool_requests=[c["name"] for c in turn.tool_calls])
        st.messages.append({"role": "assistant", "content": turn.content})

        if turn.stop_reason == "refusal":
            self._finish(st, AgentStatus.FAILED, "model_refusal")
            return []
        if not turn.tool_calls:
            if turn.stop_reason == "max_tokens":
                self._finish(st, AgentStatus.FAILED, "model_output_truncated")
            else:
                st.final_result = turn.text or "(The model finished without a summary.)"
                self._finish(st, AgentStatus.COMPLETED, "task_completed")
        return turn.tool_calls

    def _run_tools(self, st: AgentState, calls: list[dict]) -> list[dict]:
        """Execute the requested calls. Returns the results to send with a pending proposal."""
        results: list[dict] = []
        for call in calls:
            if st.steps >= st.limits.max_steps:  # checked BEFORE every attempt
                self._tracer(st).emit("limit_enforced", limit="max_steps", refused_tool=call["name"],
                                      steps=st.steps)
                self._finish(st, AgentStatus.STOPPED_LIMIT, "max_steps_reached")
                return []
            result, fatal = self._execute_model_call(st, call)
            if fatal:
                self._finish(st, AgentStatus.FAILED, fatal)
                return []
            if call["name"] == "prepare_publication" and result.success:
                d = result.data
                st.pending_approval = Proposal(proposal_id=d["proposal_id"], proposal_hash=d["proposal_hash"],
                                               collection=d["collection"], destination=d["destination"],
                                               items=d["items"], tool_use_id=call["id"])
                continue  # its tool_result is sent after the human decides
            results.append(_tool_result_block(call["id"], result.model_dump(), is_error=not result.success))
        if st.pending_approval is None:
            st.messages.append({"role": "user", "content": results})
        return results

    def _request_approval(self, st: AgentState, results: list[dict]) -> None:
        st.pending_tool_results = results
        st.transition(AgentStatus.WAITING_FOR_APPROVAL)
        self._tracer(st).emit("approval_requested", proposal=st.pending_approval.model_dump())

    def _publish_approved(self, st: AgentState) -> None:
        """Only reachable from approve(), after _check_pending accepted the human decision."""
        p = st.pending_approval
        if st.steps >= st.limits.max_steps:  # the write is a counted step too
            self._tracer(st).emit("limit_enforced", limit="max_steps", refused_tool="publish_photo",
                                  steps=st.steps)
            return self._finish(st, AgentStatus.STOPPED_LIMIT, "max_steps_reached_before_publish")
        token = self.approvals.issue(st.run_id, p.proposal_hash, approver="human")
        args = {"run_id": st.run_id, "proposal_hash": p.proposal_hash, "collection": p.collection,
                "items": [{"photo_id": i["photo_id"], "caption": i["caption"]} for i in p.items],
                "approval_token": token}
        result = self._execute(st, "publish_photo", args, origin="orchestrator", retry_number=0)
        verified = result.success and self._verify_publication(result)
        if result.success:
            result.data["verified_in_sandbox"] = verified
            st.publications.extend(result.data["published"])
        payload = {"human_decision": "approved", "publish_result": result.model_dump()}
        st.messages.append({"role": "user", "content": st.pending_tool_results
                            + [_tool_result_block(p.tool_use_id, payload, is_error=not result.success)]})
        st.pending_approval, st.pending_tool_results = None, []
        if result.success and not verified:
            self._finish(st, AgentStatus.FAILED, "publish_verification_failed")
        elif not result.success and result.error_code in FATAL_CODES | {"approval_invalid"}:
            self._finish(st, AgentStatus.FAILED, f"publish_failed:{result.error_code}")
        else:
            st.transition(AgentStatus.RUNNING)  # model observes the result and summarises

    def _execute_model_call(self, st: AgentState, call: dict) -> tuple[ToolResult, str | None]:
        name, args = call["name"], call.get("input") or {}
        key = _call_key(name, args)
        retry_number = st.failure_counts.get(key, 0)
        spec = self.registry.get(name)
        refusal: ToolResult | None = None
        fatal = None
        if name == "prepare_publication" and st.pending_approval is not None:
            refusal = ToolResult.fail("one_proposal_at_a_time", "A proposal is already awaiting approval.")
        elif key in st.nonretryable_keys:
            refusal = ToolResult.fail("not_retryable", "This exact call already failed with a non-retryable "
                                      "error. Change the arguments or the plan.")
        elif retry_number > st.limits.max_tool_retries:
            refusal = ToolResult.fail("retry_limit_exceeded",
                                      f"{name} failed {retry_number} times; retry limit reached.")
            fatal = "retry_limit_exceeded"
        elif key in st.succeeded_keys and spec is not None and spec.kind == ToolKind.READ:
            refusal = ToolResult.fail("duplicate_call", "Identical call already succeeded; reuse that result.")

        if refusal is not None:
            st.steps += 1
            self._record(st, name, args, "model", refusal, 0.0, retry_number, [])
            return refusal, fatal
        result = self._execute(st, name, args, origin="model", retry_number=retry_number)
        if result.success:
            st.succeeded_keys.append(key)
        elif result.error_code not in BLOCKED_CODES:
            st.failure_counts[key] = retry_number + 1
            if not result.retryable:
                st.nonretryable_keys.append(key)
        else:
            st.nonretryable_keys.append(key)
        if not result.success and result.error_code in FATAL_CODES:
            fatal = f"unrecoverable_tool_error:{result.error_code}"
        return result, fatal

    def _execute(self, st: AgentState, name: str, args: dict, origin: str, retry_number: int) -> ToolResult:
        ctx = ToolContext(sandbox=self.sandbox, approvals=self.approvals, quality=self.quality, run_id=st.run_id)
        # The span name comes from the registry, never from the model's text.
        spec = self.registry.get(name)
        with observability.span(name if spec else "unknown_tool", "tool", origin=origin,
                                retry_number=retry_number) as run:
            result, duration = self.registry.dispatch(name, args, ctx, origin=origin)
            flags = scan_for_injection(json.dumps(result.data, default=str)) if result.success else []
            observability.annotate(run, success=result.success, error_code=result.error_code,
                                   injection_flags=flags or None)
        st.steps += 1
        self._record(st, name, args, origin, result, duration, retry_number, flags)
        return result

    def _record(self, st, name, args, origin, result: ToolResult, duration, retry_number, flags) -> None:
        if result.success:
            status = "success"
        else:
            status = "blocked" if result.error_code in BLOCKED_CODES else "error"
        rec = ToolCallRecord(step=st.steps, tool=name, origin=origin, arguments=redact(args), status=status,
                             error_code=result.error_code, error_message=result.error_message,
                             retry_number=retry_number, duration_ms=duration, injection_flags=flags)
        st.tool_history.append(rec)
        self._tracer(st).emit("tool_call", **rec.model_dump(exclude={"timestamp"}), retryable=result.retryable)

    def _check_pending(self, st: AgentState, proposal_hash: str) -> bool:
        """True if a decision on ``proposal_hash`` may be applied now."""
        if st.status != AgentStatus.WAITING_FOR_APPROVAL or st.pending_approval is None:
            # Double click / rerun after the decision was applied: idempotent no-op.
            self._tracer(st).emit("approval_ignored", status=st.status.value, proposal_hash=proposal_hash)
            return False
        p = st.pending_approval
        if proposal_hash != p.proposal_hash or compute_proposal_hash(p.collection, p.items) != p.proposal_hash:
            st.approval_history.append(ApprovalRecord(proposal_hash=proposal_hash, decision="mismatch"))
            self._tracer(st).emit("approval_decision", decision="mismatch", proposal_hash=proposal_hash,
                                  expected=p.proposal_hash)
            self.store.save(st)
            raise ApprovalMismatch("The approved proposal does not match the pending one.")
        return True

    def _verify_publication(self, result: ToolResult) -> bool:
        for item in result.data["published"]:
            rec = self.sandbox.get_publication(item["publication_key"])
            if rec is None or rec["photo_id"] != item["photo_id"]:
                return False
            if not (self.sandbox.published_dir / rec["image_file"]).is_file():
                return False
        return True

    @staticmethod
    def _budget_exhausted(st: AgentState) -> str | None:
        lim = st.limits
        if st.llm_calls >= lim.max_llm_calls:
            return "max_llm_calls_reached"
        if st.active_seconds >= lim.max_active_seconds:
            return "time_budget_exhausted"
        if lim.max_total_tokens is not None and (st.input_tokens or 0) + (st.output_tokens or 0) >= lim.max_total_tokens:
            return "token_budget_exhausted"
        return None

    @staticmethod
    def _finish(st: AgentState, status: AgentStatus, reason: str) -> None:
        st.transition(status)
        st.stop_reason = reason

    def _commit(self, st: AgentState, was_terminal: bool) -> None:
        if st.is_terminal and not was_terminal:
            self._tracer(st).emit(
                "run_finished", status=st.status.value, stop_reason=st.stop_reason, steps=st.steps,
                max_steps=st.limits.max_steps, llm_calls=st.llm_calls, active_seconds=st.active_seconds,
                input_tokens=st.input_tokens, output_tokens=st.output_tokens,
                cost_usd=estimate_cost_usd(st.model, st.input_tokens, st.output_tokens),
                is_mock_provider=st.is_mock_provider, publications=len(st.publications),
                final_result=st.final_result)
        self.store.save(st)

    def _tracer(self, st: AgentState) -> Tracer:
        return Tracer(self.store.run_dir(st.run_id), st.run_id)
