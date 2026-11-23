"""Turn downloaded photos into safe sandbox records (owner of the rules: Skander).

  * deny by default: a new photo is private with consent pending, so the
    existing policy (security/policy.py) refuses it until a human marks it;
  * the image is decoded and re-encoded, so EXIF (GPS position, camera serial)
    and anything appended to the file are dropped;
  * no text from Google (file name, camera name) enters the catalog, so it
    cannot carry instructions to the model;
  * importing the same photo twice does not add it twice.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from datetime import datetime

from PIL import Image, ImageOps

from importers.google_photos import GooglePhotosError
from sandbox import SandboxError

ID_PREFIX = "gp_"
FORMATS = ("JPEG", "PNG", "WEBP")
MAX_PIXELS = 50_000_000


def photo_id_for(media_id: str) -> str:
    return ID_PREFIX + hashlib.sha256(media_id.encode("utf-8")).hexdigest()[:16]


def clean_jpeg(data: bytes) -> bytes:
    """Re-encode to a plain RGB JPEG without any metadata."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=FORMATS) as img:
                if img.width * img.height > MAX_PIXELS:
                    raise ValueError("too many pixels")
                upright = ImageOps.exif_transpose(img).convert("RGB")
        plain = Image.new("RGB", upright.size)  # new object: no info dict, no EXIF, no ICC comment
        plain.paste(upright)
        out = io.BytesIO()
        plain.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception as exc:
        raise GooglePhotosError("bad_image", "A file is not a readable JPEG, PNG or WebP photo.") from exc


def _captured_at(value) -> str:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return "unknown"


def build_record(item: dict) -> dict:
    pid = photo_id_for(item["id"])
    return {
        "photo_id": pid, "file": f"{pid}.jpg", "title": f"Imported photo {pid[-6:]}",
        "category": "people", "shoot": "Google Photos import",
        "captured_at": _captured_at(item.get("createTime")), "camera": "n/a", "lens": "n/a",
        "focal_length_mm": None, "aperture": "n/a", "iso": None, "shutter": "n/a",
        "visibility": "private", "client_consent": "pending", "consent_simulated": False,
        "description": "",
    }


def import_session(client, sandbox, session_id: str) -> dict:
    """Import every picked photo. One bad photo never blocks the others."""
    added, existing, failed = [], [], []
    for item in client.list_items(session_id):
        media = item.get("mediaFile")
        if item.get("type") != "PHOTO" or not isinstance(item.get("id"), str) or not isinstance(media, dict):
            failed.append("Skipped an item that is not a photo.")
            continue
        record = build_record(item)
        try:
            sandbox.get_photo(record["photo_id"])
            existing.append(record["photo_id"])
            continue
        except SandboxError as exc:
            if exc.code != "photo_not_found":
                raise
        try:
            data = clean_jpeg(client.download(str(media.get("baseUrl", ""))))
        except GooglePhotosError as exc:
            failed.append(exc.message)
            continue
        (added if sandbox.add_photo(record, data) else existing).append(record["photo_id"])
    client.delete_session(session_id)
    return {"added": added, "existing": existing, "failed": failed}
