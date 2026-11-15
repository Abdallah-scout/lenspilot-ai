"""Model providers. Owner: Abdallah.

A provider turns (system, messages, tools) into the model's next turn. The
conversation format is Anthropic's Messages format for every provider, so
mocks and the live model are interchangeable in the loop.

``OpenAIProvider`` is the default live path (OpenAI Chat Completions; it
translates to and from the Anthropic format). ``AnthropicProvider`` is the
Claude path via the official SDK. ``OllamaProvider`` runs a free local
model through the same OpenAI-compatible code.
Mock providers live in agent/mock_providers.py and set ``is_mock = True``;
the UI and traces always show that flag.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from dataclasses import dataclass, field
from typing import Protocol

from agent.config import DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_OPENAI_MODEL


@dataclass
class ModelTurn:
    content: list[dict]  # assistant content blocks, JSON-serialisable, sent back verbatim
    stop_reason: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def tool_calls(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text").strip()


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ModelProvider(Protocol):
    name: str
    model: str
    is_mock: bool

    def next_turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn: ...


# Models that accept the server-side refusal fallback beta in the "default" form.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


class AnthropicProvider:
    """Live Claude tool calling via the official ``anthropic`` SDK."""

    name = "anthropic"
    is_mock = False

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT, client=None):
        import anthropic  # imported lazily so offline tests never need credentials

        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic(max_retries=2, timeout=120.0)

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def next_turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        a = self._anthropic
        kwargs = dict(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
        )
        if self.model in _FALLBACK_MODELS:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        t0 = time.perf_counter()
        try:
            resp = self.client.beta.messages.create(**kwargs)
        except a.AuthenticationError as exc:
            raise ProviderError("model_auth_error", "Anthropic credentials were rejected.") from exc
        except a.RateLimitError as exc:
            raise ProviderError("model_rate_limited", "Model rate limit reached (after SDK retries).") from exc
        except a.BadRequestError as exc:
            raise ProviderError("model_bad_request", f"Model request rejected: {exc.message}") from exc
        except a.APIStatusError as exc:
            raise ProviderError("model_unavailable", f"Model API error {exc.status_code}.") from exc
        except a.APIConnectionError as exc:
            raise ProviderError("model_unavailable", "Could not reach the model API.") from exc
        usage = resp.usage
        return ModelTurn(
            content=[b.model_dump(exclude_none=True) for b in resp.content],
            stop_reason=resp.stop_reason or "unknown",
            model=resp.model,
            input_tokens=(usage.input_tokens or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0),
            output_tokens=usage.output_tokens,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )


# OpenAI finish_reason -> the Anthropic-style stop reasons the loop understands.
_OPENAI_STOP = {"tool_calls": "tool_use", "function_call": "tool_use", "stop": "end_turn",
                "length": "max_tokens", "content_filter": "refusal"}


def anthropic_to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Translate the loop's Anthropic-format history into Chat Completions messages.

    * assistant ``text`` blocks -> ``content``; ``tool_use`` blocks -> ``tool_calls``
    * user ``tool_result`` blocks -> one ``role="tool"`` message each (same id)
    * user strings / ``text`` blocks -> ``role="user"``
    Other block types (e.g. thinking) are dropped: OpenAI has no equivalent.
    """
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            texts = [b["text"] for b in content if b.get("type") == "text" and b.get("text")]
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in content if b.get("type") == "tool_use"]
            msg: dict = {"role": "assistant", "content": "\n".join(texts) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        user_texts = []
        for b in content:
            if b.get("type") == "tool_result":
                body = b.get("content")
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                            "content": body if isinstance(body, str) else json.dumps(body)})
            elif b.get("type") == "text":
                user_texts.append(b["text"])
        if user_texts:
            out.append({"role": "user", "content": "\n".join(user_texts)})
    return out


def anthropic_to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["input_schema"]}} for t in tools]


class OpenAIProvider:
    """Live tool calling via the official ``openai`` SDK (Chat Completions).

    The loop keeps Anthropic's message format; this class translates on the
    way out and back, so the loop, tools, approval and stop rules are unchanged.
    """

    name = "openai"
    is_mock = False
    extra_body: dict | None = None  # provider-specific request fields (used by OllamaProvider)

    def __init__(self, model: str = DEFAULT_OPENAI_MODEL, client=None):
        import openai  # imported lazily so offline tests never need credentials

        self._openai = openai
        self.model = model
        self.client = client or openai.OpenAI(max_retries=2, timeout=120.0)

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("OPENAI_API_KEY"))

    def next_turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        o = self._openai
        kwargs = dict(model=self.model, messages=anthropic_to_openai_messages(system, messages),
                      max_completion_tokens=4000)
        if tools:
            kwargs["tools"] = anthropic_to_openai_tools(tools)
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        t0 = time.perf_counter()
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except o.AuthenticationError as exc:
            raise ProviderError("model_auth_error", "OpenAI credentials were rejected.") from exc
        except o.RateLimitError as exc:
            # 429 covers both real rate limits and an account with no credit.
            if {getattr(exc, "code", None), getattr(exc, "type", None)} & {"insufficient_quota",
                                                                            "credit_balance_exhausted"}:
                raise ProviderError("model_quota_exhausted", "The OpenAI account has no credit left "
                                    "(insufficient_quota). Add credit in the OpenAI billing settings.") from exc
            raise ProviderError("model_rate_limited", "OpenAI rate limit reached (after SDK retries).") from exc
        except o.BadRequestError as exc:
            raise ProviderError("model_bad_request", f"Model request rejected: {exc.message}") from exc
        except o.APIStatusError as exc:
            raise ProviderError("model_unavailable", f"Model API error {exc.status_code}.") from exc
        except o.APIConnectionError as exc:
            raise ProviderError("model_unavailable", "Could not reach the model API.") from exc
        choice = resp.choices[0]
        msg = choice.message
        content: list[dict] = []
        if msg.content:
            content.append({"type": "text", "text": msg.content})
        if getattr(msg, "refusal", None):
            content.append({"type": "text", "text": msg.refusal})
        for call in msg.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None
            if not isinstance(args, dict):
                # Not a JSON object: pass a marker the strict schema rejects (invalid_arguments).
                args = {"_unparseable_arguments": (call.function.arguments or "")[:200]}
            content.append({"type": "tool_use", "id": call.id, "name": call.function.name, "input": args})
        stop = "refusal" if getattr(msg, "refusal", None) else _OPENAI_STOP.get(choice.finish_reason, "unknown")
        if msg.tool_calls and stop == "end_turn":
            stop = "tool_use"
        usage = resp.usage
        return ModelTurn(
            content=content,
            stop_reason=stop,
            model=resp.model,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )


