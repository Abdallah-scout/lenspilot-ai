"""AnthropicProvider request/response handling, offline (stub client, real SDK response types).

These verify the integration code path; they are NOT a live inference test.
A live run is done manually with ANTHROPIC_API_KEY set (see docs/demo_script.md).
"""

from types import SimpleNamespace

import pytest
from anthropic.types.beta import BetaMessage

from agent.providers import AnthropicProvider, ProviderError
from agent.registry import ToolRegistry


class _StubMessages:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.kwargs = response, exc, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


def _provider(stub):
    client = SimpleNamespace(beta=SimpleNamespace(messages=stub))
    return AnthropicProvider(model="claude-opus-5-5", client=client)


def _message(content, stop_reason="tool_use"):
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 80},
    })


def test_request_shape_and_tool_use_parsing():
    stub = _StubMessages(_message([
        {"type": "text", "text": "Listing photos."},
        {"type": "tool_use", "id": "toolu_1", "name": "list_photos", "input": {"category": "restaurant"}},
    ]))
    tools = ToolRegistry().model_tool_definitions()
    turn = _provider(stub).next_turn("sys", [{"role": "user", "content": "hi"}], tools)
    assert stub.kwargs["tools"] == tools and stub.kwargs["system"] == "sys"
    assert stub.kwargs["thinking"] == {"type": "adaptive"}
    assert stub.kwargs["fallbacks"] == "default"
    assert "publish_photo" not in [t["name"] for t in stub.kwargs["tools"]]
    assert turn.tool_calls == [{"type": "tool_use", "id": "toolu_1", "name": "list_photos",
                                "input": {"category": "restaurant"}}]
    assert turn.text == "Listing photos." and turn.input_tokens == 1200 and turn.output_tokens == 80


def test_api_errors_become_provider_errors():
    import anthropic
    import httpx2  # the HTTP client used by anthropic>=2
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.APIConnectionError(request=req)
    with pytest.raises(ProviderError) as info:
        _provider(_StubMessages(exc=exc)).next_turn("s", [{"role": "user", "content": "x"}], [])
    assert info.value.code == "model_unavailable"
