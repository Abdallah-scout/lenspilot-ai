"""Publication eligibility policy.

INTEGRATION BASELINE — owner: Skander (security policy).
Deterministic rules; the LLM is never consulted. Called by
prepare_publication (before approval) AND again by publish_photo (right
before the write), so a consent change between the two is caught.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def check_publication_eligibility(photo: dict) -> PolicyDecision:
    reasons: list[str] = []
    if photo.get("visibility") != "public":
        reasons.append(f"visibility is {photo.get('visibility')!r} (must be 'public')")
    if photo.get("client_consent") != "granted":
        reasons.append(f"client consent is {photo.get('client_consent')!r} (must be 'granted')")
    return PolicyDecision(allowed=not reasons, reasons=reasons)
