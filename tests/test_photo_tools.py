from __future__ import annotations

import pytest

from agent.mock_providers import tool_use
from agent.registry import ToolRegistry
from tools.schemas import ToolContext

FIRST = {"photo_id": "rest_001", "caption": "Warm candlelight fills the dining room."}
SECOND = {"photo_id": "rest_002", "caption": "Copper lamps glow above the chef's pass."}


@pytest.fixture
def call_tool(services):
    registry = ToolRegistry()
    context = ToolContext(services.sandbox, services.approvals, services.quality)

    def call(name, arguments, origin="model"):
        result, _ = registry.dispatch(name, arguments, context, origin=origin)
        return result

    return call


@pytest.mark.parametrize(
    ("tool", "arguments", "origin"),
    [
        ("list_photos", {"category": "landscape"}, "model"),
        ("get_photo_metadata", {"photo_ids": ["REST_001"]}, "model"),
        ("get_photo_metadata", {"photo_ids": ["rest_001\n"]}, "model"),
        ("evaluate_photo_quality", {"photo_ids": []}, "model"),
        ("read_style_guide", {"format": "text"}, "model"),
        ("prepare_publication", {"collection": "featured", "items": [FIRST, FIRST]}, "model"),
        ("prepare_publication", {"collection": "featured", "items": [{**FIRST, "caption": "hi"}]}, "model"),
        (
            "publish_photo",
            {
                "collection": "featured",
                "items": [FIRST, FIRST],
                "proposal_hash": "unused",
                "run_id": "run",
                "approval_token": "unused",
            },
            "orchestrator",
        ),
    ],
)
def test_dispatch_rejects_invalid_arguments(call_tool, services, tool, arguments, origin):
    result = call_tool(tool, arguments, origin)
    assert not result.success and result.error_code == "invalid_arguments"
    assert services.sandbox.published_records() == []


def test_read_tools_use_installed_fixtures(call_tool):
    listing = call_tool("list_photos", {"category": "restaurant"})
    assert listing.success and listing.data["count"] == 6
    metadata = call_tool("get_photo_metadata", {"photo_ids": ["rest_001", "unknown"]})
    assert metadata.success and metadata.data["missing"] == ["unknown"]
    assert metadata.data["photos"][0]["publication_eligibility"]["allowed"]
    quality = call_tool("evaluate_photo_quality", {"photo_ids": ["rest_001", "rest_005"]})
    assert quality.success
    first, second = quality.data["assessments"]
    assert 0 <= second["score"] < first["score"] <= 100
    style = call_tool("read_style_guide", {})
    assert style.success and "Studio caption style guide" in str(style.data["style_guide"])


def test_unreadable_photo_returns_a_stable_error(call_tool, services):
    services.sandbox.image_path("rest_001").write_bytes(b"broken image")
    result = call_tool("evaluate_photo_quality", {"photo_ids": ["rest_001"]})
    assert not result.success and result.error_code == "corrupted_data"


def test_publish_checks_all_sources_before_writing(call_tool, services):
    args = {"collection": "featured", "items": [FIRST, SECOND]}
    proposal = call_tool("prepare_publication", args)
    assert proposal.success
    assert services.sandbox.published_records() == []
    proposal_hash = proposal.data["proposal_hash"]
    approval = services.approvals.issue("run", proposal_hash, approver="human")
    publish_args = {**args, "run_id": "run", "proposal_hash": proposal_hash, "approval_token": approval}
    missing = services.sandbox.image_path("rest_002")
    source = missing.read_bytes()
    missing.unlink()
    failed = call_tool("publish_photo", publish_args, "orchestrator")
    assert not failed.success and failed.error_code == "image_missing"
    assert services.sandbox.published_records() == []

    missing.write_bytes(source)
    published = call_tool("publish_photo", publish_args, "orchestrator")
    assert published.success and all(item["created"] for item in published.data["published"])
    missing.unlink()
    repeated = call_tool("publish_photo", publish_args, "orchestrator")
    assert repeated.success and all(item["already_published"] for item in repeated.data["published"])
    assert len(services.sandbox.published_records()) == 2


def test_agent_verifies_the_published_image(make_agent, services, monkeypatch):
    args = {"collection": "featured", "items": [FIRST]}
    agent, _ = make_agent([[tool_use("prepare_publication", args)]])
    state = agent.run(agent.start("Publish the dining-room photo").run_id)
    original_write = services.sandbox.write_publication

    def remove_image_after_write(*args):
        record, created = original_write(*args)
        (services.sandbox.published_dir / record["image_file"]).unlink()
        return record, created

    monkeypatch.setattr(services.sandbox, "write_publication", remove_image_after_write)
    state = agent.approve(state.run_id, state.pending_approval.proposal_hash)
    assert state.stop_reason == "publish_verification_failed"
