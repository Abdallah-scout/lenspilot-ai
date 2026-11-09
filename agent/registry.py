"""Central tool registry: allowlist + schema validation + safe dispatch. Owner: Abdallah.

The model can only reach code through ``dispatch``. A tool name not in the
registry, a tool not exposed to the model, or arguments that fail the
Pydantic schema are rejected BEFORE any handler runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from pydantic import BaseModel, ValidationError

from sandbox import SandboxError
from tools import photo_tools as pt
from tools.schemas import (
    EmptyInput,
    ListPhotosInput,
    PhotoIdsInput,
    PreparePublicationInput,
    PublishInput,
    ToolContext,
    ToolResult,
)


class ToolKind(str, Enum):
    READ = "read"
    PROPOSE = "propose"  # no side effect, triggers the human checkpoint
    WRITE = "write"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], ToolResult]
    kind: ToolKind
    expose_to_model: bool = True
    requires_approval: bool = False


@dataclass
class FaultInjector:
    """Deterministic fault injection for demos/tests (clearly labelled in traces).

    ``failures = {"evaluate_photo_quality": 1}`` makes the next call to that tool
    return a retryable ``tool_unavailable`` error once.
    """

    failures: dict[str, int] = field(default_factory=dict)

    def take(self, tool: str) -> bool:
        remaining = self.failures.get(tool, 0)
        if remaining > 0:
            self.failures[tool] = remaining - 1
            return True
        return False


def default_specs() -> list[ToolSpec]:
    return [
        ToolSpec("list_photos", "List photos in the sandbox catalog, optionally filtered by category "
                 "('restaurant' or 'interior'). Returns ids, titles, categories.",
                 ListPhotosInput, pt.list_photos, ToolKind.READ),
        ToolSpec("get_photo_metadata", "Get technical metadata, simulated consent/visibility, publication "
                 "eligibility and the (untrusted) description for 1-10 photo ids.",
                 PhotoIdsInput, pt.get_photo_metadata, ToolKind.READ),
        ToolSpec("evaluate_photo_quality", "Compute an image-quality score 0-100 for 1-10 photos "
                 "(sharpness/exposure/contrast heuristic). >= 60 means publish-ready.",
                 PhotoIdsInput, pt.evaluate_photo_quality, ToolKind.READ),
        ToolSpec("read_style_guide", "Read the studio caption style guide.",
                 EmptyInput, pt.read_style_guide, ToolKind.READ),
        ToolSpec("prepare_publication", "Propose publishing 1-5 photos with captions to a sandbox "
                 "collection. Checks policy and returns a proposal. Does NOT publish: a human must "
                 "approve, then the system publishes and tells you the result.",
                 PreparePublicationInput, pt.prepare_publication, ToolKind.PROPOSE),
        ToolSpec("publish_photo", "Write approved photos to the sandbox portfolio. Orchestrator-only.",
                 PublishInput, pt.publish_photo, ToolKind.WRITE,
                 expose_to_model=False, requires_approval=True),
    ]


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None, faults: FaultInjector | None = None):
        self._specs = {s.name: s for s in (specs or default_specs())}
        self.faults = faults or FaultInjector()

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def model_tool_definitions(self) -> list[dict]:
        """Anthropic ``tools`` param: only model-exposed tools, schemas from Pydantic."""
        return [
            {"name": s.name, "description": s.description, "input_schema": s.input_model.model_json_schema()}
            for s in self._specs.values()
            if s.expose_to_model
        ]

    def dispatch(self, name: str, raw_args: dict, ctx: ToolContext, origin: str) -> tuple[ToolResult, float]:
        """Validate and execute. Returns (result, duration_ms). Never raises for tool errors."""
        t0 = time.perf_counter()
        result = self._dispatch(name, raw_args, ctx, origin)
        return result, round((time.perf_counter() - t0) * 1000, 2)

    def _dispatch(self, name: str, raw_args: dict, ctx: ToolContext, origin: str) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult.fail("unknown_tool", f"Tool {name!r} does not exist. Available: "
                                   f"{[s.name for s in self._specs.values() if s.expose_to_model]}")
        if origin == "model" and not spec.expose_to_model:
            return ToolResult.fail("tool_not_allowed", f"Tool {name!r} cannot be called by the agent. "
                                   "Publishing happens only after human approval of prepare_publication.")
        if not isinstance(raw_args, dict):
            return ToolResult.fail("invalid_arguments", "Tool arguments must be a JSON object.")
        try:
            args = spec.input_model.model_validate(raw_args)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
            return ToolResult.fail("invalid_arguments", f"Invalid arguments for {name}: {problems}")
        if self.faults.take(name):
            return ToolResult.fail("tool_unavailable", f"{name} is temporarily unavailable "
                                   "[simulated fault]. You may retry.", retryable=True)
        try:
            return spec.handler(ctx, args)
        except SandboxError as exc:
            return ToolResult.fail(exc.code, exc.message, exc.retryable)
        except Exception as exc:  # unexpected bug: reported as a failure, never as success
            return ToolResult.fail("internal_error", f"{name} failed unexpectedly ({type(exc).__name__}).")
