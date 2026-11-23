"""The LangGraph loop and the LangSmith opt-in (owner of the safety checks: Skander)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent import observability
from agent.config import Limits
from agent.graph import RUN_GRAPH, TURN_GRAPH, TurnContext
from agent.mock_providers import LoopingMockProvider
from agent.state import AgentStatus
from tests.conftest import HAPPY_TURNS, published_count

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------- LangGraph
def test_publish_is_only_reachable_from_the_approval_entry():
    edges = {(e.source, e.target) for e in TURN_GRAPH.get_graph().edges}
    assert {s for s, t in edges if t == "publish"} == {"__start__"}
    assert {t for s, t in edges if s == "wait_for_human"} == {"__end__"}
    assert "publish" not in {t for s, t in edges if s in ("call_model", "run_tools")}


def test_run_pauses_for_the_human_then_publishes(make_agent, services):
    agent, _ = make_agent(HAPPY_TURNS)
    st = agent.run(agent.start("three photos").run_id)
    assert st.status == AgentStatus.WAITING_FOR_APPROVAL and published_count(services) == 0
    assert agent.run(st.run_id).status == AgentStatus.WAITING_FOR_APPROVAL  # running again changes nothing
    agent.approve(st.run_id, st.pending_approval.proposal_hash)
    assert agent.run(st.run_id).status == AgentStatus.COMPLETED and published_count(services) == 2


def test_publish_node_without_a_pending_proposal_writes_nothing(make_agent, services):
    """Even calling the graph's publish entry directly cannot publish without a proposal."""
    agent, _ = make_agent(HAPPY_TURNS)
    st = agent.start("x")
    with pytest.raises(AttributeError):
        TURN_GRAPH.invoke({"action": "publish"}, context=TurnContext(agent, st))
    assert published_count(services) == 0


def test_graph_survives_a_new_agent_object(make_agent, services):
    """No in-memory checkpoint: a restarted process resumes from state.json alone."""
    agent, _ = make_agent(HAPPY_TURNS)
    st = agent.run(agent.start("x").run_id)
    fresh, _ = make_agent([HAPPY_TURNS[-1]])
    assert fresh.approve(st.run_id, st.pending_approval.proposal_hash).publications
    assert published_count(services) == 2


@pytest.mark.parametrize("calls", [1, 2, 5])
def test_model_call_limit_ends_the_run(make_agent, calls):
    agent, _ = make_agent(provider=LoopingMockProvider(), limits=Limits(max_llm_calls=calls, max_steps=50))
    st = agent.run(agent.start("loop").run_id)
    assert st.status == AgentStatus.STOPPED_LIMIT and st.llm_calls <= calls


def test_recursion_limit_is_an_independent_stop(make_agent, monkeypatch):
    """With the budget check disabled on purpose, the graph's own bound still ends the loop."""
    agent, _ = make_agent(provider=LoopingMockProvider(), limits=Limits(max_llm_calls=3, max_steps=50))
    monkeypatch.setattr(type(agent), "_check_budget", lambda self, st: None)
    turns = []
    agent.run(agent.start("loop").run_id, on_step=turns.append)
    assert 1 <= len(turns) <= 4


def test_cancel_between_turns_stops_the_graph(make_agent):
    agent, _ = make_agent(provider=LoopingMockProvider())
    run_id = agent.start("loop").run_id
    st = agent.run(run_id, on_step=lambda s: agent.cancel(run_id))
    assert st.status == AgentStatus.CANCELLED and st.llm_calls == 1


def test_graph_state_holds_only_routing_values(make_agent):
    agent, _ = make_agent(HAPPY_TURNS)
    out = RUN_GRAPH.get_output_jsonschema()["properties"]
    assert set(out) == {"status", "steps", "llm_calls"}
    assert set(TURN_GRAPH.get_output_jsonschema()["properties"]) == {
        "action", "status", "steps", "llm_calls", "has_calls", "proposal_pending"}


# ----------------------------------------------------------------- LangSmith
def test_tracing_is_off_under_pytest():
    assert observability.ON is False and os.environ["LANGSMITH_TRACING"] == "false"
    from langsmith.utils import tracing_is_enabled
    assert not tracing_is_enabled()


def test_env_file_is_not_loaded_during_collection():
    """config.load_env_file runs at import, before PYTEST_CURRENT_TEST exists."""
    import agent.config as config
    assert "pytest" in sys.modules and 'if "pytest" in sys.modules' in Path(config.__file__).read_text("utf-8")


@pytest.mark.parametrize("env, expected", [
    ({}, False),
    ({"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "k"}, False),  # leftover from another project
    ({"LENSPILOT_LANGSMITH": "1"}, False),  # no key
    ({"LENSPILOT_LANGSMITH": "1", "LANGSMITH_API_KEY": "k"}, True),
])
def test_tracing_needs_an_explicit_opt_in(env, expected):
    # An empty value keeps .env from filling it in: load_env_file uses setdefault.
    base = {k: v for k, v in os.environ.items() if not k.startswith(("LANGSMITH", "LANGCHAIN", "LENSPILOT_LANGSMITH"))}
    blank = {"LENSPILOT_LANGSMITH": "", "LANGSMITH_API_KEY": ""}
    code = ("import json, os, agent.observability as o; print(json.dumps([o.ON, os.environ['LANGSMITH_TRACING'], "
            "os.environ.get('LANGSMITH_HIDE_INPUTS'), os.environ.get('LANGSMITH_HIDE_OUTPUTS')]))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**base, **blank, **env},
                         capture_output=True, text=True, check=True).stdout
    on, tracing, hide_in, hide_out = json.loads(out)
    assert on is expected and tracing == str(expected).lower()
    if expected:
        assert hide_in == hide_out == "true"
