"""Human checkpoint and write safety (requirements 13-17) + injection defences."""

import pytest

from agent.config import Limits
from agent.loop import ApprovalMismatch
from agent.mock_providers import CompromisedMockProvider, HeuristicMockPlanner, text, tool_use
from agent.registry import ToolRegistry
from agent.state import AgentStatus
from conftest import published_count
from tools.schemas import ToolContext

PROPOSE = [[tool_use("prepare_publication", {"collection": "restaurants", "items": [
    {"photo_id": "rest_001", "caption": "Candlelit dining room at dusk."}]})], [text("Done.")]]


def _waiting(make_agent, turns=PROPOSE, **kw):
    agent, prov = make_agent(turns, **kw)
    st = agent.run(agent.start("publish rest_001").run_id)
    assert st.status == AgentStatus.WAITING_FOR_APPROVAL
    return agent, prov, st


def test_no_write_before_approval(make_agent, services):  # 13
    _, _, st = _waiting(make_agent)
    assert published_count(services) == 0
    assert not any(r.tool == "publish_photo" for r in st.tool_history)


def test_rejection_prevents_publication(make_agent, services):  # 14
    agent, prov, st = _waiting(make_agent)
    st = agent.reject(st.run_id, st.pending_approval.proposal_hash, reason="wrong caption")
    assert st.status == AgentStatus.CANCELLED and st.stop_reason == "approval_rejected"
    st = agent.run(st.run_id)
    assert published_count(services) == 0 and len(prov.received) == 1


def test_approved_publication_executes_once(make_agent, services):  # 15
    agent, _, st = _waiting(make_agent)
    st = agent.approve(st.run_id, st.pending_approval.proposal_hash)
    assert published_count(services) == 1
    assert st.publications[0]["created"] is True
    st = agent.run(st.run_id)
    assert st.status == AgentStatus.COMPLETED


def test_repeated_approval_does_not_duplicate(make_agent, services):  # 16, 22
    agent, _, st = _waiting(make_agent)
    h = st.pending_approval.proposal_hash
    for _ in range(5):  # simulates double clicks / Streamlit reruns
        st = agent.approve(st.run_id, h)
    assert published_count(services) == 1
    assert sum(r.tool == "publish_photo" for r in st.tool_history) == 1


def test_same_photo_in_new_run_is_idempotent(make_agent, services):
    for _ in range(2):
        agent, _, st = _waiting(make_agent)
        st = agent.approve(st.run_id, st.pending_approval.proposal_hash)
    assert published_count(services) == 1
    assert st.publications[0]["already_published"] is True


def test_altered_proposal_invalidates_approval(make_agent, services):  # 17
    agent, _, st = _waiting(make_agent)
    with pytest.raises(ApprovalMismatch):
        agent.approve(st.run_id, "0" * 64)
    # Tamper with the stored proposal itself (e.g. caption edited after display).
    st = agent.store.load(st.run_id)
    original_hash = st.pending_approval.proposal_hash
    st.pending_approval.items[0]["caption"] = "Edited after the human looked at it."
    agent.store.save(st)
    with pytest.raises(ApprovalMismatch):
        agent.approve(st.run_id, original_hash)
    assert published_count(services) == 0


def test_write_tool_cannot_be_invoked_directly(services):
    reg = ToolRegistry()
    ctx = ToolContext(sandbox=services.sandbox, approvals=services.approvals, quality=services.quality, run_id="r")
    args = {"run_id": "r", "proposal_hash": "x", "collection": "restaurants",
            "items": [{"photo_id": "rest_001", "caption": "Candlelit dining room at dusk."}],
            "approval_token": "forged"}
    res, _ = reg.dispatch("publish_photo", args, ctx, origin="model")
    assert res.error_code == "tool_not_allowed"
    res, _ = reg.dispatch("publish_photo", args, ctx, origin="orchestrator")
    assert res.error_code == "approval_invalid"  # forged token / hash rejected even internally
    assert published_count(services) == 0
    assert "publish_photo" not in [t["name"] for t in reg.model_tool_definitions()]


def test_private_photo_is_blocked_by_policy(make_agent, services):
    turns = [[tool_use("prepare_publication", {"collection": "restaurants", "items": [
        {"photo_id": "rest_004", "caption": "Guests at the wedding dinner."}]})], [text("Cannot publish.")]]
    agent, _ = make_agent(turns)
    st = agent.run(agent.start("publish rest_004").run_id)
    assert st.tool_history[0].error_code == "policy_denied" and st.pending_approval is None
    assert st.status == AgentStatus.COMPLETED and published_count(services) == 0


def test_compromised_model_cannot_publish(make_agent, services):
    agent, _ = make_agent(provider=CompromisedMockProvider())
    st = agent.run(agent.start("Show me my restaurant photos").run_id)
    codes = [r.error_code for r in st.tool_history]
    assert "tool_not_allowed" in codes and "policy_denied" in codes
    assert st.status == AgentStatus.COMPLETED and published_count(services) == 0


def test_step_limit_blocks_publish_even_after_approval(make_agent, services):
    agent, _, st = _waiting(make_agent, limits=Limits(max_steps=1))
    st = agent.approve(st.run_id, st.pending_approval.proposal_hash)
    assert st.status == AgentStatus.STOPPED_LIMIT and st.steps == 1
    assert published_count(services) == 0


def test_cancel_while_waiting_prevents_publication(make_agent, services):
    agent, _, st = _waiting(make_agent)
    h = st.pending_approval.proposal_hash
    agent.cancel(st.run_id)
    st = agent.approve(st.run_id, h)
    assert st.status == AgentStatus.CANCELLED and published_count(services) == 0


def test_consent_revoked_between_proposal_and_approval(make_agent, services):
    import json
    agent, _, st = _waiting(make_agent)
    cat = json.loads(services.sandbox.catalog_path.read_text())
    next(p for p in cat["photos"] if p["photo_id"] == "rest_001")["client_consent"] = "denied"
    services.sandbox.catalog_path.write_text(json.dumps(cat))
    st = agent.approve(st.run_id, st.pending_approval.proposal_hash)
    assert published_count(services) == 0
    assert st.tool_history[-1].error_code == "policy_denied"


def test_full_heuristic_flow_blocks_private_and_publishes_rest(make_agent, services):
    agent, _ = make_agent(provider=HeuristicMockPlanner())
    st = agent.run(agent.start("best three restaurant photographs").run_id)
    ids = [i["photo_id"] for i in st.pending_approval.items]
    assert "rest_004" not in ids and len(ids) == 3
    agent.run(agent.approve(st.run_id, st.pending_approval.proposal_hash).run_id)
    assert {r["photo_id"] for r in services.sandbox.published_records()} == set(ids)
