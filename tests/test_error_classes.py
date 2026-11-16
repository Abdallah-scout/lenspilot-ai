"""One test per error class in errors.md (owner: Skander).

The assignment asks for "an error taxonomy with a test per class". Every
code in errors.md section 2 has a ``test_<code>`` function here, and
``test_every_errors_md_class_has_a_test`` fails if a code is added to
errors.md without a matching test. Each test triggers the real failure
(no mocking of the code under test) and checks what the agent does next.
"""

from __future__ import annotations

import errno
import json
import re
from pathlib import Path

from agent.mock_providers import text, tool_use
from agent.registry import ToolKind, ToolRegistry, ToolSpec, default_specs
from agent.state import AgentStatus
from conftest import published_count
from tools.schemas import EmptyInput, ToolContext, compute_proposal_hash

ROOT = Path(__file__).resolve().parent.parent
PROPOSE_REST_001 = tool_use("prepare_publication", {"collection": "restaurants", "items": [
    {"photo_id": "rest_001", "caption": "Candlelit dining room at dusk."}]})


def _run(make_agent, turns, **kw):
    agent, _ = make_agent(turns, **kw)
    return agent, agent.run(agent.start("error class test").run_id)


def _catalog(services) -> dict:
    return json.loads(services.sandbox.catalog_path.read_text(encoding="utf-8"))


def _save_catalog(services, cat: dict) -> None:
    services.sandbox.catalog_path.write_text(json.dumps(cat), encoding="utf-8")


# --------------------------------------------------------------------------- recoverable
def test_tool_unavailable(make_agent):
    """Transient fault -> retryable observation -> the retry succeeds -> task completes."""
    call = tool_use("list_photos", {})
    _, st = _run(make_agent, [[call], [call], [text("Listed.")]], faults={"list_photos": 1})
    first, second = st.tool_history
    assert first.error_code == "tool_unavailable" and second.status == "success"
    assert second.retry_number == 1 and st.status == AgentStatus.COMPLETED


def test_invalid_arguments(make_agent):
    _, st = _run(make_agent, [[tool_use("list_photos", {"category": "food"})], [text("ok")]])
    assert st.tool_history[0].error_code == "invalid_arguments" and st.status == AgentStatus.COMPLETED


def test_unknown_tool(make_agent):
    _, st = _run(make_agent, [[tool_use("delete_everything", {})], [text("ok")]])
    assert st.tool_history[0].error_code == "unknown_tool" and st.tool_history[0].status == "blocked"


def test_tool_not_allowed(make_agent, services):
    _, st = _run(make_agent, [[tool_use("publish_photo", {})], [text("ok")]])
    assert st.tool_history[0].error_code == "tool_not_allowed" and published_count(services) == 0


def test_photo_not_found(make_agent):
    _, st = _run(make_agent, [[tool_use("get_photo_metadata", {"photo_ids": ["nope_999"]})], [text("ok")]])
    assert st.tool_history[0].error_code == "photo_not_found" and st.status == AgentStatus.COMPLETED


def test_empty_result(make_agent, services):
    cat = _catalog(services)
    cat["photos"] = [p for p in cat["photos"] if p["category"] != "interior"]
    _save_catalog(services, cat)
    _, st = _run(make_agent, [[tool_use("list_photos", {"category": "interior"})], [text("None found.")]])
    assert st.tool_history[0].error_code == "empty_result" and st.status == AgentStatus.COMPLETED


def test_image_missing(make_agent, services):
    (services.sandbox.photos_dir / "rest_001.jpg").unlink()
    _, st = _run(make_agent, [[tool_use("evaluate_photo_quality", {"photo_ids": ["rest_001"]})], [text("ok")]])
    assert st.tool_history[0].error_code == "image_missing" and st.status == AgentStatus.COMPLETED


def test_policy_denied(make_agent, services):
    turns = [[tool_use("prepare_publication", {"collection": "restaurants", "items": [
        {"photo_id": "int_003", "caption": "Showroom kitchen island in soft light."}]})], [text("ok")]]
    _, st = _run(make_agent, turns)
    assert st.tool_history[0].error_code == "policy_denied"
    assert st.pending_approval is None and published_count(services) == 0


# --------------------------------------------------------------------------- loop guards
def test_duplicate_call(make_agent):
    call = tool_use("list_photos", {})
    _, st = _run(make_agent, [[call], [tool_use("list_photos", {})], [text("ok")]])
    assert [r.error_code for r in st.tool_history] == [None, "duplicate_call"]


def test_not_retryable(make_agent):
    bad = {"category": "food"}
    _, st = _run(make_agent, [[tool_use("list_photos", bad)], [tool_use("list_photos", bad)], [text("ok")]])
    assert [r.error_code for r in st.tool_history] == ["invalid_arguments", "not_retryable"]


