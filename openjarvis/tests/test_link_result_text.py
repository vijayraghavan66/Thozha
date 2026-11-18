"""Completed Link answers are stored in full; only the preview is shortened."""
from __future__ import annotations

import hashlib

from openjarvis.tasks.supervisor import (
    MAX_RESULT_TEXT_BYTES,
    RESULT_TEXT_CUT_NOTICE,
    SupervisorStore,
    bounded_result_text,
)

KEY = "link-result-text-0001"


def _finish(tmp_path, answer: str) -> dict:
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    created = store.create_link_task("What is Java?", capability_scope=["read_only"], idempotency_key=KEY)
    digest = created["objective_digest"]
    store.start_link_task(
        created["task_id"],
        objective_digest=digest,
        objective_text_digest=digest,
        capability_scope=["read_only"],
        idempotency_key=KEY,
    )
    store.finish_link_task(created["task_id"], result_text=answer)
    return store.link_task_run_by_idempotency_key(KEY)


def test_long_multiparagraph_answer_is_stored_exactly(tmp_path):
    paragraph = (
        "Java is a programming language and software platform. Developers use it to build web services, "
        "business applications, and Android apps.\n\n"
    )
    answer = (paragraph * 40) + "```python\nprint('code block survives')\n```\n" + "Tamil: தமிழ் உரை ✓ — end."
    assert len(answer) > 1000
    row = _finish(tmp_path, answer)
    assert row["result_text"] == answer
    assert row["result_digest"] == hashlib.sha256(answer.encode("utf-8")).hexdigest()
    assert row["result_summary"] == answer[:1000]


def test_short_answer_round_trips_unchanged(tmp_path):
    row = _finish(tmp_path, "JARVIS_CHAT_OK")
    assert row["result_text"] == "JARVIS_CHAT_OK"


def test_cut_is_stated_when_answer_exceeds_storage_cap():
    too_long = "தமிழ்" * (MAX_RESULT_TEXT_BYTES // 2)
    kept = bounded_result_text(too_long)
    assert kept.endswith(RESULT_TEXT_CUT_NOTICE)
    assert len(kept.encode("utf-8")) <= MAX_RESULT_TEXT_BYTES
    kept.encode("utf-8").decode("utf-8")  # no broken multi-byte character at the cut
    assert bounded_result_text("x" * 100) == "x" * 100
