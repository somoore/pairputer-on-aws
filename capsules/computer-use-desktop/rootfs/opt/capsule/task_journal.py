#!/usr/bin/env python3
"""Crash-safe SQLite event journal, task state, idempotency, and approvals."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from evidence import canonical_json, redact
from task_contract import ACTIVE_STATES, TaskState, assert_transition

MAX_EVENT_BYTES = 64 * 1024


class TaskJournal:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                contract_json TEXT NOT NULL,
                contract_digest TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                current_step INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                needs_json TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                task_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );
            CREATE INDEX IF NOT EXISTS events_task_seq ON events(task_id, sequence);
            CREATE TABLE IF NOT EXISTS idempotency (
                task_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                action_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                evidence_ids_json TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY(task_id, idempotency_key),
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                action_digest TEXT NOT NULL,
                human_epoch INTEGER NOT NULL,
                world_revision INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                nonce TEXT NOT NULL,
                status TEXT NOT NULL,
                token_digest TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );
            CREATE INDEX IF NOT EXISTS approvals_task ON approvals(task_id, status);
            CREATE TABLE IF NOT EXISTS runtime_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def flush(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(FULL)")

    @staticmethod
    def _event_json(payload: Any) -> str:
        encoded = canonical_json(payload)
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            clean = {
                "payload_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                "payload_bytes": len(encoded.encode()),
                "truncated": True,
            }
            encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        return encoded

    def _append(self, conn: sqlite3.Connection, task_id: str | None, event_type: str, payload: Any) -> int:
        now = time.time()
        event_id = hashlib.sha256(f"{task_id}:{event_type}:{now}:{threading.get_ident()}".encode()).hexdigest()
        cursor = conn.execute(
            "INSERT INTO events(event_id,task_id,event_type,payload_json,created_at) VALUES (?,?,?,?,?)",
            (event_id, task_id, event_type, self._event_json(payload), now),
        )
        return int(cursor.lastrowid)

    def append_event(self, task_id: str | None, event_type: str, payload: Any = None) -> int:
        with self.transaction() as conn:
            return self._append(conn, task_id, str(event_type), payload or {})

    def create_task(self, task_id: str, contract: dict[str, Any], contract_digest: str) -> None:
        now = time.time()
        encoded = self._event_json(contract)
        stored_digest = hashlib.sha256(json.dumps(json.loads(encoded), sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO tasks(task_id,state,contract_json,contract_digest,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (task_id, TaskState.QUEUED.value, encoded, stored_digest, now, now),
            )
            self._append(conn, task_id, "TASK_CREATED", {"contract_digest": stored_digest, "contract": contract})

    def replace_contract(self, task_id: str, contract: dict[str, Any], digest: str, revision: dict[str, Any]) -> None:
        encoded = self._event_json(contract)
        stored_digest = hashlib.sha256(json.dumps(json.loads(encoded), sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET contract_json=?, contract_digest=?, updated_at=? WHERE task_id=?",
                (encoded, stored_digest, time.time(), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
            self._append(conn, task_id, "CONTRACT_REVISED", {"revision": revision, "contract_digest": stored_digest})

    def transition(self, task_id: str, new_state: TaskState | str, *, reason: str = "", needs: Any = None) -> TaskState:
        new = TaskState(new_state)
        with self.transaction() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            old = TaskState(row["state"])
            if old == new:
                return old
            assert_transition(old, new)
            conn.execute(
                "UPDATE tasks SET state=?,updated_at=?,needs_json=?,last_error=? WHERE task_id=?",
                (
                    new.value, time.time(), self._event_json(needs) if needs is not None else None,
                    str(reason)[:2048] if new == TaskState.FAILED else None, task_id,
                ),
            )
            self._append(conn, task_id, "TASK_STATE_CHANGED", {"from": old.value, "to": new.value, "reason": reason, "needs": needs})
        return new

    def state(self, task_id: str) -> TaskState:
        with self._lock:
            row = self._conn.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TaskState(row["state"])

    def update_step(self, task_id: str, index: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE tasks SET current_step=?,updated_at=? WHERE task_id=?", (int(index), time.time(), task_id))

    def task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        data = dict(row)
        encoded = data.pop("contract_json")
        data["contract"] = json.loads(encoded)
        observed_digest = hashlib.sha256(json.dumps(data["contract"], sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest()
        if not _constant_time_equal(observed_digest, str(data["contract_digest"])):
            raise JournalIntegrityError("stored task contract failed its integrity check")
        data["needs"] = json.loads(data.pop("needs_json")) if data.get("needs_json") else None
        return data

    def active_tasks(self) -> tuple[dict[str, Any], ...]:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks WHERE state IN ({placeholders}) ORDER BY created_at",  # noqa: S608 - generated placeholders only
                tuple(state.value for state in ACTIVE_STATES),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def events(self, task_id: str, *, after: int = 0, limit: int = 1000) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE task_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (task_id, int(after), min(max(int(limit), 1), 5000)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return tuple(result)

    def begin_action(self, task_id: str, key: str, action_digest: str) -> str:
        """Atomically reserve an idempotency key; return the durable status."""

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT action_digest,status FROM idempotency WHERE task_id=? AND idempotency_key=?",
                (task_id, key),
            ).fetchone()
            if row:
                if row["action_digest"] != action_digest:
                    raise IdempotencyConflict("idempotency key reused for a different action")
                return str(row["status"])
            conn.execute(
                "INSERT INTO idempotency VALUES (?,?,?,?,?,?,?)",
                (task_id, key, action_digest, "PREPARED", None, None, time.time()),
            )
            self._append(conn, task_id, "ACTION_PREPARED", {"idempotency_key": key, "action_digest": action_digest})
        return "NEW"

    def mark_action(self, task_id: str, key: str, status: str, *, result: Any = None, evidence_ids: Iterable[str] = ()) -> None:
        allowed = {"PREPARED", "COMMITTED", "VERIFIED", "FAILED", "UNKNOWN_OUTCOME"}
        if status not in allowed:
            raise ValueError("invalid idempotency status")
        with self.transaction() as conn:
            row = conn.execute("SELECT status FROM idempotency WHERE task_id=? AND idempotency_key=?", (task_id, key)).fetchone()
            if row is None:
                raise KeyError(key)
            old = str(row["status"])
            permitted = {
                "PREPARED": {"COMMITTED", "FAILED", "UNKNOWN_OUTCOME"},
                "COMMITTED": {"VERIFIED", "FAILED", "UNKNOWN_OUTCOME"},
                "UNKNOWN_OUTCOME": {"COMMITTED", "VERIFIED", "FAILED"},
                "FAILED": set(), "VERIFIED": set(),
            }
            if status != old and status not in permitted[old]:
                raise IdempotencyConflict(f"invalid idempotency transition {old} -> {status}")
            conn.execute(
                "UPDATE idempotency SET status=?,result_json=?,evidence_ids_json=?,updated_at=? WHERE task_id=? AND idempotency_key=?",
                (status, self._event_json(result) if result is not None else None, json.dumps(tuple(evidence_ids)), time.time(), task_id, key),
            )
            event = {"COMMITTED": "ACTION_COMMITTED", "VERIFIED": "ACTION_VERIFIED", "FAILED": "ACTION_FAILED", "UNKNOWN_OUTCOME": "ACTION_UNKNOWN"}.get(status)
            if event:
                self._append(conn, task_id, event, {"idempotency_key": key, "result": result, "evidence_ids": tuple(evidence_ids)})

    def idempotency(self, task_id: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM idempotency WHERE task_id=? AND idempotency_key=?", (task_id, key)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
        result["evidence_ids"] = json.loads(result.pop("evidence_ids_json")) if result.get("evidence_ids_json") else []
        return result

    def store_approval_request(self, request: dict[str, Any]) -> None:
        now = time.time()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request["approval_id"], request["task_id"], request["step_id"], request["action_id"],
                    request["action_digest"], int(request["human_epoch"]), int(request["world_revision"]),
                    float(request["expires_at"]), request["nonce"], "REQUESTED", None, now, now,
                ),
            )
            self._append(conn, request["task_id"], "APPROVAL_REQUESTED", redact(request))

    def grant_approval(self, approval_id: str, token_digest: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise KeyError(approval_id)
            if row["status"] != "REQUESTED" or float(row["expires_at"]) <= time.time():
                raise ApprovalConflict("approval is not pending and current")
            conn.execute("UPDATE approvals SET status='GRANTED',token_digest=?,updated_at=? WHERE approval_id=?", (token_digest, time.time(), approval_id))
            self._append(conn, row["task_id"], "APPROVAL_GRANTED", {"approval_id": approval_id})
            return dict(row)

    def consume_approval(self, approval_id: str, token_digest: str, expected: dict[str, Any]) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None or row["status"] != "GRANTED":
                raise ApprovalConflict("approval is missing, stale, or already used")
            if float(row["expires_at"]) <= time.time():
                conn.execute("UPDATE approvals SET status='EXPIRED',updated_at=? WHERE approval_id=?", (time.time(), approval_id))
                raise ApprovalConflict("approval expired")
            for field in ("task_id", "step_id", "action_id", "action_digest", "human_epoch", "world_revision"):
                if str(row[field]) != str(expected[field]):
                    raise ApprovalConflict(f"approval binding changed: {field}")
            if not _constant_time_equal(str(row["token_digest"]), token_digest):
                raise ApprovalConflict("invalid approval token")
            conn.execute("UPDATE approvals SET status='CONSUMED',updated_at=? WHERE approval_id=?", (time.time(), approval_id))
            self._append(conn, row["task_id"], "APPROVAL_CONSUMED", {"approval_id": approval_id, "action_id": row["action_id"]})

    def expire_approvals(self, task_id: str | None = None, *, reason: str = "state_changed") -> int:
        with self.transaction() as conn:
            if task_id:
                rows = conn.execute("SELECT approval_id,task_id FROM approvals WHERE task_id=? AND status IN ('REQUESTED','GRANTED')", (task_id,)).fetchall()
            else:
                rows = conn.execute("SELECT approval_id,task_id FROM approvals WHERE status IN ('REQUESTED','GRANTED')").fetchall()
            for row in rows:
                conn.execute("UPDATE approvals SET status='EXPIRED',updated_at=? WHERE approval_id=?", (time.time(), row["approval_id"]))
                self._append(conn, row["task_id"], "APPROVAL_EXPIRED", {"approval_id": row["approval_id"], "reason": reason})
            return len(rows)

    def approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def set_meta(self, key: str, value: Any) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO runtime_meta VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, self._event_json(value), time.time()),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM runtime_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left.encode(), right.encode())


class IdempotencyConflict(RuntimeError):
    pass


class ApprovalConflict(RuntimeError):
    pass


class JournalIntegrityError(RuntimeError):
    pass
