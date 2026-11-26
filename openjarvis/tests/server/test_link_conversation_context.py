"""Owner-bound, persistent conversation context for Android Link task turns."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from openjarvis.tasks.supervisor import SupervisorStore


def _complete_turn(store: SupervisorStore, *, owner: str, conversation: str, key: str, prompt: str, answer: str):
    scope = ["assistant"]
    run = store.create_link_task(
        prompt,
        capability_scope=scope,
        idempotency_key=key,
        owner_actor=owner,
        conversation_id=conversation,
    )
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    store.start_link_task(
        run["task_id"],
        objective_digest=digest,
        objective_text_digest=digest,
        capability_scope=scope,
        idempotency_key=key,
    )
    store.finish_link_task(
        run["task_id"], result_text=answer, conversation_user_text=prompt,
    )
    store.transition(run["task_id"], "VERIFYING", expected="RUNNING")
    store.transition(run["task_id"], "COMPLETED", expected="VERIFYING")
    return run


def test_context_survives_store_reopen_and_includes_follow_up(tmp_path):
    db = str(tmp_path / "supervisor.db")
    owner = "device:" + "a" * 32
    conversation = "1" * 32
    store = SupervisorStore(db)
    first = _complete_turn(
        store,
        owner=owner,
        conversation=conversation,
        key="1" * 32,
        prompt="What is Java?",
        answer="Java is a programming language that runs on the JVM.",
    )
    store.close()

    # A new store instance models app/service restart; the completed exchange is durable.
    store = SupervisorStore(db)
    followup = store.create_link_task(
        "gimme some examples",
        capability_scope=["assistant"],
        idempotency_key="2" * 32,
        owner_actor=owner,
        conversation_id=conversation,
    )
    messages = store.link_conversation_context(followup["task_id"], "gimme some examples")
    assert messages == [
        {"role": "user", "content": "What is Java?"},
        {"role": "assistant", "content": "Java is a programming language that runs on the JVM."},
        {"role": "user", "content": "gimme some examples"},
    ]
    assert first["task_id"] != followup["task_id"]
    store.close()


def test_conversation_context_isolated_by_owner_and_conversation(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    conversation = "a" * 32
    _complete_turn(
        store,
        owner="device:" + "1" * 32,
        conversation=conversation,
        key="1" * 32,
        prompt="First owner's private topic",
        answer="First owner's private answer",
    )
    other_owner_turn = store.create_link_task(
        "Second owner's question",
        capability_scope=["assistant"],
        idempotency_key="2" * 32,
        owner_actor="device:" + "2" * 32,
        conversation_id=conversation,
    )
    other_owner_context = store.link_conversation_context(
        other_owner_turn["task_id"], "Second owner's question",
    )
    assert other_owner_context == [{"role": "user", "content": "Second owner's question"}]

    other_conversation_turn = store.create_link_task(
        "New conversation question",
        capability_scope=["assistant"],
        idempotency_key="3" * 32,
        owner_actor="device:" + "1" * 32,
        conversation_id="b" * 32,
    )
    assert store.link_conversation_context(
        other_conversation_turn["task_id"], "New conversation question",
    ) == [{"role": "user", "content": "New conversation question"}]
    store.close()


def test_idempotency_replay_does_not_duplicate_turn_and_binds_conversation(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    args = {
        "capability_scope": ["assistant"],
        "idempotency_key": "4" * 32,
        "owner_actor": "device:" + "4" * 32,
        "conversation_id": "c" * 32,
    }
    first = store.create_link_task("Java question", **args)
    replay = store.create_link_task("Java question", **args)
    assert first["created"] is True
    assert replay["created"] is False
    assert first["task_id"] == replay["task_id"]
    assert len(store.link_conversation_context(first["task_id"], "Java question")) == 1

    with pytest.raises(ValueError, match="different conversation"):
        store.create_link_task(
            "Java question", **{**args, "conversation_id": "d" * 32},
        )
    store.close()


def test_legacy_link_turn_without_conversation_id_stays_isolated(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    run = store.create_link_task(
        "legacy one-turn prompt",
        capability_scope=["assistant"],
        idempotency_key="5" * 32,
        owner_actor="device:" + "5" * 32,
    )
    assert store.link_conversation_context(run["task_id"], "legacy one-turn prompt") == [
        {"role": "user", "content": "legacy one-turn prompt"},
    ]
    store.close()


def test_existing_supervisor_schema_migrates_conversation_storage_additively(tmp_path):
    db = tmp_path / "legacy-supervisor.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """CREATE TABLE link_task_runs(
            task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            objective_digest TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL,
            objective_text TEXT, result_digest TEXT, result_summary TEXT,
            created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, last_error TEXT
        )"""
    )
    connection.commit()
    connection.close()

    store = SupervisorStore(str(db))
    columns = {row["name"] for row in store._db.execute("PRAGMA table_info(link_task_runs)")}
    assert {"conversation_id", "owner_actor", "result_text"}.issubset(columns)
    tables = {
        row["name"]
        for row in store._db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "link_conversation_messages" in tables
    store.close()
