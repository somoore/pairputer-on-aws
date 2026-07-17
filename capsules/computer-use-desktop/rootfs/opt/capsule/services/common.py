from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import threading
import time
import uuid
from pathlib import Path

MAX_TEXT_BYTES = int(os.environ.get("PAIRPUTER_MAX_TEXT_BYTES", "1048576"))
MAX_RESULT_BYTES = int(os.environ.get("PAIRPUTER_MAX_RESULT_BYTES", "2097152"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str], limit: int = 64 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("file exceeds hash limit")
            digest.update(chunk)
    return digest.hexdigest()


def mime_for(path: str | os.PathLike[str]) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def evidence(kind: str, **values) -> dict:
    return {"kind": kind, "observedAt": time.time(), **values}


def action_result(*, accepted: bool, action_id: str | None, state: dict,
                  actuator: str, summary: str, data=None, evidence_items=None,
                  reason: str = "", retry_safety: str = "safe", warnings=None) -> dict:
    result = {
        "accepted": bool(accepted),
        "actionId": action_id or str(uuid.uuid4()),
        "reason": reason,
        "humanEpoch": int(state.get("humanEpoch", 0)),
        "startingWorldRevision": int(state.get("worldRevision", 0)),
        "endingWorldRevision": int(state.get("worldRevision", 0)) + (1 if accepted else 0),
        "actuator": actuator,
        "presentationMethod": "semantic",
        "summary": summary[:500],
        "data": data or {},
        "evidence": evidence_items or [],
        "retrySafety": retry_safety,
        "warnings": warnings or [],
    }
    encoded = json.dumps(result, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        result["data"] = {"truncated": True}
        result["warnings"] = [*result["warnings"], "result_truncated"]
    return result


def require_action_envelope(request: dict) -> tuple[str, int, int, str]:
    required = ("action_id", "expected_human_epoch", "expected_world_revision", "idempotency_key")
    missing = [key for key in required if key not in request]
    if missing:
        raise ValueError("missing action envelope fields: " + ", ".join(missing))
    action_id = str(request["action_id"])
    idempotency = str(request["idempotency_key"])
    if not action_id or len(action_id) > 128 or not idempotency or len(idempotency) > 256:
        raise ValueError("invalid action_id or idempotency_key")
    return action_id, int(request["expected_human_epoch"]), int(request["expected_world_revision"]), idempotency


class IdempotencyStore:
    """Small bounded, durable result cache bound to the complete request."""

    def __init__(self, state_dir: str | os.PathLike[str], max_entries: int = 512):
        self.path = Path(state_dir) / "idempotency.json"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_entries = max_entries
        self._lock = threading.RLock()

    @staticmethod
    def _request_digest(request: dict) -> str:
        try:
            encoded = json.dumps(request, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("idempotent request must be canonical JSON") from exc
        return hashlib.sha256(encoded).hexdigest()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("idempotency state is corrupt") from exc
        if not isinstance(value, dict):
            raise RuntimeError("idempotency state has an invalid shape")
        return value

    def get(self, key: str, request: dict):
        with self._lock:
            entry = self._read().get(key)
        if entry is None:
            return None
        if not isinstance(entry, dict) or set(entry) != {"requestDigest", "result"}:
            raise RuntimeError("idempotency entry has an invalid shape")
        if entry["requestDigest"] != self._request_digest(request):
            raise ValueError("idempotency key was already used for a different request")
        if not isinstance(entry["result"], dict):
            raise RuntimeError("idempotency result has an invalid shape")
        return entry["result"]

    def put(self, key: str, request: dict, value: dict) -> None:
        entry = {"requestDigest": self._request_digest(request), "result": value}
        with self._lock:
            entries = self._read()
            existing = entries.get(key)
            if existing is not None and existing != entry:
                raise ValueError("idempotency key was already used for a different result")
            entries[key] = entry
            while len(entries) > self.max_entries:
                entries.pop(next(iter(entries)))
            tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}")
            tmp.write_text(json.dumps(entries, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