def test_retry_limit_exceeded(make_agent):
    call = tool_use("list_photos", {})
    _, st = _run(make_agent, [[call]] * 6, faults={"list_photos": 10})
    assert st.tool_history[-1].error_code == "retry_limit_exceeded"
    assert st.status == AgentStatus.FAILED and st.stop_reason == "retry_limit_exceeded"


def test_one_proposal_at_a_time(make_agent, services):
    second = tool_use("prepare_publication", {"collection": "restaurants", "items": [
        {"photo_id": "rest_002", "caption": "Plates lined up on the pass."}]})
    _, st = _run(make_agent, [[PROPOSE_REST_001, second]])
    assert [r.error_code for r in st.tool_history] == [None, "one_proposal_at_a_time"]
    assert st.status == AgentStatus.WAITING_FOR_APPROVAL and published_count(services) == 0


def test_approval_invalid(services):
    items = [{"photo_id": "rest_001", "caption": "Candlelit dining room at dusk."}]
    args = {"run_id": "run_x", "proposal_hash": compute_proposal_hash("restaurants", items),
            "collection": "restaurants", "items": items, "approval_token": "self-approved"}
    ctx = ToolContext(sandbox=services.sandbox, approvals=services.approvals, quality=services.quality,
                      run_id="run_x")
    res, _ = ToolRegistry().dispatch("publish_photo", args, ctx, origin="orchestrator")
    assert res.error_code == "approval_invalid" and not res.retryable and published_count(services) == 0


# --------------------------------------------------------------------------- fatal: run FAILED
def test_sandbox_unavailable(make_agent):
    agent, _ = make_agent([[tool_use("list_photos", {})], [text("never reached")]])
    agent.sandbox.catalog_path.unlink()
    st = agent.run(agent.start("x").run_id)
    assert st.tool_history[0].error_code == "sandbox_unavailable"
    assert st.status == AgentStatus.FAILED and st.stop_reason == "unrecoverable_tool_error:sandbox_unavailable"


def test_corrupted_data(make_agent, services):
    services.sandbox.catalog_path.write_text("{not json", encoding="utf-8")
    _, st = _run(make_agent, [[tool_use("list_photos", {})], [text("never reached")]])
    assert st.tool_history[0].error_code == "corrupted_data"
    assert st.status == AgentStatus.FAILED and st.stop_reason == "unrecoverable_tool_error:corrupted_data"


def test_internal_error(make_agent):
    def boom(ctx, args):
        raise ZeroDivisionError("bug")
    agent, _ = make_agent([[tool_use("buggy_tool", {})], [text("never reached")]])
    agent.registry = ToolRegistry(specs=default_specs() + [
        ToolSpec("buggy_tool", "test-only tool that crashes", EmptyInput, boom, ToolKind.READ)])
    st = agent.run(agent.start("x").run_id)
    rec = st.tool_history[0]
    assert rec.error_code == "internal_error" and "Traceback" not in (rec.error_message or "")
    assert st.status == AgentStatus.FAILED and st.stop_reason == "unrecoverable_tool_error:internal_error"


def test_write_failed(make_agent, services, monkeypatch):
    agent, _ = make_agent([[PROPOSE_REST_001], [text("never reached")]])
    st = agent.run(agent.start("x").run_id)
    assert st.status == AgentStatus.WAITING_FOR_APPROVAL

    def disk_full(src, dst):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(Path, "rename", disk_full)
    st = agent.approve(st.run_id, st.pending_approval.proposal_hash)
    assert st.tool_history[-1].error_code == "write_failed"
    assert st.status == AgentStatus.FAILED and st.stop_reason == "publish_failed:write_failed"
    assert published_count(services) == 0  # no publication record was created


def test_path_forbidden(make_agent, services):
    cat = _catalog(services)
    next(p for p in cat["photos"] if p["photo_id"] == "rest_001")["file"] = "../catalog.json"
    _save_catalog(services, cat)
    _, st = _run(make_agent, [[tool_use("evaluate_photo_quality", {"photo_ids": ["rest_001"]})], [text("x")]])
    assert st.tool_history[0].error_code == "path_forbidden"
    assert st.status == AgentStatus.FAILED and st.stop_reason == "unrecoverable_tool_error:path_forbidden"


# --------------------------------------------------------------------------- coverage guard
def test_every_errors_md_class_has_a_test():
    doc = (ROOT / "errors.md").read_text(encoding="utf-8")
    section = doc.split("## 2.", 1)[1].split("## 3.", 1)[0]
    codes = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", section, flags=re.MULTILINE))
    assert len(codes) >= 18, codes
    missing = sorted(c for c in codes if f"test_{c}" not in globals())
    assert not missing, f"errors.md classes without a test in this file: {missing}"
