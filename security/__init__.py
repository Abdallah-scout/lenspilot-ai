"""Public API of the security package (owner: Skander).

Import from here, not from the submodules: approval (human tokens),
injection (untrusted text) and policy (what may be published).
"""

from security.approval import ApprovalAuthority
from security.injection import scan_for_injection, wrap_untrusted
from security.policy import PolicyDecision, check_caption, check_proposal, check_publication_eligibility

__all__ = [
    "ApprovalAuthority",
    "PolicyDecision",
    "check_caption",
    "check_proposal",
    "check_publication_eligibility",
    "scan_for_injection",
    "wrap_untrusted",
]
