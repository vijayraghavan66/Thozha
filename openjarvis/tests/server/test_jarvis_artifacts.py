from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.server import supervisor_routes as routes
from openjarvis.tasks.artifact_store import ArtifactStore
from openjarvis.tasks.supervisor import LINK_SCOPE_CAPABILITIES

TASK = "a" * 32
DEVICE = "b" * 32


class FakeSupervisor:
    def __init__(self, *, owner=f"device:{DEVICE}", state="COMPLETED"):
        self.task = {
            "state": state,
            "recovery_json": json.dumps({"source": "jarvis_link", "scope": "development", "capability_scope": ["development"], "owner_actor": owner}),
            "capabilities_json": json.dumps(sorted(LINK_SCOPE_CAPABILITIES["development"])),
        }

    def get_task(self, task_id):
        return self.task if task_id == TASK else None


def export(files):
    manifest = [{"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for path, data in sorted(files.items())]
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {"files": len(files), "bytes": sum(map(len, files.values())), "manifest_sha256": hashlib.sha256(encoded).hexdigest(), "file_manifest": manifest, "payloads": files}


def test_artifact_api_requires_task_owner_and_delivers_verified_zip(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "archives", tmp_path / "archives.db")
    files = {"README.md": b"hello", "src/app.py": b"print('ok')"}
    artifact = store.create_bundle(task_id=TASK, device_id=DEVICE, export=export(files))
    monkeypatch.setattr(routes, "_device_actor", lambda request: DEVICE)
    monkeypatch.setattr(routes, "get_supervisor_store", lambda: FakeSupervisor())
    monkeypatch.setattr(routes, "_artifact_store", lambda: store)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    listed = client.get(f"/v1/jarvis/tasks/{TASK}/artifacts")
    assert listed.status_code == 200
    assert listed.json()["artifacts"][0]["archive_sha256"] == artifact.archive_sha256
    response = client.get(f"/v1/jarvis/tasks/{TASK}/artifacts/{artifact.artifact_id}/download")
    assert response.status_code == 200
    assert response.headers["x-content-sha256"] == hashlib.sha256(response.content).hexdigest()
    with zipfile.ZipFile(BytesIO(response.content)) as bundle:
        assert bundle.read("src/app.py") == files["src/app.py"]
    store.close()


def test_artifact_api_hides_other_device_tasks_and_incomplete_tasks(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "archives", tmp_path / "archives.db")
    monkeypatch.setattr(routes, "_device_actor", lambda request: DEVICE)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    monkeypatch.setattr(routes, "get_supervisor_store", lambda: FakeSupervisor(owner="device:" + "c" * 32))
    assert client.get(f"/v1/jarvis/tasks/{TASK}/artifacts").status_code == 404
    monkeypatch.setattr(routes, "get_supervisor_store", lambda: FakeSupervisor(state="RUNNING"))
    assert client.get(f"/v1/jarvis/tasks/{TASK}/artifacts").status_code == 409
    store.close()
