"""Human-approval tokens.

INTEGRATION BASELINE — owner: Skander (approval policy / write protection).

A token is an HMAC over (run_id, proposal_hash). Consequences:
  * it can only be minted by code holding the secret (the orchestrator's
    approve() path, triggered by a human click) — never by the model;
  * it is bound to ONE exact proposal: changing a caption, photo, or the
    collection changes the hash and invalidates the token;
  * it is bound to ONE run: tokens cannot be replayed into another run.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path


class ApprovalAuthority:
    def __init__(self, secret: bytes | None = None, secret_file: Path | None = None):
        self._secret = secret or self._load_secret(secret_file)

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
        return self._mac(run_id, proposal_hash)

    def verify(self, token: str, run_id: str, proposal_hash: str) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token, self._mac(run_id, proposal_hash))

    def _mac(self, run_id: str, proposal_hash: str) -> str:
        msg = f"lenspilot-approval|{run_id}|{proposal_hash}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
