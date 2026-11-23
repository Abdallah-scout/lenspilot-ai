"""Dashboard: overview of sandbox, runs and agent state. Owner: Abdallah."""

from __future__ import annotations

import streamlit as st

from security import check_publication_eligibility
from ui.common import active_run_id, get_services, provider_label, status_badge


def render(pages: dict) -> None:
    sv = get_services()
    photos = sv.sandbox.catalog()
    published = sv.sandbox.published_records()
    runs = sv.store.list_runs(limit=8)

    # Hero: the work itself. Latest published prints, else the first frames of the sheet.
    strip = ([str(sv.sandbox.published_dir / r["image_file"]) for r in published[-4:]]
             or [str(sv.sandbox.image_path(p["photo_id"])) for p in photos[:4]])
    if strip:
        cols = st.columns(len(strip), gap="small")
        for col, path in zip(cols, strip):
            col.image(path, width="stretch")
    st.title("LensPilot")
    st.markdown('<p class="lp-lede">An assistant that reviews your shoot, scores each frame, drafts captions '
                "in your studio style and prepares a portfolio update. It publishes only to a local sandbox, "
                "and only after you approve the exact proposal.</p>", unsafe_allow_html=True)

    eligible = sum(check_publication_eligibility(p).allowed for p in photos)
    c = st.columns(4)
    for col, value, label in zip(c, [len(photos), eligible, len(published), len(sv.store.list_runs(500))],
                                 ["photographs in the sandbox", "eligible to publish",
                                  "published", "agent runs logged"]):
        col.markdown(f'<div class="lp-stat">{value}</div><div class="lp-muted">{label}</div>',
                     unsafe_allow_html=True)

    st.write("")
    left, right = st.columns([2, 3])
    with left:
        st.subheader("Current run")
        rid = active_run_id()
        if rid:
            state = sv.store.load(rid)
            status_badge(state.status)
            st.markdown(f"{state.request[:160]}")
            st.caption(f"{state.steps}/{state.limits.max_steps} steps, {provider_label(state.provider)}")
        else:
            st.caption("No run in this session yet.")
        st.page_link(pages["workspace"], label="Open the agent workspace", icon=":material/smart_toy:")
        if "gallery" in pages:
            st.page_link(pages["gallery"], label="Browse the contact sheet", icon=":material/photo_library:")
        st.page_link(pages["trace"], label="Inspect execution traces", icon=":material/timeline:")
    with right:
        st.subheader("Recent runs")
        if not runs:
            st.caption("Runs appear here after you start the agent.")
        else:
            st.dataframe([{"Run": r.run_id, "Status": r.status.value, "Steps": f"{r.steps}/{r.limits.max_steps}",
                           "Provider": r.provider, "Stop reason": r.stop_reason or "",
                           "Request": r.request[:60]} for r in runs], hide_index=True, width="stretch")
