"""Uniform inspect/prepare/execute/verify/recover/rollback skill lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from control_client import ControlClient, ControlLease
from state_fusion import DesktopSnapshot


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    capability: str
    effect: str
    risk: str
    interruptibility: str
    idempotency: str
    presentation_modes: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    timeout_seconds: float
    retry_policy: str
    safe_fallbacks: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillContext:
    task_id: str
    step_id: str
    action_id: str
    snapshot: DesktopSnapshot
    control: ControlClient
    services: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedEffect:
    action: Mapping[str, Any]
    preconditions: Mapping[str, Any]
    private: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawResult:
    committed: bool
    result: Mapping[str, Any]
    retry_safety: str = "safe"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verification:
    verified: bool
    predicates: Mapping[str, bool]
    observed: Mapping[str, Any]
    summary: str


@dataclass(frozen=True)
class RecoveryPlan:
    disposition: str
    reason: str
    retry_safe: bool
    action: Mapping[str, Any] | None = None


class BaseSkill:
    definition: SkillDefinition

    def canonical_action(self, args: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return policy input with model-supplied policy labels removed."""

        clean = {key: value for key, value in args.items() if key not in {
            "effect", "kind", "risk", "risk_class", "capability", "requires_approval",
        }}
        return {"kind": self.definition.effect, "capability": self.definition.capability, **clean}

    async def inspect(self, args: Mapping[str, Any], snapshot: DesktopSnapshot, context: SkillContext) -> Mapping[str, Any]:
        return {"ready": True}

    async def prepare(self, args: Mapping[str, Any], snapshot: DesktopSnapshot, context: SkillContext) -> PreparedEffect:
        return PreparedEffect(action=dict(args), preconditions=await self.inspect(args, snapshot, context))

    async def execute(self, prepared: PreparedEffect, lease: ControlLease, context: SkillContext) -> RawResult:
        raise NotImplementedError

    async def verify(self, prepared: PreparedEffect, raw: RawResult, snapshot: DesktopSnapshot, context: SkillContext) -> Verification:
        raise NotImplementedError

    async def recover(self, failure: Exception, snapshot: DesktopSnapshot, context: SkillContext) -> RecoveryPlan:
        return RecoveryPlan("fail", type(failure).__name__, False)

    async def rollback(self, prepared: PreparedEffect, raw: RawResult, context: SkillContext) -> RawResult:
        return RawResult(False, {"rolled_back": False}, retry_safety="unknown_outcome")


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, BaseSkill] = {}

    def register(self, skill: BaseSkill) -> None:
        name = skill.definition.name
        if not name or name in self._skills:
            raise ValueError(f"invalid or duplicate skill: {name}")
        self._skills[name] = skill

    def get(self, name: str) -> BaseSkill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise UnknownSkill(name) from exc

    def definitions(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._skills[name].definition for name in sorted(self._skills))


class UnknownSkill(KeyError):
    pass
