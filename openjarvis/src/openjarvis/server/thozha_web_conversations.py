"""Owner-scoped browser conversations for Thozha Web.

This module is intentionally not mounted by the production application yet.
The caller must supply the browser-session dependency; Android device tokens
are never accepted here. Message generation uses the configured OpenJarvis
engine directly, with tools disabled. The SSE route exposes public text
deltas when the engine supports them.

Startup reconciliation assumes exactly one server process uses this database.
Multi-worker deployments need a shared worker lease before mounting this router;
otherwise one process could classify another process's live inference as stale.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from openjarvis.core.paths import get_config_dir

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_MAX_MESSAGE_CHARS = 16_000
_MAX_CONVERSATION_MESSAGES = 80


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=160)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)


class ConversationStore:
    """SQLite persistence for conversations, messages and lifecycle events."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            path = Path(self.db_path)
            from openjarvis.security.file_utils import secure_create

            secure_create(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 10000")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS web_conversations (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_web_conversations_owner_updated
                    ON web_conversations(owner_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS web_messages (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL REFERENCES
                        web_conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT,
                    request_hash TEXT,
                    response_to TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_id, conversation_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_web_messages_owner_conversation
                    ON web_messages(owner_id, conversation_id, created_at, id);
                CREATE TABLE IF NOT EXISTS web_conversation_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL REFERENCES
                        web_conversations(id) ON DELETE CASCADE,
                    message_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_web_events_owner_conversation
                    ON web_conversation_events(owner_id, conversation_id, sequence);
                """
            )
            self._db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _event(
        self,
        owner_id: str,
        conversation_id: str,
        message_id: str,
        kind: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO web_conversation_events("
            "owner_id,conversation_id,message_id,event_type,created_at,details) "
            "VALUES(?,?,?,?,?,?)",
            (
                owner_id,
                conversation_id,
                message_id,
                kind,
                self._now(),
                json.dumps(details or {}),
            ),
        )

    def create(self, owner_id: str, title: str | None = None) -> dict[str, Any]:
        now, conversation_id = self._now(), str(uuid.uuid4())
        cleaned = (title or "New conversation").strip() or "New conversation"
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO web_conversations("
                "id,owner_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (conversation_id, owner_id, cleaned, now, now),
            )
        return self.get(owner_id, conversation_id)

    def list(
        self, owner_id: str, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM web_conversations WHERE owner_id=? "
                "AND (? OR archived_at IS NULL) ORDER BY updated_at DESC",
                (owner_id, int(include_archived)),
            ).fetchall()
        return [self._conversation_dict(row, include_messages=False) for row in rows]

    def get(self, owner_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM web_conversations WHERE owner_id=? AND id=?",
                (owner_id, conversation_id),
            ).fetchone()
            if row is None:
                return None
            conversation = self._conversation_dict(row, include_messages=False)
            messages = self._db.execute(
                "SELECT * FROM web_messages WHERE owner_id=? "
                "AND conversation_id=? ORDER BY rowid",
                (owner_id, conversation_id),
            ).fetchall()
            events = self._db.execute(
                "SELECT sequence,message_id,event_type,created_at,details "
                "FROM web_conversation_events WHERE owner_id=? "
                "AND conversation_id=? ORDER BY sequence",
                (owner_id, conversation_id),
            ).fetchall()
        conversation["messages"] = [self._message_dict(message) for message in messages]
        conversation["events"] = [
            {**dict(event), "details": json.loads(event["details"])} for event in events
        ]
        return conversation

    @staticmethod
    def _conversation_dict(row, *, include_messages: bool) -> dict[str, Any]:
        result = {
            key: row[key]
            for key in ("id", "title", "created_at", "updated_at", "archived_at")
        }
        if include_messages:
            result["messages"] = []
        return result

    @staticmethod
    def _message_dict(row) -> dict[str, Any]:
        """Return public fields only, never ownership or replay metadata."""
        fields = (
            "id",
            "conversation_id",
            "role",
            "content",
            "status",
            "response_to",
            "error_code",
            "created_at",
            "updated_at",
        )
        return {field: row[field] for field in fields}

    def archive(self, owner_id: str, conversation_id: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE web_conversations SET archived_at=?,updated_at=? "
                "WHERE owner_id=? AND id=? AND archived_at IS NULL",
                (self._now(), self._now(), owner_id, conversation_id),
            )
            return cur.rowcount > 0

    def delete(self, owner_id: str, conversation_id: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM web_conversations WHERE owner_id=? AND id=?",
                (owner_id, conversation_id),
            )
            return cur.rowcount > 0

    def submit(
        self, owner_id: str, conversation_id: str, key: str, content: str
    ) -> tuple[dict[str, Any], bool]:
        request_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = self._now()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                conversation = self._db.execute(
                    "SELECT archived_at FROM web_conversations "
                    "WHERE owner_id=? AND id=?",
                    (owner_id, conversation_id),
                ).fetchone()
                if conversation is None:
                    raise KeyError("conversation_not_found")
                if conversation["archived_at"] is not None:
                    raise ValueError("conversation_archived")
                existing = self._db.execute(
                    "SELECT * FROM web_messages WHERE owner_id=? "
                    "AND conversation_id=? AND idempotency_key=?",
                    (owner_id, conversation_id, key),
                ).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise ValueError("idempotency_key_reused")
                    response = self._db.execute(
                        "SELECT * FROM web_messages WHERE owner_id=? AND response_to=?",
                        (owner_id, existing["id"]),
                    ).fetchone()
                    self._db.commit()
                    return {
                        "user": self._message_dict(existing),
                        "assistant": self._message_dict(response) if response else None,
                    }, True
                user_id, response_id = str(uuid.uuid4()), str(uuid.uuid4())
                self._db.execute(
                    "INSERT INTO web_messages("
                    "id,owner_id,conversation_id,role,content,status,"
                    "idempotency_key,request_hash,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id,
                        owner_id,
                        conversation_id,
                        "user",
                        content,
                        "accepted",
                        key,
                        request_hash,
                        now,
                        now,
                    ),
                )
                self._db.execute(
                    "INSERT INTO web_messages("
                    "id,owner_id,conversation_id,role,content,status,response_to,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        response_id,
                        owner_id,
                        conversation_id,
                        "assistant",
                        "",
                        "queued",
                        user_id,
                        now,
                        now,
                    ),
                )
                self._db.execute(
                    "UPDATE web_conversations SET updated_at=? "
                    "WHERE owner_id=? AND id=?",
                    (now, owner_id, conversation_id),
                )
                self._event(owner_id, conversation_id, user_id, "accepted")
                self._event(owner_id, conversation_id, response_id, "queued")
                self._db.commit()
                return {
                    "user": self._message_by_id(user_id),
                    "assistant": self._message_by_id(response_id),
                }, False
            except Exception:
                self._db.rollback()
                raise

    def _message_by_id(self, message_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT * FROM web_messages WHERE id=?", (message_id,)
        ).fetchone()
        return dict(row)

    def claim(
        self, owner_id: str, assistant_id: str
    ) -> tuple[dict[str, Any], list[dict[str, str]]] | None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                response = self._db.execute(
                    "SELECT * FROM web_messages WHERE owner_id=? AND id=? "
                    "AND role='assistant' AND status='queued'",
                    (owner_id, assistant_id),
                ).fetchone()
                if response is None:
                    self._db.commit()
                    return None
                self._db.execute(
                    "UPDATE web_messages SET status='running',updated_at=? "
                    "WHERE id=? AND status='queued'",
                    (self._now(), assistant_id),
                )
                context = self._db.execute(
                    "SELECT role,content FROM web_messages WHERE owner_id=? "
                    "AND conversation_id=? AND rowid <= "
                    "(SELECT rowid FROM web_messages WHERE id=?) ORDER BY rowid",
                    (owner_id, response["conversation_id"], response["response_to"]),
                ).fetchall()
                self._event(
                    owner_id, response["conversation_id"], assistant_id, "started"
                )
                self._db.commit()
                return dict(response), [
                    dict(item) for item in context[-_MAX_CONVERSATION_MESSAGES:]
                ]
            except Exception:
                self._db.rollback()
                raise

    def cancellation_requested(self, owner_id: str, assistant_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT cancel_requested FROM web_messages WHERE owner_id=? "
                "AND id=? AND role='assistant'",
                (owner_id, assistant_id),
            ).fetchone()
        return row is None or bool(row[0])

    def finish(
        self,
        owner_id: str,
        assistant_id: str,
        *,
        content: str = "",
        status: str,
        error_code: str | None = None,
    ) -> None:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT conversation_id,cancel_requested,status FROM web_messages "
                "WHERE owner_id=? AND id=? AND role='assistant'",
                (owner_id, assistant_id),
            ).fetchone()
            if row is None or row["status"] not in {"queued", "running"}:
                return
            final_status = "cancelled" if row["cancel_requested"] else status
            final_content = "" if final_status == "cancelled" else content
            final_error = None if final_status == "cancelled" else error_code
            self._db.execute(
                "UPDATE web_messages SET content=?,status=?,error_code=?,updated_at=? "
                "WHERE owner_id=? AND id=?",
                (
                    final_content,
                    final_status,
                    final_error,
                    self._now(),
                    owner_id,
                    assistant_id,
                ),
            )
            self._db.execute(
                "UPDATE web_conversations SET updated_at=? WHERE owner_id=? AND id=?",
                (self._now(), owner_id, row["conversation_id"]),
            )
            self._event(
                owner_id,
                row["conversation_id"],
                assistant_id,
                final_status,
                {"error_code": error_code} if error_code else None,
            )

    def cancel(
        self, owner_id: str, conversation_id: str, assistant_id: str
    ) -> str | None:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT status FROM web_messages WHERE owner_id=? "
                "AND conversation_id=? AND id=? AND role='assistant'",
                (owner_id, conversation_id, assistant_id),
            ).fetchone()
            if row is None:
                return None
            if row["status"] in {"completed", "failed", "cancelled"}:
                return row["status"]
            if row["status"] == "queued":
                self._db.execute(
                    "UPDATE web_messages SET status='cancelled',updated_at=? "
                    "WHERE owner_id=? AND id=?",
                    (self._now(), owner_id, assistant_id),
                )
                self._event(owner_id, conversation_id, assistant_id, "cancelled")
                return "cancelled"
            cancel_row = self._db.execute(
                "SELECT cancel_requested FROM web_messages WHERE owner_id=? AND id=?",
                (owner_id, assistant_id),
            ).fetchone()
            if cancel_row and cancel_row["cancel_requested"]:
                return "cancellation_requested"
            self._db.execute(
                "UPDATE web_messages SET cancel_requested=1,updated_at=? "
                "WHERE owner_id=? AND id=?",
                (self._now(), owner_id, assistant_id),
            )
            self._event(
                owner_id, conversation_id, assistant_id, "cancellation_requested"
            )
            return "cancellation_requested"

    def reconcile_startup(self) -> list[tuple[str, str]]:
        """Fail uncertain in-flight calls; return accepted queued work to resume.

        Only queued messages are safe to dispatch after a restart. A running
        ``generate`` call may already have reached a provider, so it is marked
        interrupted and is never replayed automatically.
        """
        with self._lock, self._db:
            running = self._db.execute(
                "SELECT owner_id,conversation_id,id FROM web_messages "
                "WHERE role='assistant' AND status='running'"
            ).fetchall()
            for row in running:
                self._db.execute(
                    "UPDATE web_messages SET status='failed',error_code=?,updated_at=? "
                    "WHERE owner_id=? AND id=? AND status='running'",
                    (
                        "process_interrupted",
                        self._now(),
                        row["owner_id"],
                        row["id"],
                    ),
                )
                self._event(
                    row["owner_id"],
                    row["conversation_id"],
                    row["id"],
                    "failed",
                    {"error_code": "process_interrupted"},
                )
            queued = self._db.execute(
                "SELECT owner_id,id FROM web_messages "
                "WHERE role='assistant' AND status='queued'"
            ).fetchall()
            return [(row["owner_id"], row["id"]) for row in queued]

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _principal_owner(principal: Any) -> str:
    user_id = (
        principal.get("user_id")
        if isinstance(principal, dict)
        else getattr(principal, "user_id", None)
    )
    session_id = (
        principal.get("session_id")
        if isinstance(principal, dict)
        else getattr(principal, "session_id", None)
    )
    expires = (
        principal.get("expires_at")
        if isinstance(principal, dict)
        else getattr(principal, "expires_at", None)
    )
    if (
        not isinstance(user_id, str)
        or not user_id.strip()
        or not isinstance(session_id, str)
        or not session_id.strip()
    ):
        raise HTTPException(status_code=401, detail="Invalid web session principal")
    try:
        if isinstance(expires, datetime):
            expiry = expires
        elif isinstance(expires, (int, float)) and not isinstance(expires, bool):
            expiry = datetime.fromtimestamp(expires, tz=timezone.utc)
        else:
            expiry = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=401, detail="Invalid web session expiry"
        ) from None
    if expiry <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Web session expired")
    return user_id


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {encoded}\n\n"


def _stream_engine(engine: Any, model: str) -> Any:
    selector = getattr(engine, "_engine_for", None)
    return selector(model) if callable(selector) else engine


def _engine_stream_mode(engine: Any, selected: Any) -> str:
    """Honor the selected engine's capability, including older Codex adapters."""
    engine_id = str(getattr(selected, "engine_id", "")).lower()
    module = type(selected).__module__.lower()
    capability = getattr(selected, "web_chat_stream_mode", None)
    if callable(capability):
        return capability()
    if "codex_sdk" in engine_id or module.endswith(".codex_sdk"):
        return "buffered"
    return "engine_stream" if callable(getattr(engine, "stream", None)) else "buffered"


async def _cancellable_chunks(
    chunks: AsyncIterator[str], cancelled: Callable[[], bool]
) -> AsyncIterator[str]:
    pending = None
    try:
        while not cancelled():
            pending = asyncio.ensure_future(anext(chunks))
            while not pending.done():
                await asyncio.wait((pending,), timeout=0.25)
                if cancelled():
                    return
            finished, pending = pending, None
            try:
                chunk = finished.result()
            except StopAsyncIteration:
                return
            if not cancelled():
                yield chunk
    finally:
        try:
            if pending is not None:
                pending.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending
        finally:
            close = getattr(chunks, "aclose", None)
            if callable(close):
                await close()


def _require_conversation(
    store: ConversationStore, owner_id: str, conversation_id: str
) -> dict[str, Any]:
    conversation = store.get(owner_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def create_web_conversations_router(
    require_web_session: Callable[..., Any],
    *,
    store: ConversationStore | None = None,
    engine_provider: Callable[[Request], Any] | None = None,
    model_provider: Callable[[Request], str] | None = None,
) -> APIRouter:
    """Create a router with an injected browser-session dependency.

    Production integration must pass the real ``require_web_session``. The
    optional providers are seams for app-state configuration and deterministic
    tests; no unauthenticated default is supplied. Run one API process per
    conversation database until shared worker ownership is implemented.
    """
    if require_web_session is None:
        raise ValueError("require_web_session dependency is required")
    if store is None:
        db_path = get_config_dir() / "thozha_web_conversations.db"
        store = ConversationStore(db_path)
    router = APIRouter(prefix="/v1/web/conversations", tags=["web-conversations"])
    workers: dict[str, asyncio.Task] = {}
    workers_lock = threading.RLock()
    startup_recovery_lock = threading.Lock()
    startup_recovered = False

    async def ensure_startup_recovery(request: Request) -> None:
        nonlocal startup_recovered
        with startup_recovery_lock:
            if startup_recovered:
                return
            queued = store.reconcile_startup()
            startup_recovered = True
        for owner_id, assistant_id in queued:
            await schedule(request, owner_id, assistant_id)

    def engine_for(request: Request):
        return (
            engine_provider(request)
            if engine_provider
            else getattr(request.app.state, "engine", None)
        )

    def model_for(request: Request):
        model = (
            model_provider(request)
            if model_provider
            else getattr(request.app.state, "model", None)
        )
        return model if isinstance(model, str) and model else None

    async def run_response(request: Request, owner_id: str, assistant_id: str) -> None:
        claim = store.claim(owner_id, assistant_id)
        if claim is None:
            return
        response, history = claim
        conversation_id = response["conversation_id"]
        if store.cancellation_requested(owner_id, assistant_id):
            store.finish(owner_id, assistant_id, status="cancelled")
            return
        engine = engine_for(request)
        model = model_for(request)
        if engine is None or model is None:
            store.finish(
                owner_id, assistant_id, status="failed", error_code="engine_unavailable"
            )
            return
        try:
            from openjarvis.core.types import Message, Role

            messages = [
                Message(role=Role(item["role"]), content=item["content"])
                for item in history
            ]
            # Explicitly empty tool list: browser chat is conversational only.
            result = await asyncio.to_thread(
                engine.generate,
                messages,
                model=model,
                temperature=0.7,
                max_tokens=1024,
                tools=[],
            )
            if store.cancellation_requested(owner_id, assistant_id):
                store.finish(owner_id, assistant_id, status="cancelled")
                return
            text = result.get("content") if isinstance(result, dict) else None
            if not isinstance(text, str) or not text.strip():
                store.finish(
                    owner_id,
                    assistant_id,
                    status="failed",
                    error_code="empty_engine_response",
                )
                return
            store.finish(owner_id, assistant_id, content=text, status="completed")
        except asyncio.CancelledError:
            store.finish(owner_id, assistant_id, status="cancelled")
            raise
        except Exception as exc:
            # Error message could contain provider details or prompt content;
            # persist a stable code and log only the class name and IDs.
            logger.warning(
                "Web conversation generation failed "
                "conversation_id=%s message_id=%s error_type=%s",
                conversation_id,
                assistant_id,
                type(exc).__name__,
            )
            store.finish(
                owner_id, assistant_id, status="failed", error_code="generation_failed"
            )

    async def schedule(request: Request, owner_id: str, assistant_id: str) -> None:
        with workers_lock:
            existing = workers.get(assistant_id)
            if existing is not None and not existing.done():
                return
            task = asyncio.create_task(run_response(request, owner_id, assistant_id))
            workers[assistant_id] = task
            task.add_done_callback(lambda _: workers.pop(assistant_id, None))

    @router.post("", status_code=201)
    async def create_conversation(
        body: ConversationCreate,
        request: Request,
        principal=Depends(require_web_session),
    ):
        await ensure_startup_recovery(request)
        owner_id = _principal_owner(principal)
        return store.create(owner_id, body.title)

    @router.get("")
    async def list_conversations(
        request: Request,
        include_archived: bool = False,
        principal=Depends(require_web_session),
    ):
        await ensure_startup_recovery(request)
        owner_id = _principal_owner(principal)
        return {"items": store.list(owner_id, include_archived=include_archived)}

    @router.get("/{conversation_id}")
    async def get_conversation(
        conversation_id: str,
        request: Request,
        principal=Depends(require_web_session),
    ):
        await ensure_startup_recovery(request)
        owner_id = _principal_owner(principal)
        return _require_conversation(store, owner_id, conversation_id)

    @router.post("/{conversation_id}/archive")
    async def archive_conversation(
        conversation_id: str,
        request: Request,
        principal=Depends(require_web_session),
    ):
        await ensure_startup_recovery(request)
        owner_id = _principal_owner(principal)
        if not store.archive(owner_id, conversation_id):
            _require_conversation(store, owner_id, conversation_id)
        return {"id": conversation_id, "archived": True}

    @router.delete("/{conversation_id}", status_code=204)
    async def delete_conversation(
        conversation_id: str,
        request: Request,
        principal=Depends(require_web_session),
    ):
        await ensure_startup_recovery(request)
        owner_id = _principal_owner(principal)
        if not store.delete(owner_id, conversation_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return Response(status_code=204)

    @router.post("/{conversation_id}/messages", status_code=202)
    async def submit_message(
        conversation_id: str,
        body: MessageCreate,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        principal=Depends(require_web_session),
    ):
        await ensure_startup_recovery(request)
        owner_id = _principal_owner(principal)
        if idempotency_key is None or not _ID_RE.fullmatch(idempotency_key):
            raise HTTPException(
                status_code=400, detail="A valid Idempotency-Key header is required"
            )
        content = body.content.strip()
        if not content:
            raise HTTPException(
                status_code=422, detail="Message content cannot be blank"
            )
        try:
            result, replay = store.submit(
                owner_id, conversation_id, idempotency_key, content
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Conversation not found"
            ) from None
        except ValueError as exc:
            detail = (
                "Conversation is archived"
                if str(exc) == "conversation_archived"
                else "Idempotency key was already used for different content"
            )
            raise HTTPException(status_code=409, detail=detail) from None
        assistant = result["assistant"]
        if assistant and assistant["status"] == "queued":
            await schedule(request, owner_id, assistant["id"])
        return {
            "conversation_id": conversation_id,
            "message_id": result["user"]["id"],
            "response_id": assistant["id"] if assistant else None,
            "status": assistant["status"] if assistant else "accepted",
            "replayed": replay,
        }

    @router.post("/{conversation_id}/messages/stream")
    async def stream_message(
        conversation_id: str,
        body: MessageCreate,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        principal=Depends(require_web_session),
    ):
        """Stream response chunks for engines that produce them incrementally.

        The Codex SDK implementation yields a single completed response. It is
        marked `buffered` in the stream event and is not represented as token
        streaming. In either case the assistant result and lifecycle remain
        durable and use the same duplicate-claim gate as normal Web messages.
        """
        await ensure_startup_recovery(request)
        owner_id = _principal_owner(principal)
        if idempotency_key is None or not _ID_RE.fullmatch(idempotency_key):
            raise HTTPException(
                status_code=400, detail="A valid Idempotency-Key header is required"
            )
        content = body.content.strip()
        if not content:
            raise HTTPException(
                status_code=422, detail="Message content cannot be blank"
            )
        try:
            result, replay = store.submit(
                owner_id, conversation_id, idempotency_key, content
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Conversation not found"
            ) from None
        except ValueError as exc:
            detail = (
                "Conversation is archived"
                if str(exc) == "conversation_archived"
                else "Idempotency key was already used for different content"
            )
            raise HTTPException(status_code=409, detail=detail) from None

        assistant = result["assistant"]
        response_id = assistant["id"]
        if assistant["status"] in {"completed", "failed", "cancelled"}:

            async def replay_events():
                yield _sse_event(
                    "accepted",
                    {
                        "conversation_id": conversation_id,
                        "message_id": result["user"]["id"],
                        "response_id": response_id,
                        "status": assistant["status"],
                        "replayed": True,
                    },
                )
                if assistant["status"] == "completed":
                    yield _sse_event(
                        "message",
                        {"response_id": response_id, "content": assistant["content"]},
                    )
                yield _sse_event(
                    "done",
                    {
                        "response_id": response_id,
                        "status": assistant["status"],
                        "replayed": True,
                    },
                )

            return StreamingResponse(
                replay_events(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        if assistant["status"] != "queued":
            raise HTTPException(
                status_code=409,
                detail="Message is already being processed; use conversation status",
            )
        claim = store.claim(owner_id, response_id)
        if claim is None:
            raise HTTPException(
                status_code=409,
                detail="Message was claimed by another response worker",
            )
        claimed_response, history = claim
        engine = engine_for(request)
        model = model_for(request)
        if engine is None or model is None:
            store.finish(
                owner_id,
                response_id,
                status="failed",
                error_code="engine_unavailable",
            )
            raise HTTPException(
                status_code=503, detail="Conversation engine unavailable"
            )

        async def stream_events():
            from openjarvis.core.types import Message, Role

            messages = [
                Message(role=Role(item["role"]), content=item["content"])
                for item in history
            ]
            parts: list[str] = []
            finalized = False
            try:
                selected = _stream_engine(engine, model)
                mode = _engine_stream_mode(engine, selected)
                logger.info(
                    "web_chat event=accepted wall_ms=%d", time.time_ns() // 1_000_000
                )
                yield _sse_event(
                    "accepted",
                    {
                        "conversation_id": conversation_id,
                        "message_id": result["user"]["id"],
                        "response_id": response_id,
                        "status": "running",
                        "replayed": replay,
                    },
                )
                yield _sse_event("started", {"response_id": response_id})
                yield _sse_event(
                    "stream_mode", {"response_id": response_id, "mode": mode}
                )
                stream_method = getattr(engine, "stream", None)
                if callable(stream_method):
                    options = (
                        {"web_chat": True}
                        if callable(getattr(selected, "web_chat_stream_mode", None))
                        else {}
                    )
                    chunks = stream_method(
                        messages,
                        model=model,
                        temperature=0.7,
                        max_tokens=1024,
                        tools=[],
                        **options,
                    )
                    async with aclosing(
                        _cancellable_chunks(
                            aiter(chunks),
                            lambda: store.cancellation_requested(owner_id, response_id),
                        )
                    ) as cancellable:
                        async for chunk in cancellable:
                            if not isinstance(chunk, str) or not chunk:
                                continue
                            if not parts:
                                logger.info(
                                    "web_chat event=first_sse_delta wall_ms=%d",
                                    time.time_ns() // 1_000_000,
                                )
                            parts.append(chunk)
                            yield _sse_event(
                                "delta", {"response_id": response_id, "content": chunk}
                            )
                else:
                    generated = await asyncio.to_thread(
                        engine.generate,
                        messages,
                        model=model,
                        temperature=0.7,
                        max_tokens=1024,
                        tools=[],
                    )
                    text = (
                        generated.get("content")
                        if isinstance(generated, dict)
                        else None
                    )
                    if isinstance(text, str) and text:
                        parts.append(text)
                        yield _sse_event(
                            "delta", {"response_id": response_id, "content": text}
                        )
                if store.cancellation_requested(owner_id, response_id):
                    store.finish(owner_id, response_id, status="cancelled")
                    final_status = "cancelled"
                elif not parts:
                    store.finish(
                        owner_id,
                        response_id,
                        status="failed",
                        error_code="empty_engine_response",
                    )
                    final_status = "failed"
                else:
                    store.finish(
                        owner_id,
                        response_id,
                        content="".join(parts),
                        status="completed",
                    )
                    final_status = "completed"
                finalized = True
                logger.info(
                    "web_chat event=persisted status=%s wall_ms=%d",
                    final_status,
                    time.time_ns() // 1_000_000,
                )
                yield _sse_event(
                    "done",
                    {
                        "response_id": response_id,
                        "status": final_status,
                        "stream_mode": mode,
                    },
                )
            except asyncio.CancelledError:
                store.cancel(owner_id, conversation_id, response_id)
                store.finish(owner_id, response_id, status="cancelled")
                finalized = True
                raise
            except Exception as exc:
                logger.warning(
                    "Web conversation stream failed "
                    "conversation_id=%s message_id=%s error_type=%s",
                    conversation_id,
                    response_id,
                    type(exc).__name__,
                )
                store.finish(
                    owner_id,
                    response_id,
                    status="failed",
                    error_code="generation_failed",
                )
                finalized = True
                yield _sse_event(
                    "error",
                    {"response_id": response_id, "code": "generation_failed"},
                )
            finally:
                # Covers GeneratorExit/disconnect as well as task cancellation.
                if not finalized:
                    store.cancel(owner_id, conversation_id, response_id)
                    store.finish(owner_id, response_id, status="cancelled")

        return StreamingResponse(
            stream_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.post("/{conversation_id}/messages/{message_id}/cancel")
    async def cancel_message(
        conversation_id: str,
        message_id: str,
        request: Request,
        principal=Depends(require_web_session),
    ):
        await ensure_startup_recovery(request)
        owner_id = _principal_owner(principal)
        status = store.cancel(owner_id, conversation_id, message_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Message not found")
        return {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "status": status,
        }

    # Expose only the store in router state for controlled lifecycle integration
    # and tests; no owner identity is stored on the router.
    router.conversation_store = store
    router.conversation_workers = workers
    return router


__all__ = ["ConversationStore", "create_web_conversations_router"]
