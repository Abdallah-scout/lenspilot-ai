"""Human-approval tokens.

Owner: Skander (approval policy / write protection).
(Started as an integration baseline by Abdallah; hardened by Skander.)

Tests: tests/test_security.py::TestApprovalTokens (unit) and the forged, replayed and
expired token cases in tests/test_injection_attacks.py (end to end).

A token is ``<issued_at>.<hmac>`` where the HMAC covers
(run_id, proposal_hash, issued_at). Consequences:
  * it can only be minted by code holding the secret (the orchestrator's
    approve() path, triggered by a human click) — never by the model;
  * it is bound to ONE exact proposal: changing a caption, photo, or the
    collection changes the hash and invalidates the token;
  * it is bound to ONE run: tokens cannot be replayed into another run;
  * it EXPIRES (default 15 minutes) and cannot be dated in the future, so a
    leaked or logged token is useless later. The issue time is inside the
    MAC, so editing the timestamp breaks the signature.

``verify`` keeps its boolean contract (used by tools/photo_tools.py);
``check`` returns the precise reason for logs, tests and errors.md.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Callable

DEFAULT_TTL_SECONDS = 15 * 60
CLOCK_SKEW_SECONDS = 5
_HEX64 = re.compile(r"[0-9a-f]{64}")

# Outcomes of ApprovalAuthority.check()
TOKEN_OK = "ok"
TOKEN_MALFORMED = "malformed"
TOKEN_MISMATCH = "mismatch"      # wrong run, wrong proposal, wrong secret, or tampered
TOKEN_EXPIRED = "expired"
TOKEN_FROM_FUTURE = "not_yet_valid"


class ApprovalAuthority:
    def __init__(self, secret: bytes | None = None, secret_file: Path | None = None,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS, clock: Callable[[], float] = time.time):
        self._secret = secret or self._load_secret(secret_file)
        if len(self._secret) < 16:
            raise ValueError("Approval secret must be at least 16 bytes.")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive.")
        self.ttl_seconds = ttl_seconds
        self._clock = clock

    @staticmethod
    def _load_secret(secret_file: Path | None) -> bytes:
        env = os.environ.get("LENSPILOT_APPROVAL_SECRET")
        if env:
            return env.encode()
        if secret_file is None:
            return secrets.token_bytes(32)  # per-process secret
        if not secret_file.exists():
            secret_file.parent.mkdir(parents=True, exist_ok=True)
            secret_file.write_bytes(secrets.token_bytes(32))
            secret_file.chmod(0o600)
        return secret_file.read_bytes()

    def issue(self, run_id: str, proposal_hash: str, approver: str) -> str:
        if approver != "human":
            raise PermissionError("Only a human approver can issue an approval token.")
        if not run_id or not proposal_hash:
            raise ValueError("run_id and proposal_hash are required to issue a token.")
        issued_at = int(self._clock())
        return f"{issued_at}.{self._mac(run_id, proposal_hash, issued_at)}"

    def check(self, token: object, run_id: str, proposal_hash: str) -> str:
        """Return one of the TOKEN_* outcomes. Constant-time on the MAC compare."""
        if not isinstance(token, str) or token.count(".") != 1:
            return TOKEN_MALFORMED
        ts_part, mac_part = token.split(".")
        if not ts_part.isdigit() or not _HEX64.fullmatch(mac_part):
            return TOKEN_MALFORMED
        issued_at = int(ts_part)
        if not hmac.compare_digest(mac_part, self._mac(run_id, proposal_hash, issued_at)):
            return TOKEN_MISMATCH
        now = self._clock()
        if issued_at > now + CLOCK_SKEW_SECONDS:
            return TOKEN_FROM_FUTURE
        if now - issued_at > self.ttl_seconds:
            return TOKEN_EXPIRED
        return TOKEN_OK

    def verify(self, token: object, run_id: str, proposal_hash: str) -> bool:
        return self.check(token, run_id, proposal_hash) == TOKEN_OK

    def _mac(self, run_id: str, proposal_hash: str, issued_at: int) -> str:
        msg = f"lenspilot-approval|{run_id}|{proposal_hash}|{issued_at}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
