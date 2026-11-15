"""Stop-rule edge cases (owner: Skander).

tests/test_stopping.py (Abdallah) proves the main limits. These tests probe
the boundaries: bursts larger than the budget, a proposal on the last step,
an exact budget, zero retries, the token budget, and decisions that arrive
after the run has already ended.
"""

from __future__ import annotations

import pytest

from agent.config import Limits
from agent.mock_providers import LoopingMockProvider, text, tool_use
from agent.providers import ModelTurn
from agent.state import AgentStatus
from conftest import published_count

PROPOSE = tool_use("prepare_publication", {"collection": "restaurants", "items": [
    {"photo_id": "rest_001", "caption": "Candlelit dining room at dusk."}]})


@pytest.mark.parametrize("max_steps", [1, 2, 3, 5])
@pytest.mark.parametrize("burst", [1, 4, 10])
def test_steps_never_exceed_the_limit(make_agent, max_steps, burst):
    """Grid over budget x tool calls per model turn: the count is checked BEFORE each attempt."""
    turn = [tool_use("list_photos", {"category": c}) for c in (["restaurant", "interior"] * 5)[:burst]]
    agent, _ = make_agent([turn] * 20, limits=Limits(max_steps=max_steps, max_llm_calls=50))
    st = agent.run(agent.start("x").run_id)
    assert st.steps <= max_steps and len(st.tool_history) == st.steps


def test_proposal_on_the_last_step_cannot_publish(make_agent, services):
    agent, _ = make_agent([[tool_use("list_photos", {})], [PROPOSE]], limits=Limits(max_steps=2))
    st = agent.run(agent.start("x").run_id)
    assert st.status == AgentStatus.WAITING_FOR_APPROVAL and st.steps == 2
    st = agent.approve(st.run_id, st.pending_approval.proposal_hash)
    assert st.status == AgentStatus.STOPPED_LIMIT and st.stop_reason == "max_steps_reached_before_publish"
    assert st.steps == 2 and published_count(services) == 0


def test_exact_budget_is_enough(make_agent, services):
    """Propose (step 1) + publish (step 2) fits exactly in max_steps=2: no off-by-one."""
    agent, _ = make_agent([[PROPOSE], [text("Published.")]], limits=Limits(max_steps=2))
    st = agent.run(agent.start("x").run_id)
    st = agent.run(agent.approve(st.run_id, st.pending_approval.proposal_hash).run_id)
    assert st.status == AgentStatus.COMPLETED and st.steps == 2 and published_count(services) == 1


def test_zero_retries_allowed(make_agent):
    call = tool_use("list_photos", {})
    agent, _ = make_agent([[call]] * 3, faults={"list_photos": 5}, limits=Limits(max_tool_retries=0))
    st = agent.run(agent.start("x").run_id)
    assert [r.error_code for r in st.tool_history] == ["tool_unavailable", "retry_limit_exceeded"]
    assert st.status == AgentStatus.FAILED


class _TokenHungryProvider:
    name, model, is_mock = "mock-token-hungry", "mock-token-hungry", True

    def __init__(self):
        self.calls = 0

    def next_turn(self, system, messages, tools):
        self.calls += 1
        return ModelTurn(content=[tool_use("list_photos", {"category": ["restaurant", "interior"][self.calls % 2]})],
                         stop_reason="tool_use", model=self.model, input_tokens=70_000, output_tokens=10_000)


def test_token_budget_stops_the_run(make_agent):
    prov = _TokenHungryProvider()
    agent, _ = make_agent(provider=prov, limits=Limits(max_steps=50, max_llm_calls=50, max_total_tokens=200_000))
    st = agent.run(agent.start("x").run_id)
    assert st.status == AgentStatus.STOPPED_LIMIT and st.stop_reason == "token_budget_exhausted"
    assert prov.calls == 3  # 80k, 160k, 240k -> stop before a 4th call


def test_run_loop_is_bounded_even_without_tool_calls_counting(make_agent):
    """A model that loops forever is stopped by the LLM-call cap even with a huge step budget."""
    agent, _ = make_agent(provider=LoopingMockProvider(), limits=Limits(max_steps=50, max_llm_calls=3))
    st = agent.run(agent.start("x").run_id)
    assert st.llm_calls == 3 and st.status == AgentStatus.STOPPED_LIMIT


def test_approval_after_rejection_does_nothing(make_agent, services):
    agent, _ = make_agent([[PROPOSE]])
    st = agent.run(agent.start("x").run_id)
    h = st.pending_approval.proposal_hash
    agent.reject(st.run_id, h)
    st = agent.approve(st.run_id, h)
    assert st.status == AgentStatus.CANCELLED and published_count(services) == 0


def test_approval_after_limit_stop_does_nothing(make_agent, services):
    agent, _ = make_agent([[PROPOSE]], limits=Limits(max_steps=1))
    st = agent.run(agent.start("x").run_id)
    h = st.pending_approval.proposal_hash
    agent.approve(st.run_id, h)            # refused: no step left
    st = agent.approve(st.run_id, h)       # a second click on a terminal run
    assert st.status == AgentStatus.STOPPED_LIMIT and published_count(services) == 0
