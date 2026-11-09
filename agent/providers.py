"""Model providers. Owner: Abdallah.

A provider turns (system, messages, tools) into the model's next turn. The
conversation format is Anthropic's Messages format for every provider, so
mocks and the live model are interchangeable in the loop.

``AnthropicProvider`` is the real LLM path (Claude via the official SDK).
Mock providers live in agent/mock_providers.py and set ``is_mock = True``;
the UI and traces always show that flag.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Protocol

from agent.config import DEFAULT_EFFORT, DEFAULT_MODEL


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
