from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from resumeproof.core import ApprovalRequired, ReconciliationRequired, RetryLimitReached, WorkflowEngine
from resumeproof.cli import demo
from resumeproof.store import Journal, ToolLedger


@pytest.fixture
def engine(tmp_path: Path) -> WorkflowEngine:
    return WorkflowEngine(Journal(tmp_path / "journal.sqlite3"), ToolLedger(tmp_path / "tool.sqlite3"))


def approved(engine: WorkflowEngine, *, mode: str = "idempotent", retries: int = 2) -> str:
    workflow_id = engine.create(
        {"operation": "charge", "account": "acct_fixture", "amount_cents": 1250},
        idempotency_key="order-fixture-1",
        tool_mode=mode,
        max_retries=retries,
    )
    engine.approve(workflow_id, actor="fixture-reviewer")
    return workflow_id


def test_approval_is_bound_to_canonical_action_digest(engine: WorkflowEngine) -> None:
    workflow_id = approved(engine)
    engine.replace_action(workflow_id, {"operation": "charge", "account": "acct_fixture", "amount_cents": 9900})

    with pytest.raises(ApprovalRequired, match="digest"):
        engine.resume(workflow_id)

    timeline = engine.timeline(workflow_id)
    assert timeline[-1]["event"] == "approval_invalidated"


def test_idempotent_resume_after_real_process_crash_does_not_duplicate(tmp_path: Path) -> None:
    journal_path = tmp_path / "journal.sqlite3"
    ledger_path = tmp_path / "tool.sqlite3"
    engine = WorkflowEngine(Journal(journal_path), ToolLedger(ledger_path))
    workflow_id = approved(engine)

    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "resumeproof.cli",
            "_worker",
            "--journal",
            str(journal_path),
            "--ledger",
            str(ledger_path),
            "--workflow",
            workflow_id,
            "--crash-after-effect",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 86
    assert engine.ledger.effect_count() == 1
    assert engine.get(workflow_id)["status"] == "running"

    result = engine.resume(workflow_id)
    assert result["status"] == "completed"
    assert engine.ledger.effect_count() == 1
    assert result["receipt"]["replayed"] is True

    repeated = engine.resume(workflow_id)
    assert repeated["status"] == "completed"
    assert repeated["receipt"]["receipt_id"] == result["receipt"]["receipt_id"]
    assert engine.ledger.effect_count() == 1


def test_reusing_an_idempotency_key_for_a_different_action_is_rejected(engine: WorkflowEngine) -> None:
    first_id = approved(engine)
    engine.resume(first_id)
    second_id = engine.create(
        {"operation": "charge", "account": "acct_fixture", "amount_cents": 7777},
        idempotency_key="order-fixture-1",
        tool_mode="idempotent",
        max_retries=2,
    )
    engine.approve(second_id, actor="fixture-reviewer")

    with pytest.raises(ReconciliationRequired, match="different action"):
        engine.resume(second_id)
    assert engine.ledger.effect_count() == 1


def test_non_idempotent_crash_becomes_unknown_and_requires_reconciliation(tmp_path: Path) -> None:
    journal_path = tmp_path / "journal.sqlite3"
    ledger_path = tmp_path / "tool.sqlite3"
    engine = WorkflowEngine(Journal(journal_path), ToolLedger(ledger_path))
    workflow_id = approved(engine, mode="non_idempotent")

    crashed = subprocess.run(
        [sys.executable, "-m", "resumeproof.cli", "_worker", "--journal", str(journal_path),
         "--ledger", str(ledger_path), "--workflow", workflow_id, "--crash-after-effect"],
        check=False,
    )
    assert crashed.returncode == 86
    assert engine.ledger.effect_count() == 1

    with pytest.raises(ReconciliationRequired):
        engine.resume(workflow_id)
    assert engine.get(workflow_id)["status"] == "unknown"
    assert engine.ledger.effect_count() == 1


def test_concurrent_resumes_claim_one_attempt_and_one_effect(engine: WorkflowEngine) -> None:
    workflow_id = approved(engine)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: engine.resume(workflow_id), range(8)))

    assert all(outcome["status"] == "completed" for outcome in outcomes)
    assert engine.ledger.effect_count() == 1
    assert engine.get(workflow_id)["attempts"] == 1


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_ledger_receipt_stops_for_reconciliation(engine: WorkflowEngine, damage: str) -> None:
    workflow_id = approved(engine)
    crashed = subprocess.run(
        [sys.executable, "-m", "resumeproof.cli", "_worker", "--journal", str(engine.journal.path),
         "--ledger", str(engine.ledger.path), "--workflow", workflow_id, "--crash-after-effect"],
        check=False,
    )
    assert crashed.returncode == 86
    receipt = engine.ledger.receipt_for_key("order-fixture-1")
    if damage == "missing":
        engine.ledger.delete_receipt(receipt["receipt_id"])
    else:
        engine.ledger.corrupt_receipt(receipt["receipt_id"])

    with pytest.raises(ReconciliationRequired):
        engine.resume(workflow_id)
    assert engine.get(workflow_id)["status"] == "unknown"


