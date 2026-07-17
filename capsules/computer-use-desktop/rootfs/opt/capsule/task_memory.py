#!/usr/bin/env python3
"""Explicit durable task ledgers with provenance and contradiction tracking."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from evidence import canonical_json, redact_text


TRUSTED_CONTRACT_SOURCES = frozenset({"direct_human", "approved_host"})
UNTRUSTED_CONTENT_SOURCES = frozenset({
    "webpage", "document", "email", "chat_message", "terminal_output", "code_comment",
    "filename", "screenshot", "tool_output", "download",
})


class TaskMemory:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS constraints (
                entry_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                content TEXT NOT NULL, source TEXT NOT NULL, scope TEXT NOT NULL,
                conflicts_with TEXT, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS constraints_task ON constraints(task_id, revision, created_at);
            CREATE TABLE IF NOT EXISTS facts (
                entry_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, key TEXT NOT NULL,
                value_json TEXT NOT NULL, provenance TEXT NOT NULL, confidence REAL NOT NULL,
                world_revision INTEGER NOT NULL, contradiction_of TEXT, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS facts_task_key ON facts(task_id, key, created_at);
            CREATE TABLE IF NOT EXISTS artifacts (
                entry_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, path TEXT NOT NULL,
                digest TEXT NOT NULL, mime_type TEXT NOT NULL, size INTEGER NOT NULL,
                kind TEXT NOT NULL, exported INTEGER NOT NULL, world_revision INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS artifacts_task ON artifacts(task_id, path, created_at);
            CREATE TABLE IF NOT EXISTS assumptions (
                entry_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, statement TEXT NOT NULL,
                source TEXT NOT NULL, confidence REAL NOT NULL, affects_required INTEGER NOT NULL,
                affects_risky INTEGER NOT NULL, resolved INTEGER NOT NULL, resolution TEXT,
                created_at REAL NOT NULL, resolved_at REAL
            );
            CREATE INDEX IF NOT EXISTS assumptions_task ON assumptions(task_id, resolved, created_at);
            """
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_constraint(
        self, task_id: str, content: str, *, revision: int, source: str,
        scope: str = "task", conflicts_with: str | None = None,
    ) -> str:
        if source not in TRUSTED_CONTRACT_SOURCES:
            raise ProvenanceViolation("untrusted content cannot add or revise constraints")
        entry_id = f"constraint_{uuid.uuid4().hex}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO constraints VALUES (?,?,?,?,?,?,?,?)",
                (entry_id, task_id, int(revision), redact_text(content), source, redact_text(scope, limit=256), conflicts_with, time.time()),
            )
        return entry_id

    def record_fact(
        self, task_id: str, key: str, value: Any, *, provenance: str,
        confidence: float, world_revision: int, contradiction_of: str | None = None,
    ) -> str:
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be in [0,1]")
        if provenance not in UNTRUSTED_CONTENT_SOURCES:
            raise ProvenanceViolation("facts require a recognized untrusted-content provenance")
        entry_id = f"fact_{uuid.uuid4().hex}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    entry_id, task_id, redact_text(key, limit=256), canonical_json(value),
                    redact_text(provenance, limit=256), float(confidence), int(world_revision),
                    contradiction_of, time.time(),
                ),
            )
        return entry_id

    def register_artifact(
        self, task_id: str, *, path: str, digest: str, mime_type: str,
        size: int, kind: str, world_revision: int, exported: bool = False,
    ) -> str:
        entry_id = f"artifact_{uuid.uuid4().hex}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id, task_id, redact_text(path, limit=2048), digest,
                    redact_text(mime_type, limit=256), int(size), redact_text(kind, limit=128),
                    int(exported), int(world_revision), time.time(),
                ),
            )
        return entry_id

    def record_assumption(
        self, task_id: str, statement: str, *, source: str, confidence: float,
        affects_required: bool = False, affects_risky: bool = False,
    ) -> str:
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be in [0,1]")
        entry_id = f"assumption_{uuid.uuid4().hex}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO assumptions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id, task_id, redact_text(statement), redact_text(source, limit=256),
                    float(confidence), int(affects_required), int(affects_risky), 0, None,
                    time.time(), None,
                ),
            )
        return entry_id

    def resolve_assumption(self, entry_id: str, resolution: str) -> None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE assumptions SET resolved=1,resolution=?,resolved_at=? WHERE entry_id=? AND resolved=0",
                (redact_text(resolution), time.time(), entry_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(entry_id)

    def blocking_assumptions(self, task_id: str, *, threshold: float = 0.75) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM assumptions WHERE task_id=? AND resolved=0
                   AND confidence<? AND (affects_required=1 OR affects_risky=1)
                   ORDER BY created_at""",
                (task_id, float(threshold)),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def latest_facts(self, task_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT f.* FROM facts f JOIN (
                     SELECT key,MAX(created_at) created_at FROM facts WHERE task_id=? GROUP BY key
                   ) latest ON f.key=latest.key AND f.created_at=latest.created_at
                   WHERE f.task_id=? ORDER BY f.key""",
                (task_id, task_id),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["value"] = json.loads(item.pop("value_json"))
            result[item["key"]] = item
        return result

    def snapshot(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            constraints = [dict(row) for row in self._conn.execute("SELECT * FROM constraints WHERE task_id=? ORDER BY revision,created_at", (task_id,))]
            artifacts = [dict(row) for row in self._conn.execute("SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,))]
            assumptions = [dict(row) for row in self._conn.execute("SELECT * FROM assumptions WHERE task_id=? ORDER BY created_at", (task_id,))]
        return {
            "constraints": constraints,
            "facts": self.latest_facts(task_id),
            "artifacts": artifacts,
            "assumptions": assumptions,
            "digest": hashlib.sha256(canonical_json([constraints, artifacts, assumptions]).encode()).hexdigest(),
        }


class ProvenanceViolation(PermissionError):
    pass
