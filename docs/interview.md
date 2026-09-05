# Interview notes

## Why two databases?

A single transaction across the journal and effect table would erase the acknowledgement gap the project is meant to demonstrate. Real APIs do not enlist in an agent’s SQLite transaction. Two files make the ordering and failure explicit.

## Is this exactly once?

No. The idempotent path produces one simulated effect because the tool cooperates: it durably enforces a unique key and returns the original receipt. An arbitrary API without that contract can execute zero, one, or multiple times around timeouts and crashes.

## Why bind approval to a digest?

Approving only a workflow ID permits the payload to change after review. ResumeProof canonicalizes JSON, stores its SHA-256 digest with approval, and checks equality at claim time. Replacing the action invalidates approval.

## Why does a running non-idempotent workflow become unknown?

After restart there is no trustworthy observation that distinguishes “the process died before the request arrived” from “the effect committed and the response was lost.” Retrying could duplicate the effect; declaring failure could hide a successful effect. `unknown` preserves both possibilities for reconciliation.

## What does the concurrency test establish?

Eight threads resume one approved workflow against real SQLite files. They converge on one attempt and one effect. This exercises the database boundary and unique-key behavior instead of mocking calls. It is a local concurrency result, not a multi-host linearizability claim.

## What would production add?

Use the external provider’s idempotency and receipt lookup APIs, authenticated approvers, structured authorization policy, lease ownership and heartbeats for long calls, backoff scheduling, encryption and retention controls, replicated storage, metrics, and an operator reconciliation queue. Receipt signatures or provider-authenticated lookups are stronger than a local hash against local tampering.
