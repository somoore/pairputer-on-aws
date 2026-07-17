#!/usr/bin/env python3
"""Process-singleton bridge API over :class:`BrainRuntime`.

``get_brain()`` constructs exactly one runtime per guest process.  Its default
database is the persistent ``PAIRPUTER_DESKTOP_BRAIN_DB`` path; tests and the
bridge may inject a database path, state fusion, control client, skill registry,
service adapters, and approval signing key through the ``Brain`` constructor.

Bridge integration should import the module functions ``submit_task``,
``continue_task``, ``task_status``, ``cancel_task``, ``approve_action``,
``observe``, ``before_freeze``, and ``after_thaw``.  They all share the same
single worker and durable journal.  No provider client or credential is used.
Running this file retains a bounded JSON-lines diagnostic protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from desktop_brain_runtime import BrainRuntime, DEFAULT_STATE_PATH


class Brain:
    """Injectable owner of one ``BrainRuntime`` and its start lifecycle."""

    def __init__(self, database_path: str | Path, **kwargs: Any):
        self.runtime = BrainRuntime(database_path, **kwargs)
        self._started = False
        self._start_lock: asyncio.Lock | None = None

    async def start(self) -> None:
        if self._started:
            return
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if not self._started:
                await self.runtime.start()
                self._started = True

    async def drive_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.start()
        plan = payload.get("plan")
        return await self.runtime.submit_task(
            payload, plan=plan, conflict=str(payload.get("conflict", "reject")),
            created_by="approved_host",
        )

    async def continue_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.start()
        return await self.runtime.continue_task(
            str(payload["task_id"]), plan=payload.get("plan"),
            action_approval_token=payload.get("action_approval_token"),
            trigger=str(payload.get("trigger", "explicit")),
            idle_seconds=float(payload.get("idle_seconds", 0)),
        )

    def task_status(self, task_id: str) -> dict[str, Any]:
        return self.runtime.status(task_id)

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        await self.start()
        return await self.runtime.cancel_task(task_id)

    def approve_action(self, approval_id: str) -> dict[str, Any]:
        return {"approval_id": approval_id,
                "action_approval_token": self.runtime.approve(approval_id)}

    async def observe(self) -> dict[str, Any]:
        await self.start()
        return asdict(await self.runtime.state_fusion.observe())

    async def ground(self, intent: str, nodes: Any = None, *, top_k: int = 12) -> dict[str, Any]:
        """Rank on-screen elements against a natural-language intent (Prune4Web-style).
        If `nodes` is not supplied, ground against the current observation's accessibility
        nodes. Returns a short ranked candidate list the drive/host can act on."""
        from element_grounding import rank_elements
        await self.start()
        if nodes is None:
            snapshot = asdict(await self.runtime.state_fusion.observe())
            acc = snapshot.get("accessibility") if isinstance(snapshot, dict) else None
            nodes = acc.get("nodes", []) if isinstance(acc, dict) else []
        return rank_elements(str(intent or ""), list(nodes or []), top_k=int(top_k))

    async def before_freeze(self) -> dict[str, Any]:
        await self.start()
        await self.runtime.freeze_barrier()
        return {"ok": True, "barrier": "recorded", "human_epoch": self.runtime.control.human_epoch}

    async def after_thaw(self) -> dict[str, Any]:
        await self.start()
        await self.runtime.thaw_reconcile()
        return {"ok": True, "reconciliation": "required", "human_epoch": self.runtime.control.human_epoch}


_singleton: Brain | None = None
_singleton_guard = threading.RLock()


def get_brain(database_path: str | Path | None = None, **dependencies: Any) -> Brain:
    """Return the process singleton, rejecting ambiguous reconfiguration."""

    global _singleton
    requested_path = str(database_path or os.environ.get("PAIRPUTER_DESKTOP_BRAIN_DB", DEFAULT_STATE_PATH))
    with _singleton_guard:
        if _singleton is None:
            _singleton = Brain(requested_path, **dependencies)
        elif str(_singleton.runtime.database_path) != requested_path or dependencies:
            raise RuntimeError("desktop brain singleton is already configured")
        return _singleton


async def submit_task(payload: Mapping[str, Any]) -> dict[str, Any]:
    return await get_brain().drive_task(payload)


async def continue_task(payload: Mapping[str, Any]) -> dict[str, Any]:
    return await get_brain().continue_task(payload)


def task_status(task_id: str) -> dict[str, Any]:
    return get_brain().task_status(task_id)


async def cancel_task(task_id: str) -> dict[str, Any]:
    return await get_brain().cancel_task(task_id)


def approve_action(approval_id: str) -> dict[str, Any]:
    return get_brain().approve_action(approval_id)


async def observe() -> dict[str, Any]:
    return await get_brain().observe()


async def before_freeze() -> dict[str, Any]:
    return await get_brain().before_freeze()


async def after_thaw() -> dict[str, Any]:
    return await get_brain().after_thaw()


async def shutdown() -> None:
    global _singleton
    with _singleton_guard:
        instance = _singleton
        _singleton = None
    if instance is not None:
        await instance.runtime.close()


async def _main() -> None:
    brain = Brain(sys.argv[1] if len(sys.argv) > 1 else "/tmp/pairputer-desktop-brain.sqlite3")
    await brain.start()
    try:
        for line in sys.stdin:
            request = json.loads(line)
            operation = request.get("operation")
            if operation == "drive_task":
                result = await brain.drive_task(request.get("args") or {})
            elif operation == "continue_task":
                result = await brain.continue_task(request.get("args") or {})
            elif operation == "task_status":
                result = brain.task_status(str((request.get("args") or {})["task_id"]))
            elif operation == "cancel_task":
                result = await brain.cancel_task(str((request.get("args") or {})["task_id"]))
            elif operation == "approve_action":
                result = brain.approve_action(str((request.get("args") or {})["approval_id"]))
            elif operation == "observe":
                result = await brain.observe()
            elif operation == "before_freeze":
                result = await brain.before_freeze()
            elif operation == "after_thaw":
                result = await brain.after_thaw()
            else:
                result = {"accepted": False, "error": "unknown_operation"}
            print(json.dumps(result, sort_keys=True), flush=True)
    finally:
        await brain.runtime.close()


if __name__ == "__main__":
    asyncio.run(_main())
