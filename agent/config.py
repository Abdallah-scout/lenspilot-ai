"""Configuration: stop limits, model choice, pricing. Owner: Abdallah."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


def load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from the project's .env (git-ignored) into os.environ.

    Existing environment variables win. Skipped while pytest runs, so the
    offline test suite can never pick up a real key and make paid calls.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    path = path or Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if key.strip() and value:
            os.environ.setdefault(key.strip(), value)


load_env_file()

MAX_AGENT_STEPS = 8
MAX_TOOL_RETRIES = 2


class Limits(BaseModel):
    """Hard limits enforced by code, never by the model.

    max_steps         every tool-call ATTEMPT counts (success, failure, blocked, retry)
    max_tool_retries  re-attempts of an identical failed call
    max_llm_calls     model requests per run (stops text-only / approval-wait loops)
    max_active_seconds  wall time spent executing (excludes time waiting for a human)
    max_total_tokens  optional token budget (None = disabled)
    """

    max_steps: int = Field(default=MAX_AGENT_STEPS, ge=1, le=50)
    max_tool_retries: int = Field(default=MAX_TOOL_RETRIES, ge=0, le=5)
    max_llm_calls: int = Field(default=MAX_AGENT_STEPS + 4, ge=1, le=100)
    max_active_seconds: float = Field(default=180.0, gt=0)
    max_total_tokens: int | None = Field(default=200_000, ge=1)


DEFAULT_MODEL = os.environ.get("LENSPILOT_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = os.environ.get("LENSPILOT_EFFORT", "medium")
DEFAULT_OPENAI_MODEL = os.environ.get("LENSPILOT_OPENAI_MODEL", "gpt-4.1-mini")

# USD per million tokens (input, output). Source: Anthropic price list, 2026-10.
# Unknown models -> cost reported as unavailable (None), never as 0.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    # OpenAI list prices (USD / MTok). Check https://openai.com/api/pricing before quoting in the report.
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
    # Local Ollama models: no API charge (electricity/hardware not counted).
    "llama3.1": (0.0, 0.0),
    "llama3.1:latest": (0.0, 0.0),
    "qwen3:14b": (0.0, 0.0),
    "lenspilot-qwen3": (0.0, 0.0),  # qwen3:14b with a 16k context window (see README)
}


def estimate_cost_usd(model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    price = PRICING_PER_MTOK.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 6)