def test_completed_journal_receipt_is_revalidated(engine: WorkflowEngine) -> None:
    workflow_id = approved(engine)
    engine.resume(workflow_id)
    with engine.journal.connect() as db:
        db.execute("UPDATE workflows SET receipt_json='{}' WHERE id=?", (workflow_id,))

    with pytest.raises(ReconciliationRequired, match="journal receipt"):
        engine.resume(workflow_id)
    assert engine.get(workflow_id)["status"] == "unknown"


@pytest.mark.parametrize("terminal", ["completed", "failed", "unknown"])
def test_approval_cannot_reopen_terminal_or_unknown_state(engine: WorkflowEngine, terminal: str) -> None:
    workflow_id = approved(engine, retries=1)
    if terminal == "completed":
        engine.resume(workflow_id)
    elif terminal == "failed":
        engine.ledger.set_failures_before_effect(1)
        with pytest.raises(RetryLimitReached):
            engine.resume(workflow_id)
    else:
        other = approved(engine, mode="non_idempotent")
        crashed = subprocess.run(
            [sys.executable, "-m", "resumeproof.cli", "_worker", "--journal", str(engine.journal.path),
             "--ledger", str(engine.ledger.path), "--workflow", other, "--crash-after-effect"],
            check=False,
        )
        assert crashed.returncode == 86
        with pytest.raises(ReconciliationRequired):
            engine.resume(other)
        workflow_id = other

    with pytest.raises(ValueError, match="pending approval"):
        engine.approve(workflow_id, actor="second-reviewer")
    assert engine.get(workflow_id)["status"] == terminal


def test_action_cannot_change_after_execution_starts(engine: WorkflowEngine) -> None:
    workflow_id = approved(engine)
    engine.resume(workflow_id)

    with pytest.raises(ValueError, match="cannot replace"):
        engine.replace_action(workflow_id, {"operation": "charge", "amount_cents": 1})


def test_action_cannot_change_while_crashed_attempt_is_running(engine: WorkflowEngine) -> None:
    workflow_id = approved(engine)
    crashed = subprocess.run(
        [sys.executable, "-m", "resumeproof.cli", "_worker", "--journal", str(engine.journal.path),
         "--ledger", str(engine.ledger.path), "--workflow", workflow_id, "--crash-after-effect"],
        check=False,
    )
    assert crashed.returncode == 86

    with pytest.raises(ValueError, match="cannot replace"):
        engine.replace_action(workflow_id, {"operation": "charge", "amount_cents": 1})


def test_unknown_outcome_has_explicit_terminal_reconciliation(engine: WorkflowEngine) -> None:
    workflow_id = approved(engine, mode="non_idempotent")
    crashed = subprocess.run(
        [sys.executable, "-m", "resumeproof.cli", "_worker", "--journal", str(engine.journal.path),
         "--ledger", str(engine.ledger.path), "--workflow", workflow_id, "--crash-after-effect"],
        check=False,
    )
    assert crashed.returncode == 86
    with pytest.raises(ReconciliationRequired):
        engine.resume(workflow_id)

    resolved = engine.reconcile(workflow_id, resolution="confirmed_applied", actor="operator")
    assert resolved["status"] == "reconciled_applied"
    with pytest.raises(ValueError, match="unknown"):
        engine.reconcile(workflow_id, resolution="confirmed_not_applied", actor="operator")


def test_retry_cap_is_durable_and_bounded(engine: WorkflowEngine) -> None:
    workflow_id = approved(engine, retries=2)
    engine.ledger.set_failures_before_effect(2)

    first = engine.resume(workflow_id)
    assert first["status"] == "retry_wait"
    with pytest.raises(RetryLimitReached):
        engine.resume(workflow_id)

    row = engine.get(workflow_id)
    assert row["attempts"] == 2
    assert row["status"] == "failed"
    assert engine.ledger.effect_count() == 0


def test_cli_accepts_action_fixture_and_emits_json(tmp_path: Path) -> None:
    action = tmp_path / "action.json"
    action.write_text(json.dumps({"operation": "notify", "target": "fixture-user"}), encoding="utf-8")
    journal = tmp_path / "journal.sqlite3"
    ledger = tmp_path / "tool.sqlite3"

    created = subprocess.run(
        [sys.executable, "-m", "resumeproof.cli", "create", "--journal", str(journal),
         "--ledger", str(ledger), "--action", str(action), "--key", "fixture-notify"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(created.stdout)
    assert payload["status"] == "pending_approval"
    assert len(payload["action_digest"]) == 64


def test_demo_measures_invariants_and_refuses_to_overwrite_receipts(tmp_path: Path) -> None:
    output = tmp_path / "run"
    result = demo(output)
    assert result["idempotent"]["effect_count_after_repeated_resume"] == 1
    assert result["total_simulated_effects"] == 2
    assert json.loads((output / "receipt.json").read_text(encoding="utf-8")) == result

    with pytest.raises(ValueError, match="empty output directory"):
        demo(output)
