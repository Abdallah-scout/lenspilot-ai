"""LensPilot Streamlit entry point.  Run:  streamlit run app.py   (owner: Abdallah)"""

from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="LensPilot", page_icon=":material/camera:", layout="wide")

from ui import dashboard, execution, gallery, portfolio, workspace  # noqa: E402
from ui.common import apply_theme, get_services, provider_label, provider_options  # noqa: E402

apply_theme()

with st.sidebar:
    st.markdown("### LensPilot")
    options = provider_options()
    provider = st.selectbox("Agent model", options, format_func=provider_label, key="provider")
    if not {"ollama", "openai", "anthropic"} & set(options):
        st.caption("Set LENSPILOT_OLLAMA_MODEL (local, free) or OPENAI_API_KEY in .env to enable the "
                   "live agent. Until then only mock providers are available.")
    simulate_fault = st.toggle("Simulate a quality-tool outage", key="simulate_fault",
                               help="The first quality evaluation of the next run fails with a "
                                    "retryable error. Labelled [simulated fault] in the trace.")
    st.divider()
    with st.popover("Reset sandbox", icon=":material/restart_alt:"):
        st.write("Deletes every published photo and restores the original catalog. Run logs are kept.")
        if st.button("Reset now", type="primary"):
            get_services().sandbox.reset()
            st.session_state.pop("active_run_id", None)
            st.toast("Sandbox reset.")

def dashboard_page() -> None:
    dashboard.render(pages)


def workspace_page() -> None:
    workspace.render(provider, simulate_fault)


pages: dict = {}
pages["dashboard"] = st.Page(dashboard_page, title="Dashboard", icon=":material/home:",
                             url_path="dashboard", default=True)
pages["gallery"] = st.Page(gallery.render, title="Gallery", icon=":material/photo_library:", url_path="gallery")
pages["workspace"] = st.Page(workspace_page, title="Agent workspace",
                             icon=":material/smart_toy:", url_path="workspace")
pages["portfolio"] = st.Page(portfolio.render, title="Published portfolio", icon=":material/collections:",
                             url_path="portfolio")
pages["trace"] = st.Page(execution.render, title="Execution trace", icon=":material/timeline:",
                         url_path="trace")

st.navigation(list(pages.values())).run()
