# Kill a workflow between the effect and its acknowledgement

Retries are comforting until the process dies at the worst possible moment: after an external action has happened, but before the caller records the acknowledgement. A restarted agent sees a durable `running` state and has to choose between retrying, which might duplicate the action, and stopping, which might leave work unfinished.

`resumeproof` is a small, offline lab for making that decision visible. It uses a workflow journal and a simulated tool ledger in two separate SQLite files. The tool ledger stands in for a merchant or messaging provider, but there is no network client, real purchase, or social message. The demo deliberately kills a child process with exit code 86 after the tool ledger commits and before the journal acknowledges the result.

## Run the crash

Python 3.11 or newer is required. From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\resumeproof demo --output-dir demo-output
```

Use `.venv/bin/python` and `.venv/bin/resumeproof` on macOS or Linux. The output directory must be new or empty; the CLI refuses to overwrite evidence. Open `demo-output/receipt.json` and `timeline.txt`, then inspect `workflow-journal.sqlite3` and `simulated-tool-ledger.sqlite3` independently.

The checked-in receipt records two workflows. Both child processes exit 86. The idempotent workflow resumes with the same key, receives the original receipt marked `replayed: true`, and remains at one simulated effect even after repeated resume. The non-idempotent workflow cannot establish whether its effect happened before the crash, so recovery records `unknown` and requires reconciliation. Across both examples the measured total is two simulated effects: one safe replay and one deliberately ambiguous call.

That is the failure worth studying. A successful HTTP response, or a process that exits cleanly, does not describe what happened in the gap between the provider's commit and the caller's acknowledgement. The journal records the orchestrator's state; the tool ledger records the tool's durable fact. They never share a transaction, which keeps the boundary visible instead of pretending a local database transaction can include an arbitrary API.

## What the design makes explicit

Approval is bound to the canonical action bytes. `resumeproof` stores a SHA-256 action digest when a workflow is created and copies that digest into the approval record. Replacing an action invalidates approval and returns the workflow to `pending_approval`. Execution therefore cannot quietly use a payload different from the one a human reviewed.

The two stores have different responsibilities. The journal owns workflow identity, status, attempts, approval, acknowledgement, and an append-only event timeline. The tool ledger owns effects, invocations, idempotency keys, receipts, and receipt hashes. SQLite `BEGIN IMMEDIATE` transactions fence state transitions. A per-journal OS file lock spans claim, tool call, and acknowledgement; process death releases it. This v0.1 serializes workflows sharing one journal, which is a local coordination boundary rather than a multi-host lease protocol.

Known failures before an effect are retryable. The first attempt counts toward `max_retries`, and the cap is durable. Ambiguous failures do not consume retries because another attempt could make the uncertainty worse. Missing or corrupted tool receipts also stop for reconciliation. An operator must explicitly choose `confirmed_applied` or `confirmed_not_applied`; the actor and decision are appended to the timeline. Actor labels are audit data in this version, not authentication.

The idempotent result depends on cooperation from the tool. The ledger enforces one effect per non-null key and can return the original receipt. An arbitrary external API may have no equivalent key or receipt lookup. ResumeProof cannot infer whether such a request reached the provider, cannot guarantee exactly-once delivery, and cannot turn a local hash into a provider-authenticated fact. Production work would add authenticated approval, provider lookup, leases or heartbeats, backoff, retention and backup policy, observability, and a real reconciliation queue.

## Inspectable evidence

The repository's verification receipt records 17 passing tests in 6.75 seconds on Windows with Python 3.12.9. It also records successful source compilation, a wheel build with SHA-256 `78c98eac84806ff44c6b7c8a83fd6455b05d6879a2b63f2487bf143a6c616cc9`, the crash demo, and a standalone create/approve/resume/timeline flow. Docker was not run because the Docker CLI was unavailable on the verification machine. Timings and local tool availability can vary.

Try the fixture flow yourself:

```powershell
$id = (.venv\Scripts\resumeproof create --journal journal.sqlite3 --ledger tool.sqlite3 --action examples/charge.json --key order-42 | ConvertFrom-Json).id
.venv\Scripts\resumeproof approve --journal journal.sqlite3 --ledger tool.sqlite3 --workflow $id --actor human-reviewer
.venv\Scripts\resumeproof resume --journal journal.sqlite3 --ledger tool.sqlite3 --workflow $id
.venv\Scripts\resumeproof timeline --journal journal.sqlite3 --ledger tool.sqlite3 --workflow $id
```

For an owner exercise, make a fresh output directory, run the demo, and predict the final status and effect counts before opening the receipt. Then change the action fixture and repeat the approval flow. Finally, take an `unknown` workflow and reconcile it twice with different resolutions; observe that only the first explicit decision is accepted. Explain which fact came from the journal, which came from the tool ledger, and which conclusion still required a human.

This project was built with AI assistance from a user-directed brief and an agent-developed plan. The implementation, tests, receipts, and limitations are available for inspection. The exercise is part of the ownership check: being able to explain the crash boundary matters more than repeating a claim about “exactly once.”

Repository: https://github.com/amasen02/resumeproof
