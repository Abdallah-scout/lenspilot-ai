"""Shared fixtures. All tests are offline: mock providers + temporary sandbox."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import Limits  # noqa: E402
from agent.integration import build_agent, build_services  # noqa: E402
from agent.mock_providers import ScriptedProvider, text, tool_use  # noqa: E402


@pytest.fixture
def services(tmp_path):
    return build_services(tmp_path / "data")


@pytest.fixture
def make_agent(services):
    def _make(turns=None, provider=None, faults=None, limits=None):
        prov = provider or ScriptedProvider(turns or [])
        return build_agent(services, prov, faults=faults, limits=limits or Limits()), prov
    return _make


HAPPY_TURNS = [
    [text("Listing."), tool_use("list_photos", {"category": "restaurant"})],
    [tool_use("get_photo_metadata", {"photo_ids": ["rest_001", "rest_002"]}),
     tool_use("evaluate_photo_quality", {"photo_ids": ["rest_001", "rest_002"]})],
    [tool_use("read_style_guide", {})],
    [tool_use("prepare_publication", {"collection": "restaurants", "items": [
        {"photo_id": "rest_001", "caption": "Candlelit dining room, warm light on oak tables."},
        {"photo_id": "rest_002", "caption": "Plates lined up on the pass during service."}]})],
    [text("Published two photos.")],
]


def published_count(services) -> int:
    return len(services.sandbox.published_records())
