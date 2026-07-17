"""Provenance-preserving cross-application planning primitive."""

from __future__ import annotations

import re

from .base import BaseSkill, RawResult, SkillDefinition, Verification
from task_memory import UNTRUSTED_CONTENT_SOURCES


class CopyFactSkill(BaseSkill):
    definition = SkillDefinition(
        "cross_app.copy_fact", "cross_app.transfer", "workspace_write", "local_reversible",
        "atomic_commit", "stable_key", ("visible", "hybrid"), ("fact_copied_with_provenance",),
        30, "expected_target_state",
    )

    def canonical_action(self, args):
        target = args.get("target")
        if not isinstance(target, dict) or target.get("type") not in {"workspace", "app"}:
            return {"kind": "unknown", "capability": self.definition.capability}
        clean = {key: value for key, value in args.items() if key not in {
            "effect", "kind", "risk", "risk_class", "capability", "requires_approval",
            "path", "app",
        }}
        scope = {"path": target.get("path")} if target["type"] == "workspace" else {"app": target.get("app")}
        return {"kind": self.definition.effect, "capability": self.definition.capability, **clean, **scope}

    async def inspect(self, args, snapshot, context):
        provenance = str(args.get("provenance") or "")
        source_digest = str(args.get("source_digest") or "").lower()
        if (not args.get("fact_key") or not isinstance(args.get("target"), dict) or
                provenance not in UNTRUSTED_CONTENT_SOURCES or
                not re.fullmatch(r"[0-9a-f]{64}", source_digest)):
            raise ValueError("fact_key, target, recognized content provenance, and source_digest are required")
        return {"provenance_bound": True, "source_digest": source_digest}

    async def execute(self, prepared, lease, context):
        adapter = context.services.get("cross_app")
        if adapter is None:
            raise RuntimeError("cross-app adapter is unavailable")
        context.control.checkpoint(lease)
        result = adapter.copy_fact(
            dict(prepared.action), task_id=context.task_id, action_id=context.action_id,
            expected_human_epoch=lease.human_epoch,
            expected_world_revision=lease.world_revision,
        )
        if hasattr(result, "__await__"):
            result = await result
        return RawResult(True, dict(result or {}))

    async def verify(self, prepared, raw, snapshot, context):
        adapter = context.services.get("cross_app")
        result = adapter.verify_fact(dict(prepared.action))
        if hasattr(result, "__await__"):
            result = await result
        result = dict(result or {})
        verified = (bool(result.get("verified")) and
                    result.get("provenance") == prepared.action.get("provenance") and
                    result.get("source_digest") == prepared.action.get("source_digest") and
                    result.get("fact_key") == prepared.action.get("fact_key"))
        return Verification(verified, {"fact_copied_with_provenance": verified}, result, "target fact and provenance observed")
