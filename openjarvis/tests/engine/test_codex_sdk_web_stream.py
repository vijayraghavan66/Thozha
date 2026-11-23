from contextlib import aclosing
from types import SimpleNamespace

import pytest

from openjarvis.core.types import Message, Role
from openjarvis.engine.codex_sdk import CodexSDKEngine


@pytest.mark.asyncio
async def test_codex_web_stream_only_yields_final_answer_and_checks_canonical(
    monkeypatch,
):
    sdk = pytest.importorskip("openai_codex")
    from openai_codex.generated.v2_all import (
        AgentMessageDeltaNotification,
        AgentMessageThreadItem,
        ItemCompletedNotification,
        ItemStartedNotification,
        MessagePhase,
        TurnCompletedNotification,
        TurnStatus,
    )

    finished = False

    def message(cls, message_id, phase, text=""):
        item = AgentMessageThreadItem(
            id=message_id, phase=phase, text=text, type="agentMessage"
        )
        return cls.model_construct(turn_id="turn", item=SimpleNamespace(root=item))

    def delta(item_id, text, turn_id="turn"):
        return AgentMessageDeltaNotification(
            turn_id=turn_id, thread_id="thread", item_id=item_id, delta=text
        )

    class FakeTurn:
        id = "turn"

        async def stream(self):
            nonlocal finished
            packets = [
                message(ItemStartedNotification, "comment", MessagePhase.commentary),
                delta("comment", "private commentary"),
                message(ItemStartedNotification, "answer", MessagePhase.final_answer),
                delta("answer", "different turn", turn_id="other"),
                delta("answer", "Caf"),
                delta("answer", "\u00e9\n```"),
                message(
                    ItemCompletedNotification,
                    "answer",
                    MessagePhase.final_answer,
                    "Caf\u00e9\n```",
                ),
            ]
            for packet in packets:
                yield SimpleNamespace(payload=packet)
            finished = True
            yield SimpleNamespace(
                payload=TurnCompletedNotification.model_construct(
                    turn=SimpleNamespace(id="turn", status=TurnStatus.completed),
                )
            )

    class FakeCodex:
        def __init__(self, *, config):
            self._client = SimpleNamespace()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def thread_start(self, **kwargs):
            async def turn(*args, **kwargs):
                return FakeTurn()

            return SimpleNamespace(turn=turn)

    monkeypatch.setattr(sdk, "AsyncCodex", FakeCodex)
    monkeypatch.setattr(
        "openjarvis.engine.codex_sdk._config_overrides", lambda *a, **k: ()
    )
    engine = CodexSDKEngine()
    prompt = [Message(role=Role.USER, content="A safe sample.")]
    async with aclosing(engine.stream(prompt, tools=[], web_chat=True)) as chunks:
        assert await anext(chunks) == "Caf"
        assert not finished
        assert [chunk async for chunk in chunks] == ["\u00e9\n```"]
    assert finished
