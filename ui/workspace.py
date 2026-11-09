"""Agent workspace: request -> live progress -> approval -> result. Owner: Abdallah.

Rerun safety: the script only *advances* a run whose persisted status is
RUNNING (Agent.step is a no-op otherwise), so rerendering never restarts a
finished run, and approval happens only through explicit button callbacks.
"""

from __future__ import annotations

import streamlit as st

from agent.state import AgentStatus
from ui.approval import render_approval
from ui.common import active_run_id, get_agent, get_services, provider_label, status_badge
from ui.execution import tool_history_table

EXAMPLES = [
    "Find the best three restaurant photographs from my latest shoot, evaluate their quality, "
    "write professional captions and prepare them for publication.",
    "Select two high-quality interior photographs and prepare a portfolio collection.",
    "Publish the private wedding dinner photo (rest_004) to the featured collection.",
    "Prepare a restaurant collection, but exclude anything private or without client consent.",
]

STOP_TEXT = {
    "task_completed": "The agent finished the task.",
    "approval_rejected": "You rejected the proposal. Nothing was written.",
    "user_cancelled": "You cancelled the run. No further steps were taken.",
    "max_steps_reached": "The step limit was reached, so the agent was stopped.",
    "max_steps_reached_before_publish": "The step limit was reached before publishing, so nothing was written.",
    "max_llm_calls_reached": "The model-call budget was reached, so the agent was stopped.",
    "time_budget_exhausted": "The time budget was reached, so the agent was stopped.",
    "token_budget_exhausted": "The token budget was reached, so the agent was stopped.",
    "retry_limit_exceeded": "A tool kept failing after the allowed retries.",
}


def _use_example() -> None:
    choice = st.session_state.get("example_pick")
    if choice:
        st.session_state["request_text"] = choice


def _cancel(run_id: str, provider: str) -> None:
    get_agent(provider).cancel(run_id)
    st.session_state["flash"] = ("info", "Run cancelled.")


def _drive(state) -> None:
    faults = None
    if st.session_state.get("fault_run") == state.run_id:
        faults = st.session_state.pop("fault_plan", None)
        st.session_state.pop("fault_run", None)
    agent = get_agent(state.provider, faults)
    seen = len(state.tool_history)
    with st.status("LensPilot is working", expanded=True) as box:
        def on_step(s):
            nonlocal seen
            for r in s.tool_history[seen:]:
                mark = "✓" if r.status == "success" else "✗"
                box.write(f"{mark} Step {r.step}: {r.tool} ({r.status}{', ' + r.error_code if r.error_code else ''})")
            seen = len(s.tool_history)
        final = agent.run(state.run_id, on_step=on_step)
        box.update(label=f"Paused: {final.status.value}", state="complete")
    st.rerun()


def render(provider: str, simulate_fault: bool) -> None:
    store = get_services().store
    st.title("Agent workspace")
    st.markdown('<p class="lp-lede">Describe what you want published. The agent plans and uses tools on its own, '
                "but nothing is written until you approve the exact proposal.</p>", unsafe_allow_html=True)

    st.selectbox("Start from an example", [None, *EXAMPLES], key="example_pick", on_change=_use_example,
                 format_func=lambda x: "Choose an example request" if x is None else x)
    request = st.text_area("Request", key="request_text", height=100,
                           placeholder="e.g. Find my three best restaurant photos and prepare captions.")

    run_id = active_run_id()
    state = store.load(run_id) if run_id else None
    busy = state is not None and not state.is_terminal
    if st.button("Start agent", type="primary", disabled=busy, icon=":material/play_arrow:",
                 help="Finish or cancel the current run first." if busy else None):
        try:
            state = get_agent(provider).start(request)
        except ValueError as exc:
            st.error(f"{exc} Write a request and try again.")
        else:
            st.session_state["active_run_id"] = state.run_id
            if simulate_fault:
                st.session_state["fault_run"] = state.run_id
                st.session_state["fault_plan"] = {"evaluate_photo_quality": 1}

    if state is None:
        return
    st.divider()
    head, cancel = st.columns([5, 1])
    with head:
        status_badge(state.status)
        st.markdown(f"**{state.run_id}** using {provider_label(state.provider)}")
        if state.is_mock_provider:
            st.caption("Mock provider: decisions are deterministic rules, not AI inference.")
    if not state.is_terminal:
        cancel.button("Cancel run", on_click=_cancel, args=(state.run_id, state.provider),
                      icon=":material/stop:")
    st.progress(min(1.0, state.steps / state.limits.max_steps),
                text=f"{state.steps} of {state.limits.max_steps} steps used, {state.llm_calls} model calls")

    flash = st.session_state.pop("flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])

    if state.status == AgentStatus.RUNNING:
        _drive(state)
    elif state.status == AgentStatus.WAITING_FOR_APPROVAL:
        render_approval(state)
    else:
        msg = STOP_TEXT.get(state.stop_reason or "", f"Stopped: {state.stop_reason}.")
        kind = {AgentStatus.COMPLETED: st.success, AgentStatus.FAILED: st.error}.get(state.status, st.warning)
        kind(msg)
        if state.final_result:
            # Model output can be steered by injected tool data: render as plain text,
            # never markdown (an injected ![](https://...) image would exfiltrate on load).
            st.markdown("**Agent summary**")
            st.text(state.final_result)
        if state.publications:
            created = sum(p["created"] for p in state.publications)
            st.caption(f"{created} new publication(s), {len(state.publications) - created} already existed.")

    st.subheader("Tool activity")
    tool_history_table(state)
