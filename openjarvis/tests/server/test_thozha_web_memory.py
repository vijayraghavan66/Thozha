from __future__ import annotations

import concurrent.futures
import stat
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from openjarvis.server.thozha_web_auth import WebPrincipal
from openjarvis.server.thozha_web_memory import (
    MemoryIdempotencyConflict,
    MemoryQuotaExceeded,
    WebMemoryStore,
    create_web_memory_router,
)


def _key() -> str:
    return uuid.uuid4().hex


def _create(store: WebMemoryStore, owner: str, content: str, key: str | None = None):
    return store.create(
        owner,
        content=content,
        title=None,
        tags=[],
        idempotency_key=key or _key(),
    )


def test_owner_scoping_and_search_are_isolated(tmp_path):
    store = WebMemoryStore(tmp_path / "private" / "memory.db")
    item_a, _ = _create(store, "owner-a", "Chennai weather is humid", "a" * 32)
    item_b, _ = _create(store, "owner-b", "Chennai weather is sunny", "b" * 32)

    assert store.get("owner-a", item_a["id"])["content"] == "Chennai weather is humid"
    assert store.get("owner-b", item_a["id"]) is None
    assert store.search("owner-a", "humid") == [item_a]
    assert store.search("owner-a", "sunny") == []
    assert store.list("owner-b") == [item_b]
    assert store.delete("owner-a", item_b["id"]) is False
    store.close()


def test_chat_context_requires_explicit_opt_in_relevance_and_owner_scope(tmp_path):
    store = WebMemoryStore(tmp_path / "private" / "memory.db")
    chosen, _ = store.create(
        "owner-a", content="I prefer concise Kotlin examples.", title="Code style",
        tags=["chat_context"], idempotency_key="a" * 32,
    )
    _create(store, "owner-a", "I live in Chennai", "b" * 32)
    _create(store, "owner-a", "Kotlin is my bank password", "c" * 32)
    sensitive, _ = store.create(
        "owner-a", content="Kotlin medical note", title="Private",
        tags=["chat_context", "sensitive"], idempotency_key="d" * 32,
    )
    store.create(
        "owner-b", content="Kotlin examples are preferred", title="Other owner",
        tags=["chat_context"], idempotency_key="e" * 32,
    )

    result = store.retrieve_chat_context("owner-a", "Explain Kotlin interfaces")
    assert [item["id"] for item in result] == [chosen["id"]]
    assert sensitive["id"] not in {item["id"] for item in result}
    assert store.retrieve_chat_context("owner-a", "weather in Chennai") == []
    assert store.retrieve_chat_context("owner-b", "Kotlin examples")
    assert len(result[0]["content"]) < 4_000
    store.create(
        "owner-a", content=("Kotlin context " * 500), title="Long context",
        tags=["chat_context"], idempotency_key="f" * 32,
    )
    bounded = store.retrieve_chat_context("owner-a", "Kotlin")
    assert bounded and len(bounded[0]["content"]) <= 4_000
    assert sum(len(item["content"]) for item in bounded) <= 4_000
    store.close()


def test_persistence_idempotency_and_conflict(tmp_path):
    path = tmp_path / "private" / "memory.db"
    store = WebMemoryStore(path)
    item, replayed = _create(store, "owner", "Remember the meeting at 3", "c" * 32)
    same, duplicate = _create(store, "owner", "Remember the meeting at 3", "c" * 32)
    assert item["id"] == same["id"]
    assert replayed is False and duplicate is True
    with pytest.raises(MemoryIdempotencyConflict):
        _create(store, "owner", "Different content", "c" * 32)
    store.close()

    reopened = WebMemoryStore(path)
    assert reopened.get("owner", item["id"])["content"] == "Remember the meeting at 3"
    assert reopened.list("other") == []
    reopened.close()


def test_storage_permissions_are_restricted(tmp_path):
    path = tmp_path / "private" / "memory.db"
    store = WebMemoryStore(path)
    assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.close()


def test_quota_and_input_limits(tmp_path, monkeypatch):
    import openjarvis.server.thozha_web_memory as module

    monkeypatch.setattr(module, "_MAX_OWNER_ITEMS", 1)
    store = WebMemoryStore(tmp_path / "private" / "memory.db")
    _create(store, "owner", "one", "d" * 32)
    with pytest.raises(MemoryQuotaExceeded):
        _create(store, "owner", "two", "e" * 32)
    with pytest.raises(ValueError):
        store.search("owner", "")
    with pytest.raises(ValueError):
        store.create(
            "owner",
            content="x" * (module._MAX_CONTENT_BYTES + 1),
            title=None,
            tags=[],
            idempotency_key="f" * 32,
        )
    store.close()


def test_concurrent_writes_remain_unique_and_owner_scoped(tmp_path):
    store = WebMemoryStore(tmp_path / "private" / "memory.db")

    def add(index: int):
        return _create(store, "owner", f"memory {index}", f"{index:032x}")[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(add, range(32)))
    assert len({item["id"] for item in results}) == 32
    assert len(store.list("owner", limit=100)) == 32
    assert store.list("different-owner") == []
    store.close()


def test_routes_require_session_and_preserve_owner_scope(tmp_path):
    now = int(datetime.now(timezone.utc).timestamp()) + 3600
    current_owner = {"id": "owner-a"}

    def require_session():
        return WebPrincipal(current_owner["id"], "session-test", now)

    store = WebMemoryStore(tmp_path / "private" / "memory.db")
    app = FastAPI()
    app.include_router(create_web_memory_router(require_session, store=store))
    with TestClient(app) as client:
        created = client.post(
            "/v1/web/memory",
            headers={"Idempotency-Key": "1" * 32},
            json={"content": "My passport expires in June", "tags": ["travel"]},
        )
        assert created.status_code == 201
        memory = created.json()["memory"]
        assert created.json()["replayed"] is False
        assert (
            client.get("/v1/web/memory/search?q=passport").json()["search_mode"]
            == "substring"
        )
        assert len(client.get("/v1/web/memory/search?q=passport").json()["items"]) == 1
        assert client.get("/v1/web/memory", params={"limit": 999}).status_code == 422
        assert (
            client.post(
                "/v1/web/memory",
                headers={"Idempotency-Key": "2" * 32},
                json={"content": "   "},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/web/memory",
                headers={"Idempotency-Key": "3" * 32},
                json={"content": "test", "owner_id": "owner-b"},
            ).status_code
            == 422
        )

        current_owner["id"] = "owner-b"
        assert client.get(f"/v1/web/memory/{memory['id']}").status_code == 404
        assert client.delete(f"/v1/web/memory/{memory['id']}").status_code == 404

        current_owner["id"] = "owner-a"
        assert client.delete(f"/v1/web/memory/{memory['id']}").status_code == 204
        assert client.get(f"/v1/web/memory/{memory['id']}").status_code == 404
    store.close()


def test_unauthenticated_request_fails_closed(tmp_path):
    def denied():
        raise HTTPException(status_code=401, detail="Web session required")

    store = WebMemoryStore(tmp_path / "private" / "memory.db")
    app = FastAPI()
    app.include_router(create_web_memory_router(denied, store=store))
    with TestClient(app) as client:
        assert client.get("/v1/web/memory").status_code == 401
        assert (
            client.post(
                "/v1/web/memory",
                headers={"Idempotency-Key": "4" * 32},
                json={"content": "test"},
            ).status_code
            == 401
        )
    store.close()
