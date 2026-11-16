from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent.integration import build_services
from sandbox import Sandbox, SandboxError

KEY = "a" * 32
RECORD = {"photo_id": "rest_001", "caption": "Candlelit dining room.", "published_at": "2026-11-16T12:00:00Z"}


def test_ensure_and_reset(tmp_path):
    sandbox = Sandbox(tmp_path / "sandbox").ensure()
    sibling = tmp_path / "run-log.txt"
    sibling.write_text("run evidence", encoding="utf-8")
    unrelated = sandbox.root / "unrelated.txt"
    unrelated.touch()
    source = sandbox.image_path(RECORD["photo_id"])
    original = source.read_bytes()

    sandbox.write_publication(KEY, RECORD, source)
    sandbox.ensure()
    assert len(sandbox.published_records()) == 1
    sandbox.catalog_path.unlink()
    sandbox.ensure()
    assert len(sandbox.published_records()) == 1
    source.unlink()
    sandbox.reset()
    assert len(sandbox.catalog()) == 10
    assert source.read_bytes() == original
    assert sandbox.published_records() == []
    assert unrelated.is_file() and sibling.read_text(encoding="utf-8") == "run evidence"


def test_default_sandbox_matches_app_settings(tmp_path, monkeypatch):
    data = tmp_path / "data"
    override = tmp_path / "photos-demo"
    monkeypatch.setenv("LENSPILOT_DATA_DIR", str(data))
    monkeypatch.delenv("LENSPILOT_SANDBOX_DIR", raising=False)
    assert build_services().sandbox.root == Sandbox().root == data / "sandbox"
    monkeypatch.setenv("LENSPILOT_SANDBOX_DIR", str(override))
    assert build_services().sandbox.root == Sandbox().root == override
    assert build_services(tmp_path / "evaluation").sandbox.root == tmp_path / "evaluation" / "sandbox"


def test_malformed_catalog_reports_corrupted_data(services):
    sandbox = services.sandbox
    sandbox.catalog_path.write_text('{"photos": [null]}', encoding="utf-8")
    with pytest.raises(SandboxError) as error:
        sandbox.catalog()
    assert error.value.code == "corrupted_data"


def test_legacy_publication_remains_readable_and_idempotent(services):
    sandbox = services.sandbox
    source = sandbox.image_path(RECORD["photo_id"])
    legacy = {**RECORD, "image_file": f"{KEY}.jpg"}
    shutil.copy2(source, sandbox.published_dir / legacy["image_file"])
    (sandbox.published_dir / f"{KEY}.json").write_text(json.dumps(legacy), encoding="utf-8")

    assert sandbox.write_publication(KEY, {**RECORD, "caption": "Different caption."}, source) == (legacy, False)
    assert sandbox.published_records() == [legacy]
    assert (sandbox.published_dir / legacy["image_file"]).read_bytes() == source.read_bytes()


def test_failed_publication_cleans_staging_and_can_be_retried(services, monkeypatch):
    sandbox = services.sandbox
    source = sandbox.image_path(RECORD["photo_id"])

    def fail_copy(src, dst):
        dst.write_bytes(b"incomplete image")
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copy2", fail_copy)
        with pytest.raises(SandboxError) as error:
            sandbox.write_publication(KEY, RECORD, source)
    assert error.value.code == "write_failed"
    assert list(sandbox.published_dir.iterdir()) == []

    record, created = sandbox.write_publication(KEY, RECORD, source)
    assert created and sandbox.get_publication(KEY) == record
    assert (sandbox.published_dir / record["image_file"]).read_bytes() == source.read_bytes()


def test_concurrent_writes_keep_the_winning_record_and_image(services, monkeypatch):
    sandbox = services.sandbox
    sources = [sandbox.image_path("rest_001"), sandbox.image_path("rest_002")]
    barrier = Barrier(2)
    copy = shutil.copy2

    def sync_copy(src, dst):
        barrier.wait(timeout=5)
        return copy(src, dst)

    def publish(index):
        return sandbox.write_publication(KEY, {**RECORD, "caption": f"Caption {index}"}, sources[index])

    monkeypatch.setattr(shutil, "copy2", sync_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, range(2)))
    assert sum(created for _, created in results) == 1
    winner = next(index for index, (_, created) in enumerate(results) if created)
    record, _ = results[winner]
    assert results[1 - winner] == (record, False)
    assert sandbox.published_records() == [record]
    assert (sandbox.published_dir / record["image_file"]).read_bytes() == sources[winner].read_bytes()
