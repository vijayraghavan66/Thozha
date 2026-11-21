from __future__ import annotations

import hashlib
import json
import os
import zipfile
from io import BytesIO

import pytest

from openjarvis.tasks.artifact_store import ArtifactStore, ArtifactStoreError

TASK = "a" * 32
DEVICE = "b" * 32
OTHER_DEVICE = "c" * 32


def make_export(files: dict[str, bytes]) -> dict:
    manifest = [
        {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(files.items())
    ]
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {
        "files": len(files),
        "bytes": sum(map(len, files.values())),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "file_manifest": manifest,
        "payloads": files,
    }


def test_bundle_is_deterministic_and_download_integrity_verified(tmp_path):
    with ArtifactStore(tmp_path / "artifacts", tmp_path / "store.db") as store:
        export = make_export({"README.md": b"hello\n", "tests/test_app.py": b"assert True\n"})
        first = store.create_bundle(task_id=TASK, device_id=DEVICE, export=export)
        second = store.create_bundle(task_id=TASK, device_id=DEVICE, export=export)
        assert first.artifact_id == second.artifact_id
        assert first.archive_sha256 == second.archive_sha256
        listed = store.list_for_task(TASK, DEVICE)
        assert len(listed) == 1
        item, data = store.download(TASK, DEVICE, first.artifact_id)
        assert item.archive_sha256 == hashlib.sha256(data).hexdigest()
        with zipfile.ZipFile(BytesIO(data)) as bundle:
            assert set(bundle.namelist()) == {"MANIFEST.json", "README.md", "tests/test_app.py"}
            manifest = json.loads(bundle.read("MANIFEST.json"))
            assert manifest["task_id"] == TASK
            assert manifest["manifest_sha256"] == export["manifest_sha256"]
            assert bundle.read("README.md") == b"hello\n"
        assert os.stat(tmp_path / "artifacts").st_mode & 0o777 == 0o700
        artifact_path = tmp_path / "artifacts" / TASK / first.file_name
        assert os.stat(artifact_path).st_mode & 0o777 == 0o600


def test_task_and_device_binding_prevents_cross_access(tmp_path):
    with ArtifactStore(tmp_path / "artifacts", tmp_path / "store.db") as store:
        artifact = store.create_bundle(task_id=TASK, device_id=DEVICE, export=make_export({"app.py": b"pass"}))
        assert store.list_for_task(TASK, OTHER_DEVICE) == []
        with pytest.raises(ArtifactStoreError):
            store.download(TASK, OTHER_DEVICE, artifact.artifact_id)
        with pytest.raises(ArtifactStoreError):
            store.download("d" * 32, DEVICE, artifact.artifact_id)


def test_expired_archive_is_removed_and_not_downloadable(tmp_path):
    now = [1_000_000.0]
    with ArtifactStore(tmp_path / "artifacts", tmp_path / "store.db", clock=lambda: now[0]) as store:
        item = store.create_bundle(task_id=TASK, device_id=DEVICE, export=make_export({"app.py": b"pass"}))
        now[0] += 8 * 24 * 60 * 60
        assert store.list_for_task(TASK, DEVICE) == []
        with pytest.raises(ArtifactStoreError):
            store.download(TASK, DEVICE, item.artifact_id)


def test_invalid_paths_and_manifest_tampering_are_rejected(tmp_path):
    with ArtifactStore(tmp_path / "artifacts", tmp_path / "store.db") as store:
        for bad in ("../escape", "/absolute", "a\\b", "", "dir//file"):
            with pytest.raises(ArtifactStoreError):
                store.create_bundle(task_id=TASK, device_id=DEVICE, export=make_export({bad: b"x"}))
        invalid = make_export({"app.py": b"safe"})
        invalid["file_manifest"][0]["sha256"] = "0" * 64
        with pytest.raises(ArtifactStoreError):
            store.create_bundle(task_id=TASK, device_id=DEVICE, export=invalid)


def test_corrupted_archive_is_not_served(tmp_path):
    with ArtifactStore(tmp_path / "artifacts", tmp_path / "store.db") as store:
        item = store.create_bundle(task_id=TASK, device_id=DEVICE, export=make_export({"app.py": b"pass"}))
        path = tmp_path / "artifacts" / TASK / item.file_name
        path.write_bytes(b"tampered")
        with pytest.raises(ArtifactStoreError):
            store.download(TASK, DEVICE, item.artifact_id)


def test_preexisting_task_directory_symlink_is_rejected_without_chmod(tmp_path):
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o755)
    with ArtifactStore(root, tmp_path / "store.db") as store:
        (root / TASK).symlink_to(outside, target_is_directory=True)
        with pytest.raises(ArtifactStoreError):
            store.create_bundle(task_id=TASK, device_id=DEVICE, export=make_export({"app.py": b"pass"}))
    assert os.stat(outside).st_mode & 0o777 == 0o755
