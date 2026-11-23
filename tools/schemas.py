"""Tool input schemas, result envelope and shared context.

Owner: Khalil (tool schemas).
Contract consumed by agent/registry.py — see docs/integration_contracts.md.
Every input model forbids extra fields, so the model cannot smuggle
unexpected arguments (e.g. a file path) into a tool.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

PhotoId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")]
Collection = Literal["restaurants", "interiors", "people", "events", "featured"]


class ToolResult(BaseModel):
    """Uniform result envelope returned by every tool (never a traceback)."""

    success: bool
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False

    @classmethod
    def ok(cls, data: Any) -> "ToolResult":
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, code: str, message: str, retryable: bool = False) -> "ToolResult":
        return cls(success=False, error_code=code, error_message=message, retryable=retryable)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListPhotosInput(_Strict):
    category: Literal["restaurant", "interior", "people", "event"] | None = Field(
        default=None, description="Optional category filter."
    )


class PhotoIdsInput(_Strict):
    photo_ids: list[PhotoId] = Field(min_length=1, max_length=10, description="1-10 photo ids.")


class EmptyInput(_Strict):
    pass


class PublicationItem(_Strict):
    photo_id: PhotoId
    caption: str = Field(min_length=10, max_length=280)


class PreparePublicationInput(_Strict):
    collection: Collection
    items: list[PublicationItem] = Field(min_length=1, max_length=5)

    @field_validator("items")
    @classmethod
    def _unique_photos(cls, items: list[PublicationItem]) -> list[PublicationItem]:
        ids = [i.photo_id for i in items]
        if len(ids) != len(set(ids)):
            raise ValueError("each photo may appear only once in a proposal")
        return items


class PublishInput(PreparePublicationInput):
    """Internal input for the write tool. Not exposed to the model."""

    run_id: str
    proposal_hash: str
    approval_token: str


def compute_proposal_hash(collection: str, items: list[dict]) -> str:
    """Canonical hash of exactly what will be written."""
    canonical = {
        "collection": collection,
        "items": [{"photo_id": i["photo_id"], "caption": i["caption"]} for i in items],
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def idempotency_key(collection: str, photo_id: str) -> str:
    """One photo can be published at most once per collection."""
    return hashlib.sha256(f"{collection}|{photo_id}".encode()).hexdigest()[:32]


@dataclass
class ToolContext:
    """Everything a tool may touch. Tools get nothing else (no globals)."""

    sandbox: Any  # sandbox.Sandbox
    approvals: Any  # security.ApprovalAuthority
    quality: Any  # tools.quality.QualityEstimator
    run_id: str = ""
    extras: dict = field(default_factory=dict)
