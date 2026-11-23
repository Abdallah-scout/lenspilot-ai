"""Safety layer of LensPilot — single entry point.

Owner: Skander (safety + failure engineering).

Review tip: start at SAFETY_INVARIANTS (S1-S8); each entry names the code that
enforces the rule and the test that proves it.

The proposal lists ``agent/safety.py`` as Skander's hand-off. The actual
implementation lives in the ``security`` package (already wired into the
tools, the loop and the UI); this module re-exports it and documents, in one
place, every safety invariant and where it is enforced in code.

Nothing here is consulted by the model. Every control is deterministic code
that holds even if the model is fully compromised (see threat_model.md).
"""

from __future__ import annotations

from security.approval import (
    DEFAULT_TTL_SECONDS,
    TOKEN_EXPIRED,
    TOKEN_FROM_FUTURE,
    TOKEN_MALFORMED,
    TOKEN_MISMATCH,
    TOKEN_OK,
    ApprovalAuthority,
)
from security.injection import (
    has_hidden_characters,
    normalize_for_scan,
    scan_for_injection,
    strip_invisible,
    wrap_untrusted,
)
from security.policy import (
    PolicyDecision,
    check_caption,
    check_proposal,
    check_publication_eligibility,
)

# Each invariant -> where it is enforced -> which test proves it.
SAFETY_INVARIANTS: list[dict[str, str]] = [
    {"id": "S1", "rule": "The model cannot call the write tool.",
     "enforced_in": "agent/registry.py (publish_photo expose_to_model=False)",
     "proven_by": "tests/test_injection_attacks.py::test_model_cannot_call_publish_even_when_told_to"},
    {"id": "S2", "rule": "No write without an explicit human yes bound to the exact proposal.",
     "enforced_in": "security/approval.py (HMAC token) + tools/photo_tools.py::publish_photo",
     "proven_by": "tests/test_security.py::TestApprovalTokens"},
    {"id": "S3", "rule": "Approval tokens expire and cannot be replayed into another run or proposal.",
     "enforced_in": "security/approval.py::ApprovalAuthority.check",
     "proven_by": "tests/test_security.py::TestApprovalTokens"},
    {"id": "S4", "rule": "Private or unconsented photos are never published (checked twice).",
     "enforced_in": "security/policy.py::check_publication_eligibility (prepare + publish)",
     "proven_by": "tests/test_security.py::TestEligibility, tests/test_error_classes.py::test_policy_denied"},
    {"id": "S5", "rule": "Free text from the dataset is delivered as data, flagged in the trace.",
     "enforced_in": "security/injection.py::wrap_untrusted + agent/loop.py scan",
     "proven_by": "tests/test_injection_attacks.py"},
    {"id": "S6", "rule": "Writes only go to <sandbox>/published and are idempotent.",
     "enforced_in": "sandbox/manager.py::write_publication",
     "proven_by": "tests/test_error_classes.py::test_path_forbidden"},
    {"id": "S7", "rule": "The run always stops (steps, retries, LLM calls, time, tokens).",
     "enforced_in": "agent/loop.py + agent/config.py::Limits",
     "proven_by": "tests/test_stop_edge_cases.py, tests/test_stopping.py"},
    {"id": "S8", "rule": "Model-written captions must not carry instructions, links, markup or hidden text.",
     "enforced_in": "security/policy.py::check_proposal, called in tools/photo_tools.py (prepare + publish)",
     "proven_by": "tests/test_security.py::TestCaptionGuard, "
                  "tests/test_injection_attacks.py::test_injected_caption_never_reaches_the_human"},
]

__all__ = [
    "ApprovalAuthority",
    "DEFAULT_TTL_SECONDS",
    "PolicyDecision",
    "SAFETY_INVARIANTS",
    "TOKEN_EXPIRED",
    "TOKEN_FROM_FUTURE",
    "TOKEN_MALFORMED",
    "TOKEN_MISMATCH",
    "TOKEN_OK",
    "check_caption",
    "check_proposal",
    "check_publication_eligibility",
    "has_hidden_characters",
    "normalize_for_scan",
    "scan_for_injection",
    "strip_invisible",
    "wrap_untrusted",
]
