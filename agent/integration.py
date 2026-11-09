"""Composition root: wires teammates' modules into the agent. Owner: Abdallah.

This is the ONLY place that knows which concrete implementation of each
team module is used. Swapping in Khalil's KonCept512 wrapper or Skander's
extended policy means changing this file, not the loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agent.config import DEFAULT_MODEL, Limits
from agent.loop import Agent
from agent.mock_providers import CompromisedMockProvider, HeuristicMockPlanner, LoopingMockProvider
from agent.providers import AnthropicProvider, ModelProvider
from agent.registry import FaultInjector, ToolRegistry
from agent.store import RunStore
from sandbox import Sandbox
from security import ApprovalAuthority
from tools.quality import HeuristicQualityEstimator, QualityEstimator

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROVIDER_LABELS = {
    "anthropic": "Claude (live LLM)",
    "mock-heuristic": "Offline mock planner (rule-based, NOT an LLM)",
    "mock-looping": "Mock: runaway looping model",
    "mock-compromised": "Mock: injection-compromised model",
}


@dataclass
class Services:
    sandbox: Sandbox
    store: RunStore
    approvals: ApprovalAuthority
    quality: QualityEstimator


def build_services(data_dir: Path | None = None, quality: QualityEstimator | None = None) -> Services:
    data_dir = Path(data_dir or os.environ.get("LENSPILOT_DATA_DIR", PROJECT_ROOT / "var"))
    sandbox = Sandbox(data_dir / "sandbox").ensure()
    return Services(
        sandbox=sandbox,
        store=RunStore(data_dir / "runs"),
        approvals=ApprovalAuthority(secret_file=data_dir / ".approval_secret"),
        quality=quality or HeuristicQualityEstimator(),
    )


def make_provider(name: str, model: str = DEFAULT_MODEL) -> ModelProvider:
    if name == "anthropic":
        return AnthropicProvider(model=model)
    if name == "mock-heuristic":
        return HeuristicMockPlanner()
    if name == "mock-looping":
        return LoopingMockProvider()
    if name == "mock-compromised":
        return CompromisedMockProvider()
    raise ValueError(f"Unknown provider {name!r}")


def build_agent(services: Services, provider: ModelProvider, faults: dict[str, int] | None = None,
                limits: Limits | None = None) -> Agent:
    return Agent(
        provider=provider,
        registry=ToolRegistry(faults=FaultInjector(dict(faults or {}))),
        store=services.store,
        sandbox=services.sandbox,
        approvals=services.approvals,
        quality=services.quality,
        limits=limits,
    )
