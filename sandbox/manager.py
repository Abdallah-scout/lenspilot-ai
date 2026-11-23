"""Isolated, resettable filesystem sandbox for LensPilot.

Owner: Khalil (sandbox / publication backend).

Layout (default root: ``var/sandbox``, override with LENSPILOT_SANDBOX_DIR)::

    catalog.json        copy of the fixture catalog (read-only for tools)
    style_guide.md      caption style guide
    photos/             source images
    published/          one directory per publication, keyed by idempotency key

Nothing outside the sandbox root is ever written. ``reset()`` restores the
pristine fixture state, so every demo scenario starts from the same state.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "sandbox" / "fixtures"
PHOTO_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
KEY_RE = re.compile(r"^[a-f0-9]{16,64}$")
IMPORTED_ID_RE = re.compile(r"^gp_[a-f0-9]{16}$")
_CATALOG_LOCK = threading.Lock()


class SandboxError(Exception):
    """Expected sandbox failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class Sandbox:
    def __init__(self, root: Path | str | None = None, fixtures: Path = FIXTURES_DIR,
                 imported_only: bool = False):
        if root is None:
            data_dir = Path(os.environ.get("LENSPILOT_DATA_DIR") or PROJECT_ROOT / "var")
            root = os.environ.get("LENSPILOT_SANDBOX_DIR") or data_dir / "sandbox"
        self.root = Path(root).resolve()
        self.fixtures = fixtures
        self.imported_only = imported_only  # True: the demo photos do not exist for any reader or tool
        self.photos_dir = self.root / "photos"
        self.published_dir = self.root / "published"
        self.catalog_path = self.root / "catalog.json"
        self.style_guide_path = self.root / "style_guide.md"

    # ---- lifecycle -------------------------------------------------------
    def ensure(self) -> "Sandbox":
        if not self.catalog_path.is_file():
            self._install_fixtures(clear_portfolio=False)
        return self

    def reset(self) -> None:
        """Restore fixtures and clear the portfolio between runs."""
        self._install_fixtures(clear_portfolio=True)

    def _install_fixtures(self, *, clear_portfolio: bool) -> None:
        if self.root == PROJECT_ROOT or PROJECT_ROOT.is_relative_to(self.root):
            raise SandboxError("path_forbidden", "Sandbox root overlaps the project checkout.")
        fixtures = self.fixtures.resolve()
        if self.root.is_relative_to(fixtures) or fixtures.is_relative_to(self.root):
            raise SandboxError("path_forbidden", "Sandbox root overlaps the fixture source.")
        for path in (self.photos_dir, self.published_dir, self.catalog_path, self.style_guide_path):
            if path.is_symlink() or path.resolve() != path:
                raise SandboxError("path_forbidden", "Sandbox data paths must not redirect elsewhere.")
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".fixtures-", dir=self.root) as tmp:
            staged = Path(tmp)
            staged_photos = staged / "photos"
            staged_photos.mkdir()
            for image in (fixtures / "images").iterdir():
                if image.is_file():
                    shutil.copy2(image, staged_photos / image.name)
            shutil.copy2(fixtures / "catalog.json", staged / "catalog.json")
            shutil.copy2(fixtures / "style_guide.md", staged / "style_guide.md")
            if clear_portfolio:
                self.catalog_path.unlink(missing_ok=True)
                for directory in (self.photos_dir, self.published_dir):
                    if directory.is_dir():
                        shutil.rmtree(directory)
            self.published_dir.mkdir(exist_ok=True)
            self.photos_dir.mkdir(exist_ok=True)
            for image in staged_photos.iterdir():
                os.replace(image, self.photos_dir / image.name)
            os.replace(staged / "style_guide.md", self.style_guide_path)
            os.replace(staged / "catalog.json", self.catalog_path)

    # ---- reads ------------------------------------------------------------
    def catalog(self) -> list[dict]:
        photos = self._full_catalog()
        if self.imported_only:
            photos = [p for p in photos if IMPORTED_ID_RE.fullmatch(p["photo_id"])]
        return photos

    def _full_catalog(self) -> list[dict]:
        try:
            photos = json.loads(self.catalog_path.read_text(encoding="utf-8"))["photos"]
        except FileNotFoundError as exc:
            raise SandboxError("sandbox_unavailable", "Sandbox is not initialised.") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise SandboxError("corrupted_data", "Catalog must contain a list of photos.") from exc
        if not isinstance(photos, list) or any(
            not isinstance(photo, dict)
            or not all(isinstance(photo.get(field), str) for field in ("photo_id", "file", "title", "category", "shoot"))
            or not PHOTO_ID_RE.fullmatch(photo["photo_id"])
            for photo in photos
        ):
            raise SandboxError("corrupted_data", "Catalog contains malformed photo records.")
        if len({photo["photo_id"] for photo in photos}) != len(photos):
            raise SandboxError("corrupted_data", "Catalog contains duplicate photo ids.")
        return photos

    def get_photo(self, photo_id: str) -> dict:
        if not PHOTO_ID_RE.fullmatch(photo_id):
            raise SandboxError("invalid_arguments", f"Malformed photo id: {photo_id!r}")
        for record in self.catalog():
            if record["photo_id"] == photo_id:
                return record
        raise SandboxError("photo_not_found", f"No photo with id {photo_id!r} in the sandbox.")

    def image_path(self, photo_id: str) -> Path:
        record = self.get_photo(photo_id)
        path = (self.photos_dir / record["file"]).resolve()
        if not path.is_relative_to(self.photos_dir) or self.photos_dir.resolve() != self.photos_dir:
            raise SandboxError("path_forbidden", "Image path escapes the sandbox.")
        if not path.is_file():
            raise SandboxError("image_missing", f"Image file for {photo_id!r} is missing.")
        return path

    def style_guide(self) -> str:
        try:
            return self.style_guide_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SandboxError("sandbox_unavailable", "Style guide missing.") from exc

    def published_records(self) -> list[dict]:
        directory = self._publication_dir()
        keys = {
            path.stem
            for path in directory.glob("*")
            if path.suffix in ("", ".json") and KEY_RE.fullmatch(path.stem)
        }
        records = (self.get_publication(key) for key in keys)
        if self.imported_only:
            records = (r for r in records if r is not None and IMPORTED_ID_RE.fullmatch(r["photo_id"]))
        return sorted((record for record in records if record is not None), key=lambda r: r["published_at"])

    def get_publication(self, key: str) -> dict | None:
        directory = self._publication_dir(key)
        for path in (directory / "record.json", directory.with_suffix(".json")):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
        return None

    # ---- imports: human actions from the UI, not reachable by the model ---
    def add_photo(self, record: dict, image: bytes) -> bool:
        """Add an imported photo. Always private with consent pending. False if already there."""
        pid = record["photo_id"]
        if not IMPORTED_ID_RE.fullmatch(pid):
            raise SandboxError("invalid_arguments", f"Not an imported photo id: {pid!r}")
        record = {**record, "file": f"{pid}.jpg", "visibility": "private", "client_consent": "pending"}
        with _CATALOG_LOCK:
            photos = self._full_catalog()
            if any(p["photo_id"] == pid for p in photos):
                return False
            if self.photos_dir.is_symlink() or self.photos_dir.resolve() != self.photos_dir:
                raise SandboxError("path_forbidden", "Photo directory escapes the sandbox.")
            self._atomic_write(self.photos_dir / record["file"], image)
            self._write_catalog([*photos, record])
        return True

    def update_imported_photo(self, photo_id: str, *, title: str, category: str,
                              visibility: str, client_consent: str, description: str | None = None) -> None:
        """Record the human's decision about an imported photo. Fixture photos cannot be changed."""
        allowed = (visibility in ("public", "private") and client_consent in ("granted", "pending", "denied")
                   and category in ("restaurant", "interior", "people", "event"))
        if not IMPORTED_ID_RE.fullmatch(photo_id) or not allowed:
            raise SandboxError("invalid_arguments", "Only imported photos can be edited, with known values.")
        with _CATALOG_LOCK:
            self.get_photo(photo_id)
            self._write_catalog([
                {**p, "title": title, "category": category, "visibility": visibility,
                 "client_consent": client_consent,
                 "description": p.get("description", "") if description is None else description} if p["photo_id"] == photo_id else p
                for p in self._full_catalog()])

    def _write_catalog(self, photos: list[dict]) -> None:
        self._atomic_write(self.catalog_path, json.dumps({"photos": photos}, indent=2).encode("utf-8"))

    def _atomic_write(self, target: Path, data: bytes) -> None:
        if target.is_symlink():
            raise SandboxError("path_forbidden", "Refusing to write through a link.")
        try:
            fd, tmp = tempfile.mkstemp(prefix=".write-", dir=self.root)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except OSError as exc:
            raise SandboxError("write_failed", f"Could not write the photo: {exc.strerror}") from exc

    # ---- the single publishing write ------------------------------------
    def write_publication(self, key: str, record: dict, image_src: Path) -> tuple[dict, bool]:
        """Commit one image and its record together, returning (record, created)."""
        target = self._publication_dir(key)
        existing = self.get_publication(key)
        if existing is not None:
            return existing, False
        image_src = image_src.resolve()
        if not image_src.is_relative_to(self.photos_dir):
            raise SandboxError("path_forbidden", "Publication source escapes the photo directory.")
        image_name = f"image{image_src.suffix}"
        record = {**record, "image_file": f"{key}/{image_name}"}
        try:
            with tempfile.TemporaryDirectory(prefix=".publish-", dir=self.published_dir) as tmp:
                staged = Path(tmp) / "publication"
                staged.mkdir()
                shutil.copy2(image_src, staged / image_name)
                (staged / "record.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
                try:
                    # A committed target is nonempty, so rename cannot replace it.
                    staged.rename(target)
                except OSError:
                    existing = self.get_publication(key)
                    if existing is not None:
                        return existing, False
                    raise
        except OSError as exc:
            raise SandboxError("write_failed", f"Could not write publication: {exc.strerror}") from exc
        return record, True

    def _publication_dir(self, key: str | None = None) -> Path:
        if key is not None and not KEY_RE.fullmatch(key):
            raise SandboxError("invalid_arguments", "Malformed idempotency key.")
        directory = self.published_dir if key is None else self.published_dir / key
        if directory.is_symlink() or directory.resolve() != directory:
            raise SandboxError("path_forbidden", "Publication directory escapes the sandbox.")
        return directory
