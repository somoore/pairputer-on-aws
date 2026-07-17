"""Deterministic skill registry for the desktop execution brain."""

from .base import (
    BaseSkill,
    PreparedEffect,
    RawResult,
    RecoveryPlan,
    SkillContext,
    SkillDefinition,
    SkillRegistry,
    Verification,
)
from .app_skills import OpenApplicationSkill
from .browser_skills import BrowserInteractSkill, BrowserNavigateSkill, BrowserQuerySkill
from .code_skills import RunCommandSkill
from .cross_app_skills import CopyFactSkill
from .workspace_skills import (
    CreateArtifactSkill,
    CreateDirectorySkill,
    InspectArtifactSkill,
    MoveArtifactSkill,
    PatchArtifactSkill,
    TrashArtifactSkill,
)


def default_registry() -> SkillRegistry:
    registry = SkillRegistry()
    for skill in (
        InspectArtifactSkill(), CreateDirectorySkill(), CreateArtifactSkill(), PatchArtifactSkill(), MoveArtifactSkill(),
        TrashArtifactSkill(), RunCommandSkill(), OpenApplicationSkill(), BrowserNavigateSkill(),
        BrowserQuerySkill(), BrowserInteractSkill(), CopyFactSkill(),
    ):
        registry.register(skill)
    return registry


__all__ = ["default_registry", "SkillRegistry", "SkillContext"]
