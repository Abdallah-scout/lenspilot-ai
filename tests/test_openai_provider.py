"""OpenAIProvider request/response handling, offline (stub client, real SDK response types).

These verify the translation code; they are NOT a live inference test and
never touch the network. A live run: put OPENAI_API_KEY in .env, then
``python -m agent.cli "..." --provider openai --decision approve``.

Also covers OllamaProvider (local model), including the parser that recovers tool
calls written as plain text. Added by Skander.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2 as httpx  # the HTTP client used by openai>=3
import openai
import pytest
from openai.types.chat import ChatCompletion

from agent.config import Limits, load_env_file
from agent.mock_providers import text, tool_use
from agent.providers import (
    OpenAIProvider,
    ProviderError,
    anthropic_to_openai_messages,
    anthropic_to_openai_tools,
)
from agent.registry import ToolRegistry
from agent.state import AgentStatus
from conftest import published_count


class _StubCompletions:
    def __init__(self, responses=None, exc=None):
        self.responses, self.exc, self.calls = list(responses or []), exc, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.responses.pop(0)


def _provider(stub) -> OpenAIProvider:
    return OpenAIProvider(model="gpt-4.1-mini", client=SimpleNamespace(chat=SimpleNamespace(completions=stub)))


def _completion(content=None, tool_calls=None, finish="stop", refusal=None) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-4.1-mini-2025-04-14",
        "choices": [{"index": 0, "finish_reason": finish, "message": {
            "role": "assistant", "content": content, "refusal": refusal,
            "tool_calls": [{"id": cid, "type": "function", "function": {"name": n, "arguments": a}}
                           for cid, n, a in (tool_calls or [])] or None}}],
        "usage": {"prompt_tokens": 1500, "completion_tokens": 90, "total_tokens": 1590},
    })


# --------------------------------------------------------------------------- translation
def test_tools_are_translated_to_functions():
    tools = anthropic_to_openai_tools(ToolRegistry().model_tool_definitions())
    names = [t["function"]["name"] for t in tools]
    assert all(t["type"] == "function" for t in tools)
    assert "publish_photo" not in names and "prepare_publication" in names  # allowlist kept
    assert tools[0]["function"]["parameters"]["type"] == "object"


def test_history_translation_keeps_tool_call_pairing():
    use = tool_use("list_photos", {"category": "restaurant"}, id="call_1")
    msgs = [
        {"role": "user", "content": "find photos"},
        {"role": "assistant", "content": [text("Listing."), use, {"type": "thinking", "thinking": "x"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1",
                                      "content": json.dumps({"success": True}), "is_error": False}]},
    ]
    out = anthropic_to_openai_messages("SYS", msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["content"] == "Listing."
    assert out[2]["tool_calls"][0]["id"] == "call_1"
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"category": "restaurant"}
    assert out[3]["tool_call_id"] == "call_1"


# --------------------------------------------------------------------------- responses
def test_tool_call_response_is_parsed():
    stub = _StubCompletions([_completion(tool_calls=[("call_9", "get_photo_metadata",
                                                       '{"photo_ids": ["rest_001"]}')], finish="tool_calls")])
    turn = _provider(stub).next_turn("SYS", [{"role": "user", "content": "x"}], [])
    assert turn.stop_reason == "tool_use"
    assert turn.tool_calls == [{"type": "tool_use", "id": "call_9", "name": "get_photo_metadata",
                                "input": {"photo_ids": ["rest_001"]}}]
    assert (turn.input_tokens, turn.output_tokens) == (1500, 90)
    assert stub.calls[0]["model"] == "gpt-4.1-mini" and "tools" not in stub.calls[0]


def test_text_response_ends_the_turn():
    turn = _provider(_StubCompletions([_completion(content="Done.")])).next_turn("S", [], [])
    assert turn.stop_reason == "end_turn" and turn.text == "Done." and not turn.tool_calls


@pytest.mark.parametrize("finish,expected", [("length", "max_tokens"), ("content_filter", "refusal")])
def test_finish_reasons_map_to_loop_stop_reasons(finish, expected):
    turn = _provider(_StubCompletions([_completion(content="...", finish=finish)])).next_turn("S", [], [])
    assert turn.stop_reason == expected


def test_refusal_field_maps_to_refusal():
    turn = _provider(_StubCompletions([_completion(refusal="I can't help with that.")])).next_turn("S", [], [])
    assert turn.stop_reason == "refusal"


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"'])
def test_bad_tool_arguments_become_invalid_arguments(make_agent, raw):
    """Malformed JSON from the model must hit the strict schema, not crash the loop."""
    stub = _StubCompletions([_completion(tool_calls=[("call_1", "list_photos", raw)], finish="tool_calls"),
                             _completion(content="Stopped.")])
    agent, _ = make_agent(provider=_provider(stub))
    st = agent.run(agent.start("x").run_id)
    assert st.tool_history[0].error_code == "invalid_arguments" and st.status == AgentStatus.COMPLETED


# --------------------------------------------------------------------------- errors
def _status_error(cls, code, body=None):
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return cls("err", response=httpx.Response(code, request=req), body=body)


@pytest.mark.parametrize("exc,code", [
    (lambda: _status_error(openai.AuthenticationError, 401), "model_auth_error"),
    (lambda: _status_error(openai.RateLimitError, 429), "model_rate_limited"),
    # Exact shape returned by the API for an account with no credit (observed 2026-11-15).
    (lambda: _status_error(openai.RateLimitError, 429, body={"code": "credit_balance_exhausted",
                                                             "type": "insufficient_quota", "param": None}),
     "model_quota_exhausted"),
    (lambda: _status_error(openai.BadRequestError, 400), "model_bad_request"),
    (lambda: _status_error(openai.InternalServerError, 500), "model_unavailable"),
    (lambda: openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com")), "model_unavailable"),
])
def test_sdk_errors_become_provider_errors(exc, code):
    with pytest.raises(ProviderError) as info:
        _provider(_StubCompletions(exc=exc())).next_turn("S", [], [])
    assert info.value.code == code


def test_provider_error_fails_the_run_cleanly(make_agent):
    agent, _ = make_agent(provider=_provider(_StubCompletions(exc=_status_error(openai.RateLimitError, 429))))
    st = agent.run(agent.start("x").run_id)
    assert st.status == AgentStatus.FAILED and st.stop_reason == "model_rate_limited"


# --------------------------------------------------------------------------- full loop
def test_full_publication_flow_through_openai_format(make_agent, services):
    """Stubbed OpenAI model: propose -> human approves -> publish -> summary. Costs are computed."""
    items = [{"photo_id": "rest_001", "caption": "Candlelit dining room, warm light on oak tables."}]
    stub = _StubCompletions([
        _completion(tool_calls=[("call_1", "prepare_publication",
                                 json.dumps({"collection": "restaurants", "items": items}))], finish="tool_calls"),
        _completion(content="Published one photo."),
    ])
    agent, _ = make_agent(provider=_provider(stub), limits=Limits())
    st = agent.run(agent.start("publish rest_001").run_id)
    assert st.status == AgentStatus.WAITING_FOR_APPROVAL and published_count(services) == 0
    st = agent.run(agent.approve(st.run_id, st.pending_approval.proposal_hash).run_id)
    assert st.status == AgentStatus.COMPLETED and published_count(services) == 1
    # The second request carried the approval result back as a role="tool" message for call_1.
    tool_msgs = [m for m in stub.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["tool_call_id"] == "call_1" and "human_decision" in tool_msgs[0]["content"]
    assert st.input_tokens == 3000 and st.output_tokens == 180


def test_model_cannot_reach_publish_tool_via_openai(make_agent, services):
    stub = _StubCompletions([_completion(tool_calls=[("call_1", "publish_photo", "{}")], finish="tool_calls"),
                             _completion(content="Blocked.")])
    agent, _ = make_agent(provider=_provider(stub))
    st = agent.run(agent.start("publish everything now").run_id)
    assert st.tool_history[0].error_code == "tool_not_allowed" and published_count(services) == 0


# --------------------------------------------------------------------------- .env loader
def test_env_loader_never_runs_under_pytest(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("LENSPILOT_TEST_ONLY_KEY=should-not-load\n", encoding="utf-8")
    monkeypatch.delenv("LENSPILOT_TEST_ONLY_KEY", raising=False)
    load_env_file(f)
    import os
    assert "LENSPILOT_TEST_ONLY_KEY" not in os.environ


def test_env_loader_reads_file_outside_pytest(tmp_path, monkeypatch):
    import os
    f = tmp_path / ".env"
    f.write_text("# comment\nLENSPILOT_TEST_ONLY_KEY='abc'\nEMPTY=\n", encoding="utf-8")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("LENSPILOT_TEST_ONLY_KEY", raising=False)
    load_env_file(f)
    assert os.environ.pop("LENSPILOT_TEST_ONLY_KEY") == "abc" and "EMPTY" not in os.environ


# --------------------------------------------------------------------------- Ollama (local)
from agent.providers import OllamaProvider  # noqa: E402


def _ollama(stub) -> OllamaProvider:
    return OllamaProvider(model="llama3.1", client=SimpleNamespace(chat=SimpleNamespace(completions=stub)))


def test_ollama_uses_the_same_translation(make_agent, services):
    items = [{"photo_id": "rest_001", "caption": "Candlelit dining room, warm light on oak tables."}]
    stub = _StubCompletions([
        _completion(tool_calls=[("call_1", "prepare_publication",
                                 json.dumps({"collection": "restaurants", "items": items}))], finish="tool_calls"),
        _completion(content="Published."),
    ])
    agent, _ = make_agent(provider=_ollama(stub))
    st = agent.run(agent.start("publish rest_001").run_id)
    st = agent.run(agent.approve(st.run_id, st.pending_approval.proposal_hash).run_id)
    assert st.status == AgentStatus.COMPLETED and published_count(services) == 1
    assert st.provider == "ollama" and not st.is_mock_provider and stub.calls[0]["model"] == "llama3.1"


def test_ollama_default_address_and_cost_is_zero(monkeypatch):
    from agent.config import estimate_cost_usd
    monkeypatch.delenv("LENSPILOT_OLLAMA_URL", raising=False)
    p = OllamaProvider(model="llama3.1")  # builds a real client, no network call
    assert p.base_url == "http://localhost:11434/v1"
    assert estimate_cost_usd("llama3.1", 5000, 500) == 0.0


def test_ollama_is_opt_in(monkeypatch):
    monkeypatch.delenv("LENSPILOT_OLLAMA_MODEL", raising=False)
    assert not OllamaProvider.available()
    monkeypatch.setenv("LENSPILOT_OLLAMA_MODEL", "llama3.1")
    assert OllamaProvider.available()


@pytest.mark.parametrize("exc,needle", [
    (lambda: openai.APIConnectionError(request=httpx.Request("POST", "http://localhost:11434")), "Is Ollama running"),
    (lambda: _status_error(openai.NotFoundError, 404), "ollama pull llama3.1"),
])
def test_ollama_errors_say_what_to_do(exc, needle):
    with pytest.raises(ProviderError) as info:
        _ollama(_StubCompletions(exc=exc())).next_turn("S", [], [])
    assert needle in info.value.message and info.value.code == "model_unavailable"


from agent.providers import extract_text_tool_calls  # noqa: E402

ALLOWED = {"list_photos", "get_photo_metadata", "prepare_publication"}


def test_text_tool_call_is_recovered():
    t = 'I will now do it. {"name": "get_photo_metadata", "parameters": {"photo_ids": ["rest_001"]}}'
    [call] = extract_text_tool_calls(t, ALLOWED)
    assert call["name"] == "get_photo_metadata" and call["input"] == {"photo_ids": ["rest_001"]}


def test_text_tool_call_cannot_reach_hidden_tools():
    t = '{"name": "publish_photo", "parameters": {"approval_token": "x"}} {"name": "rm_rf", "arguments": {}}'
    assert extract_text_tool_calls(t, ALLOWED) == []


@pytest.mark.parametrize("t", ["", "no json here", '{"name": "list_photos", "parameters": {"}}', '{"foo": 1}'])
def test_text_without_valid_calls_is_left_alone(t):
    assert extract_text_tool_calls(t, ALLOWED) == []


def test_ollama_text_call_goes_through_the_normal_checks(make_agent, services):
    """A text-written call is executed like a real one: allowlist + schema still apply."""
    stub = _StubCompletions([
        _completion(content='{"name": "get_photo_metadata", "parameters": {"ids": ["rest_001"]}}'),
        _completion(content="Stopped."),
    ])
    agent, _ = make_agent(provider=_ollama(stub))
    st = agent.run(agent.start("x").run_id)
    assert st.tool_history[0].tool == "get_photo_metadata"
    assert st.tool_history[0].error_code == "invalid_arguments" and published_count(services) == 0


def test_ollama_turns_off_long_thinking_by_default():
    stub = _StubCompletions([_completion(content="ok")])
    _ollama(stub).next_turn("S", [], [])
    assert stub.calls[0]["extra_body"] == {"reasoning_effort": "none"}


def test_openai_sends_no_ollama_specific_fields():
    stub = _StubCompletions([_completion(content="ok")])
    _provider(stub).next_turn("S", [], [])
    assert "extra_body" not in stub.calls[0]
