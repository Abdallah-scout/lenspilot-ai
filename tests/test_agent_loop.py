"""Agent behaviour: dispatch, observations, completion, failures (requirements 1-7)."""

import json

from agent.mock_providers import HeuristicMockPlanner, ScriptedProvider, text, tool_use
from agent.state import AgentStatus
from agent.tracing import read_trace, summarize_trace
from conftest import HAPPY_TURNS, published_count


def test_successful_multi_tool_trajectory(make_agent, services):  # 1
    agent, _ = make_agent(HAPPY_TURNS)
    st = agent.run(agent.start("Publish my two best restaurant photos").run_id)
    assert st.status == AgentStatus.WAITING_FOR_APPROVAL
    st = agent.run(agent.approve(st.run_id, st.pending_approval.proposal_hash).run_id)
    assert st.status == AgentStatus.COMPLETED and st.stop_reason == "task_completed"
    assert [r.tool for r in st.tool_history] == [
        "list_photos", "get_photo_metadata", "evaluate_photo_quality",
        "read_style_guide", "prepare_publication", "publish_photo"]
    assert published_count(services) == 2


def test_dispatch_uses_requested_tool_and_validated_args(make_agent):  # 2
    agent, _ = make_agent([[tool_use("list_photos", {"category": "interior"})], [text("ok")]])
    st = agent.run(agent.start("list interiors").run_id)
    rec = st.tool_history[0]
    assert rec.tool == "list_photos" and rec.arguments == {"category": "interior"} and rec.status == "success"


def test_observations_are_returned_to_the_model(make_agent):  # 3
    agent, prov = make_agent([[tool_use("list_photos", {"category": "restaurant"}, id="t1")], [text("done")]])
    agent.run(agent.start("list").run_id)
    second_call_messages = prov.received[1]
    result_block = second_call_messages[-1]["content"][0]
    assert result_block["type"] == "tool_result" and result_block["tool_use_id"] == "t1"
    payload = json.loads(result_block["content"])
    assert payload["success"] and payload["data"]["count"] == 6


def test_observation_changes_next_decision(make_agent):
    """The heuristic planner's proposal is driven by observed quality scores."""
    agent, _ = make_agent(provider=HeuristicMockPlanner())
    st = agent.run(agent.start("best two interior photos").run_id)
    chosen = [i["photo_id"] for i in st.pending_approval.items]
    assert chosen == ["int_002", "int_001"]  # highest measured scores, eligible


def test_completion_detection(make_agent):  # 4
    agent, _ = make_agent([[text("Nothing to do.")]])
    st = agent.run(agent.start("hello").run_id)
    assert st.status == AgentStatus.COMPLETED and st.final_result == "Nothing to do." and st.steps == 0


def test_invalid_tool_name_and_invalid_args_are_rejected(make_agent):  # 5
    agent, _ = make_agent([
        [tool_use("delete_everything", {})],
        [tool_use("get_photo_metadata", {"photo_ids": ["../../etc/passwd"]})],
        [tool_use("list_photos", {"category": "restaurant", "path": "/tmp"})],
        [text("giving up")],
    ])
    st = agent.run(agent.start("x").run_id)
    codes = [r.error_code for r in st.tool_history]
    assert codes == ["unknown_tool", "invalid_arguments", "invalid_arguments"]
    assert all(r.status == "blocked" for r in st.tool_history)
    assert st.status == AgentStatus.COMPLETED  # rejected calls are observations, not crashes


def test_recoverable_failure_is_retried_and_run_continues(make_agent):  # 6
    agent, _ = make_agent(provider=HeuristicMockPlanner(), faults={"evaluate_photo_quality": 1})
    st = agent.run(agent.start("best two interior photos").run_id)
    q = [r for r in st.tool_history if r.tool == "evaluate_photo_quality"]
    assert [(r.status, r.retry_number) for r in q] == [("error", 0), ("success", 1)]
    assert st.status == AgentStatus.WAITING_FOR_APPROVAL


def test_unrecoverable_failure_stops_run(make_agent, services):  # 7
    agent, _ = make_agent([[tool_use("list_photos", {})], [tool_use("list_photos", {"category": "interior"})]])
    st = agent.start("x")
    services.sandbox.catalog_path.write_text("{corrupt", encoding="utf-8")
    st = agent.run(st.run_id)
    assert st.status == AgentStatus.FAILED
    assert st.stop_reason == "unrecoverable_tool_error:corrupted_data"
    assert st.llm_calls == 1  # no further model calls after the fatal error


def test_trace_records_every_step(make_agent):
    agent, _ = make_agent(HAPPY_TURNS)
    st = agent.run(agent.start("x").run_id)
    st = agent.run(agent.approve(st.run_id, st.pending_approval.proposal_hash).run_id)
    events = read_trace(agent.store.run_dir(st.run_id) / "trace.jsonl")
    summary = summarize_trace(events)
    assert summary["steps"] == st.steps == 6 == summary["tool_calls"]
    assert summary["status"] == "COMPLETED" and summary["approvals"] == ["approved"]
    assert summary["input_tokens"] is None and summary["cost_usd"] is None  # mock: unavailable, not 0
    assert "self-approved" not in (agent.store.run_dir(st.run_id) / "trace.jsonl").read_text()
    assert all("approval_token" not in e.get("arguments", {}) or e["arguments"]["approval_token"] == "[REDACTED]"
               for e in events if e["event"] == "tool_call")


def test_injected_metadata_is_flagged_in_trace(make_agent):
    agent, _ = make_agent([[tool_use("get_photo_metadata", {"photo_ids": ["rest_006"]})], [text("ok")]])
    st = agent.run(agent.start("x").run_id)
    assert {"override_instructions", "approval_bypass"} <= set(st.tool_history[0].injection_flags)
