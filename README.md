# ResumeProof

ResumeProof kills a local agent workflow between a simulated side effect and its acknowledgement, restarts it in a new process, and leaves both databases available for inspection. It demonstrates what idempotency can prevent and when a system must admit that an outcome is unknown.

All actions are synthetic. The “merchant” is a separate local SQLite database; this package has no network client and cannot make a real purchase or send a message.

## Quick start

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\resumeproof demo --output-dir demo-output
```

On macOS/Linux, use `.venv/bin/python` and `.venv/bin/resumeproof`. The one-command demo starts child processes that deliberately exit with code 86 after the tool ledger commits. It then resumes both workflows. Inspect `demo-output/receipt.json`, `timeline.txt`, `workflow-journal.sqlite3`, and `simulated-tool-ledger.sqlite3`.

The checked-in [raw receipt](demo-receipts/receipt.json) records an actual run: the idempotent workflow completed with one effect after a crash and repeated resume; the non-idempotent workflow stopped in `unknown` for human reconciliation. The [verification receipt](demo-receipts/verification.txt) records 17 passing tests in 6.75 seconds on Python 3.12.9 / Windows. Timings vary by machine.

## Fixture-driven CLI

```powershell
$id = (.venv\Scripts\resumeproof create --journal journal.sqlite3 --ledger tool.sqlite3 --action examples/charge.json --key order-42 | ConvertFrom-Json).id
.venv\Scripts\resumeproof approve --journal journal.sqlite3 --ledger tool.sqlite3 --workflow $id --actor human-reviewer
.venv\Scripts\resumeproof resume --journal journal.sqlite3 --ledger tool.sqlite3 --workflow $id
.venv\Scripts\resumeproof timeline --journal journal.sqlite3 --ledger tool.sqlite3 --workflow $id
```

An `unknown` workflow can only be closed through an explicit, audited operator decision:

```powershell
.venv\Scripts\resumeproof reconcile --journal journal.sqlite3 --ledger tool.sqlite3 --workflow $id --resolution confirmed_applied --actor operator-name
```

Every state transition is journaled. Approval stores the SHA-256 digest of canonical action JSON. Editing an unstarted action changes its digest, moves it back to `pending_approval`, and prevents execution until someone approves the new bytes. A crash-released OS lock spans claim, tool call, and acknowledgement; SQLite transactions fence each state change. Concurrent local resumptions therefore share one durable attempt count. The lock is per journal and serializes its workflows in this v0.1.

## What this proves

- A cooperating tool with a durable idempotency key can return its original receipt after the caller crashes, preventing a duplicate simulated effect.
- A non-idempotent call interrupted in the acknowledgement gap cannot safely be retried. ResumeProof records `unknown` and stops.
- Missing, altered, or action-mismatched tool receipts become reconciliation cases rather than success.
- Known failures before the effect use a persisted, bounded retry count.

It does not prove exactly-once delivery for arbitrary APIs. SQLite on one machine is not a distributed transaction, OS crash durability depends on filesystem and SQLite settings, and the demo does not model timeouts where a remote service remains unreachable. Production systems also need authentication, authorization, retention, backups, observability, and a real reconciliation workflow.

See [architecture](docs/architecture.md), [demo guide](docs/demo.md), [interview notes](docs/interview.md), and [security policy](SECURITY.md).

## Development

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

ResumeProof was built with AI assistance and independently reviewable tests and receipts. License: MIT © 2026 Ama Senevirathne.
