"""Application open/focus abstraction with process-plus-window verification."""

from __future__ import annotations

import inspect
from typing import Any, Mapping

from .base import BaseSkill, RawResult, SkillDefinition, Verification


class OpenApplicationSkill(BaseSkill):
    definition = SkillDefinition(
        "app.open", "app.open", "app_focus", "local_reversible", "interruptible",
        "retryable", ("visible", "hybrid"), ("application_open",), 30, "observe_process_and_window",
    )

    async def inspect(self, args, snapshot, context):
        if not str(args.get("app") or "").strip():
            raise AppUnavailable("app identifier is required")
        return {"app": str(args["app"])}

    async def execute(self, prepared, lease, context):
        adapter = context.services.get("apps")
        if adapter is None or not hasattr(adapter, "open"):
            raise AppUnavailable("app adapter is unavailable")
        context.control.checkpoint(lease)
        result = adapter.open(
            str(prepared.action["app"]), task_id=context.task_id, action_id=context.action_id,
            expected_human_epoch=lease.human_epoch,
            expected_world_revision=lease.world_revision,
        )
        if inspect.isawaitable(result):
            result = await result
        return RawResult(True, dict(result or {}))

    async def verify(self, prepared, raw, snapshot, context):
        adapter = context.services.get("apps")
        state = adapter.state(str(prepared.action["app"]))
        if inspect.isawaitable(state):
            state = await state
        state = dict(state or {})
        verified = bool(state.get("process_running")) and bool(state.get("top_level_window"))
        return Verification(verified, {"application_open": verified}, state, "application process and accessible top-level window observed")


class AppUnavailable(RuntimeError):
    pass
