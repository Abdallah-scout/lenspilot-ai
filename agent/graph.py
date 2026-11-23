"""The agent loop expressed as LangGraph state graphs.

Two small graphs, both compiled without a checkpointer:

  TURN GRAPH (one model turn, or one approved publication)
    START -> check_budget -> call_model -> run_tools -> wait_for_human -> END
    START -> publish -> END
  RUN GRAPH (repeats turns until the run pauses or stops)
    START -> turn -> turn -> ... -> END

Why no LangGraph checkpointer / interrupt(): the human pause is already a
persisted state (WAITING_FOR_APPROVAL in var/runs/<id>/state.json) that is
reloaded under a lock for every decision. A second copy of the state in a
checkpointer could disagree with it, so state.json stays the only truth.

The graph state carries only routing values. The run itself (messages, photo
data, proposal) travels in the runtime context, which is not part of the
graph's inputs or outputs, so a tracer (LangSmith) never receives it.

The graphs only decide the ORDER of the steps. Limits, permissions and
approvals stay enforced in agent/loop.py, the registry and security/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import agent.observability  # noqa: F401  (must come before langgraph)
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from typing_extensions import TypedDict  # pydantic needs this one on Python < 3.12

from agent.state import AgentState, AgentStatus

TURN_RECURSION_LIMIT = 10  # the longest path has 4 nodes


class TurnState(TypedDict, total=False):
    action: str  # "step" | "publish"
    status: str
    steps: int
    llm_calls: int
    has_calls: bool
    proposal_pending: bool


@dataclass
class TurnContext:
    agent: Any  # agent.loop.Agent
    st: AgentState
    calls: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)


class RunLoopState(TypedDict, total=False):
    status: str
    steps: int
    llm_calls: int


@dataclass
class RunContext:
    agent: Any
    run_id: str
    on_step: Callable[[AgentState], None] | None = None


def _summary(st: AgentState) -> dict:
    return {"status": st.status.value, "steps": st.steps, "llm_calls": st.llm_calls}


# ---- turn graph nodes ------------------------------------------------------
def check_budget(state: TurnState, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    ctx.agent._check_budget(ctx.st)
    return _summary(ctx.st)


def call_model(state: TurnState, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    ctx.calls = ctx.agent._call_model(ctx.st)
    return {**_summary(ctx.st), "has_calls": bool(ctx.calls)}


def run_tools(state: TurnState, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    ctx.results = ctx.agent._run_tools(ctx.st, ctx.calls)
    return {**_summary(ctx.st), "proposal_pending": ctx.st.pending_approval is not None}


def wait_for_human(state: TurnState, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    ctx.agent._request_approval(ctx.st, ctx.results)
    return _summary(ctx.st)


def publish(state: TurnState, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    ctx.agent._publish_approved(ctx.st)
    return _summary(ctx.st)


def _running(state: dict) -> bool:
    return state["status"] == AgentStatus.RUNNING.value


def build_turn_graph():
    g = StateGraph(TurnState, context_schema=TurnContext)
    for node in (check_budget, call_model, run_tools, wait_for_human, publish):
        g.add_node(node.__name__, node)
    g.add_conditional_edges(START, lambda s: s["action"], {"step": "check_budget", "publish": "publish"})
    g.add_conditional_edges("check_budget", _running, {True: "call_model", False: END})
    g.add_conditional_edges("call_model", lambda s: _running(s) and s["has_calls"], {True: "run_tools", False: END})
    g.add_conditional_edges("run_tools", lambda s: _running(s) and s["proposal_pending"],
                            {True: "wait_for_human", False: END})
    g.add_edge("wait_for_human", END)
    g.add_edge("publish", END)
    return g.compile(name="lenspilot_turn")


# ---- run graph -------------------------------------------------------------
def turn(state: RunLoopState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    st = ctx.agent.step(ctx.run_id)  # takes the run lock, loads and saves state.json
    if ctx.on_step:
        ctx.on_step(st)
    return _summary(st)


def build_run_graph():
    g = StateGraph(RunLoopState, context_schema=RunContext)
    g.add_node("turn", turn)
    g.add_edge(START, "turn")
    g.add_conditional_edges("turn", _running, {True: "turn", False: END})
    return g.compile(name="lenspilot_run")


TURN_GRAPH = build_turn_graph()
RUN_GRAPH = build_run_graph()


if __name__ == "__main__":  # python -m agent.graph  -> diagrams for the docs
    print(RUN_GRAPH.get_graph().draw_mermaid())
    print(TURN_GRAPH.get_graph().draw_mermaid())
