Retries become dangerous when a process dies after an external effect but before its acknowledgement.

`resumeproof` makes that gap reproducible offline: a child process crashes with exit code 86 after a simulated tool commit. Two separate SQLite ledgers preserve the boundary. With an idempotency key, resume returns the original receipt (`replayed: true`) and repeated resume leaves one effect. Without cooperation from the tool, recovery stops at `unknown` for explicit reconciliation.

The design also binds approval to a SHA-256 action digest, uses a per-journal OS lock across claim/call/acknowledgement, persists bounded pre-effect retries, and records an append-only timeline. The checked-in verification records 17 passing tests in 6.75s on Python 3.12.9 / Windows; the demo measured two total simulated effects. It documents exactly where local evidence ends: arbitrary APIs still need provider reconciliation.

Run: `.venv\Scripts\resumeproof demo --output-dir demo-output`

Built with AI assistance from a user-directed brief. Code, receipts, tests, and limits are inspectable. https://github.com/amasen02/resumeproof
