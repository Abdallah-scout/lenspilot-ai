"""Stopping conditions: proof that limits hold (requirements 8-12)."""

import pytest

from agent.config import Limits
from agent.mock_providers import LoopingMockProvider, ScriptedProvider, text, tool_use
from agent.state import AgentState, AgentStatus, InvalidTransition
from agent.tracing import read_trace


@pytest.mark.parametrize("max_steps", [1, 3, 8])
def test_looping_model_is_stopped_at_exactly_max_steps(make_agent, max_steps):  # 8, 9
    agent, _ = make_agent(provider=LoopingMockProvider(), limits=Limits(max_steps=max_steps, max_llm_calls=100))
    st = agent.run(agent.start("loop forever").run_id)
    assert st.status == AgentStatus.STOPPED_LIMIT and st.stop_reason == "max_steps_reached"
    assert st.steps == max_steps == len(st.tool_history)
    events = read_trace(agent.store.run_dir(st.run_id) / "trace.jsonl")
    tool_events = [e for e in events if e["event"] == "tool_call"]
    assert len(tool_events) == max_steps and max(e["step"] for e in tool_events) == max_steps


def test_parallel_tool_requests_cannot_overshoot_limit(make_agent):  # 9
    burst = [tool_use("list_photos", {"category": c}) for c in ["restaurant", "interior"] * 5]
    agent, _ = make_agent([burst], limits=Limits(max_steps=3))
    st = agent.run(agent.start("x").run_id)
    assert st.steps == 3 and st.status == AgentStatus.STOPPED_LIMIT


def test_retries_count_toward_steps_and_retry_limit(make_agent):  # 10
    call = tool_use("evaluate_photo_quality", {"photo_ids": ["rest_001"]})
    agent, _ = make_agent([[call]] * 6, faults={"evaluate_photo_quality": 10})
    st = agent.run(agent.start("x").run_id)
    # 1 attempt + 2 retries executed, 4th attempt refused -> run fails.
    assert [r.retry_number for r in st.tool_history] == [0, 1, 2, 3]
    assert st.tool_history[-1].error_code == "retry_limit_exceeded"
    assert st.steps == 4 and st.status == AgentStatus.FAILED and st.stop_reason == "retry_limit_exceeded"


def test_llm_call_budget_stops_text_free_runaway(make_agent):
    agent, _ = make_agent(provider=LoopingMockProvider(), limits=Limits(max_steps=50, max_llm_calls=4))
    st = agent.run(agent.start("x").run_id)
    assert st.llm_calls == 4 and st.stop_reason == "max_llm_calls_reached"


def test_time_budget(make_agent):
    agent, _ = make_agent(provider=LoopingMockProvider(), limits=Limits(max_active_seconds=0.000001))
    st = agent.run(agent.start("x").run_id)
    assert st.status == AgentStatus.STOPPED_LIMIT and st.stop_reason == "time_budget_exhausted"


def test_cancellation_prevents_further_execution(make_agent):  # 11
    agent, prov = make_agent([[tool_use("list_photos", {})]] * 5)
    st = agent.start("x")
    agent.step(st.run_id)
    st = agent.cancel(st.run_id)
    assert st.status == AgentStatus.CANCELLED and st.stop_reason == "user_cancelled"
    st = agent.run(st.run_id)
    assert st.steps == 1 and len(prov.received) == 1  # no more model or tool calls


def test_completed_run_cannot_restart(make_agent):  # 12
    agent, prov = make_agent([[text("done")], [tool_use("list_photos", {})]])
    st = agent.run(agent.start("x").run_id)
    assert st.status == AgentStatus.COMPLETED
    for _ in range(3):
        st = agent.step(st.run_id)
    assert st.status == AgentStatus.COMPLETED and len(prov.received) == 1 and st.steps == 0


def test_terminal_states_have_no_outgoing_transitions():
    for terminal in [AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED, AgentStatus.STOPPED_LIMIT]:
        st = AgentState(run_id="run_000000000000", request="x", provider="p", model="m",
                        is_mock_provider=True, status=terminal)
        for target in AgentStatus:
            with pytest.raises(InvalidTransition):
                st.transition(target)


def test_waiting_for_approval_consumes_no_llm_calls(make_agent):
    turns = [[tool_use("prepare_publication", {"collection": "restaurants", "items": [
        {"photo_id": "rest_001", "caption": "Candlelit dining room at dusk."}]})]]
    agent, prov = make_agent(turns)
    st = agent.run(agent.start("x").run_id)
    assert st.status == AgentStatus.WAITING_FOR_APPROVAL
    for _ in range(10):
        st = agent.step(st.run_id)
    assert len(prov.received) == 1 and st.llm_calls == 1
