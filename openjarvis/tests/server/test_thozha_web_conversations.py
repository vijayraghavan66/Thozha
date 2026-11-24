"""Focused tests for the isolated Thozha browser conversation API."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from openjarvis.server.thozha_web_conversations import (
    ConversationStore,
    MessageCreate,
    _principal_owner,
    create_web_conversations_router,
)
from openjarvis.server.thozha_web_memory import WebMemoryStore

try:
    from openjarvis.server.thozha_web_auth import WebPrincipal
except ImportError:

    @dataclass(frozen=True)
    class WebPrincipal:
        """Fallback for the auth branch's public dataclass contract."""

        user_id: str
        session_id: str
        expires_at: int


class FakeEngine:
    engine_id = "fake-test-engine"

    def __init__(
        self,
        *,
        response="A real test response.",
        gate=None,
        error=None,
        stream_chunks=None,
    ):
        self.response = response
        self.gate = gate
        self.error = error
        self.stream_chunks = stream_chunks
        self.calls = []
        self.stream_calls = 0
        self.stream_inputs = []

    def generate(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.gate is not None:
            self.gate.wait(timeout=30)
        if self.error:
            raise self.error
        return {"content": self.response, "usage": {}}

    async def stream(self, *args, **kwargs):
        self.stream_calls += 1
        self.stream_inputs.append((args, kwargs))
        for chunk in self.stream_chunks or ["should not be used"]:
            yield chunk


def make_client(
    tmp_path, *, owner="alice", engine=None, expired=False, store=None, memory_store=None
):
    app = FastAPI()
    engine = engine or FakeEngine()
    store = store or ConversationStore(tmp_path / "conversations.db")
    app.state.engine = engine
    app.state.model = "fixture-model"

    async def require_web_session(request: Request):
        expires = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
            if expired
            else datetime.now(timezone.utc) + timedelta(hours=1)
        )
        return {
            "user_id": request.headers.get("x-test-user", owner),
            "session_id": "test-session-identity",
            "expires_at": expires,
        }

    app.include_router(
        create_web_conversations_router(
            require_web_session, store=store, memory_store=memory_store
        )
    )
    return TestClient(app), engine, store


def create_conversation(client, title="Demo"):
    response = client.post("/v1/web/conversations", json={"title": title})
    assert response.status_code == 201
    return response.json()["id"]


def submit(client, conversation_id, key="request-key-00000001", content="Explain Java"):
    return client.post(
        f"/v1/web/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": key},
        json={"content": content},
    )


