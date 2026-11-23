"""Human approval checkpoint UI. Owner: Abdallah.

Buttons use on_click callbacks that call Agent.approve / Agent.reject with
the run id AND the proposal hash that was rendered. The agent re-checks both
under a lock, so a rerun, a double click or a stale page can never cause a
second write or approve a different proposal.
"""

from __future__ import annotations

import html

import streamlit as st

from agent.loop import ApprovalMismatch
from agent.state import AgentState
from sandbox import SandboxError
from ui.common import eligibility_tags, get_agent, get_services, meter, quality_for


def _decide(run_id: str, provider: str, proposal_hash: str, approve: bool) -> None:
    agent = get_agent(provider)
    try:
        if approve:
            agent.approve(run_id, proposal_hash)
            st.session_state["flash"] = ("success", "Approved. Publishing to the sandbox.")
        else:
            agent.reject(run_id, proposal_hash, reason=st.session_state.get(f"reason_{proposal_hash}", ""))
            st.session_state["flash"] = ("info", "Rejected. Nothing was published.")
    except ApprovalMismatch:
        st.session_state["flash"] = ("error", "This proposal changed after you saw it. "
                                              "The approval was not applied.")


def render_approval(state: AgentState) -> None:
    p = state.pending_approval
    if p is None:
        return
    sb = get_services().sandbox
    try:
        for item in p.items:
            sb.image_path(item["photo_id"])
    except SandboxError:
        st.warning("A photo in this proposal is no longer available. Cancel this run and start a new one.")
        return
    st.subheader("Approve this publication?")
    st.markdown(
        f'<div class="lp-proof"><span class="lp-muted">Proposal {p.proposal_id} from run {state.run_id}. '
        f"Destination <b>{html.escape(p.destination)}</b> (local sandbox only). "
        f"{len(p.items)} photograph(s) will be written when you approve.</span></div>",
        unsafe_allow_html=True)

    flags = sorted({f for r in state.tool_history for f in r.injection_flags})
    if flags:
        st.warning("Instruction-like text was found in tool output during this run "
                   f"({', '.join(flags)}). It was handed to the agent as data only. "
                   "Check the captions below before approving.", icon=":material/shield:")

    cols = st.columns(len(p.items))
    for col, item in zip(cols, p.items):
        with col:
            photo = sb.get_photo(item["photo_id"])
            st.image(str(sb.image_path(item["photo_id"])), width="stretch")
            q = quality_for(item["photo_id"])
            st.markdown(f'<div class="lp-edge"><span>{item["photo_id"]}</span>'
                        f'<span>Q {q["score"]:.0f}</span></div>' if q else "", unsafe_allow_html=True)
            if q:
                st.markdown(meter(q["score"]), unsafe_allow_html=True)
            st.markdown(f'<div class="lp-title">{html.escape(item["title"])}</div>'
                        f'<p class="lp-caption">“{html.escape(item["caption"])}”</p>'
                        f"{eligibility_tags(photo)}", unsafe_allow_html=True)
            if q and q["score"] < 60:
                st.caption("Quality is below the publish-ready threshold of 60.")

    st.text_input("Reason (optional, recorded if you reject)", key=f"reason_{p.proposal_hash}")
    a, r, _ = st.columns([1, 1, 3])
    a.button("Approve and publish", type="primary", key=f"approve_{p.proposal_hash}",
             on_click=_decide, args=(state.run_id, state.provider, p.proposal_hash, True),
             icon=":material/check:")
    r.button("Reject", key=f"reject_{p.proposal_hash}",
             on_click=_decide, args=(state.run_id, state.provider, p.proposal_hash, False),
             icon=":material/close:")
