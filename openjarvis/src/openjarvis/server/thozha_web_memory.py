"""Persistent, owner-scoped browser memory.

This store is deliberately separate from OpenJarvis' global memory backends:
those backends do not currently carry a Thozha Web owner scope. Search uses
deterministic case-insensitive substring matching and is not semantic search.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from openjarvis.core.paths import get_config_dir
from openjarvis.server.thozha_web_auth import WebAuthConfigurationError

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_MEMORY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_CONTENT_BYTES = 16 * 1024
_MAX_TAGS = 16
_MAX_TAG_BYTES = 64
_MAX_OWNER_ITEMS = 5_000
_MAX_OWNER_BYTES = 20 * 1024 * 1024
_CHAT_CONTEXT_TAG = "chat_context"
_CHAT_CONTEXT_EXCLUDED_TAGS = frozenset({"sensitive", "private", "no_chat", "never_inject"})
_CHAT_CONTEXT_TOKEN_RE = re.compile(r"[\w]{3,}", re.UNICODE)
_CHAT_CONTEXT_STOP_WORDS = frozenset({
    "about", "after", "again", "also", "because", "before", "being", "could",
    "does", "from", "have", "here", "into", "just", "more", "most", "other",
    "please", "should", "some", "than", "that", "their", "there", "these",
    "they", "this", "those", "through", "what", "when", "where", "which",
    "while", "with", "would", "your",
})
_MAX_CHAT_CONTEXT_ITEMS = 3
_MAX_CHAT_CONTEXT_CHARS = 4_000


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=_MAX_CONTENT_BYTES)
    title: str | None = Field(default=None, max_length=160)
    tags: list[str] = Field(default_factory=list, max_length=_MAX_TAGS)

    model_config = ConfigDict(extra="forbid")


class MemoryQuotaExceeded(Exception):
    """The owner-scoped memory budget would be exceeded."""


class MemoryIdempotencyConflict(Exception):
    """An idempotency key was reused for different memory content."""


class WebMemoryStore:
    """SQLite persistence with owner-scoped queries and bounded storage."""

    def __init__(
        self,
        database_path: str | Path | None = None,
    ) -> None:
        self.path = Path(
            database_path
            if database_path is not None
            else get_config_dir() / "thozha_web_memory.db"
        ).expanduser()
        if self.path.is_symlink():
            raise WebAuthConfigurationError("Web memory database must not be a symlink")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = self.path.parent.stat()
        if (
            parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) & 0o077
        ):
            raise WebAuthConfigurationError(
                "Web memory database directory must be owner-only"
            )
        created_database = not self.path.exists()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS web_memories(
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                content_bytes INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(owner_id, idempotency_key)
            )"""
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_memories_owner_updated "
            "ON web_memories(owner_id, updated_at DESC, id DESC)"
        )
        self._db.commit()
        try:
            os.chmod(self.path, 0o600)
            if stat.S_IMODE(self.path.stat().st_mode) != 0o600:
                raise OSError("memory database mode is not 0600 after chmod")
        except OSError as exc:
            self._db.close()
            if created_database:
                for suffix in ("", "-wal", "-shm"):
                    try:
                        Path(f"{self.path}{suffix}").unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
            raise WebAuthConfigurationError(
                "Web memory database must be restricted to mode 0600"
            ) from exc

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "tags": json.loads(row["tags_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create(
        self,
        owner_id: str,
        *,
        content: str,
        title: str | None,
        tags: list[str],
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        cleaned_content = content.strip()
        cleaned_title = (title or "").strip() or cleaned_content[:80]
        cleaned_tags = sorted({tag.strip() for tag in tags if tag.strip()})
        if not cleaned_content:
            raise ValueError("Memory content cannot be blank")
        if len(cleaned_content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise ValueError("Memory content exceeds byte limit")
        if len(cleaned_title.encode("utf-8")) > 160:
            raise ValueError("Memory title exceeds byte limit")
        if len(cleaned_tags) > _MAX_TAGS or any(
            len(tag.encode("utf-8")) > _MAX_TAG_BYTES for tag in cleaned_tags
        ):
            raise ValueError("Memory tags exceed limits")
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise ValueError("Idempotency key is invalid")

        digest_payload = json.dumps(
            {"title": cleaned_title, "content": cleaned_content, "tags": cleaned_tags},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        request_hash = hashlib.sha256(digest_payload).hexdigest()
        now = self._now()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                previous = self._db.execute(
                    "SELECT * FROM web_memories WHERE owner_id=? AND idempotency_key=?",
                    (owner_id, idempotency_key),
                ).fetchone()
                if previous is not None:
                    if previous["request_hash"] != request_hash:
                        raise MemoryIdempotencyConflict
                    self._db.commit()
                    return self._public(previous), True

                usage = self._db.execute(
                    "SELECT COUNT(*) AS count, COALESCE(SUM(content_bytes),0) AS bytes "
                    "FROM web_memories WHERE owner_id=?",
                    (owner_id,),
                ).fetchone()
                content_bytes = len(cleaned_content.encode("utf-8"))
                if (
                    int(usage["count"]) >= _MAX_OWNER_ITEMS
                    or int(usage["bytes"]) + content_bytes > _MAX_OWNER_BYTES
                ):
                    raise MemoryQuotaExceeded

                memory_id = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO web_memories(id,owner_id,title,content,tags_json,"
                    "content_bytes,idempotency_key,request_hash,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        memory_id,
                        owner_id,
                        cleaned_title,
                        cleaned_content,
                        json.dumps(cleaned_tags, ensure_ascii=False),
                        content_bytes,
                        idempotency_key,
                        request_hash,
                        now,
                        now,
                    ),
                )
                row = self._db.execute(
                    "SELECT * FROM web_memories WHERE id=? AND owner_id=?",
                    (memory_id, owner_id),
                ).fetchone()
                self._db.commit()
                return self._public(row), False
            except BaseException:
                self._db.rollback()
                raise

    def list(
        self, owner_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not 0 <= offset <= _MAX_OWNER_ITEMS:
            raise ValueError("offset is outside the supported range")
        with self._lock:
            cursor = self._db.execute(
                "SELECT * FROM web_memories WHERE owner_id=? "
                "ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
                (owner_id, limit, offset),
            )
            return [self._public(row) for row in cursor.fetchall()]

    def get(self, owner_id: str, memory_id: str) -> dict[str, Any] | None:
        if not _MEMORY_ID_RE.fullmatch(memory_id):
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM web_memories WHERE owner_id=? AND id=?",
                (owner_id, memory_id),
            ).fetchone()
            return self._public(row) if row else None

    def search(
        self, owner_id: str, query: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        term = query.strip()
        if not term or len(term.encode("utf-8")) > 256:
            raise ValueError("query must be non-empty and under 256 UTF-8 bytes")
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM web_memories WHERE owner_id=? "
                "AND (instr(lower(content),lower(?))>0 "
                "OR instr(lower(title),lower(?))>0 "
                "OR instr(lower(tags_json),lower(?))>0) "
                "ORDER BY updated_at DESC,id DESC LIMIT ?",
                (owner_id, term, term, term, limit),
            ).fetchall()
            return [self._public(row) for row in rows]

    @staticmethod
    def _chat_tokens(value: str) -> set[str]:
        return {
            token.casefold()
            for token in _CHAT_CONTEXT_TOKEN_RE.findall(value)
            if token.casefold() not in _CHAT_CONTEXT_STOP_WORDS
        }

    def retrieve_chat_context(
        self, owner_id: str, query: str, *, limit: int = _MAX_CHAT_CONTEXT_ITEMS
    ) -> list[dict[str, Any]]:
        """Retrieve relevant owner memories only after an explicit chat_context opt-in."""
        if not isinstance(owner_id, str) or not owner_id.strip():
            return []
        query_tokens = self._chat_tokens(query[:_MAX_CONTENT_BYTES])
        bounded_limit = min(max(int(limit), 0), _MAX_CHAT_CONTEXT_ITEMS)
        if not query_tokens or bounded_limit == 0:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM web_memories WHERE owner_id=? "
                "ORDER BY updated_at DESC,id DESC LIMIT 200",
                (owner_id,),
            ).fetchall()
        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for row in rows:
            tags = [str(tag).casefold() for tag in json.loads(row["tags_json"])]
            tag_set = set(tags)
            if _CHAT_CONTEXT_TAG not in tag_set or tag_set & _CHAT_CONTEXT_EXCLUDED_TAGS:
                continue
            title = str(row["title"])
            content = str(row["content"])
            memory_tokens = self._chat_tokens(f"{title} {content} {' '.join(tags)}")
            overlap = query_tokens & memory_tokens
            if not overlap:
                continue
            score = len(overlap) * 2 + len(query_tokens & self._chat_tokens(title))
            ranked.append((score, str(row["updated_at"]), self._public(row)))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected: list[dict[str, Any]] = []
        total_chars = 0
        for _, _, memory in ranked:
            remaining = _MAX_CHAT_CONTEXT_CHARS - total_chars
            if remaining <= 0 or len(selected) >= bounded_limit:
                break
            content = str(memory["content"])[:remaining]
            if not content.strip():
                continue
            selected.append({
                "id": memory["id"],
                "title": str(memory["title"])[:160],
                "content": content,
                "tags": memory["tags"],
            })
            total_chars += len(content)
        return selected

    def delete(self, owner_id: str, memory_id: str) -> bool:
        if not _MEMORY_ID_RE.fullmatch(memory_id):
            return False
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM web_memories WHERE owner_id=? AND id=?",
                (owner_id, memory_id),
            )
            return cursor.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _owner_id(principal: Any) -> str:
    value = (
        principal.get("user_id")
        if isinstance(principal, dict)
        else getattr(principal, "user_id", None)
    )
    session_id = (
        principal.get("session_id")
        if isinstance(principal, dict)
        else getattr(principal, "session_id", None)
    )
    expires_at = (
        principal.get("expires_at")
        if isinstance(principal, dict)
        else getattr(principal, "expires_at", None)
    )
    if not isinstance(value, str) or not value.strip() or not session_id:
        raise HTTPException(status_code=401, detail="Invalid Web session principal")
    try:
        expiry = int(expires_at)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=401, detail="Invalid Web session expiry"
        ) from None
    if expiry <= int(datetime.now(timezone.utc).timestamp()):
        raise HTTPException(status_code=401, detail="Web session expired")
    return value


