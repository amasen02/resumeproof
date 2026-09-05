from __future__ import annotations

import os
from typing import Any

from .store import CorruptReceipt, Journal, ToolLedger, ToolPreEffectError


class ApprovalRequired(RuntimeError):
    pass


class ReconciliationRequired(RuntimeError):
    pass


class RetryLimitReached(RuntimeError):
    pass


class WorkflowEngine:
    def __init__(self, journal: Journal, ledger: ToolLedger):
        self.journal = journal
        self.ledger = ledger

    def create(self, action: dict[str, Any], *, idempotency_key: str, tool_mode: str = "idempotent", max_retries: int = 3) -> str:
        return self.journal.create(action, idempotency_key, tool_mode, max_retries)

    def approve(self, workflow_id: str, *, actor: str) -> None:
        self.journal.approve(workflow_id, actor)

    def replace_action(self, workflow_id: str, action: dict[str, Any]) -> None:
        self.journal.replace_action(workflow_id, action)

    def get(self, workflow_id: str) -> dict[str, Any]:
        return self.journal.get(workflow_id)

    def timeline(self, workflow_id: str) -> list[dict[str, Any]]:
        return self.journal.timeline(workflow_id)

    def reconcile(self, workflow_id: str, *, resolution: str, actor: str) -> dict[str, Any]:
        with self.journal.execution_lock():
            return self.journal.reconcile(workflow_id, resolution, actor)

    def resume(self, workflow_id: str, *, crash_after_effect: bool = False) -> dict[str, Any]:
        with self.journal.execution_lock():
            current = self.journal.claim(workflow_id)
            status = current["status"]
            if status in {"completed", "reconciled_applied", "reconciled_not_applied"}:
                return current
            if status == "failed":
                raise RetryLimitReached(current["last_error"] or "retry limit reached")
            if status == "unknown":
                raise ReconciliationRequired(current["last_error"] or "unknown outcome")
            if current["approval_digest"] != current["action_digest"]:
                raise ApprovalRequired("approval is missing or bound to a stale action digest")
            if current.get("recovered") and current["tool_mode"] == "non_idempotent":
                reason = "prior process stopped during a non-idempotent call; outcome is unknown"
                self.journal.mark_unknown(workflow_id, reason)
                raise ReconciliationRequired(reason)
            try:
                receipt = self.ledger.apply(current["action"], current["idempotency_key"], current["tool_mode"])
            except CorruptReceipt as error:
                self.journal.mark_unknown(workflow_id, str(error))
                raise ReconciliationRequired(str(error)) from error
            except ToolPreEffectError as error:
                updated = self.journal.mark_retry(workflow_id, str(error))
                if updated["status"] == "failed":
                    raise RetryLimitReached(str(error)) from error
                return updated
            if crash_after_effect:
                os._exit(86)
            return self.journal.complete(workflow_id, receipt)
