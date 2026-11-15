"""Shared UI services, theme and small render helpers. Owner: Abdallah.

Separation of state (see docs/integration_contracts.md):
  * agent orchestration state -> RunStore JSON files (var/runs/<id>/state.json)
  * durable sandbox state     -> var/sandbox (catalog, published/)
  * execution logs            -> var/runs/<id>/trace.jsonl
  * UI session state          -> st.session_state: only the active run id and
    widget values. Nothing authoritative lives in session state.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from agent.integration import PROVIDER_LABELS, Services, build_agent, build_services, make_provider
from agent.loop import Agent
from agent.providers import AnthropicProvider, OllamaProvider, OpenAIProvider
from agent.state import AgentStatus
from security import check_publication_eligibility

STATUS_STYLE = {
    AgentStatus.IDLE: ("gray", "Idle"),
    AgentStatus.RUNNING: ("blue", "Running"),
    AgentStatus.WAITING_FOR_APPROVAL: ("orange", "Waiting for your approval"),
    AgentStatus.COMPLETED: ("green", "Completed"),
    AgentStatus.FAILED: ("red", "Failed"),
    AgentStatus.CANCELLED: ("gray", "Cancelled"),
    AgentStatus.STOPPED_LIMIT: ("violet", "Stopped by limit"),
}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&display=swap');
:root {
  --lp-bg: #1d1e20; --lp-panel: #27282b; --lp-line: #3a3b3f; --lp-ink: #e4e1db;
  --lp-muted: #9a968e; --lp-brass: #c4a56a; --lp-sage: #86a27f; --lp-brick: #c26e5e;
}
html, body, .stApp, .stMarkdown p, label, button p, input, textarea { font-family: 'Instrument Sans', system-ui, sans-serif; }
h1, h2, h3 { font-family: 'Instrument Sans', sans-serif; font-weight: 600; letter-spacing: -0.015em; }
h1 { font-size: 2.1rem !important; }
.lp-serif { font-family: 'Newsreader', Georgia, serif; }
.lp-title { font-family: 'Newsreader', Georgia, serif; font-size: 1.12rem; line-height: 1.3; color: var(--lp-ink); margin: .35rem 0 .1rem; }
.stMarkdown p.lp-caption, .lp-caption { font-family: 'Newsreader', Georgia, serif; font-style: italic; font-size: 1.02rem; line-height: 1.5; color: var(--lp-ink); }
.lp-muted { color: var(--lp-muted); font-size: .86rem; line-height: 1.45; }
.stMarkdown p.lp-lede, .lp-lede { color: var(--lp-muted); font-size: 1.05rem; max-width: 62ch; line-height: 1.55; }
/* Contact-sheet film edge under each print */
.lp-edge { display: flex; justify-content: space-between; align-items: center; background: #141516;
  color: var(--lp-brass); font-size: .74rem; letter-spacing: .04em; padding: 3px 8px; margin-top: -6px;
  border-radius: 0 0 3px 3px; }
.lp-meter { height: 3px; background: var(--lp-line); border-radius: 2px; margin: 6px 0 4px; }
.lp-meter > span { display: block; height: 3px; border-radius: 2px; background: var(--lp-brass); }
.lp-meter.low > span { background: var(--lp-brick); }
.lp-tag { display: inline-block; font-size: .74rem; padding: 1px 8px; border-radius: 10px; margin-right: 4px;
  border: 1px solid var(--lp-line); color: var(--lp-muted); }
.lp-tag.ok { border-color: var(--lp-sage); color: var(--lp-sage); }
.lp-tag.no { border-color: var(--lp-brick); color: var(--lp-brick); }
.lp-proof { border: 1px dashed var(--lp-brass); border-radius: 4px; padding: 14px 16px; }
.lp-stat { font-size: 1.9rem; font-weight: 600; color: var(--lp-ink); line-height: 1.1; }
.lp-step { border-left: 2px solid var(--lp-line); padding: 2px 0 8px 12px; margin-left: 6px; }
.lp-step.success { border-color: var(--lp-sage); } .lp-step.error, .lp-step.blocked { border-color: var(--lp-brick); }
.lp-step.human { border-color: var(--lp-brass); }
img { border-radius: 3px 3px 0 0; }
</style>
"""


def apply_theme() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


@st.cache_resource
def get_services() -> Services:
    return build_services()


def provider_options() -> list[str]:
    opts = ["mock-heuristic", "mock-looping", "mock-compromised"]
    live = (["ollama"] if OllamaProvider.available() else []) + \
           (["openai"] if OpenAIProvider.available() else []) + \
           (["anthropic"] if AnthropicProvider.available() else [])
    return live + opts


def get_agent(provider_name: str, faults: dict | None = None) -> Agent:
    return build_agent(get_services(), make_provider(provider_name), faults=faults)


def provider_label(name: str) -> str:
    return PROVIDER_LABELS.get(name, name)


def status_badge(status: AgentStatus) -> None:
    color, label = STATUS_STYLE[status]
    st.badge(label, color=color)


@st.cache_data(show_spinner=False)
def _assess(path: str, mtime: float) -> dict:
    return get_services().quality.assess(Path(path))


def quality_for(photo_id: str) -> dict | None:
    try:
        p = get_services().sandbox.image_path(photo_id)
    except Exception:
        return None
    return _assess(str(p), p.stat().st_mtime)


def eligibility_tags(photo: dict) -> str:
    d = check_publication_eligibility(photo)
    if d.allowed:
        return '<span class="lp-tag ok">eligible</span>'
    return "".join(f'<span class="lp-tag no">{r.split(" (")[0]}</span>' for r in d.reasons)


def meter(score: float) -> str:
    cls = "lp-meter low" if score < 60 else "lp-meter"
    return f'<div class="{cls}"><span style="width:{max(2, min(100, score))}%"></span></div>'


def active_run_id() -> str | None:
    return st.session_state.get("active_run_id")
