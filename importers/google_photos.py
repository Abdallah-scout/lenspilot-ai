"""Google Photos Picker API client (read-only).

The user picks photos on Google's own page; this code can only see the photos
they picked (scope photospicker.mediaitems.readonly). It cannot list, change
or delete anything in their library.

Files (both under var/, which is git-ignored):
  var/google/client_secret.json  the user's own OAuth client (type "Desktop app")
  var/google/token.json          created by the sign-in
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
API = "https://photospicker.googleapis.com/v1"
MAX_ITEMS = 10  # photos per import
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DOWNLOAD_SIZE = "=w2048-h2048"  # a scaled copy is enough for a portfolio
TIMEOUT = 30


class GooglePhotosError(Exception):
    """Expected failure with a message that is safe to show to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def google_dir() -> Path:
    root = os.environ.get("LENSPILOT_DATA_DIR") or Path(__file__).resolve().parent.parent / "var"
    return Path(root) / "google"


def client_secret_path() -> Path:
    return google_dir() / "client_secret.json"


def token_path() -> Path:
    return google_dir() / "token.json"


# ---- sign-in -----------------------------------------------------------------
def _save_token(creds) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_credentials():
    """Valid saved credentials, refreshed if needed, or None (the user must sign in)."""
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not token_path().is_file():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(token_path()), [SCOPE])
        if not creds.valid:
            creds.refresh(Request())
            _save_token(creds)
    except (ValueError, GoogleAuthError):  # damaged, revoked or expired (7 days in "Testing" mode)
        return None
    return creds


def sign_in():
    """Open Google's consent page in the browser of this PC and wait for the answer."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not client_secret_path().is_file():
        raise GooglePhotosError("not_configured", f"Missing {client_secret_path()}.")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path()), [SCOPE])
        creds = flow.run_local_server(host="localhost", port=0, timeout_seconds=180)
    except Exception as exc:  # cancelled, timed out, wrong client type
        raise GooglePhotosError("sign_in_failed", "Google sign-in was not completed. Try again.") from exc
    _save_token(creds)
    return creds


def sign_out() -> None:
    """Delete the local token. Access can also be removed at myaccount.google.com/permissions."""
    token_path().unlink(missing_ok=True)


# ---- picker --------------------------------------------------------------------
def _is_google_media_url(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.port in (None, 443) and "@" not in parts.netloc
            and (parts.hostname or "").endswith(".googleusercontent.com"))


class PickerClient:
    def __init__(self, credentials, http=None):
        import requests
        self._creds = credentials
        self._http = http or requests.Session()
        self._network_error = requests.RequestException

    def _request(self, method: str, url: str, **kw):
        # Redirects are refused so the bearer token can never follow one to another host.
        try:
            return self._http.request(method, url, headers={"Authorization": f"Bearer {self._creds.token}"},
                                      timeout=TIMEOUT, allow_redirects=False, **kw)
        except self._network_error as exc:
            raise GooglePhotosError("network", "Could not reach Google Photos.") from exc

    def _api(self, method: str, path: str, **kw) -> dict:
        resp = self._request(method, f"{API}/{path}", **kw)
        if resp.status_code == 401:
            raise GooglePhotosError("signed_out", "The Google sign-in expired. Sign in again.")
        if resp.status_code != 200:
            try:
                status = resp.json()["error"]["status"]
            except (ValueError, KeyError, TypeError):
                status = ""
            if status == "FAILED_PRECONDITION":
                raise GooglePhotosError("not_ready", "Google Photos is not ready: finish choosing photos, and "
                                        "check that this account uses Google Photos.")
            raise GooglePhotosError("api_error", f"Google Photos refused the request ({resp.status_code}).")
        try:
            return resp.json()
        except ValueError as exc:
            raise GooglePhotosError("api_error", "Google Photos sent an unreadable answer.") from exc

    def create_session(self) -> dict:
        s = self._api("POST", "sessions", json={"pickingConfig": {"maxItemCount": str(MAX_ITEMS)}})
        uri = s.get("pickerUri", "")
        host = urlsplit(uri).hostname or ""
        if not s.get("id") or not uri.startswith("https://") or not host.endswith(".google.com"):
            raise GooglePhotosError("api_error", "Google Photos sent an unexpected picker address.")
        return {"id": s["id"], "picker_uri": uri + "/autoclose"}

    def is_done(self, session_id: str) -> bool:
        return bool(self._api("GET", f"sessions/{session_id}").get("mediaItemsSet"))

    def list_items(self, session_id: str) -> list[dict]:
        data = self._api("GET", "mediaItems", params={"sessionId": session_id, "pageSize": MAX_ITEMS})
        items = data.get("mediaItems") or []
        return [i for i in items if isinstance(i, dict)][:MAX_ITEMS]

    def delete_session(self, session_id: str) -> None:
        try:
            self._api("DELETE", f"sessions/{session_id}")
        except GooglePhotosError:
            pass  # it expires on its own

    def download(self, base_url: str) -> bytes:
        if not _is_google_media_url(base_url):
            raise GooglePhotosError("bad_url", "Refused a download address that is not Google's.")
        resp = self._request("GET", base_url + DOWNLOAD_SIZE, stream=True)
        try:
            if resp.status_code != 200:
                raise GooglePhotosError("download_failed", f"A photo could not be downloaded ({resp.status_code}).")
            data = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                data += chunk
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise GooglePhotosError("too_large", "A photo is larger than 25 MB.")
        except self._network_error as exc:
            raise GooglePhotosError("network", "The download was interrupted.") from exc
        finally:
            resp.close()
        return bytes(data)
