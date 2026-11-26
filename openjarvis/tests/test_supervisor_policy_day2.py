"""Adversarial, non-executing tests for the live generic-tool boundary."""
from __future__ import annotations

import json
import shutil

import pytest

from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.tasks.supervisor import (
    SupervisorStore,
    bind_task,
    requires_approval,
)
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec


class RecordingShell(BaseTool):
    def __init__(self):
        self.calls: list[dict] = []

    @property
    def spec(self):
        return ToolSpec(
            name="shell_exec",
            description="record-only boundary test; does not execute shell commands",
            parameters={},
            requires_confirmation=True,
            required_capabilities=["code:execute"],
        )

    def execute(self, **params):
        self.calls.append(params)
        return ToolResult(tool_name="shell_exec", content="recorded only")


def _running_task(store: SupervisorStore) -> str:
    task_id = store.create_task("Non-executing supervisor authorization test")
    store.transition(task_id, "RUNNING")
    return task_id


@pytest.mark.parametrize(
    "command",
    [
        "diskutil eraseDisk APFS TestDisk disk2",
        "diskutil partitionDisk disk2 GPT JHFS+ Test 100%",
        "diskutil apfs deleteVolume disk3s1",
        "git branch -D release",
        "git branch -m main old-main",
        "git clean -fd",
        "git reset --hard",
        "git checkout -B replacement",
        "git status; rm -rf /tmp/not-executed",
        "/tmp/diskutil list",
        "./diskutil list",
        "/tmp/git status",
    ],
)
def test_mutating_ambiguous_and_substituted_commands_classify_for_approval(command):
    assert requires_approval("shell_exec", {"command": command}, declared=True)


def test_only_exact_diskutil_inspection_verbs_are_approval_free():
    if shutil.which("diskutil") is None:
        pytest.skip("diskutil is only available on macOS")
    assert not requires_approval("shell_exec", {"command": "diskutil list"}, declared=True)
    assert not requires_approval("shell_exec", {"command": "diskutil info disk0"}, declared=True)
    assert requires_approval("shell_exec", {"command": "diskutil apfs list"}, declared=True)


def test_executor_blocks_mutating_diskutil_with_exact_pending_record(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    store.schedule_approval_notification = lambda _approval_id: None
    task_id = _running_task(store)
    shell = RecordingShell()
    executor = ToolExecutor([shell], supervisor_store=store)

    with bind_task(task_id):
        result = executor.execute(ToolCall(
            id="erase-disk-test",
            name="shell_exec",
            arguments=json.dumps({"command": "diskutil eraseDisk APFS TestDisk disk2"}),
        ))

    assert not result.success
    assert result.metadata["error_code"] == "APPROVAL_REQUIRED"
    assert shell.calls == []
    approval = store.approval(result.metadata["approval_id"])
    assert approval["status"] == "PENDING"
    assert approval["safe_args"] == {"command": "diskutil eraseDisk APFS TestDisk disk2"}
    assert store.get_task(task_id)["state"] == "WAITING_APPROVAL"
    store.close()


def test_readonly_command_can_use_executor_without_per_command_approval(tmp_path):
    if shutil.which("df") is None:
        pytest.skip("df is unavailable on this platform")
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    shell = RecordingShell()
    executor = ToolExecutor([shell], supervisor_store=store)
    result = executor.execute(ToolCall(
        id="df-test", name="shell_exec", arguments=json.dumps({"command": "df -h"}),
    ))
    assert result.success
    assert shell.calls == [{"command": "df -h"}]
    assert store.pending_approvals(10) == []
    store.close()


def test_unbound_mutating_tool_without_interactive_owner_fails_closed(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    store.schedule_approval_notification = lambda _approval_id: None
    shell = RecordingShell()
    executor = ToolExecutor([shell], supervisor_store=store, interactive=False)
    result = executor.execute(ToolCall(
        id="git-reset-test", name="shell_exec",
        arguments=json.dumps({"command": "git reset --hard"}),
    ))
    assert not result.success
    assert result.metadata["error_code"] == "APPROVAL_REQUIRED"
    assert shell.calls == []
    assert store.approval(result.metadata["approval_id"])["status"] == "PENDING"
    store.close()
