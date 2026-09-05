from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    encoded = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CorruptReceipt(RuntimeError):
    pass


class ToolPreEffectError(RuntimeError):
    pass


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def execution_lock(self) -> Iterator[None]:
        """Serialize local execution across the call; process death releases the lock."""
        lock_path = self.path.with_name(self.path.name + ".resumeproof.lock")
        with lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _initialize(self) -> None:
        with closing(self.connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    action_json TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    tool_mode TEXT NOT NULL CHECK(tool_mode IN ('idempotent','non_idempotent')),
                    status TEXT NOT NULL,
                    approval_digest TEXT,
                    approved_by TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL,
                    receipt_json TEXT,
                    receipt_digest TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    event TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def create(self, action: dict[str, Any], key: str, mode: str, max_retries: int) -> str:
        if mode not in {"idempotent", "non_idempotent"}:
            raise ValueError("tool_mode must be idempotent or non_idempotent")
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        workflow_id = str(uuid.uuid4())
        action_text = canonical_json(action)
        action_digest = digest(action_text)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO workflows(id,action_json,action_digest,idempotency_key,tool_mode,status,max_retries) VALUES(?,?,?,?,?,'pending_approval',?)",
                (workflow_id, action_text, action_digest, key, mode, max_retries),
            )
            self._event(db, workflow_id, "created", {"action_digest": action_digest})
            db.commit()
        return workflow_id

    @staticmethod
    def _event(db: sqlite3.Connection, workflow_id: str, event: str, detail: dict[str, Any] | None = None) -> None:
        db.execute(
            "INSERT INTO events(workflow_id,event,detail_json) VALUES(?,?,?)",
            (workflow_id, event, canonical_json(detail or {})),
        )

    def get(self, workflow_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise KeyError(f"workflow not found: {workflow_id}")
        return self._decode(dict(row))

    def timeline(self, workflow_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT sequence,event,detail_json,occurred_at FROM events WHERE workflow_id=? ORDER BY sequence",
                (workflow_id,),
            ).fetchall()
        return [
            {"sequence": row["sequence"], "event": row["event"], "detail": json.loads(row["detail_json"]), "occurred_at": row["occurred_at"]}
            for row in rows
        ]

    def approve(self, workflow_id: str, actor: str) -> None:
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT action_digest,status FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"workflow not found: {workflow_id}")
            if row["status"] != "pending_approval":
                db.rollback()
                raise ValueError(f"workflow is not pending approval: {row['status']}")
            db.execute(
                "UPDATE workflows SET approval_digest=action_digest,approved_by=?,status='approved',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (actor, workflow_id),
            )
            self._event(db, workflow_id, "approved", {"actor": actor, "action_digest": row["action_digest"]})
            db.commit()

    def replace_action(self, workflow_id: str, action: dict[str, Any]) -> None:
        action_text = canonical_json(action)
        action_digest = digest(action_text)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None:
                db.rollback()
                raise KeyError(f"workflow not found: {workflow_id}")
            if row["status"] not in {"pending_approval", "approved"}:
                db.rollback()
                raise ValueError(f"cannot replace action after execution starts: {row['status']}")
            db.execute(
                "UPDATE workflows SET action_json=?,action_digest=?,status='pending_approval',approval_digest=NULL,approved_by=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (action_text, action_digest, workflow_id),
            )
            self._event(db, workflow_id, "approval_invalidated", {"action_digest": action_digest})
            db.commit()

    def claim(self, workflow_id: str) -> dict[str, Any]:
        recovered = False
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None:
                db.rollback()
                raise KeyError(f"workflow not found: {workflow_id}")
            current = dict(row)
            if current["status"] == "completed":
                receipt_text = current["receipt_json"]
                if not receipt_text or digest(receipt_text) != current["receipt_digest"]:
                    reason = "completed journal receipt is missing or its digest is invalid"
                    db.execute("UPDATE workflows SET status='unknown',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (reason, workflow_id))
                    self._event(db, workflow_id, "reconciliation_required", {"reason": reason})
                    current["status"] = "unknown"
                    current["last_error"] = reason
                db.commit()
                return self._decode(current) | {"recovered": False}
            if current["status"] in {"failed", "unknown", "reconciled_applied", "reconciled_not_applied"}:
                db.commit()
                return self._decode(current) | {"recovered": False}
            if current["approval_digest"] != current["action_digest"]:
                db.commit()
                return self._decode(current) | {"recovered": False}
            if current["status"] == "running":
                recovered = True
                self._event(db, workflow_id, "recovery_detected", {"tool_mode": current["tool_mode"]})
            else:
                attempts = current["attempts"] + 1
                db.execute(
                    "UPDATE workflows SET status='running',attempts=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (attempts, workflow_id),
                )
                self._event(db, workflow_id, "attempt_started", {"attempt": attempts})
            row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            db.commit()
        return self._decode(dict(row)) | {"recovered": recovered}

    def mark_retry(self, workflow_id: str, error: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT attempts,max_retries,status FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None or row["status"] != "running":
                db.rollback()
                raise ValueError("retry transition requires a running workflow")
            terminal = row["attempts"] >= row["max_retries"]
            status = "failed" if terminal else "retry_wait"
            db.execute("UPDATE workflows SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, error, workflow_id))
            self._event(db, workflow_id, "retry_exhausted" if terminal else "retry_scheduled", {"error": error})
            db.commit()
        return self.get(workflow_id)

    def complete(self, workflow_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        receipt_text = canonical_json(receipt)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE workflows SET status='completed',receipt_json=?,receipt_digest=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",
                (receipt_text, digest(receipt_text), workflow_id),
            )
            if changed.rowcount != 1:
                db.rollback()
                raise ValueError("completion transition requires a running workflow")
            self._event(db, workflow_id, "acknowledged", {"receipt_id": receipt["receipt_id"], "replayed": receipt["replayed"]})
            db.commit()
        return self.get(workflow_id)

    def mark_unknown(self, workflow_id: str, reason: str) -> None:
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE workflows SET status='unknown',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'", (reason, workflow_id))
            if changed.rowcount != 1:
                db.rollback()
                raise ValueError("unknown transition requires a running workflow")
            self._event(db, workflow_id, "reconciliation_required", {"reason": reason})
            db.commit()

    def reconcile(self, workflow_id: str, resolution: str, actor: str) -> dict[str, Any]:
        if resolution not in {"confirmed_applied", "confirmed_not_applied"}:
            raise ValueError("resolution must be confirmed_applied or confirmed_not_applied")
        status = "reconciled_applied" if resolution == "confirmed_applied" else "reconciled_not_applied"
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None:
                db.rollback()
                raise KeyError(f"workflow not found: {workflow_id}")
            if row["status"] != "unknown":
                db.rollback()
                raise ValueError(f"only an unknown workflow can be reconciled: {row['status']}")
            db.execute("UPDATE workflows SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, workflow_id))
            self._event(db, workflow_id, "reconciled", {"actor": actor, "resolution": resolution})
            db.commit()
        return self.get(workflow_id)

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["action"] = json.loads(value.pop("action_json"))
        try:
            value["receipt"] = json.loads(value["receipt_json"]) if value["receipt_json"] else None
        except json.JSONDecodeError:
            value["receipt"] = None
        value.pop("receipt_json")
        return value


class ToolLedger:
    """A simulated external system in a physically separate SQLite database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS effects (
                    receipt_id TEXT PRIMARY KEY,
                    idempotency_key TEXT,
                    action_digest TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_effect_per_key ON effects(idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS invocations (
                    invocation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT,
                    receipt_id TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                """
            )

    def set_failures_before_effect(self, count: int) -> None:
        with closing(self.connect()) as db:
            db.execute("INSERT OR REPLACE INTO config(key,value) VALUES('failures_before_effect',?)", (count,))

    def _existing(self, db: sqlite3.Connection, key: str, action_digest: str) -> dict[str, Any] | None:
        invocation = db.execute(
            "SELECT receipt_id,action_digest FROM invocations WHERE idempotency_key=? ORDER BY created_at LIMIT 1", (key,)
        ).fetchone()
        if invocation is None:
            return None
        if invocation["action_digest"] != action_digest:
            raise CorruptReceipt("idempotency key was already used for a different action")
        row = db.execute("SELECT * FROM effects WHERE receipt_id=?", (invocation["receipt_id"],)).fetchone()
        if row is None:
            raise CorruptReceipt("tool invocation exists but its receipt is missing")
        if digest(row["receipt_json"]) != row["receipt_digest"]:
            raise CorruptReceipt("tool receipt digest does not match stored receipt")
        receipt = json.loads(row["receipt_json"])
        if receipt["action_digest"] != invocation["action_digest"]:
            raise CorruptReceipt("tool invocation and receipt disagree")
        receipt["replayed"] = True
        return receipt

    def apply(self, action: dict[str, Any], key: str, mode: str) -> dict[str, Any]:
        action_text = canonical_json(action)
        action_digest = digest(action_text)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if mode == "idempotent":
                existing = self._existing(db, key, action_digest)
                if existing is not None:
                    db.commit()
                    return existing
            config = db.execute("SELECT value FROM config WHERE key='failures_before_effect'").fetchone()
            if config and config["value"] > 0:
                db.execute("UPDATE config SET value=value-1 WHERE key='failures_before_effect'")
                db.commit()
                raise ToolPreEffectError("simulated failure before side effect")
            receipt_id = str(uuid.uuid4())
            receipt = {
                "receipt_id": receipt_id,
                "action_digest": action_digest,
                "simulated": True,
                "replayed": False,
            }
            receipt_text = canonical_json(receipt)
            stored_key = key if mode == "idempotent" else None
            db.execute(
                "INSERT INTO effects(receipt_id,idempotency_key,action_digest,action_json,receipt_json,receipt_digest) VALUES(?,?,?,?,?,?)",
                (receipt_id, stored_key, action_digest, action_text, receipt_text, digest(receipt_text)),
            )
            db.execute(
                "INSERT INTO invocations(invocation_id,idempotency_key,receipt_id,action_digest) VALUES(?,?,?,?)",
                (str(uuid.uuid4()), stored_key, receipt_id, action_digest),
            )
            db.commit()
            return receipt

    def effect_count(self) -> int:
        with closing(self.connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM effects").fetchone()[0])

    def receipt_for_key(self, key: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            invocation = db.execute("SELECT receipt_id,action_digest FROM invocations WHERE idempotency_key=?", (key,)).fetchone()
            if invocation is None:
                raise KeyError(f"receipt not found for key: {key}")
            receipt = self._existing(db, key, invocation["action_digest"])
            assert receipt is not None
            return receipt

    def delete_receipt(self, receipt_id: str) -> None:
        with closing(self.connect()) as db:
            db.execute("DELETE FROM effects WHERE receipt_id=?", (receipt_id,))

    def corrupt_receipt(self, receipt_id: str) -> None:
        with closing(self.connect()) as db:
            db.execute("UPDATE effects SET receipt_json='{}' WHERE receipt_id=?", (receipt_id,))
