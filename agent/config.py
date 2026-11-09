"""Configuration: stop limits, model choice, pricing. Owner: Abdallah."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

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

# USD per million tokens (input, output). Source: Anthropic price list, 2026-10.
# Unknown models -> cost reported as unavailable (None), never as 0.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def estimate_cost_usd(model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    price = PRICING_PER_MTOK.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 6)