class OllamaProvider(OpenAIProvider):
    """A free model running locally in Ollama, through Ollama's OpenAI-compatible API.

    Same translation and tool calling as OpenAIProvider; only the address,
    the model and the error messages differ. No key and no cost: the model
    runs on this computer. Enabled by setting LENSPILOT_OLLAMA_MODEL (e.g. in .env).
    """

    name = "ollama"
    is_mock = False
    # Reasoning models (qwen3, gpt-oss) otherwise "think" for 10-170 s per step.
    # Set LENSPILOT_OLLAMA_THINK=1 to keep thinking on.
    extra_body = None if os.environ.get("LENSPILOT_OLLAMA_THINK") == "1" else {"reasoning_effort": "none"}

    def __init__(self, model: str | None = None, base_url: str | None = None, client=None):
        import openai

        self.base_url = base_url or os.environ.get("LENSPILOT_OLLAMA_URL", "http://localhost:11434/v1")
        model = model or os.environ.get("LENSPILOT_OLLAMA_MODEL", "llama3.1")
        # Ollama ignores the key, but the SDK requires one. Local models are slower: longer timeout.
        client = client or openai.OpenAI(base_url=self.base_url, api_key="ollama", max_retries=1, timeout=300.0)
        super().__init__(model=model, client=client)

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("LENSPILOT_OLLAMA_MODEL"))

    # Small local models (e.g. Llama 3.1 8B) tend to invent ids and skip steps. This hint is added
    # for Ollama only, so the shared system prompt used by OpenAI/Claude stays unchanged.
    LOCAL_MODEL_HINT = """

Extra rules for this model:
1. Your FIRST tool call must be list_photos. Pass category "restaurant" or "interior" ONLY if the user
   clearly asked for one; otherwise call it with no arguments ({}) to see every photo. Never pass "null".
2. Only use photo_ids that appeared in a list_photos result. Never invent ids such as "photo1".
3. Then call get_photo_metadata and evaluate_photo_quality with those ids, then read_style_guide.
4. Then call prepare_publication with eligible photos (publication_eligibility.allowed is true) and
   captions of one or two plain sentences. Do not ask the user for ids; find them with the tools.
5. The user cannot answer questions during the run: a reply without a tool call ENDS the task. If a tool
   returns an error, read it, fix the arguments and call the tool again. Only reply with plain text once
   the publication result has arrived, or when nothing eligible can be published (then say why).
6. If the request mentions something the catalog does not have (e.g. an "event"), pick the photos that
   best fit from the catalog and say so in your summary.
7. If prepare_publication returns policy_denied, IMMEDIATELY call prepare_publication again with only the
   eligible photos (same captions). Never write that a proposal was prepared, sent or is awaiting
   approval unless prepare_publication returned status "awaiting_human_approval"."""

    def next_turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        try:
            turn = super().next_turn(system + self.LOCAL_MODEL_HINT, messages, tools)
        except ProviderError as exc:
            if "404" in exc.message:
                raise ProviderError("model_unavailable", f"Model {self.model!r} is not installed in Ollama. "
                                    f"Run: ollama pull {self.model}") from exc
            if exc.code == "model_unavailable":
                raise ProviderError("model_unavailable", f"Could not reach Ollama at {self.base_url}. "
                                    "Is Ollama running?") from exc
            raise
        if not turn.tool_calls:
            recovered = extract_text_tool_calls(turn.text, {t["name"] for t in tools})
            if recovered:
                turn.content = [b for b in turn.content if b.get("type") != "text"] + \
                               [{"type": "text", "text": "[tool call written as text, converted]"}] + recovered
                turn.stop_reason = "tool_use"
        return turn


_text_call_ids = itertools.count(1)


def extract_text_tool_calls(text: str, allowed_names: set[str]) -> list[dict]:
    """Recover tool calls that a local model wrote as JSON text instead of a real tool call.

    Accepts ``{"name": <tool>, "parameters"|"arguments"|"input": {...}}`` objects anywhere in the
    text. Only names in ``allowed_names`` (the tools actually offered to the model) are accepted,
    so this can never reach orchestrator-only tools; arguments still go through schema validation.
    """
    decoder = json.JSONDecoder()
    calls, i = [], 0
    while (start := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            i = start + 1
            continue
        i = end
        if not isinstance(obj, dict) or obj.get("name") not in allowed_names:
            continue
        args = next((obj[k] for k in ("parameters", "arguments", "input") if k in obj), {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": args[:200]}
        if not isinstance(args, dict):
            args = {"_unparseable_arguments": str(args)[:200]}
        calls.append({"type": "tool_use", "id": f"call_text_{next(_text_call_ids):04d}",
                      "name": obj["name"], "input": args})
    return calls
