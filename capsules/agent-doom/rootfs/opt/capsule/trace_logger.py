#!/usr/bin/env python3.11
"""Optional deterministic trace logging for Agent DOOM.

Trace logging is disabled unless PAIRPUTER_BRAIN_TRACE_DIR is set. When enabled,
the capsule writes compact JSONL rows that can later train a tiny action ranker
from successful deterministic runs. Runtime control remains deterministic.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class TraceLogger:
    def __init__(self) -> None:
        root = os.environ.get("PAIRPUTER_BRAIN_TRACE_DIR", "").strip()
        self.root = Path(root) if root else None
        self.steps = 0
        self.runs = 0
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def run_id(self) -> str:
        return f"{int(time.time() * 1000):x}"

    def record_step(self, run_id: str, row: dict[str, Any]) -> None:
        if not self.enabled:
            return
        payload = {"kind": "step", "run": run_id, **_compact(row)}
        self._append(run_id, payload)
        self.steps += 1

    def record_run(self, run_id: str, result: dict[str, Any]) -> None:
        if not self.enabled:
            return
        payload = {"kind": "run", "run": run_id, "at": int(time.time()), "result": _compact(result)}
        self._append(run_id, payload)
        self.runs += 1

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "runs": self.runs,
            "steps": self.steps,
        }

    def _append(self, run_id: str, payload: dict[str, Any]) -> None:
        assert self.root is not None
        path = self.root / f"{run_id}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")


class TinyPolicyRanker:
    """Scaffold for later offline ranking without making runtime stochastic."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def observe_success(self, skill: str | None) -> None:
        if not skill:
            return
        self._counts[str(skill)] = self._counts.get(str(skill), 0) + 1

    def score(self, skill: str | None) -> float:
        if not skill:
            return 0.0
        total = sum(self._counts.values()) or 1
        return float(self._counts.get(str(skill), 0)) / float(total)

    def summary(self) -> dict[str, Any]:
        return {"skills": len(self._counts), "samples": sum(self._counts.values())}


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"directive", "recent", "memory"}:
                continue
            out[str(key)[:32]] = _compact(item)
        return out
    if isinstance(value, list):
        return [_compact(item) for item in value[:16]]
    if isinstance(value, tuple):
        return [_compact(item) for item in value[:16]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:96]
