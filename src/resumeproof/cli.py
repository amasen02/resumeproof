from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from .core import ApprovalRequired, ReconciliationRequired, RetryLimitReached, WorkflowEngine
from .store import Journal, ToolLedger


def engine(args: argparse.Namespace) -> WorkflowEngine:
    return WorkflowEngine(Journal(args.journal), ToolLedger(args.ledger))


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def render_timeline(events: list[dict[str, Any]]) -> str:
    lines = ["SEQ  TIME                 EVENT", "---  -------------------  -----------------------"]
    lines.extend(f"{event['sequence']:>3}  {event['occurred_at']}  {event['event']}" for event in events)
    return "\n".join(lines)


def demo(output_dir: Path) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"demo requires an empty output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    journal_path = output_dir / "workflow-journal.sqlite3"
    ledger_path = output_dir / "simulated-tool-ledger.sqlite3"
    workflow = WorkflowEngine(Journal(journal_path), ToolLedger(ledger_path))
    action = {"operation": "charge", "account": "acct_demo", "amount_cents": 1250, "currency": "USD"}
    safe_id = workflow.create(action, idempotency_key="demo-order-safe", tool_mode="idempotent", max_retries=3)
    workflow.approve(safe_id, actor="demo-reviewer")
    crashed = subprocess.run(
        [sys.executable, "-m", "resumeproof.cli", "_worker", "--journal", str(journal_path),
         "--ledger", str(ledger_path), "--workflow", safe_id, "--crash-after-effect"],
        check=False,
    )
    if crashed.returncode != 86:
        raise RuntimeError(f"idempotent crash worker exited {crashed.returncode}, expected 86")
    resumed = workflow.resume(safe_id)
    repeated = workflow.resume(safe_id)
    safe_effect_count = workflow.ledger.effect_count()
    if resumed["status"] != "completed" or repeated["status"] != "completed":
        raise RuntimeError("idempotent workflow did not complete after restart")
    if not resumed["receipt"]["replayed"] or repeated["receipt"]["receipt_id"] != resumed["receipt"]["receipt_id"]:
        raise RuntimeError("idempotent workflow did not replay the original receipt")
    if safe_effect_count != 1:
        raise RuntimeError(f"idempotent workflow produced {safe_effect_count} effects, expected 1")

    unsafe_id = workflow.create(action, idempotency_key="demo-order-unsafe", tool_mode="non_idempotent", max_retries=3)
    workflow.approve(unsafe_id, actor="demo-reviewer")
    unsafe_crash = subprocess.run(
        [sys.executable, "-m", "resumeproof.cli", "_worker", "--journal", str(journal_path),
         "--ledger", str(ledger_path), "--workflow", unsafe_id, "--crash-after-effect"],
        check=False,
    )
    if unsafe_crash.returncode != 86:
        raise RuntimeError(f"non-idempotent crash worker exited {unsafe_crash.returncode}, expected 86")
    try:
        workflow.resume(unsafe_id)
    except ReconciliationRequired:
        pass
    unsafe_status = workflow.get(unsafe_id)["status"]
    total_effects = workflow.ledger.effect_count()
    if unsafe_status != "unknown":
        raise RuntimeError(f"non-idempotent workflow ended in {unsafe_status}, expected unknown")
    if total_effects != 2:
        raise RuntimeError(f"demo produced {total_effects} total effects, expected 2")
    result = {
        "demo": "resumeproof-v0.1",
        "simulated_only": True,
        "idempotent": {
            "workflow_id": safe_id,
            "crash_exit_code": crashed.returncode,
            "status": resumed["status"],
            "effect_count_after_repeated_resume": safe_effect_count,
            "receipt_replayed": resumed["receipt"]["replayed"],
            "same_receipt_on_repeated_resume": repeated["receipt"]["receipt_id"] == resumed["receipt"]["receipt_id"],
            "timeline": workflow.timeline(safe_id),
        },
        "non_idempotent": {
            "workflow_id": unsafe_id,
            "crash_exit_code": unsafe_crash.returncode,
            "status": unsafe_status,
            "timeline": workflow.timeline(unsafe_id),
        },
        "total_simulated_effects": total_effects,
    }
    (output_dir / "receipt.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["ResumeProof replay", "=================="]
    for label in ("idempotent", "non_idempotent"):
        lines.append(f"\n{label} workflow: {result[label]['status']}")
        lines.extend(f"  {event['sequence']:>3}  {event['occurred_at']}  {event['event']}" for event in result[label]["timeline"])
    (output_dir / "timeline.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--journal", type=Path, required=True)
    shared.add_argument("--ledger", type=Path, required=True)
    root = argparse.ArgumentParser(prog="resumeproof")
    commands = root.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", parents=[shared])
    create.add_argument("--action", type=Path, required=True)
    create.add_argument("--key", required=True)
    create.add_argument("--mode", choices=["idempotent", "non_idempotent"], default="idempotent")
    create.add_argument("--max-retries", type=int, default=3)
    approve = commands.add_parser("approve", parents=[shared])
    approve.add_argument("--workflow", required=True)
    approve.add_argument("--actor", required=True)
    resume = commands.add_parser("resume", parents=[shared])
    resume.add_argument("--workflow", required=True)
    reconcile = commands.add_parser("reconcile", parents=[shared])
    reconcile.add_argument("--workflow", required=True)
    reconcile.add_argument("--resolution", choices=["confirmed_applied", "confirmed_not_applied"], required=True)
    reconcile.add_argument("--actor", required=True)
    timeline = commands.add_parser("timeline", parents=[shared])
    timeline.add_argument("--workflow", required=True)
    timeline.add_argument("--format", choices=["text", "json"], default="text")
    worker = commands.add_parser("_worker", parents=[shared])
    worker.add_argument("--workflow", required=True)
    worker.add_argument("--crash-after-effect", action="store_true")
    demo_cmd = commands.add_parser("demo")
    demo_cmd.add_argument("--output-dir", type=Path, default=Path("demo-output"))
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            emit(demo(args.output_dir))
            return 0
        workflow = engine(args)
        if args.command == "create":
            workflow_id = workflow.create(json.loads(args.action.read_text(encoding="utf-8")), idempotency_key=args.key, tool_mode=args.mode, max_retries=args.max_retries)
            emit(workflow.get(workflow_id))
        elif args.command == "approve":
            workflow.approve(args.workflow, actor=args.actor)
            emit(workflow.get(args.workflow))
        elif args.command in {"resume", "_worker"}:
            emit(workflow.resume(args.workflow, crash_after_effect=getattr(args, "crash_after_effect", False)))
        elif args.command == "reconcile":
            emit(workflow.reconcile(args.workflow, resolution=args.resolution, actor=args.actor))
        elif args.command == "timeline":
            events = workflow.timeline(args.workflow)
            print(render_timeline(events)) if args.format == "text" else emit(events)
        return 0
    except (ApprovalRequired, ReconciliationRequired, RetryLimitReached, KeyError, ValueError, RuntimeError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
