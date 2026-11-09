"""Execution trace view: everything shown is read from the run's trace.jsonl. Owner: Abdallah."""

from __future__ import annotations

import json

import streamlit as st

from agent.state import AgentState
from agent.tracing import read_trace, summarize_trace
from ui.common import active_run_id, get_services, provider_label, status_badge


# Link/image/HTML syntax is what enables exfiltration; emphasis chars (*, _) are harmless.
_MD_ENTITIES = {"[": "&#91;", "]": "&#93;", "(": "&#40;", ")": "&#41;", "!": "&#33;",
                "`": "&#96;", "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;"}


def safe_inline(text: str) -> str:
    """Neutralise HTML *and* markdown syntax in untrusted text (model output, tool data)."""
    return "".join(_MD_ENTITIES.get(c, c) for c in str(text))


def _args_summary(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 90 else s[:87] + "..."


def tool_history_table(state: AgentState) -> None:
    if not state.tool_history:
        st.caption("No tool calls yet.")
        return
    rows = [{
        "Step": r.step, "Tool": r.tool, "By": r.origin, "Arguments": _args_summary(r.arguments),
        "Status": r.status, "Error": r.error_code or "", "Retry": r.retry_number,
        "ms": r.duration_ms, "Injection flags": ", ".join(r.injection_flags),
    } for r in state.tool_history]
    st.dataframe(rows, hide_index=True, width="stretch")


def _timeline(events: list[dict]) -> None:
    for e in events:
        kind = e["event"]
        if kind == "run_started":
            line, cls = f"Run started with {provider_label(e['provider'])}", ""
            detail = f"Request: {e['request']}"
        elif kind == "llm_call":
            if not e.get("ok"):
                line, cls, detail = f"Model call {e['llm_call']} failed", "error", e.get("error_message", "")
            else:
                req = ", ".join(e.get("tool_requests") or []) or "no tools, final answer"
                tok = (f"{e['input_tokens']} in / {e['output_tokens']} out tokens"
                       if e.get("input_tokens") is not None else "tokens unavailable (mock)")
                line, cls = f"Model call {e['llm_call']} requested: {req}", ""
                detail = f"{e.get('text') or ''}  [{tok}, {e.get('latency_ms', 0)} ms]"
        elif kind == "tool_call":
            outcome = e["status"] if e["status"] == "success" else f"{e['status']}: {e.get('error_code')}"
            line, cls = f"Step {e['step']}: {e['tool']} → {outcome}", e["status"]
            detail = f"{_args_summary(e['arguments'])}  ({e['duration_ms']} ms, retry {e.get('retry_number', 0)})"
            if e.get("injection_flags"):
                detail += f"  Injection text detected: {', '.join(e['injection_flags'])}"
            if e.get("error_message"):
                detail += f"  {e['error_message']}"
        elif kind == "approval_requested":
            line, cls = "Waiting for human approval", "human"
            detail = ", ".join(i["photo_id"] for i in e["proposal"]["items"]) + f" to {e['proposal']['collection']}"
        elif kind == "approval_decision":
            line, cls, detail = f"Human decision: {e['decision']}", "human", e.get("reason") or ""
        elif kind == "approval_ignored":
            line, cls, detail = "Repeated approval click ignored (already decided)", "human", ""
        elif kind == "limit_enforced":
            line, cls = f"Limit enforced: {e['limit']}", "blocked"
            detail = f"Refused {e.get('refused_tool', 'further work')} at step count {e.get('steps')}"
        elif kind == "run_finished":
            line, cls = f"Run finished: {e['status']} ({e['stop_reason']})", ""
            detail = f"{e['steps']}/{e['max_steps']} steps, {e['llm_calls']} model calls"
        else:
            line, cls, detail = kind, "", ""
        st.markdown(f'<div class="lp-step {cls}"><b>{safe_inline(line)}</b><br>'
                    f'<span class="lp-muted">{e["ts"][11:19]} UTC  {safe_inline(detail)}</span></div>',
                    unsafe_allow_html=True)


def _fmt(v, suffix=""):
    return "unavailable" if v is None else f"{v}{suffix}"


def render() -> None:
    store = get_services().store
    runs = store.list_runs()
    st.title("Execution trace")
    if not runs:
        st.info("No runs yet. Start one from the agent workspace.")
        return
    ids = [r.run_id for r in runs]
    default = ids.index(active_run_id()) if active_run_id() in ids else 0
    run_id = st.selectbox("Run", ids, index=default,
                          format_func=lambda i: f"{i}: {next(r for r in runs if r.run_id == i).request[:70]}")
    state = store.load(run_id)
    events = read_trace(store.run_dir(run_id) / "trace.jsonl")
    m = summarize_trace(events)

    status_badge(state.status)
    if state.is_mock_provider:
        st.caption(f"Provider: {provider_label(state.provider)}. Decisions were made by a deterministic mock, "
                   "not by an LLM.")
    else:
        st.caption(f"Provider: {provider_label(state.provider)} ({state.model}).")
    c = st.columns(5)
    c[0].metric("Steps", f"{state.steps} / {state.limits.max_steps}")
    c[1].metric("Model calls", state.llm_calls)
    c[2].metric("Failed / blocked calls", m["failed_tool_calls"])
    c[3].metric("Active time", f"{state.active_seconds:.2f} s")
    c[4].metric("Est. cost", "unavailable" if m["cost_usd"] is None else f"${m['cost_usd']:.4f}")
    st.caption(f"Tokens: {_fmt(state.input_tokens)} in, {_fmt(state.output_tokens)} out. "
               f"Stop reason: {state.stop_reason or 'still running'}.")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Timeline")
        _timeline(events)
    with right:
        st.subheader("Tool calls")
        tool_history_table(state)
        if state.final_result:
            st.subheader("Final answer")
            st.text(state.final_result)  # untrusted model output: plain text only
    with st.expander("Raw trace (JSONL events)"):
        st.json(events, expanded=False)
    with st.expander("Evaluation summary (summarize_trace)"):
        st.json(m)
    st.download_button("Download trace.jsonl", (store.run_dir(run_id) / "trace.jsonl").read_bytes(),
                       file_name=f"{run_id}.trace.jsonl", mime="application/jsonl")
