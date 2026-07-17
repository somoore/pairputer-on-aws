#!/usr/bin/env python3
"""Bounded, redacted evidence records and the success gate.

Evidence is deliberately metadata-first.  Large screenshots, documents, and
terminal transcripts belong in separately governed artifact stores; the task
journal keeps only a digest, a reference, and the predicate that was observed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

MAX_STRING = 4096
MAX_COLLECTION = 128

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+"),
    re.compile(r"(?i)\b((?:api[_-]?key|x-api-key|access[_-]?token|refresh[_-]?token|client[_-]?secret(?:_value)?|password|aws_secret_access_key)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_SENSITIVE_KEYS = {
    "authorization", "cookie", "set-cookie", "password", "passwd", "secret",
    "token", "access_token", "refresh_token", "api_key", "apikey", "private_key",
    "clipboard", "credential", "credentials",
}


def _sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if normalized in _SENSITIVE_KEYS:
        return True
    return bool(re.search(
        r"(?:^|_)(?:authorization|cookie|password|passwd|secret|token|api_key|apikey|private_key|credential|credentials|access_key)(?:_|$)",
        normalized,
    ))


def redact_text(value: str, *, limit: int = MAX_STRING) -> str:
    """Remove common credential shapes and bound attacker-controlled text."""

    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", text)
    if len(text) > limit:
        text = text[:limit] + f"...[truncated {len(text) - limit} chars]"
    return text


def redact(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact and size-bound data before it enters durable logs."""

    if depth > 8:
        return "[TRUNCATED_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_COLLECTION:
                result["_truncated"] = len(value) - MAX_COLLECTION
                break
            clean_key = redact_text(str(key), limit=256)
            result[clean_key] = "[REDACTED]" if _sensitive_key(clean_key) else redact(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        clean = [redact(item, depth=depth + 1) for item in items[:MAX_COLLECTION]]
        if len(items) > MAX_COLLECTION:
            clean.append(f"[TRUNCATED {len(items) - MAX_COLLECTION} ITEMS]")
        return clean
    if hasattr(value, "__dataclass_fields__"):
        return redact(asdict(value), depth=depth + 1)
    return redact_text(repr(value))


def canonical_json(value: Any) -> str:
    return json.dumps(redact(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    task_id: str
    step_id: str
    predicate: str
    kind: str
    reference: str
    digest: str
    observed_world_revision: int
    verified: bool
    summary: str
    created_at: float


class EvidenceStore:
    """SQLite-backed evidence metadata store."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                kind TEXT NOT NULL,
                reference TEXT NOT NULL,
                digest TEXT NOT NULL,
                observed_world_revision INTEGER NOT NULL,
                verified INTEGER NOT NULL CHECK (verified IN (0,1)),
                summary TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS evidence_task ON evidence(task_id, predicate, created_at)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record(
        self,
        *,
        task_id: str,
        step_id: str,
        predicate: str,
        kind: str,
        observed: Any,
        world_revision: int,
        verified: bool,
        reference: str = "inline:metadata",
        summary: str = "",
    ) -> Evidence:
        clean = canonical_json(observed)
        item = Evidence(
            evidence_id=f"ev_{uuid.uuid4().hex}",
            task_id=task_id,
            step_id=step_id,
            predicate=redact_text(predicate, limit=512),
            kind=redact_text(kind, limit=128),
            reference=redact_text(reference, limit=1024),
            digest=hashlib.sha256(clean.encode("utf-8")).hexdigest(),
            observed_world_revision=int(world_revision),
            verified=bool(verified),
            summary=redact_text(summary or clean, limit=1024),
            created_at=time.time(),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item.evidence_id, item.task_id, item.step_id, item.predicate, item.kind,
                    item.reference, item.digest, item.observed_world_revision,
                    int(item.verified), item.summary, item.created_at,
                ),
            )
        return item

    def list_for_task(self, task_id: str) -> tuple[Evidence, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM evidence WHERE task_id=? ORDER BY created_at, evidence_id", (task_id,)
            ).fetchall()
        return tuple(Evidence(**{**dict(row), "verified": bool(row["verified"])}) for row in rows)

    def has_verified(
        self, task_id: str, predicate: str, *, minimum_world_revision: int | None = None,
        required_references: Iterable[str] | None = None,
    ) -> bool:
        references = tuple(dict.fromkeys(str(item) for item in (required_references or ())))
        if references:
            # Every plan-bound assertion is a required verifier, not an
            # alternative. Query the latest proof independently for each exact
            # spec digest so a later weaker assertion cannot mask a failure.
            with self._lock:
                for reference in references:
                    query = ("SELECT verified FROM evidence WHERE task_id=? AND predicate=? "
                             "AND reference=?")
                    parameters: list[Any] = [task_id, predicate, reference]
                    if minimum_world_revision is not None:
                        query += " AND observed_world_revision>=?"
                        parameters.append(int(minimum_world_revision))
                    query += " ORDER BY created_at DESC, evidence_id DESC LIMIT 1"
                    row = self._conn.execute(query, tuple(parameters)).fetchone()
                    if row is None or not bool(row["verified"]):
                        return False
            return True
        query = "SELECT verified FROM evidence WHERE task_id=? AND predicate=?"
        parameters = [task_id, predicate]
        if minimum_world_revision is not None:
            query += " AND observed_world_revision>=?"
            parameters.append(int(minimum_world_revision))
        query += " ORDER BY created_at DESC, evidence_id DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, tuple(parameters)).fetchone()
        return row is not None and bool(row["verified"])

    def missing(
        self, task_id: str, predicates: Iterable[str], *, minimum_world_revision: int | None = None,
        required_references: Mapping[str, Iterable[str]] | None = None,
    ) -> tuple[str, ...]:
        return tuple(
            predicate for predicate in predicates
            if not self.has_verified(
                task_id, predicate, minimum_world_revision=minimum_world_revision,
                required_references=(required_references or {}).get(predicate),
            )
        )


class EvidenceGate:
    """The only supported path from work completion to task success."""

    def __init__(self, store: EvidenceStore):
        self.store = store

    def assert_complete(
        self, task_id: str, predicates: Iterable[str], *, minimum_world_revision: int | None = None,
        required_references: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        required = tuple(str(item) for item in predicates)
        if not required:
            raise ValueError("a task cannot succeed without at least one success predicate")
        missing = self.store.missing(
            task_id, required, minimum_world_revision=minimum_world_revision,
            required_references=required_references,
        )
        if missing:
            raise MissingEvidence(missing)


class MissingEvidence(RuntimeError):
    def __init__(self, predicates: Iterable[str]):
        self.predicates = tuple(predicates)
        super().__init__(f"missing verified evidence for: {', '.join(self.predicates)}")
