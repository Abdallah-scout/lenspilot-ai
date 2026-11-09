"""Isolated, resettable filesystem sandbox for LensPilot.

INTEGRATION BASELINE — owner: Khalil (sandbox / publication backend).
Written by Abdallah to unblock agent integration; the public contract is
documented in docs/integration_contracts.md.

Layout (default root: ``var/sandbox``, override with LENSPILOT_SANDBOX_DIR)::

    catalog.json        copy of the fixture catalog (read-only for tools)
    style_guide.md      caption style guide
    photos/             source images
    published/          the ONLY place writes go: <key>.json + <key>.<ext>

Nothing outside the sandbox root is ever written. ``reset()`` restores the
pristine fixture state, so every demo scenario starts from the same state.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "sandbox" / "fixtures"
PHOTO_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
KEY_RE = re.compile(r"^[a-f0-9]{16,64}$")


class SandboxError(Exception):
    """Expected sandbox failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class Sandbox:
    def __init__(self, root: Path | str | None = None, fixtures: Path = FIXTURES_DIR):
        default = os.environ.get("LENSPILOT_SANDBOX_DIR", PROJECT_ROOT / "var" / "sandbox")
        self.root = Path(root or default).resolve()
        self.fixtures = fixtures
        self.photos_dir = self.root / "photos"
        self.published_dir = self.root / "published"
        self.catalog_path = self.root / "catalog.json"
        self.style_guide_path = self.root / "style_guide.md"

    # ---- lifecycle -------------------------------------------------------
    def ensure(self) -> "Sandbox":
        if not self.catalog_path.exists():
            self.reset()
        return self

    def reset(self) -> None:
        """Delete all sandbox state and restore the fixtures."""
        if self.root.exists():
            shutil.rmtree(self.root)
        self.photos_dir.mkdir(parents=True)
        self.published_dir.mkdir()
        shutil.copy2(self.fixtures / "catalog.json", self.catalog_path)
        shutil.copy2(self.fixtures / "style_guide.md", self.style_guide_path)
        for img in (self.fixtures / "images").iterdir():
            if img.is_file():
                shutil.copy2(img, self.photos_dir / img.name)

    # ---- reads ------------------------------------------------------------
    def catalog(self) -> list[dict]:
        try:
            return json.loads(self.catalog_path.read_text(encoding="utf-8"))["photos"]
        except FileNotFoundError as exc:
            raise SandboxError("sandbox_unavailable", "Sandbox is not initialised.") from exc
        except (json.JSONDecodeError, KeyError) as exc:
            raise SandboxError("corrupted_data", "Catalog file is not valid JSON.") from exc

    def get_photo(self, photo_id: str) -> dict:
        if not PHOTO_ID_RE.match(photo_id):
            raise SandboxError("invalid_arguments", f"Malformed photo id: {photo_id!r}")
        for record in self.catalog():
            if record["photo_id"] == photo_id:
                return record
        raise SandboxError("photo_not_found", f"No photo with id {photo_id!r} in the sandbox.")

    def image_path(self, photo_id: str) -> Path:
        record = self.get_photo(photo_id)
        path = (self.photos_dir / record["file"]).resolve()
        if not path.is_relative_to(self.photos_dir):  # path-traversal guard
            raise SandboxError("path_forbidden", "Image path escapes the sandbox.")
        if not path.exists():
            raise SandboxError("image_missing", f"Image file for {photo_id!r} is missing.")
        return path

    def style_guide(self) -> str:
        try:
            return self.style_guide_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SandboxError("sandbox_unavailable", "Style guide missing.") from exc

    def published_records(self) -> list[dict]:
        if not self.published_dir.exists():
            return []
        records = [json.loads(p.read_text(encoding="utf-8")) for p in self.published_dir.glob("*.json")]
        return sorted(records, key=lambda r: r["published_at"])

    def get_publication(self, key: str) -> dict | None:
        path = self._record_path(key)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    # ---- the single write -----------------------------------------------
    def write_publication(self, key: str, record: dict, image_src: Path) -> tuple[dict, bool]:
        """Idempotently publish one record. Returns (record, created).

        If ``key`` already exists the existing record is returned and nothing
        is written. Creation uses ``os.link`` which fails atomically if the
        target exists, so two concurrent calls cannot both create it.
        """
        target = self._record_path(key)
        if target.exists():
            return json.loads(target.read_text(encoding="utf-8")), False
        image_dst = self.published_dir / f"{key}{image_src.suffix}"
        shutil.copy2(image_src, image_dst)
        record = {**record, "image_file": image_dst.name}
        fd, tmp = tempfile.mkstemp(dir=self.published_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2)
            os.link(tmp, target)
        except FileExistsError:
            return json.loads(target.read_text(encoding="utf-8")), False
        except OSError as exc:
            raise SandboxError("write_failed", f"Could not write publication: {exc.strerror}") from exc
        finally:
            Path(tmp).unlink(missing_ok=True)
        return record, True

    def _record_path(self, key: str) -> Path:
        if not KEY_RE.match(key):
            raise SandboxError("invalid_arguments", "Malformed idempotency key.")
        return self.published_dir / f"{key}.json"
