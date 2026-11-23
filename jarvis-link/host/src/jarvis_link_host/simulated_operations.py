"""Harmless simulated operation used to exercise approval continuation.

It performs NO I/O and calls no tool. Its only effect is the supervisor state change that a real executor
would make: consume the exact approval (single use, exact digest) and move the task COMPLETED.
"""
from __future__ import annotations

from typing import Any

SIMULATED_OPERATION = "simulated_echo"
SIMULATED_TARGET = "jarvis-link://simulated/echo"


def simulated_arguments(message: str) -> dict[str, Any]:
    if not message.isascii() or len(message) > 64:
        raise ValueError("simulated message must be short ASCII")
    return {"message": message}


def resume_approved(store: Any, *, task_id: str, approval_id: str, message: str) -> dict[str, Any]:
    """Consumes an APPROVED approval for the exact operation and completes the simulated task.

    Returns a dict with outcome 'COMPLETED' or 'NOT_CONSUMED'. The store refuses consumption for any
    other status, digest, target, expiry, or task state, so a non-approved request cannot continue.
    """
    arguments = simulated_arguments(message)
    # A restart pauses in-flight tasks (supervisor recovery). An APPROVED approval may then continue the
    # task only through the supervisor's explicit resume, which requires the approval to be APPROVED.
    task = store.get_task(task_id)
    if task is not None and task["state"] == "PAUSED":
        if not store.resume_approved_task(task_id, approval_id):
            return {"outcome": "NOT_CONSUMED"}
    consumed = store.consume_approval(approval_id, task_id=task_id, operation=SIMULATED_OPERATION,
                                      target=SIMULATED_TARGET, arguments=arguments)
    if not consumed:
        return {"outcome": "NOT_CONSUMED"}
    result = f"echo:{message}"
    store.record_event(task_id, "simulated.result", {"approval_id": approval_id, "result": result})
    # The supervisor's lifecycle requires RUNNING -> VERIFYING -> COMPLETED; no shortcut.
    store.transition(task_id, "VERIFYING", expected="RUNNING", step="simulated_echo")
    store.transition(task_id, "COMPLETED", expected="VERIFYING", step="simulated_echo")
    return {"outcome": "COMPLETED", "result": result}
