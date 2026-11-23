"""Publication policy: which photos may be published, and with which captions.

Owner: Skander (security policy).
(Started as an integration baseline by Abdallah; hardened by Skander.)

Tests: tests/test_security.py::TestEligibility, TestCaptionGuard and TestCheckProposal.

Deterministic rules; the LLM is never consulted.

``check_publication_eligibility`` is called in prepare_publication (before
approval) AND again in publish_photo (right before the write), so a consent
change between the two is caught. It is deny-by-default: anything that is
not exactly ``visibility == "public"`` and ``client_consent == "granted"``
(including a missing field, a different type, or extra whitespace) is denied.

``check_caption`` / ``check_proposal`` cover the OUTPUT side: captions are
written by the model, which may have read injected text in a description.
A caption that carries instructions, links, markup or hidden characters must
not reach the published portfolio. These functions live here so the tool
layer can call them (see threat_model.md, "Open items").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from security.injection import has_hidden_characters, scan_for_injection

CAPTION_MIN_CHARS = 10
CAPTION_MAX_CHARS = 280

_URL_RE = re.compile(r"(https?://|ftp://|www\.|\b[\w.-]+\.(com|net|org|io|xyz|ru|tk)\b)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_MARKUP_RE = re.compile(r"<\s*/?\s*[a-z][^>]*>|!\[[^\]]*\]\(|\[[^\]]+\]\([^)]+\)|javascript:", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def check_publication_eligibility(photo: dict) -> PolicyDecision:
    if not isinstance(photo, dict):
        return PolicyDecision(allowed=False, reasons=["photo record is missing or malformed"])
    reasons: list[str] = []
    if photo.get("visibility") != "public":
        reasons.append(f"visibility is {photo.get('visibility')!r} (must be 'public')")
    if photo.get("client_consent") != "granted":
        reasons.append(f"client consent is {photo.get('client_consent')!r} (must be 'granted')")
    return PolicyDecision(allowed=not reasons, reasons=reasons)


def check_caption(caption: str) -> PolicyDecision:
    """Output-side guard for a model-written caption."""
    if not isinstance(caption, str):
        return PolicyDecision(allowed=False, reasons=["caption must be text"])
    reasons: list[str] = []
    length = len(caption.strip())
    if not CAPTION_MIN_CHARS <= length <= CAPTION_MAX_CHARS:
        reasons.append(f"caption length {length} outside {CAPTION_MIN_CHARS}-{CAPTION_MAX_CHARS}")
    flags = [f for f in scan_for_injection(caption) if f != "hidden_characters"]
    if flags:
        reasons.append(f"caption contains instruction-like text ({', '.join(flags)})")
    if has_hidden_characters(caption):
        reasons.append("caption contains hidden/invisible characters")
    if _CONTROL_RE.search(caption):
        reasons.append("caption contains control characters")
    if _URL_RE.search(caption) or _EMAIL_RE.search(caption):
        reasons.append("caption contains a link or e-mail address")
    if _MARKUP_RE.search(caption):
        reasons.append("caption contains HTML/Markdown markup")
    return PolicyDecision(allowed=not reasons, reasons=reasons)


def check_proposal(items: list[dict], get_photo: Callable[[str], dict]) -> list[dict]:
    """Check every item of a proposal. Returns the blocked items (empty list = all allowed).

    Each blocked entry is ``{"photo_id": ..., "reasons": [...]}``. ``get_photo``
    is the sandbox lookup (``Sandbox.get_photo``); its errors propagate.
    """
    blocked: list[dict] = []
    for item in items:
        reasons = list(check_publication_eligibility(get_photo(item["photo_id"])).reasons)
        reasons += check_caption(item.get("caption", "")).reasons
        if reasons:
            blocked.append({"photo_id": item["photo_id"], "reasons": reasons})
    return blocked
