from __future__ import annotations

from typing import Any, Literal, TypedDict

ToolMode = Literal["idempotent", "non_idempotent"]
WorkflowStatus = Literal[
    "pending_approval", "approved", "running", "retry_wait", "unknown", "failed", "completed",
    "reconciled_applied", "reconciled_not_applied"
]


class Receipt(TypedDict):
    receipt_id: str
    action_digest: str
    simulated: Literal[True]
    replayed: bool


class TimelineEvent(TypedDict):
    sequence: int
    event: str
    detail: dict[str, Any]
    occurred_at: str