def wait_for_status(client, conversation_id, response_id, status, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        response = client.get(f"/v1/web/conversations/{conversation_id}")
        assert response.status_code == 200
        found = next(
            item for item in response.json()["messages"] if item["id"] == response_id
        )
        if found["status"] == status:
            return found
        time.sleep(0.01)
    pytest.fail(f"response did not reach {status}")


def test_persists_conversation_messages_events_and_real_engine_result(tmp_path):
    client, engine, store = make_client(tmp_path)
    conversation_id = create_conversation(client)
    response = submit(client, conversation_id)
    assert response.status_code == 202
    ack = response.json()
    assert ack["status"] in {"queued", "running", "completed"}
    result = wait_for_status(client, conversation_id, ack["response_id"], "completed")
    assert result["content"] == "A real test response."
    assert engine.stream_calls == 0
    assert len(engine.calls) == 1
    _, kwargs = engine.calls[0]
    assert kwargs["tools"] == []
    assert engine.calls[0][0][-1].content == "Explain Java"
    persisted = client.get(f"/v1/web/conversations/{conversation_id}").json()
    assert [message["role"] for message in persisted["messages"]] == [
        "user",
        "assistant",
    ]
    assert [event["event_type"] for event in persisted["events"]] == [
        "accepted",
        "queued",
        "started",
        "completed",
    ]
    assert "owner_id" not in persisted["messages"][0]
    assert "idempotency_key" not in persisted["messages"][0]
    assert "request_hash" not in persisted["messages"][0]
    store.close()


def test_conversation_rename_is_validated_owner_scoped_and_persistent(tmp_path):
    client, _, store = make_client(tmp_path)
    conversation_id = create_conversation(client, "Original")
    renamed = client.patch(
        f"/v1/web/conversations/{conversation_id}",
        json={"title": "  Chennai · notes  "},
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Chennai · notes"
    assert client.get(f"/v1/web/conversations/{conversation_id}").json()["title"] == "Chennai · notes"
    assert client.patch(
        f"/v1/web/conversations/{conversation_id}",
        headers={"x-test-user": "bob"},
        json={"title": "Other owner"},
    ).status_code == 404
    assert client.patch(
        f"/v1/web/conversations/{conversation_id}", json={"title": "   "}
    ).status_code == 422
    assert client.get(f"/v1/web/conversations/{conversation_id}").json()["title"] == "Chennai · notes"
    store.close()


def test_streaming_chat_uses_only_relevant_opted_in_owner_memory(tmp_path):
    memory_store = WebMemoryStore(tmp_path / "private" / "memory.db")
    chosen, _ = memory_store.create(
        "alice", content="I prefer concise Kotlin examples.", title="Code style",
        tags=["chat_context"], idempotency_key="memory-chat-context-1",
    )
    memory_store.create(
        "alice", content="I live in Chennai", title="City", tags=["travel"],
        idempotency_key="memory-chat-context-2",
    )
    memory_store.create(
        "alice", content="Kotlin private medical record", title="Private",
        tags=["chat_context", "sensitive"], idempotency_key="memory-chat-context-3",
    )
    memory_store.create(
        "bob", content="Kotlin examples are preferred", title="Other owner",
        tags=["chat_context"], idempotency_key="memory-chat-context-4",
    )
    engine = FakeEngine(stream_chunks=["Use a concise example."])
    client, _, store = make_client(tmp_path, engine=engine, memory_store=memory_store)
    conversation_id = create_conversation(client)
    response = client.post(
        f"/v1/web/conversations/{conversation_id}/messages/stream",
        headers={"Idempotency-Key": "memory-stream-request-0001"},
        json={"content": "Explain Kotlin interfaces"},
    )
    assert response.status_code == 200
    messages = engine.stream_inputs[0][0][0]
    memory_messages = [item.content for item in messages if "Owner memory JSON:" in item.content]
    assert len(memory_messages) == 1
    assert "concise Kotlin examples" in memory_messages[0]
    assert "medical record" not in memory_messages[0]
    assert "Other owner" not in memory_messages[0]
    conversation = client.get(f"/v1/web/conversations/{conversation_id}").json()
    used = [e for e in conversation["events"] if e["event_type"] == "completed"]
    assert len(used) == 1
    assert used[0]["details"] == {
        "memory_context_count": 1,
        "memory_ids": [chosen["id"]],
    }
    store.close()
    memory_store.close()


def test_stream_endpoint_delivers_native_chunks_and_persists_final_answer(tmp_path):
    engine = FakeEngine(stream_chunks=["Hello", " there."])
    client, _, store = make_client(tmp_path, engine=engine)
    conversation_id = create_conversation(client)
    response = client.post(
        f"/v1/web/conversations/{conversation_id}/messages/stream",
        headers={"Idempotency-Key": "stream-request-key-0001"},
        json={"content": "Say hello"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"mode":"engine_stream"' in response.text
    assert '"content":"Hello"' in response.text
    assert '"content":" there."' in response.text
    assert '"status":"completed"' in response.text
    conversation = client.get(f"/v1/web/conversations/{conversation_id}").json()
    answer = next(
        item for item in conversation["messages"] if item["role"] == "assistant"
    )
    assert answer["content"] == "Hello there."
    assert engine.stream_calls == 1
    assert engine.stream_inputs[0][0][0][-1].content == "Say hello"
    assert engine.stream_inputs[0][1]["tools"] == []

    replay = client.post(
        f"/v1/web/conversations/{conversation_id}/messages/stream",
        headers={"Idempotency-Key": "stream-request-key-0001"},
        json={"content": "Say hello"},
    )
    assert replay.status_code == 200
    assert '"replayed":true' in replay.text
    assert "event: message" in replay.text
    assert engine.stream_calls == 1
    store.close()


def test_stream_endpoint_marks_codex_output_buffered(tmp_path):
    engine = FakeEngine(stream_chunks=["Completed response."])
    engine.engine_id = "codex_sdk"
    client, _, store = make_client(tmp_path, engine=engine)
    conversation_id = create_conversation(client)
    response = client.post(
        f"/v1/web/conversations/{conversation_id}/messages/stream",
        headers={"Idempotency-Key": "stream-request-key-0002"},
        json={"content": "Explain"},
    )
    assert response.status_code == 200
    assert '"mode":"buffered"' in response.text
    assert engine.stream_calls == 1
    store.close()


@pytest.mark.asyncio
async def test_cancel_closes_engine_while_waiting_for_text():
    import asyncio
    from contextlib import aclosing

    from openjarvis.server.thozha_web_conversations import _cancellable_chunks

    waiting = asyncio.Event()
    closed = asyncio.Event()
    cancelled = asyncio.Event()

    async def provider():
        try:
            yield "first"
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    async with aclosing(_cancellable_chunks(provider(), cancelled.is_set)) as stream:
        assert await anext(stream) == "first"
        next_chunk = asyncio.create_task(anext(stream))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        cancelled.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(next_chunk, timeout=1)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_disconnected_sse_client_cancels_queued_response(tmp_path):
    client, engine, store = make_client(tmp_path)
    conversation_id = create_conversation(client)
    route = next(
        route
        for route in client.app.routes
        if getattr(route, "path", "").endswith("/messages/stream")
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/v1/web/conversations/{conversation_id}/messages/stream",
        "raw_path": b"/v1/web/conversations/stream",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 10000),
        "server": ("test", 80),
        "app": client.app,
    }
    request = StarletteRequest(scope)
    response = await route.endpoint(
        conversation_id,
        MessageCreate(content="disconnect now"),
        request,
        "stream-disconnect-key-0001",
        {
            "user_id": "alice",
            "session_id": "session",
            "expires_at": int(time.time()) + 600,
        },
    )
    iterator = response.body_iterator
    first = await anext(iterator)
    assert "event: accepted" in first
    await iterator.aclose()

    conversation = store.get("alice", conversation_id)
    assistant = next(
        item for item in conversation["messages"] if item["role"] == "assistant"
    )
    assert assistant["status"] == "cancelled"
    assert engine.stream_calls == 0
    store.close()


def test_messages_and_events_survive_store_reopen(tmp_path):
    db_path = tmp_path / "persistent.db"
    first = ConversationStore(db_path)
    conversation = first.create("alice", "Persisted")
    saved, _ = first.submit(
        "alice", conversation["id"], "persist-key-00000001", "hello"
    )
    first.claim("alice", saved["assistant"]["id"])
    first.finish(
        "alice",
        saved["assistant"]["id"],
        content="persisted answer",
        status="completed",
    )
    conversation_id, assistant_id = conversation["id"], saved["assistant"]["id"]
    first.close()

    second = ConversationStore(db_path)
    restored = second.get("alice", conversation_id)
    answer = next(item for item in restored["messages"] if item["id"] == assistant_id)
    assert answer["content"] == "persisted answer"
    assert answer["status"] == "completed"
    assert restored["events"][-1]["event_type"] == "completed"
    second.close()


def test_startup_fails_uncertain_running_and_resumes_queued_once(tmp_path):
    db_path = tmp_path / "restart.db"
    first = ConversationStore(db_path)
    conversation = first.create("alice", "Restart test")
    running, _ = first.submit(
        "alice",
        conversation["id"],
        "running-request-key-01",
        "maybe completed upstream",
    )
    queued, _ = first.submit(
        "alice", conversation["id"], "queued-request-key-001", "resume this request"
    )
    first.claim("alice", running["assistant"]["id"])
    first.close()

    recovered_store = ConversationStore(db_path)
    client, engine, _ = make_client(
        tmp_path,
        engine=FakeEngine(response="resumed result"),
        store=recovered_store,
    )
    state = client.get(f"/v1/web/conversations/{conversation['id']}")
    assert state.status_code == 200
    running_state = next(
        item
        for item in state.json()["messages"]
        if item["id"] == running["assistant"]["id"]
    )
    assert running_state["status"] == "failed"
    assert running_state["error_code"] == "process_interrupted"
    resumed = wait_for_status(
        client, conversation["id"], queued["assistant"]["id"], "completed"
    )
    assert resumed["content"] == "resumed result"
    assert len(engine.calls) == 1


def test_idempotency_returns_same_response_and_does_not_repeat_inference(tmp_path):
    client, engine, _ = make_client(tmp_path)
    conversation_id = create_conversation(client)
    first = submit(client, conversation_id)
    assert first.status_code == 202
    response_id = first.json()["response_id"]
    wait_for_status(client, conversation_id, response_id, "completed")
    replay = submit(client, conversation_id)
    assert replay.status_code == 202
    assert replay.json()["replayed"] is True
    assert replay.json()["response_id"] == response_id
    assert len(engine.calls) == 1


def test_idempotency_key_cannot_be_reused_for_modified_content(tmp_path):
    client, _, _ = make_client(tmp_path)
    conversation_id = create_conversation(client)
    assert submit(client, conversation_id).status_code == 202
    conflict = submit(client, conversation_id, content="Different request")
    assert conflict.status_code == 409


def test_owner_scoping_hides_other_users_conversations(tmp_path):
    client, _, _ = make_client(tmp_path)
    conversation_id = create_conversation(client)
    other = client.get(
        f"/v1/web/conversations/{conversation_id}", headers={"x-test-user": "mallory"}
    )
    assert other.status_code == 404
    assert (
        client.get("/v1/web/conversations", headers={"x-test-user": "mallory"}).json()[
            "items"
        ]
        == []
    )


def test_expired_principal_is_rejected(tmp_path):
    client, _, _ = make_client(tmp_path, expired=True)
    assert client.get("/v1/web/conversations").status_code == 401


def test_webprincipal_unix_expiry_contract_is_accepted():
    principal = WebPrincipal("alice", "opaque-session-id", int(time.time()) + 600)
    assert _principal_owner(principal) == "alice"


def test_message_requires_idempotency_key(tmp_path):
    client, _, _ = make_client(tmp_path)
    conversation_id = create_conversation(client)
    response = client.post(
        f"/v1/web/conversations/{conversation_id}/messages", json={"content": "hello"}
    )
    assert response.status_code == 400


def test_archived_conversation_is_readable_but_rejects_new_messages(tmp_path):
    client, _, _ = make_client(tmp_path)
    conversation_id = create_conversation(client)
    assert (
        client.post(f"/v1/web/conversations/{conversation_id}/archive").status_code
        == 200
    )
    assert client.get(f"/v1/web/conversations/{conversation_id}").status_code == 200
    assert submit(client, conversation_id).status_code == 409


@pytest.mark.asyncio
async def test_cancel_running_generation_suppresses_late_result(tmp_path):
    gate = threading.Event()
    client, engine, _ = make_client(
        tmp_path, engine=FakeEngine(response="late result", gate=gate)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://test"
    ) as async_client:
        created = await async_client.post(
            "/v1/web/conversations", json={"title": "Demo"}
        )
        conversation_id = created.json()["id"]
        response = await async_client.post(
            f"/v1/web/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "request-key-00000001"},
            json={"content": "wait and cancel"},
        )
        response_id = response.json()["response_id"]
        until = time.monotonic() + 2
        while not engine.calls and time.monotonic() < until:
            await asyncio.sleep(0.01)
        assert engine.calls
        cancelled = await async_client.post(
            f"/v1/web/conversations/{conversation_id}/messages/{response_id}/cancel"
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancellation_requested"
        gate.set()
        until = time.monotonic() + 3
        while time.monotonic() < until:
            state = await async_client.get(f"/v1/web/conversations/{conversation_id}")
            result = next(
                item for item in state.json()["messages"] if item["id"] == response_id
            )
            if result["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)
        assert result["status"] == "cancelled"
        assert result["content"] == ""


def test_queued_cancel_prevents_engine_dispatch(tmp_path):
    client, engine, store = make_client(tmp_path)
    conversation_id = create_conversation(client)
    result, _ = store.submit(
        "alice", conversation_id, "queued-cancel-key-001", "cancel me"
    )
    response_id = result["assistant"]["id"]
    assert store.cancel("alice", conversation_id, response_id) == "cancelled"
    # Replaying can return the cancelled response, but cannot dispatch again.
    replay, repeated = store.submit(
        "alice", conversation_id, "queued-cancel-key-001", "cancel me"
    )
    assert repeated is True
    assert replay["assistant"]["status"] == "cancelled"
    assert not engine.calls


def test_engine_failure_is_persisted_as_generic_failure_without_provider_leak(
    tmp_path, caplog
):
    client, _, _ = make_client(
        tmp_path, engine=FakeEngine(error=RuntimeError("private provider detail"))
    )
    conversation_id = create_conversation(client)
    response = submit(client, conversation_id)
    failure = wait_for_status(
        client, conversation_id, response.json()["response_id"], "failed"
    )
    assert failure["error_code"] == "generation_failed"
    assert "private provider detail" not in caplog.text


def test_list_archive_and_owner_delete(tmp_path):
    client, _, _ = make_client(tmp_path)
    conversation_id = create_conversation(client)
    assert len(client.get("/v1/web/conversations").json()["items"]) == 1
    assert client.delete(f"/v1/web/conversations/{conversation_id}").status_code == 204
    assert client.get(f"/v1/web/conversations/{conversation_id}").status_code == 404


def test_requires_auth_dependency(tmp_path):
    with pytest.raises(ValueError, match="require_web_session"):
        create_web_conversations_router(None, store=ConversationStore())