def create_web_memory_router(
    require_web_session: Callable[..., Any],
    *,
    store: WebMemoryStore | None = None,
) -> APIRouter:
    """Create owner-scoped routes using the browser's separate session auth."""
    if require_web_session is None:
        raise ValueError("require_web_session dependency is required")
    memory_store = store or WebMemoryStore()
    router = APIRouter(prefix="/v1/web/memory", tags=["web-memory"])

    @router.post("", status_code=201)
    async def create_memory(
        body: MemoryCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        principal=Depends(require_web_session),
    ):
        owner = _owner_id(principal)
        if idempotency_key is None or not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise HTTPException(
                status_code=400, detail="A valid Idempotency-Key is required"
            )
        try:
            record, replayed = memory_store.create(
                owner,
                content=body.content,
                title=body.title,
                tags=body.tags,
                idempotency_key=idempotency_key,
            )
        except MemoryQuotaExceeded:
            raise HTTPException(
                status_code=413, detail="Owner memory budget exceeded"
            ) from None
        except MemoryIdempotencyConflict:
            raise HTTPException(
                status_code=409, detail="Idempotency key was used for different content"
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {"memory": record, "replayed": replayed}

    @router.get("")
    async def list_memories(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=_MAX_OWNER_ITEMS),
        principal=Depends(require_web_session),
    ):
        owner = _owner_id(principal)
        items = memory_store.list(owner, limit=limit, offset=offset)
        return {"items": items, "search_mode": "substring"}

    @router.get("/search")
    async def search_memories(
        q: str = Query(min_length=1, max_length=256),
        limit: int = Query(default=50, ge=1, le=100),
        principal=Depends(require_web_session),
    ):
        owner = _owner_id(principal)
        try:
            items = memory_store.search(owner, q, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {"items": items, "search_mode": "substring"}

    @router.get("/{memory_id}")
    async def get_memory(memory_id: str, principal=Depends(require_web_session)):
        owner = _owner_id(principal)
        item = memory_store.get(owner, memory_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        return item

    @router.delete("/{memory_id}", status_code=204)
    async def delete_memory(memory_id: str, principal=Depends(require_web_session)):
        owner = _owner_id(principal)
        if not memory_store.delete(owner, memory_id):
            raise HTTPException(status_code=404, detail="Memory not found")
        return Response(status_code=204)

    router.web_memory_store = memory_store
    return router


__all__ = [
    "MemoryCreate",
    "MemoryIdempotencyConflict",
    "MemoryQuotaExceeded",
    "WebMemoryStore",
    "create_web_memory_router",
]
