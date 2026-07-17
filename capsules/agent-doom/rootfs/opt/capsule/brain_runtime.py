#!/usr/bin/env python3.11
"""Bounded objective driver for the Agent DOOM capsule.

The MCP server remains capsule-general: it forwards manifest-declared tools to
bridge paths. This module is capsule-owned and translates objectives into
restful-doom controller skills.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("agent_doom.brain")

from brain_status import BrainStatusMixin, MEMORY_PATH
from cautious_combat import (
    CAUTIOUS_COVER_AMBUSH_WINDOW,
    CAUTIOUS_DOOR_AMBUSH_MAX_UNITS,
    CAUTIOUS_RETREAT_COMMIT_STEPS,
    CautiousCombatMixin,
    FP_UNIT,
    HEALTHY_BREAK_LOS_PUSH_HEALTH,
    HEALTHY_BREAK_LOS_PUSH_REPEATS,
    ROUTE_CRITICAL_HEALTH_BREAKAWAY,
    ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE,
)
from combat_state import CombatState
from contract_eval import ContractEvalMixin, PRESERVE_HEALTH_DAMAGE_ALLOWANCE
from contract_guards import ContractGuardsMixin
from door_memory import DoorMemory
from goal_contract import GoalContract, compile_goal_contract, contract_rules, filter_allowed_skills
from map_cache import MapCache
from planner import ROUTE_THREAT_REFUSE_MULT, SpatialPlanner
from planner_routing import (
    MACRO_MAX_TICS,
    MACRO_RAW_STEER_TURN_CAP,
    MACRO_RAW_STEER_TURN_SCALE,
    PlannerRoutingMixin,
)
from probe_runtime import ProbeBatcher
from route_reactions import (
    ANTI_GRIND_ACTIONS,
    ANTI_GRIND_MOVE_EPS_UNITS,
    ANTI_GRIND_RESET_MOVE_UNITS,
    ANTI_GRIND_SKILLS,
    ANTI_GRIND_STUCK_THRESHOLD,
    HEALTH_ROUTE_SKILLS,
    MELEE_RUSH_THREATS,
    NO_KILL_DESPERATION_PANIC_ACTIONS,
    NO_KILL_DESPERATION_PANIC_REPEATS,
    NO_KILL_DESPERATION_SPRINT_BURST_TICS,
    NO_KILL_DESPERATION_SPRINT_TICS,
    NO_KILL_ROUTE_REFUSAL_MULT,
    ROTATIONAL_STALL_ESCAPE_STEPS,
    ROTATIONAL_STALL_ROUTE_SKILLS,
    ROTATIONAL_STALL_TURN_THRESHOLD,
    ROUTE_CLEAN_SHOT_TURN_DEGREES,
    ROUTE_CRITICAL_TURN_AND_BURN_COMMIT_STEPS,
    ROUTE_CRITICAL_TURN_AND_BURN_DEFLECT_DEGREES,
    ROUTE_CRITICAL_TURN_AND_BURN_DEFLECT_STEPS,
    ROUTE_CRITICAL_TURN_AND_BURN_MAX_CHAIN,
    ROUTE_CRITICAL_TURN_AND_BURN_STALL_THRESHOLD,
    ROUTE_CRITICAL_TURN_AND_BURN_TICS,
    ROUTE_HITSCAN_FLINCH_STEPS,
    ROUTE_MELEE_RUSH_HOLD_DISTANCE,
    ROUTE_THREAT_HOLD_MIN_TICS,
    ROUTE_THREAT_RELEASE_MULT,
    RouteReactionsMixin,
    USE_STUCK_ESCAPE_STEPS,
    USE_STUCK_MOVE_EPS_UNITS,
    USE_STUCK_THRESHOLD,
)
from trace_logger import TinyPolicyRanker, TraceLogger
from threat_model import classify_enemy
from vision_brain import VisionBrain
from world_memory import WorldMemory

GRPC_TARGET = os.environ.get("PAIRPUTER_DOOM_GRPC_TARGET", "127.0.0.1:50051")
GRPC_TIMEOUT_S = float(os.environ.get("PAIRPUTER_BRAIN_GRPC_TIMEOUT_S", "10"))
MAX_TICS_CAP = int(os.environ.get("PAIRPUTER_BRAIN_MAX_TICS", "10000"))
MAX_STEPS_CAP = int(os.environ.get("PAIRPUTER_BRAIN_MAX_STEPS", str(MAX_TICS_CAP)))
DEFAULT_STEPS = int(os.environ.get("PAIRPUTER_BRAIN_DEFAULT_STEPS", "24"))
COPLAY_STATE_URL = os.environ.get("PAIRPUTER_COPLAY_STATE_URL", "http://127.0.0.1:6906/")
MAX_WALL_S = float(os.environ.get("PAIRPUTER_BRAIN_MAX_WALL_S", "18"))
# Cap must exceed real game time for the largest eval budget: 7000 tics at 35 tics/s is 200s
# of pure game time before any planning overhead, so a 180s cap could never finish it.
MAX_WALL_CAP_S = float(os.environ.get("PAIRPUTER_BRAIN_MAX_WALL_CAP_S", "600"))
# A drive_goal MCP tool call must return inside the client's read timeout. Codex's default
# per-tool read timeout is short, and a chat turn often stacks a cold-boot play_capsule
# (~15-30s) BEFORE the drive in the same window — so the drive itself has to be brief. 8s
# keeps the round-trip well inside that window; the human sees continuous motion via the
# in-VM autopilot (autopilot.py drives long bursts INSIDE the VM, not through MCP), and an
# explicit max_wall_s still overrides for eval/headless use.
TOOL_SAFE_WALL_S = float(os.environ.get("PAIRPUTER_BRAIN_TOOL_SAFE_WALL_S", "8"))
MELEE_RUSH_KITE_DISTANCE = 144.0
MELEE_RUSH_PUNCH_DISTANCE = 112.0
HAZARD_ESCAPE_COMMIT_STEPS = 3
E1M1_FINAL_EXIT_COMMIT_X = (2600, 3300)
E1M1_FINAL_EXIT_COMMIT_Y = (-4900, -3500)
E1M1_FINAL_EXIT_LINES = {325, 330, 340, 341}
TERMINAL_OBJECTIVE_STATUSES = {"achieved", "failed", "interrupted"}
OBJECTIVE_ENUMS = {
    "exit_level",
    "complete_level",
    "find_enemy",
    "kill_enemy",
    "clear_area",
    "explore",
    "survive",
    "recover_health",
    "preserve_health",
}

ATTACK_HINTS = ("attack", "fight", "kill", "clear", "engage")
SHOOT_HINTS = ("shoot", "fire", "blast", "pull trigger")
ENEMY_HINTS = ("enemy", "enemies", "monster", "monsters", "imp", "demon")
FIND_HINTS = ("find", "seek", "locate", "search")
EXIT_HINTS = ("exit", "switch", "finish level")
USE_HINTS = ("use", "open", "door", "press")
RETREAT_HINTS = ("retreat", "fall back", "back up", "withdraw", "evade")
SURVIVE_HINTS = ("survive", "stay alive", "avoid death", "don't die", "do not die")
EXPLORE_HINTS = ("explore", "scout", "navigate", "progress", "move")
CEASE_FIRE_HINTS = (
    "stop shooting",
    "stop firing",
    "cease fire",
    "hold fire",
    "don't shoot",
    "dont shoot",
    "don't fire",
    "dont fire",
)

OBJECTIVE_SKILL_PRIORITY = {
    "cease_fire": ("retreat", "route_progression", "recover_stuck"),
    "complete_level": ("press_exit", "open_use_line", "route_progression", "fire", "close_visible_contact", "engage", "recover_stuck", "retreat"),
    "shoot": ("fire", "close_visible_contact", "engage", "seek_enemy", "recover_stuck"),
    "attack": ("fire", "close_visible_contact", "engage", "seek_enemy", "recover_stuck", "retreat"),
    "find_enemy": ("close_visible_contact", "seek_enemy", "engage", "route_progression", "recover_stuck"),
    "exit": ("press_exit", "open_use_line", "route_progression", "recover_stuck", "retreat"),
    "use": ("open_use_line", "press_exit", "route_progression", "recover_stuck"),
    "survive": ("retreat", "recover_stuck", "route_progression", "open_use_line"),
    "explore": ("route_progression", "open_use_line", "recover_stuck", "seek_enemy"),
}


def _normalize(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _has(text: str, *hints: str) -> bool:
    return any(hint in text for hint in hints)


def _clamp_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(lower, min(upper, parsed))


def _enum_token(value: Any) -> str:
    return "_".join(part for part in _normalize(str(value)).replace("-", "_").split("_") if part)


def _normalize_drive_goal_payload(body: dict[str, Any] | None) -> dict[str, Any]:
    """Preserve free-form goal text while accepting explicit Commander enums."""

    payload = dict(body or {})
    raw_goal = str(payload.get("goal") or "").strip()
    explicit_objective = _enum_token(payload.get("objective") or payload.get("objective_type") or "")
    if raw_goal:
        if explicit_objective in OBJECTIVE_ENUMS:
            payload["objective_type"] = explicit_objective
        payload["objective"] = raw_goal
    elif explicit_objective in OBJECTIVE_ENUMS:
        payload["objective_type"] = explicit_objective
        payload["objective"] = explicit_objective
        payload.setdefault("goal", explicit_objective.replace("_", " "))
    return payload


def _default_step_budget(max_tics: int) -> int:
    if max_tics <= 0:
        return DEFAULT_STEPS
    return max(1, min(MAX_STEPS_CAP, int(max_tics)))


@dataclass(frozen=True)
class ObjectiveDirective:
    objective: str
    normalized: str
    contract: GoalContract
    rules: tuple[str, ...]
    allowed_skills: tuple[str, ...]
    max_steps: int
    max_tics: int = 0
    max_wall_s: float = MAX_WALL_S
    explicit_allowed_skills: bool = False
    status: str = "tracking"
    unsupported: tuple[str, ...] = field(default_factory=tuple)
    ignore_human_interrupt: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "contract": self.contract.as_dict(),
            "rules": list(self.rules),
            "allowed_skills": list(self.allowed_skills),
            "max_steps": self.max_steps,
            "max_tics": self.max_tics,
            "max_wall_s": self.max_wall_s,
            "explicit_allowed_skills": self.explicit_allowed_skills,
            "status": self.status,
            "unsupported": list(self.unsupported),
            "ignore_human_interrupt": self.ignore_human_interrupt,
        }


def parse_directive(body: dict[str, Any] | None) -> ObjectiveDirective:
    payload = dict(body or {})
    # Phrase-parse the FREE-TEXT goal, not the enum. A caller may pass an explicit
    # objective ENUM ("clear_area") alongside the natural goal ("clear this room
    # safely"); feeding the enum string to the phrase parser drops the intent words
    # ("safely" -> preserve_health) and mis-parses ("clear" -> combat). Honor the enum
    # via objective_type (compile_goal_contract respects it) and keep the phrase for text.
    explicit_enum = _enum_token(payload.get("objective") or payload.get("objective_type") or "")
    goal_text = str(payload.get("goal") or payload.get("mission") or "").strip()
    if not goal_text:
        # No free text: fall back to the enum's words, else the default.
        goal_text = (explicit_enum.replace("_", " ") if explicit_enum in OBJECTIVE_ENUMS
                     else "survive, explore, and engage visible enemies")
    objective = goal_text
    if explicit_enum in OBJECTIVE_ENUMS:
        payload["objective_type"] = explicit_enum
    normalized = _normalize(objective)
    contract = compile_goal_contract(objective, payload)
    rules: list[str] = []
    unsupported: list[str] = []
    rules.extend(contract_rules(contract))
    if contract.style == "speedrun":
        rules.append("speedrun")
    kill_forbidden = contract.constraints.get("kill_budget") == 0
    if kill_forbidden:
        rules.append("no_kills")
    if contract.constraints.get("avoid_combat"):
        rules.append("avoid_combat")
    if _has(normalized, *CEASE_FIRE_HINTS):
        rules.append("cease_fire")
    if not kill_forbidden and _has(normalized, *SHOOT_HINTS):
        rules.append("shoot")
    if not kill_forbidden and _has(normalized, *ATTACK_HINTS):
        rules.append("attack")
    if not kill_forbidden and _has(normalized, *ENEMY_HINTS):
        rules.append("find_enemy")
    if _has(normalized, *EXIT_HINTS):
        rules.append("exit")
    if _has(normalized, *USE_HINTS):
        rules.append("use")
    if _has(normalized, *RETREAT_HINTS) or _has(normalized, *SURVIVE_HINTS):
        rules.append("survive")
    if _has(normalized, *EXPLORE_HINTS):
        rules.append("explore")
    if "map" in normalized:
        unsupported.append("map rendering is not available in this objective driver yet")
    if not rules:
        rules.append("explore")

    requested = payload.get("allowed_skills")
    allowed: list[str] = []
    if isinstance(requested, list):
        allowed.extend(str(item) for item in requested)
    else:
        for rule in rules:
            allowed.extend(OBJECTIVE_SKILL_PRIORITY.get(rule, ()))
        # Locked safety skills: user/LLM objectives cannot remove recovery unless an explicit allow-list
        # is supplied for an eval.
        allowed.extend(("recover_stuck", "retreat"))
    allowed = filter_allowed_skills(allowed, contract)
    max_tics = _clamp_int(payload.get("max_tics", payload.get("tics", 0)), 0, 0, MAX_TICS_CAP)
    default_steps = _default_step_budget(max_tics)
    max_steps = _clamp_int(payload.get("max_steps", payload.get("budget", default_steps)), default_steps, 1, MAX_STEPS_CAP)
    requested_wall = payload.get("max_wall_s", payload.get("wall_budget_s", 0))
    if requested_wall:
        max_wall_s = float(_clamp_int(requested_wall, int(MAX_WALL_S), 1, int(MAX_WALL_CAP_S)))
    elif max_tics > 0:
        max_wall_s = min(MAX_WALL_CAP_S, max(MAX_WALL_S, 5.0 + (float(max_tics) / 35.0) * 1.8))
    else:
        max_wall_s = MAX_WALL_S
    # Tool-safety: an MCP tool call must return well inside the client's read timeout
    # (manifest drive_goal timeoutSeconds=120; Codex's own read timeout is shorter). A long
    # max_tics used to drive synchronously for up to 600s and hit "tool read timeout". Cap the
    # wall to TOOL_SAFE_WALL_S unless the caller EXPLICITLY asked for longer via max_wall_s —
    # the agent/autopilot just makes repeated short bursts, and control returns fast (better
    # for a live demo). The in-VM autopilot supervisor can still run long via explicit wall.
    if not requested_wall:
        max_wall_s = min(max_wall_s, TOOL_SAFE_WALL_S)
    return ObjectiveDirective(
        objective=objective,
        normalized=normalized,
        contract=contract,
        rules=tuple(dict.fromkeys(rules)),
        allowed_skills=tuple(dict.fromkeys(skill for skill in allowed if skill)),
        max_steps=max_steps,
        max_tics=max_tics,
        max_wall_s=max_wall_s,
        explicit_allowed_skills=isinstance(requested, list),
        status="partial" if unsupported else "tracking",
        unsupported=tuple(unsupported),
        ignore_human_interrupt=bool(payload.get("ignore_human_interrupt", False)),
    )


class BrainRuntime(BrainStatusMixin, PlannerRoutingMixin, ContractEvalMixin, ContractGuardsMixin, RouteReactionsMixin, CautiousCombatMixin):
    """Stateful deterministic objective loop around restful-doom's controller."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Preemption: the in-VM autopilot drives the SAME RUNTIME/lock as explicit
        # MCP/human drive_goal calls. Without this, an explicit command queues behind
        # a back-to-back autopilot burst loop and the MCP socket read times out
        # ("tool read timeout"). An explicit caller sets this event before blocking on
        # the lock; an autopilot burst checks it each step and bails out early, so the
        # lock frees within a step or two and the human command runs promptly.
        self._preempt = threading.Event()
        self._bg_drive: threading.Thread | None = None  # single background drive_goal thread (no stacking)
        self._imports: dict[str, Any] | None = None
        self._controller: Any | None = None
        self._memory: Any | None = None
        self._map_cache = MapCache(timeout_s=min(2.0, GRPC_TIMEOUT_S))
        self._door_memory = DoorMemory()
        self._world_memory = WorldMemory()
        self._combat_state = CombatState()
        self._cautious_retreat_steps = 0
        self._cautious_retreat_commit_steps = 0
        self._cautious_target_id: int | None = None
        self._cautious_lure_cooldown = 0
        self._cautious_ambush_window = 0
        self._cautious_threshold_cooldown = 0
        self._cautious_recent_hit_window = 0
        self._cautious_lure_wait_steps = 0
        self._cautious_probe_steps = 0
        self._cautious_probe_cooldown = 0
        self._cautious_jiggle_peek_steps = 0
        self._cautious_jiggle_probe_attempts = 0
        self._cautious_door_ambush_hold_steps = 0
        self._cautious_door_ambush_hold_spent = 0
        self._cautious_door_ambush_line_id: int | None = None
        self._cautious_door_ambush_last_dist: float | None = None
        self._cautious_duel_target = 0
        self._cautious_duel_steps = 0
        self._cautious_cover_side_lock = 0
        self._cautious_cover_side_lock_steps = 0
        self._cautious_cover_hold_steps = 0
        self._cautious_cover_repeat_key: tuple[str, int, str] | None = None
        self._cautious_cover_repeat_count = 0
        self._cautious_post_shot_scoot = False
        self._cautious_lure_ping_wait_tics = 0
        self._cautious_lure_ping_count = 0
        self._cautious_door_tuck_steps = 0
        self._objective_baseline_kills = 0
        self._probe_batcher = ProbeBatcher()
        self._trace = TraceLogger()
        self._ranker = TinyPolicyRanker()
        self._vision = VisionBrain()
        self._planner: SpatialPlanner | None = None
        self._planner_digest: int | None = None
        self._last_level_key: tuple[int, int] | None = None
        self._last_tick: int | None = None
        self._last_plan: dict[str, Any] | None = None
        self._last_probes: dict[str, Any] | None = None
        self._recovery_forward_stalls = 0
        self._hard_wedge_steps = 0       # physical no-progress steps (any action) -> hard escape
        self._recent_positions: list[tuple[int, int]] = []  # 12-step window for oscillation wedge detection
        self._frozen_tick_steps = 0      # world tick not advancing (intermission/menu) -> press USE/FIRE
        self._hard_escape_active = 0     # remaining steps of a committed unstick maneuver
        self._hard_escape_dir = 0        # +1 turn right / -1 turn left, chosen once per escape
        self._recovery_repeat_key: tuple[str, float] | None = None
        self._recovery_repeat_count = 0
        self._planner_route_repeat_counts: dict[tuple[int, str], int] = {}
        self._frontier_route_repeat_counts: dict[tuple[int, str, str], int] = {}
        self._last_spatial_route_line_id: int | None = None
        self._planner_probe_explore_key: tuple[int, int, int, int, int, str] | None = None
        self._planner_probe_explore_count = 0
        self._planner_probe_escape_steps = 0
        self._failed_use_key: tuple[int, int, int, int, int, int | None, int | None, int | None] | None = None
        self._failed_use_count = 0
        self._failed_use_escape_steps = 0
        self._route_refusal_hold_tics = 0
        self._route_refusal_hold_key: tuple[str, int, int] | None = None
        self._route_refusal_hold_peak = 0.0
        self._route_refusal_melee_target_id = 0
        self._route_refusal_flinch_steps = 0
        self._critical_turn_and_burn_steps = 0
        self._critical_turn_and_burn_handoff_steps = 0
        self._critical_turn_and_burn_stall_count = 0
        self._critical_turn_and_burn_chain_count = 0
        self._critical_turn_and_burn_deflect_steps = 0
        self._critical_turn_and_burn_deflect_sign = 1
        self._hazard_escape_commit_steps = 0
        self._hazard_escape_last_override: tuple[int, str, Any, dict[str, Any]] | None = None
        self._wounded_return_fire_steps = 0
        self._no_kill_desperation_key: tuple[int, int, int, int, int, int] | None = None
        self._no_kill_desperation_count = 0
        self._no_kill_desperation_sprint_tics = 0
        self._anti_grind_key: tuple[int, int, int, int, int, int, str, str] | None = None
        self._anti_grind_count = 0
        self._anti_grind_escape_steps = 0
        self._rotational_stall_key: tuple[int, int, int, int, str, str] | None = None
        self._rotational_stall_count = 0
        self._rotational_stall_escape_steps = 0
        self._last_status: dict[str, Any] = {"status": "idle", "summary": "brain runtime initialized"}
        self._human_check_at = 0.0
        self._human_check_active = False
        self._ignore_human_interrupt = False

    def reset_episode(self) -> dict[str, Any]:
        with self._lock:
            self._door_memory = DoorMemory()
            self._world_memory = WorldMemory()
            self._combat_state = CombatState()
            self._cautious_retreat_steps = 0
            self._cautious_retreat_commit_steps = 0
            self._cautious_target_id = None
            self._cautious_lure_cooldown = 0
            self._cautious_ambush_window = 0
            self._cautious_threshold_cooldown = 0
            self._cautious_recent_hit_window = 0
            self._cautious_lure_wait_steps = 0
            self._cautious_probe_steps = 0
            self._cautious_probe_cooldown = 0
            self._cautious_jiggle_peek_steps = 0
            self._cautious_jiggle_probe_attempts = 0
            self._cautious_door_ambush_hold_steps = 0
            self._cautious_door_ambush_hold_spent = 0
            self._cautious_door_ambush_line_id = None
            self._cautious_door_ambush_last_dist = None
            self._cautious_duel_target = 0
            self._cautious_duel_steps = 0
            self._cautious_cover_side_lock = 0
            self._cautious_cover_side_lock_steps = 0
            self._cautious_cover_hold_steps = 0
            self._cautious_cover_repeat_key = None
            self._cautious_cover_repeat_count = 0
            self._cautious_post_shot_scoot = False
            self._cautious_lure_ping_wait_tics = 0
            self._cautious_lure_ping_count = 0
            self._cautious_door_tuck_steps = 0
            self._objective_baseline_kills = 0
            self._last_level_key = None
            self._last_tick = None
            self._last_plan = None
            self._last_probes = None
            self._recovery_forward_stalls = 0
            self._hard_wedge_steps = 0
            self._recent_positions = []
            self._frozen_tick_steps = 0
            self._hard_escape_active = 0
            self._hard_escape_dir = 0
            self._recovery_repeat_key = None
            self._recovery_repeat_count = 0
            self._planner_route_repeat_counts = {}
            self._frontier_route_repeat_counts = {}
            self._last_spatial_route_line_id = None
            self._planner_probe_explore_key = None
            self._planner_probe_explore_count = 0
            self._planner_probe_escape_steps = 0
            self._failed_use_key = None
            self._failed_use_count = 0
            self._failed_use_escape_steps = 0
            self._route_refusal_hold_tics = 0
            self._route_refusal_hold_key = None
            self._route_refusal_hold_peak = 0.0
            self._route_refusal_melee_target_id = 0
            self._route_refusal_flinch_steps = 0
            self._critical_turn_and_burn_steps = 0
            self._critical_turn_and_burn_handoff_steps = 0
            self._critical_turn_and_burn_stall_count = 0
            self._critical_turn_and_burn_chain_count = 0
            self._critical_turn_and_burn_deflect_steps = 0
            self._critical_turn_and_burn_deflect_sign = 1
            self._hazard_escape_commit_steps = 0
            self._hazard_escape_last_override = None
            self._wounded_return_fire_steps = 0
            self._no_kill_desperation_key = None
            self._no_kill_desperation_count = 0
            self._no_kill_desperation_sprint_tics = 0
            self._anti_grind_key = None
            self._anti_grind_count = 0
            self._anti_grind_escape_steps = 0
            self._rotational_stall_key = None
            self._rotational_stall_count = 0
            self._rotational_stall_escape_steps = 0
            self._vision.reset()
            self._last_status = {"status": "idle", "summary": "episode memory reset"}
            return {"ok": True, "summary": "episode memory reset"}

    def drive(self, body: dict[str, Any] | None) -> dict[str, Any]:
        directive = parse_directive(body)
        with self._lock:
            self._last_status = {
                "status": "running",
                "objective": directive.objective,
                "steps": 0,
                "summary": "objective driver started",
                "directive": directive.as_dict(),
            }
            return self._drive_locked(
                directive,
                full=bool((body or {}).get("full")),
                include_recent=bool((body or {}).get("trace_recent") or (body or {}).get("debug_recent")),
            )

    def drive_ticks(self, body: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(body or {})
        payload["max_tics"] = _clamp_int(payload.get("max_tics", payload.get("tics", 350)), 350, 1, MAX_TICS_CAP)
        payload.setdefault("budget", _default_step_budget(int(payload["max_tics"])))
        directive = parse_directive(payload)
        with self._lock:
            self._last_status = {
                "status": "running",
                "objective": directive.objective,
                "steps": 0,
                "tics": 0,
                "summary": "tic-bounded objective driver started",
                "directive": directive.as_dict(),
            }
            return self._drive_locked(
                directive,
                full=bool(payload.get("full")),
                include_recent=bool(payload.get("trace_recent") or payload.get("debug_recent")),
            )

    def drive_goal(self, body: dict[str, Any] | None) -> dict[str, Any]:
        payload = _normalize_drive_goal_payload(body)
        contract = compile_goal_contract(payload.get("objective", ""), payload)
        if "max_tics" not in payload and "tics" not in payload:
            if contract.objective == "complete_level":
                payload["max_tics"] = 4200
            elif contract.objective == "exit_level":
                payload["max_tics"] = 2800
            elif contract.objective in {"kill_enemy", "clear_area"}:
                payload["max_tics"] = 1600 if (
                    contract.style == "melee" or contract.constraints.get("ammo_budget") == 0
                ) else 900
            else:
                payload["max_tics"] = 700
        payload.setdefault("budget", _default_step_budget(int(payload["max_tics"])))
        directive = parse_directive(payload)
        # Autopilot bursts yield to explicit MCP/human commands. An explicit caller raises
        # _preempt so any in-flight autopilot burst bails out and frees the lock fast; the
        # autopilot passes source=autopilot and honors the flag inside _drive_locked.
        is_autopilot = str(payload.get("source") or "").lower() == "autopilot"
        full = bool(payload.get("full"))
        include_recent = bool(payload.get("trace_recent") or payload.get("debug_recent"))

        # Fire-and-forget for explicit chat/agent commands. A synchronous drive runs
        # ~15s+ (planner rebuild + observe + vision + the step loop), which overruns the
        # AgentCore/transport response window and surfaces as "tool read timeout" in Codex
        # — the recurring bug. The human WATCHES the drive on the live stream, so the tool
        # response doesn't need the drive's result: kick the drive onto a background thread
        # (preempting any running drive) and return the committed contract immediately.
        # Callers that need the actual result (autopilot bursts, eval/trace via full/
        # trace_recent, or explicit wait=true) still run synchronously.
        wants_result = full or include_recent or is_autopilot or bool(payload.get("wait"))
        if not wants_result:
            self._preempt.set()  # interrupt any in-flight drive so this goal takes over

            def _run_bg(d=directive):
                try:
                    with self._lock:
                        self._preempt.clear()
                        self._last_status = {
                            "status": "running", "objective": d.objective,
                            "goal": d.contract.compact(),
                            "committed_contract": self._committed_contract(d),
                            "steps": 0, "tics": 0, "summary": "goal driver started (background)",
                            "directive": d.as_dict(),
                        }
                        self._drive_locked(d, full=False, include_recent=False, is_autopilot=False)
                except Exception as exc:  # never let a bg drive crash the thread silently
                    log.warning("background drive_goal failed: %r", exc)
            # Single background drive at a time: the preempt above makes the prior one exit
            # within a step; join it briefly so threads never stack (a runaway would exhaust
            # the VM's fork/thread limit). Then start the fresh one.
            prev = self._bg_drive
            if prev is not None and prev.is_alive():
                prev.join(timeout=1.0)  # preempt makes it yield in a step; short join avoids stacking
            t = threading.Thread(target=_run_bg, name="drive_goal_bg", daemon=True)
            self._bg_drive = t
            t.start()
            return {
                "status": "driving",
                "objective": directive.objective,
                "committed_contract": self._committed_contract(directive),
                "summary": f"driving goal in background: {directive.objective}",
                "async": True,
            }

        if not is_autopilot:
            self._preempt.set()
        try:
            with self._lock:
                if not is_autopilot:
                    self._preempt.clear()  # we hold the lock now; stop signalling
                self._last_status = {
                    "status": "running",
                    "objective": directive.objective,
                    "goal": directive.contract.compact(),
                    "committed_contract": self._committed_contract(directive),
                    "steps": 0,
                    "tics": 0,
                    "summary": "goal driver started",
                    "directive": directive.as_dict(),
                }
                result = self._drive_locked(directive, full=full, include_recent=include_recent, is_autopilot=is_autopilot)
        finally:
            if not is_autopilot:
                self._preempt.clear()
        return result if full or include_recent else self._compact_goal_result(result)

    def _drive_locked(self, directive: ObjectiveDirective, *, full: bool, include_recent: bool = False, is_autopilot: bool = False) -> dict[str, Any]:
        modules = self._lazy_imports()
        controller = self._controller_for(modules)
        stub, chan = self._stub(modules)
        transitions: list[dict[str, Any]] = []
        previous_ignore_human = self._ignore_human_interrupt
        self._ignore_human_interrupt = bool(directive.ignore_human_interrupt)
        try:
            run_id = self._trace.run_id()
            deadline = time.monotonic() + (directive.max_wall_s if directive.max_tics > 0 else max(MAX_WALL_S, 30.0))
            start_state = self._observe(stub, modules)
            self._refresh_planner(stub, modules, start_state)
            self._update_world_model(start_state)
            self._combat_state.update(start_state)
            previous = start_state
            final_state = start_state
            baseline = self._metrics(start_state)
            self._objective_baseline_kills = int(baseline["kills"])
            fired = False
            damage_taken = False
            shootable_seen = bool(baseline["shootable"])
            steps_run = 0
            tics_run = 0
            achieved = self._evaluate(directive, baseline, baseline, fired=fired, shootable_seen=shootable_seen, damage_taken=damage_taken)
            last_skill = None
            stuck_steps = 0
            budget_summary = ""
            if achieved["status"] == "achieved":
                return self._finish(
                    directive,
                    achieved,
                    baseline,
                    baseline,
                    final_state,
                    0,
                    0,
                    last_skill,
                    transitions,
                    full,
                    run_id,
                    include_recent=include_recent,
                    fired=fired,
                    damage_taken=damage_taken,
                )

            for step in range(1, directive.max_steps + 1):
                # An autopilot burst yields the moment an explicit MCP/human command is
                # waiting for the lock, so the command isn't starved into a socket timeout.
                if is_autopilot and self._preempt.is_set():
                    budget_summary = "yielded to explicit command"
                    achieved = {"status": "budget_exhausted", "summary": budget_summary}
                    break
                if time.monotonic() >= deadline:
                    budget_summary = "wall-clock budget exhausted"
                    achieved = {"status": "budget_exhausted", "summary": budget_summary}
                    break
                if directive.max_tics > 0 and tics_run >= directive.max_tics:
                    budget_summary = "max tic budget exhausted"
                    achieved = {"status": "budget_exhausted", "summary": budget_summary}
                    break
                if self._human_active():
                    return self._finish(
                        directive,
                        {"status": "interrupted", "summary": "human input interrupted the objective driver"},
                        baseline,
                        self._metrics(final_state),
                        final_state,
                        steps_run,
                        tics_run,
                        last_skill,
                        transitions,
                        full,
                        run_id,
                        include_recent=include_recent,
                        fired=fired,
                        damage_taken=damage_taken,
                    )
                current_contact = self._metrics(final_state)
                combat_contact = bool(current_contact["visible_enemy"] or current_contact["shootable"])
                hazard_override = self._hazard_floor_escape_override(final_state, directive, controller, modules, stub)
                final_exit_commit_override = (
                    None
                    if hazard_override is not None
                    else self._final_exit_commit_override(final_state, directive, controller, modules, stub)
                )
                contract_override = (
                    None
                    if hazard_override is not None or final_exit_commit_override is not None
                    else self._contract_override(final_state, directive, controller, modules)
                )
                urgent_retaliation = (
                    None
                    if hazard_override is not None or final_exit_commit_override is not None or contract_override is not None
                    else self._urgent_hitscan_retaliation_override(final_state, directive, controller, modules)
                )
                wounded_route_override = None
                if (
                    hazard_override is None
                    and final_exit_commit_override is None
                    and contract_override is None
                    and urgent_retaliation is None
                    and self._wounded_complete_level_route_priority(directive, current_contact)
                ):
                    candidate = self._planner_override(final_state, directive, controller, modules, stub)
                    if self._planner_override_is_progress(candidate):
                        wounded_route_override = candidate
                cautious_override = (
                    None
                    if hazard_override is not None or final_exit_commit_override is not None or contract_override is not None
                    else self._cautious_combat_override(final_state, directive, controller, modules)
                )
                defensive_override = (
                    None
                    if hazard_override is not None or final_exit_commit_override is not None or contract_override is not None or wounded_route_override is not None or cautious_override is not None
                    else self._defensive_combat_override(final_state, directive, controller, modules)
                )
                # HARD ESCAPE — highest priority. Once the body is physically wedged (no
                # movement for many steps regardless of what skill is looping — the door/barrel
                # freeze), nothing else gets a vote: turn hard and bull forward until unstuck.
                hard_escape = self._hard_escape_override(final_state, directive, controller, modules)
                if hard_escape is not None:
                    override = hard_escape
                elif hazard_override is not None:
                    override = hazard_override
                elif final_exit_commit_override is not None:
                    override = final_exit_commit_override
                elif contract_override is not None:
                    override = contract_override
                elif urgent_retaliation is not None:
                    override = urgent_retaliation
                elif wounded_route_override is not None:
                    override = wounded_route_override
                elif cautious_override is not None:
                    override = cautious_override
                elif defensive_override is not None:
                    override = defensive_override
                elif stuck_steps >= 2 and (not combat_contact or self._route_recovery_allowed_under_contact(directive)):
                    if self._planner_first_for_stall(directive, stuck_steps):
                        override = self._planner_override(final_state, directive, controller, modules, stub)
                        if override is None:
                            override = self._stuck_recovery_override(final_state, directive, controller, modules)
                    else:
                        override = self._stuck_recovery_override(final_state, directive, controller, modules)
                        if override is None:
                            override = self._planner_override(final_state, directive, controller, modules, stub)
                else:
                    override = self._planner_override(final_state, directive, controller, modules, stub)
                if override is None:
                    index, skill = self._select_skill(controller, final_state, directive, modules)
                    action, decision = controller.action_for(index, final_state)
                else:
                    index, skill, action, decision = override
                guarded = self._guard_contract_action(final_state, directive, controller, modules, index, skill, action, decision)
                index, skill, action, decision = guarded
                exposed_idle_guard = self._exposed_idle_under_fire_guard(
                    final_state,
                    directive,
                    controller,
                    modules,
                    index,
                    skill,
                    action,
                    decision,
                )
                if exposed_idle_guard is not None:
                    index, skill, action, decision = exposed_idle_guard
                wounded_fire_guard = self._wounded_route_under_fire_guard(
                    final_state,
                    directive,
                    controller,
                    modules,
                    index,
                    skill,
                    action,
                    decision,
                )
                if wounded_fire_guard is not None:
                    index, skill, action, decision = wounded_fire_guard
                self._tune_action_for_directive(action, directive, modules, decision)
                remaining_tics = directive.max_tics - tics_run if directive.max_tics > 0 else 16
                action.duration_tics = max(1, min(16, int(remaining_tics), int(getattr(action, "duration_tics", 1) or 1)))
                if self._fire_forbidden(directive, final_state):
                    self._strip_fire(action, modules["agent_pb2"])
                macro_budget = int(remaining_tics) if directive.max_tics > 0 else MACRO_MAX_TICS
                # ponytail: hitscan damage is a per-tic dice roll while exposed, so with a
                # zero-damage contract and a live enemy in sight, long blind route macros are
                # how the agent walks into fire; force short guarded steps instead. Hidden
                # enemies can't shoot, and macro bursts already abort on first visibility.
                nearest_threat = self._nearest_enemy(final_state, prefer_visible=True)
                macro_unsafe = (
                    bool(directive.contract.constraints.get("preserve_health"))
                    and nearest_threat is not None
                    and float(nearest_threat.get("distance", 9999.0) or 9999.0) <= 512.0
                )
                if macro_unsafe or bool(directive.contract.constraints.get("preserve_health")):
                    # 8-tic bursts: a fresh sighting aborts the macro before the
                    # enemy's reaction timer can convert into a landed shot.
                    action.duration_tics = min(int(getattr(action, "duration_tics", 1) or 1), 8)
                macro = None if macro_unsafe else self._macro_route_segment(stub, modules, final_state, action, decision, max_tics=macro_budget)
                if macro is not None:
                    next_state, actual_tics = macro
                else:
                    next_state = self._run_action(stub, action, modules)
                    actual_tics = max(1, int(getattr(action, "duration_tics", 1) or 1))
                tics_run += actual_tics
                self._update_world_model(next_state)
                planner_source = str(decision.get("source", "")) == "spatial_planner"
                route_outcome = modules["_route_outcome"](skill, previous, next_state, decision=decision)
                reward = modules["RewardEngine"]().score(previous, next_state)
                reward_summary = {
                    "kill_delta": int(getattr(reward, "kill_delta", 0)),
                    "damage_delta": int(getattr(reward, "damage_delta", 0)),
                }
                self._record_route_threshold_flinch(previous, next_state, decision)
                self._record_damage_reaction(directive, reward_summary["damage_delta"], self._metrics(next_state))
                self._record_planner_outcome(previous, next_state, action, decision, route_outcome, modules)
                step_moved = math.dist(
                    (self._metrics(previous)["x"], self._metrics(previous)["y"]),
                    (self._metrics(next_state)["x"], self._metrics(next_state)["y"]),
                ) / FP_UNIT
                self._record_critical_turn_and_burn_outcome(next_state, action, decision, step_moved, modules)
                self._record_recovery_outcome(action, decision, step_moved, modules)
                if self._movement_stalled(action, step_moved, modules):
                    stuck_steps += 1
                else:
                    stuck_steps = max(0, stuck_steps - 1)
                # Hard-wedge counter: ANY step (turn, fire, forward — doesn't matter) where
                # the body physically didn't move. A turn-in-place against a wall for 50 steps
                # (the door/barrel freeze from the screenshots) never tripped _movement_stalled
                # because that only counts MOVE actions. This counts pure position delta, so a
                # genuinely wedged agent escalates no matter what skill is looping.
                if float(step_moved) < 4.0:
                    self._hard_wedge_steps += 1
                else:
                    if self._hard_wedge_steps >= 6 and self._planner is not None:
                        # Wedge episode over (real movement resumed): refund the per-line USE
                        # budget. Without this the 5-press cap was a LIFETIME budget per door —
                        # a frequently reused (auto-closing) door exhausted it over a long
                        # session and could never be wedge-opened again.
                        self._planner._wedge_use_counts.clear()
                    self._hard_wedge_steps = 0
                # Oscillation-proof wedge: a body bouncing back and forth against a door/wall
                # moves >4u per step (defeating the counter above) while netting NOTHING —
                # measured live as an 18-burst route_to_health grind at one spot. If net
                # displacement over the last 12 steps is under 16u, we are wedged no matter
                # what the per-step deltas say: force the hard escape.
                cm = self._metrics(next_state)
                self._recent_positions.append((int(cm["x"]), int(cm["y"])))
                if len(self._recent_positions) > 12:
                    self._recent_positions.pop(0)
                if len(self._recent_positions) == 12:
                    net_moved = math.dist(self._recent_positions[0], self._recent_positions[-1]) / FP_UNIT
                    if net_moved < 16.0:
                        self._hard_wedge_steps = max(self._hard_wedge_steps, 6)
                        self._recent_positions = self._recent_positions[6:]  # don't re-trip every step mid-escape
                had_shootable = bool(modules["_has_shootable_enemy"](previous) or modules["_has_shootable_enemy"](next_state))
                action_summary = modules["summarize_action"](action) or {}
                if not planner_source:
                    controller.record_action_history(action_index=index, had_shootable_target=had_shootable, route_outcome=route_outcome)
                fired_now = self._protobuf_action_fired(action, modules["agent_pb2"]) or self._action_fired(action_summary, modules["agent_pb2"])
                fired = fired or fired_now
                if fired_now:
                    self._combat_state.record_fire()
                final_state = next_state
                self._combat_state.update(next_state)
                current = self._metrics(next_state)
                # World-tick freeze = the LEVEL is not simulating (intermission after an exit,
                # menu, pause). Movement input no-ops there but observe still answers, so the
                # brain politely planned against a frozen snapshot forever — live-soaked as
                # 15+ minutes parked on E1M1's "LEVEL FINISHED" screen after hitting the exit
                # switch. DOOM advances these screens on USE/FIRE: press both and re-observe.
                if int(current.get("tick", 0) or 0) == int(current_contact.get("tick", 0) or 0):
                    self._frozen_tick_steps += 1
                else:
                    self._frozen_tick_steps = 0
                if self._frozen_tick_steps >= 3:
                    agent_pb2_f = modules["agent_pb2"]
                    for advance in (agent_pb2_f.ACTION_USE, agent_pb2_f.ACTION_SHOOT):
                        next_state = self._run_action(
                            stub,
                            agent_pb2_f.PlayerAction(action=advance, amount=1, duration_tics=2),
                            modules,
                        )
                    final_state = next_state
                    current = self._metrics(next_state)
                    self._frozen_tick_steps = 0
                damage_taken = damage_taken or int(current.get("health", 0) or 0) < int(current_contact.get("health", 0) or 0)
                vision_hint = self._maybe_request_vision(
                    directive,
                    previous,
                    next_state,
                    action,
                    decision,
                    modules,
                    step=step,
                    stuck_steps=stuck_steps,
                    step_moved=step_moved,
                    previous_metrics=current_contact,
                    current_metrics=current,
                )
                shootable_seen = shootable_seen or had_shootable or bool(current["shootable"])
                steps_run = step
                last_skill = skill
                previous = next_state
                achieved = self._evaluate(
                    directive,
                    baseline,
                    current,
                    fired=fired,
                    shootable_seen=shootable_seen,
                    damage_taken=damage_taken,
                )
                self._last_status = {
                    "status": "running",
                    "objective": directive.objective,
                    "steps": step,
                    "tics": tics_run,
                    "skill": skill,
                    "summary": achieved["summary"],
                }
                if vision_hint is not None:
                    self._last_status["vision"] = vision_hint
                self._trace.record_step(
                    run_id,
                    {
                        "step": step,
                        "tics": tics_run,
                        "skill": skill,
                        "plan": self._last_plan,
                        "delta": {
                            "kills": current["kills"] - baseline["kills"],
                            "bullets": current["bullets"] - baseline["bullets"],
                        },
                        "state": {"tick": current["tick"], "shootable": current["shootable"], "visible": current["visible_enemy"]},
                    },
                )
                if full or include_recent:
                    plan_detail = {
                        key: decision.get(key)
                        for key in (
                            "skill",
                            "action",
                            "line",
                            "special",
                            "dist",
                            "route",
                            "turn",
                            "sector",
                            "macro",
                            "route_step_kind",
                            "route_step_threat_mult",
                            "route_step_line",
                            "route_step_sector",
                            "route_remaining",
                            "threshold",
                            "release_threshold",
                            "clean_turn_threshold",
                            "hold_tics",
                            "hold_peak",
                            "sustained",
                            "state",
                            "reason",
                            "enemy",
                            "enemy_dist",
                            "enemy_threat",
                            "enemy_health",
                            "health",
                            "critical_health",
                            "hazard_sector",
                            "final_exit_commit",
                            "flinch_steps",
                            "refused_skill",
                            "refused_action",
                            "panic_repeat",
                            "sprint_tics",
                            "escape_offset",
                            "escape_dist",
                            "blocked_skill",
                            "blocked_action",
                            "repeat",
                            "remaining",
                            "escape_steps",
                        )
                        if key in decision
                    }
                    transitions.append(
                        {
                            "step": step,
                            "skill": skill,
                            "primitive": dict(decision).get("skill"),
                            "tick": int(getattr(next_state, "tick", 0)),
                            "tics": int(actual_tics),
                            "act": {
                                "a": int(action_summary.get("action", 0) or 0),
                                "b": int((action_summary.get("raw") or {}).get("buttons", 0) or 0),
                                "m": int((action_summary.get("mouse") or {}).get("buttons", 0) or 0),
                                "f": int(bool(fired_now)),
                            },
                            "pos": {
                                "x": int(current["x"] / FP_UNIT),
                                "y": int(current["y"] / FP_UNIT),
                                "ang": int(current["angle"]),
                                "hp": int(current["health"]),
                            },
                            "nav": self._navigation_metrics(next_state),
                            "plan": plan_detail,
                            "reward": reward_summary,
                        }
                    )
                    del transitions[:-512]
                if achieved["status"] in TERMINAL_OBJECTIVE_STATUSES:
                    break

            if achieved["status"] not in TERMINAL_OBJECTIVE_STATUSES:
                try:
                    reconciled = self._observe(stub, modules)
                    final_state = reconciled
                    self._update_world_model(reconciled)
                    self._combat_state.update(reconciled)
                    current = self._metrics(reconciled)
                    shootable_seen = shootable_seen or bool(current["shootable"])
                    achieved = self._evaluate(
                        directive,
                        baseline,
                        current,
                        fired=fired,
                        shootable_seen=shootable_seen,
                        damage_taken=damage_taken,
                    )
                except Exception:
                    pass
            if achieved["status"] not in TERMINAL_OBJECTIVE_STATUSES:
                achieved = {"status": "budget_exhausted", "summary": budget_summary or achieved["summary"]}
            result = self._finish(
                directive,
                achieved,
                baseline,
                self._metrics(final_state),
                final_state,
                steps_run,
                tics_run,
                last_skill,
                transitions,
                full,
                run_id,
                include_recent=include_recent,
                fired=fired,
                damage_taken=damage_taken,
            )
            # Autopilot bursts run back-to-back (~1s gap): there is no idle-absorb window
            # to protect, and the disengage's UNTRACED, hazard-blind retreat steps at the
            # end of EVERY burst were the user-visible slime death — 12 blind steps x
            # continuous bursts backed DoomGuy into the nukage in wide arcs, undoing every
            # in-drive hazard fix. Only a drive that leaves the brain truly idle disengages.
            if not self._human_active() and not is_autopilot:
                self._post_goal_disengage(stub, controller, modules, directive, final_state)
            return result
        finally:
            self._ignore_human_interrupt = previous_ignore_human
            chan.close()

    def _post_goal_disengage(
        self,
        stub: Any,
        controller: Any,
        modules: dict[str, Any],
        directive: ObjectiveDirective,
        state: Any,
    ) -> Any:
        """Leave the avatar behind cover when a goal ends mid-firefight.

        When the driver stops (budget exhausted, failure) the brain goes idle and
        nobody pilots the avatar; if the goal woke enemies that still have line of
        sight, the avatar just stands there absorbing hitscan until it dies.
        ponytail: bounded to 12 retreat steps — a hard escape, not a new objective.
        """
        try:
            for _ in range(12):
                metrics = self._metrics(state)
                if int(metrics.get("health", 0) or 0) <= 0:
                    break
                if not bool(metrics.get("visible_enemy") or metrics.get("shootable")):
                    break
                # Hazard guard: a blind retreat must never wade into nukage. If a step
                # lands on damaging floor, immediately walk back to safe ground and stop
                # retreating — standing on the walkway under fire beats melting in acid.
                if self._planner is not None:
                    x = int(metrics.get("x", 0) or 0)
                    y = int(metrics.get("y", 0) or 0)
                    if self._planner.point_is_damaging_fp(x, y):
                        plan = self._planner.hazard_escape_action(state, modules["agent_pb2"], self._door_memory)
                        if plan is not None:
                            state = self._run_action(stub, plan.action, modules)
                        break
                enemy = self._nearest_enemy(state, prefer_visible=True)
                if enemy is None:
                    break
                cover = self._cautious_cover_action(
                    state,
                    directive,
                    controller,
                    modules,
                    reason="post_goal_disengage",
                    enemy=enemy,
                    arm_commit=False,
                )
                if cover is None:
                    break
                _index, _skill, action, _decision = cover
                state = self._run_action(stub, action, modules)
                self._update_world_model(state)
        except Exception:
            pass
        return state

    def _lazy_imports(self) -> dict[str, Any]:
        if self._imports is not None:
            return self._imports
        for path in ("/opt/capsule", "/opt/capsule/rdgen"):
            if path not in sys.path:
                sys.path.insert(0, path)
        os.environ.setdefault("RESTFULDOOM_PROTO_STUBS", "/opt/capsule/rdgen")
        from restfuldoom_agent.brain import AgentMemory
        from restfuldoom_agent.client import agent_pb2, agent_pb2_grpc, summarize_action
        from restfuldoom_agent.env import SKILL_ACTIONS, SkillController, _has_shootable_enemy, _route_outcome
        from restfuldoom_agent.reward import RewardEngine

        self._imports = {
            "AgentMemory": AgentMemory,
            "RewardEngine": RewardEngine,
            "SKILL_ACTIONS": SKILL_ACTIONS,
            "SkillController": SkillController,
            "_has_shootable_enemy": _has_shootable_enemy,
            "_route_outcome": _route_outcome,
            "agent_pb2": agent_pb2,
            "agent_pb2_grpc": agent_pb2_grpc,
            "summarize_action": summarize_action,
        }
        return self._imports

    def _controller_for(self, modules: dict[str, Any]) -> Any:
        if self._controller is not None:
            return self._controller
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._memory = modules["AgentMemory"].load(MEMORY_PATH)
        self._controller = modules["SkillController"](
            memory=self._memory,
            params=self._memory.best_params(),
            policy_id="pairputer_objective_driver_v1",
        )
        return self._controller

    def _stub(self, modules: dict[str, Any]) -> tuple[Any, Any]:
        import grpc

        chan = grpc.insecure_channel(GRPC_TARGET)
        return modules["agent_pb2_grpc"].DoomAgentStub(chan), chan

    def _tune_action_for_directive(
        self,
        action: Any,
        directive: ObjectiveDirective,
        modules: dict[str, Any],
        decision: dict[str, Any] | None = None,
    ) -> None:
        if directive.contract.style != "speedrun":
            return
        if not ({"exit", "complete_level"} & set(directive.rules)):
            return
        decision = decision or {}
        if int(decision.get("dist", 9999) or 9999) <= 96:
            return
        if str(decision.get("skill", "")) in {"live_exit_line", "press_exit"}:
            return
        agent_pb2 = modules["agent_pb2"]
        action_type = int(getattr(action, "action", 0) or 0)
        if action_type == int(agent_pb2.ACTION_FORWARD):
            action.amount = max(int(getattr(action, "amount", 0) or 0), 50)
            action.duration_tics = max(int(getattr(action, "duration_tics", 1) or 1), 16)
        elif action_type in (int(agent_pb2.ACTION_STRAFE_LEFT), int(agent_pb2.ACTION_STRAFE_RIGHT)):
            action.amount = max(int(getattr(action, "amount", 0) or 0), 32)
            action.duration_tics = max(int(getattr(action, "duration_tics", 1) or 1), 12)

    def _observe(self, stub: Any, modules: dict[str, Any]) -> Any:
        return next(iter(stub.Observe(modules["agent_pb2"].ObserveRequest(), timeout=GRPC_TIMEOUT_S)))

    def _run_action(self, stub: Any, action: Any, modules: dict[str, Any]) -> Any:
        agent_pb2 = modules["agent_pb2"]
        if int(getattr(action, "action", 0) or 0) == int(agent_pb2.ACTION_USE):
            press = agent_pb2.PlayerAction()
            press.CopyFrom(action)
            press.duration_tics = 1
            release = agent_pb2.PlayerAction()
            release.duration_tics = 1
            action.duration_tics = 2
            final_state = None
            try:
                seen = 0
                for final_state in stub.GameSession(iter([press, release]), timeout=max(0.75, 0.35 + 2 / 35.0)):
                    seen += 1
                    if seen >= 2:
                        return final_state
            except Exception:
                if final_state is not None:
                    return final_state
            if final_state is None:
                for one_tic in (press, release):
                    final_state = next(iter(stub.GameSession(iter([one_tic]), timeout=GRPC_TIMEOUT_S)))
            if final_state is None:
                raise RuntimeError("GameSession returned no state for use edge")
            return final_state

        duration = max(1, min(16, int(getattr(action, "duration_tics", 1) or 1)))
        action.duration_tics = duration
        final_state = None
        def ticks():
            for _ in range(duration):
                one_tic = modules["agent_pb2"].PlayerAction()
                one_tic.CopyFrom(action)
                one_tic.duration_tics = 1
                yield one_tic

        try:
            seen = 0
            for final_state in stub.GameSession(ticks(), timeout=max(0.75, 0.35 + duration / 35.0)):
                seen += 1
                if seen >= duration:
                    return final_state
        except Exception:
            if final_state is not None:
                return final_state
        if final_state is None:
            one_tic = modules["agent_pb2"].PlayerAction()
            one_tic.CopyFrom(action)
            one_tic.duration_tics = 1
            for _ in range(duration):
                final_state = next(iter(stub.GameSession(iter([one_tic]), timeout=GRPC_TIMEOUT_S)))
        if final_state is None:
            raise RuntimeError("GameSession returned no state for action burst")
        return final_state

    def _wounded_complete_level_route_priority(self, directive: ObjectiveDirective, metrics: dict[str, Any]) -> bool:
        if directive.contract.objective != "complete_level":
            return False
        constraints = directive.contract.constraints
        if constraints.get("kill_budget") == 0 or constraints.get("avoid_combat") or constraints.get("preserve_health"):
            return False
        health = int(metrics.get("health", 100) or 100)
        try:
            enemy_distance = float(metrics.get("nearest_enemy_dist", 0.0) or 0.0)
        except Exception:
            enemy_distance = 0.0
        near_final_exit = self._e1m1_near_final_exit_commit(metrics)
        if health <= ROUTE_CRITICAL_HEALTH_BREAKAWAY and (
            int(self._critical_turn_and_burn_steps) > 0
            or int(self._critical_turn_and_burn_deflect_steps) > 0
            or (
                int(self._critical_turn_and_burn_handoff_steps) > 0
                and enemy_distance <= ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE
            )
        ) and not near_final_exit:
            return False
        return health <= 55 and bool(metrics.get("visible_enemy") or metrics.get("shootable"))

    def _e1m1_near_final_exit_commit(self, metrics: dict[str, Any]) -> bool:
        try:
            episode = int(metrics.get("episode", 0) or 0)
            map_id = int(metrics.get("map", 0) or 0)
            x_units = int(float(metrics.get("x", 0) or 0) / FP_UNIT)
            y_units = int(float(metrics.get("y", 0) or 0) / FP_UNIT)
        except Exception:
            return False
        return (
            episode == 1
            and map_id == 1
            and E1M1_FINAL_EXIT_COMMIT_X[0] <= x_units <= E1M1_FINAL_EXIT_COMMIT_X[1]
            and E1M1_FINAL_EXIT_COMMIT_Y[0] <= y_units <= E1M1_FINAL_EXIT_COMMIT_Y[1]
        )

    def _e1m1_final_exit_route_line(self, decision: dict[str, Any]) -> bool:
        for key in ("line", "line_id", "route_step_line", "route_step_use_line"):
            try:
                if int(decision.get(key, -1) or -1) in E1M1_FINAL_EXIT_LINES:
                    return True
            except Exception:
                continue
        return False

    def _reset_hazard_escape_commit(self) -> None:
        self._hazard_escape_commit_steps = 0
        self._hazard_escape_last_override = None

    def _arm_hazard_escape_commit(
        self,
        override: tuple[int, str, Any, dict[str, Any]],
        agent_pb2: Any,
    ) -> tuple[int, str, Any, dict[str, Any]]:
        _index, _skill, action, _decision = override
        if self._action_has_translation(action, agent_pb2):
            index, skill, action, decision = override
            self._hazard_escape_commit_steps = HAZARD_ESCAPE_COMMIT_STEPS
            self._hazard_escape_last_override = (index, skill, action, dict(decision))
        else:
            self._reset_hazard_escape_commit()
        return override

    def _hazard_floor_commit_override(
        self,
        agent_pb2: Any,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if self._hazard_escape_commit_steps <= 0 or self._hazard_escape_last_override is None:
            self._reset_hazard_escape_commit()
            return None
        index, skill, action, decision = self._hazard_escape_last_override
        if not self._action_has_translation(action, agent_pb2):
            self._reset_hazard_escape_commit()
            return None
        self._hazard_escape_commit_steps = max(0, int(self._hazard_escape_commit_steps) - 1)
        committed = dict(decision)
        committed.update(
            {
                "source": "hazard_floor_guard",
                "state": "hazard_floor_escape",
                "reason": "hazard_floor_escape_commit",
                "commit_steps_remaining": int(self._hazard_escape_commit_steps),
            }
        )
        return index, skill, action, committed

    def _hazard_floor_escape_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        stub: Any,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if directive.explicit_allowed_skills:
            return None
        planner = self._refresh_planner(stub, modules, state)
        if planner is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        current_known = False
        current_damaging = False
        try:
            player = planner.player_from_state(state)
            current_sector = planner.sector_for_player(state, player)
            current_known = current_sector is not None
            current_damaging = bool(current_known and planner.sector_is_damaging(int(current_sector)))
        except Exception:
            current_known = False
            current_damaging = False
        if current_known and not current_damaging:
            return self._hazard_floor_commit_override(agent_pb2)
        critical_contact = self._hazard_critical_contact_escape(state, directive, controller, modules)
        if critical_contact is not None:
            return self._arm_hazard_escape_commit(critical_contact, agent_pb2)
        plan = planner.hazard_escape_action(state, agent_pb2, self._door_memory)
        if plan is None:
            self._reset_hazard_escape_commit()
            return None
        if not self._action_has_translation(plan.action, agent_pb2):
            raw_escape = self._hazard_raw_escape_override(
                state,
                directive,
                controller,
                modules,
                plan,
                reason="hazard_floor_rotation_escape",
            )
            if raw_escape is not None:
                return self._arm_hazard_escape_commit(raw_escape, agent_pb2)
        selected = self._planner_skill_index(plan.skill, controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            return None
        index, selected_skill = selected
        decision = dict(plan.detail)
        decision.update(
            {
                "source": "hazard_floor_guard",
                "planner_skill": plan.skill,
                "state": "hazard_floor_escape",
            }
        )
        if plan.door_line_id is not None:
            decision["line_id"] = int(plan.door_line_id)
        self._last_plan = {
            "status": "active",
            "skill": selected_skill,
            "planner_skill": plan.skill,
            "kind": str(decision.get("skill", ""))[:40],
            **({"line": int(plan.door_line_id), "line_id": int(plan.door_line_id)} if plan.door_line_id is not None else {}),
        }
        return self._arm_hazard_escape_commit((index, selected_skill, plan.action, decision), agent_pb2)

    def _hazard_critical_contact_escape(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if directive.contract.objective != "complete_level":
            return None
        metrics = self._metrics(state)
        health = int(metrics.get("health", 100) or 100)
        if health > ROUTE_CRITICAL_HEALTH_BREAKAWAY:
            return None
        if not bool(metrics.get("visible_enemy") or metrics.get("shootable")):
            return None
        enemy = self._nearest_enemy(state, prefer_visible=True) or {
            "id": 0,
            "threat": "unknown",
            "turn": 0.0,
            "distance": 0.0,
            "visible": bool(metrics.get("visible_enemy")),
        }
        result = self._route_threshold_turn_and_burn_action(
            state,
            directive,
            controller,
            modules,
            reason="hazard_floor_critical_contact_escape",
            enemy=enemy,
        )
        if result is None:
            return None
        index, skill, action, decision = result
        decision = dict(decision)
        decision.update(
            {
                "source": "hazard_floor_guard",
                "state": "hazard_floor_escape",
                "reason": "hazard_floor_critical_contact_escape",
                "health": health,
                "critical_health": ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                "visible_enemy": int(bool(metrics.get("visible_enemy"))),
                "shootable": int(bool(metrics.get("shootable"))),
            }
        )
        return index, skill, action, decision

    def _hazard_raw_escape_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        plan: Any,
        *,
        reason: str,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is None:
            return None
        navigation = getattr(state, "navigation", None)
        probes = [
            probe
            for probe in getattr(navigation, "direction_probes", []) or []
            if bool(getattr(probe, "open", False))
        ]
        if probes:
            probe = max(probes, key=lambda item: int(getattr(item, "block_distance_fp", 0) or 0))
            offset = float(getattr(probe, "angle_offset_degrees", 0.0) or 0.0)
        elif bool(getattr(navigation, "forward_open", False)):
            offset = 0.0
        elif bool(getattr(navigation, "back_open", False)):
            offset = 180.0
        else:
            offset = 90.0
        side = 0
        if 45.0 <= abs(offset) <= 135.0:
            side = 54 if offset > 0 else -54
        forward = -48 if abs(offset) > 135.0 else 64
        action = agent_pb2.PlayerAction(
            duration_tics=10,
            raw=raw_cls(
                forward_move=forward,
                side_move=side,
                angle_turn=0,
            ),
        )
        index, skill = selected
        decision = dict(getattr(plan, "detail", {}) or {})
        decision.update(
            {
                "source": "hazard_floor_guard",
                "planner_skill": str(getattr(plan, "skill", "") or "route_progression"),
                "skill": "hazard_floor_raw_escape",
                "state": "hazard_floor_escape",
                "reason": reason,
                "probe": round(offset, 1),
                "turn_suppressed": 1,
            }
        )
        door_line_id = getattr(plan, "door_line_id", None)
        if door_line_id is not None:
            decision["line_id"] = int(door_line_id)
        return index, skill, action, decision

    def _action_has_translation(self, action: Any, agent_pb2: Any) -> bool:
        raw = getattr(action, "raw", None)
        if raw is not None and any(int(getattr(raw, field, 0) or 0) for field in ("forward_move", "side_move")):
            return True
        action_type = int(getattr(action, "action", 0) or 0)
        amount = int(getattr(action, "amount", 0) or 0)
        if not action_type or not amount:
            return False
        translational = {
            int(getattr(agent_pb2, "ACTION_FORWARD", -1000)),
            int(getattr(agent_pb2, "ACTION_BACKWARD", -1001)),
            int(getattr(agent_pb2, "ACTION_STRAFE_LEFT", -1002)),
            int(getattr(agent_pb2, "ACTION_STRAFE_RIGHT", -1003)),
        }
        return action_type in translational

    def _final_exit_commit_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        stub: Any,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if directive.contract.objective != "complete_level":
            return None
        if not self._e1m1_near_final_exit_commit(self._metrics(state)):
            return None
        candidate = self._final_exit_direct_override(state, directive, controller, modules, stub)
        if candidate is None:
            candidate = self._planner_override(state, directive, controller, modules, stub)
        if not self._planner_override_is_progress(candidate):
            return None
        self._critical_turn_and_burn_steps = 0
        self._critical_turn_and_burn_handoff_steps = 0
        self._critical_turn_and_burn_deflect_steps = 0
        index, skill, action, decision = candidate
        committed = dict(decision)
        committed["final_exit_commit"] = 1
        return index, skill, action, committed

    def _final_exit_direct_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        stub: Any,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        planner = self._refresh_planner(stub, modules, state)
        if planner is None:
            return None
        player = planner._player(state)
        if player is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        self._update_world_model(state)
        plan = planner._live_exit_line_action(state, player, agent_pb2, self._door_memory)
        if plan is None:
            plan = planner._line_objective_action(player, agent_pb2, self._door_memory, exit_only=True, state=state)
        if plan is None:
            plan = planner._remembered_progression_line_action(
                state,
                player,
                agent_pb2,
                self._door_memory,
                prefer_exit=True,
            )
        if plan is None:
            return None
        selected = self._planner_skill_index(plan.skill, controller, state, modules, directive)
        if selected is None:
            return None
        index, selected_skill = selected
        decision = dict(plan.detail)
        decision.update(
            {
                "source": "spatial_planner",
                "planner_skill": plan.skill,
                "final_exit_direct": 1,
            }
        )
        if plan.door_line_id is not None:
            decision["line_id"] = int(plan.door_line_id)
        self._last_plan = {
            "status": "active",
            "skill": selected_skill,
            "planner_skill": plan.skill,
            "kind": str(decision.get("skill", ""))[:40],
            **({"line": int(plan.door_line_id), "line_id": int(plan.door_line_id)} if plan.door_line_id is not None else {}),
        }
        return index, selected_skill, plan.action, decision

    def _contract_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        contract = directive.contract
        constraints = contract.constraints
        agent_pb2 = modules["agent_pb2"]
        if constraints.get("kill_budget") == 0:
            metrics = self._metrics(state)
            enemy = self._nearest_enemy(state, prefer_visible=True)
            enemy_distance = float(enemy["distance"]) if enemy is not None else 9999.0
            speedrun_exit = directive.contract.style == "speedrun" and {"exit", "complete_level"} & set(directive.rules)
            preserve_health = bool(constraints.get("preserve_health"))
            if speedrun_exit and not preserve_health:
                # "No kills" means never attack; it should not make an exit race orbit
                # visible monsters. Keep the spatial planner in control until the
                # exit is reached; terminal guards still strip every fire action.
                return None
            if metrics["shootable"] or (metrics["visible_enemy"] and (enemy_distance <= 512 or not speedrun_exit)):
                return self._safe_contract_action(
                    state,
                    directive,
                    controller,
                    modules,
                    reason="avoid_combat_no_kills",
                )
            return None
        if constraints.get("weapon_policy") != "fist_only":
            return None
        selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("seek_enemy", controller, state, modules, directive)
        if selected is None:
            return None
        index, selected_skill = selected
        weapon = self._weapon_id(state)
        if weapon != 0:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SWITCH_WEAPON, amount=1, duration_tics=4)
            return index, selected_skill, action, {"source": "goal_contract", "skill": "switch_to_fist"}
        enemy = self._nearest_enemy(state, prefer_visible=True)
        if enemy is None:
            return None
        if not bool(enemy["visible"]):
            # Hidden enemy coordinates are not a movement target; walls lie. Let the spatial
            # planner route to a visibility/vantage cell, then take over for close melee.
            return None
        rush = self._melee_rush_contract_action(state, enemy, agent_pb2)
        if rush is not None:
            return index, selected_skill, rush[0], rush[1]
        dist = float(enemy["distance"])
        turn = float(enemy["turn"])
        if abs(turn) > 12:
            action_type = agent_pb2.ACTION_TURN_LEFT if turn > 0 else agent_pb2.ACTION_TURN_RIGHT
            action = agent_pb2.PlayerAction(action=action_type, amount=max(6, min(30, int(abs(turn)))), duration_tics=4)
            return index, selected_skill, action, {"source": "goal_contract", "skill": "melee_align", "turn": round(turn, 1)}
        if dist <= 112:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=8)
            return index, selected_skill, action, {"source": "goal_contract", "skill": "fist_punch", "dist": round(dist, 1)}
        action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=42 if dist > 160 else 28, duration_tics=12)
        return index, selected_skill, action, {"source": "goal_contract", "skill": "melee_close", "dist": round(dist, 1)}

    def _melee_rush_contract_action(
        self,
        state: Any,
        enemy: dict[str, Any],
        agent_pb2: Any,
    ) -> tuple[Any, dict[str, Any]] | None:
        if str(enemy.get("threat") or "unknown") not in MELEE_RUSH_THREATS:
            return None
        dist = float(enemy.get("distance", 9999.0) or 9999.0)
        turn = float(enemy.get("turn", 0.0) or 0.0)
        if dist > 192.0:
            return None
        if abs(turn) > 70.0:
            return None
        navigation = getattr(state, "navigation", None)
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        decision = {
            "source": "goal_contract",
            "skill": "melee_rush_kite_punch",
            "state": "kite_backward",
            "dist": round(dist, 1),
            "turn": round(turn, 1),
            "threat": str(enemy.get("threat") or "unknown"),
        }
        if raw_cls is not None:
            buttons = 1 if dist <= MELEE_RUSH_PUNCH_DISTANCE and abs(turn) <= 45.0 else 0
            if bool(getattr(navigation, "back_open", False)):
                side = self._best_open_cover_side(navigation)
                action = agent_pb2.PlayerAction(
                    duration_tics=6,
                    raw=raw_cls(
                        forward_move=-42 if dist <= MELEE_RUSH_KITE_DISTANCE else -22,
                        side_move=self._raw_side_move_for_cover_side(side, 18) if side else 0,
                        angle_turn=self._raw_steer_turn_units(turn),
                        buttons=buttons,
                    ),
                )
                return action, {**decision, "action": "backpedal_punch", "skill": "melee_rush_kite_punch"}
            side = self._best_open_cover_side(navigation)
            if side:
                action = agent_pb2.PlayerAction(
                    duration_tics=6,
                    raw=raw_cls(
                        side_move=self._raw_side_move_for_cover_side(side, 54),
                        angle_turn=self._raw_steer_turn_units(turn),
                        buttons=buttons,
                    ),
                )
                return action, {
                    **decision,
                    "action": "sidestep_punch",
                    "side": "left" if side > 0 else "right",
                }
            if buttons:
                action = agent_pb2.PlayerAction(
                    duration_tics=4,
                    raw=raw_cls(angle_turn=self._raw_steer_turn_units(turn), buttons=1),
                )
                return action, {**decision, "action": "stand_punch"}
        if abs(turn) > 12.0:
            action_type = agent_pb2.ACTION_TURN_LEFT if turn > 0 else agent_pb2.ACTION_TURN_RIGHT
            action = agent_pb2.PlayerAction(action=action_type, amount=max(6, min(30, int(abs(turn)))), duration_tics=4)
            return action, {"source": "goal_contract", "skill": "melee_rush_align", "turn": round(turn, 1)}
        if dist <= MELEE_RUSH_PUNCH_DISTANCE:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=6)
            return action, {**decision, "action": "fist_punch"}
        return None

    def _defensive_combat_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        constraints = directive.contract.constraints
        if directive.contract.objective != "complete_level":
            return None
        if (
            constraints.get("kill_budget") == 0
            or constraints.get("avoid_combat")
            or constraints.get("ammo_budget") == 0
            or constraints.get("weapon_policy") == "fist_only"
            or constraints.get("preserve_health")
            or self._fire_forbidden(directive, state)
        ):
            return None
        metrics = self._metrics(state)
        health = int(metrics.get("health", 100) or 100)
        if health <= ROUTE_CRITICAL_HEALTH_BREAKAWAY and (
            int(self._critical_turn_and_burn_steps) > 0
            or int(self._critical_turn_and_burn_handoff_steps) > 0
            or int(self._critical_turn_and_burn_deflect_steps) > 0
        ):
            return None
        enemy = self._nearest_enemy(state, prefer_visible=True)
        if enemy is None or not bool(enemy.get("visible")):
            return None
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        turn = float(enemy.get("turn", 0.0) or 0.0)
        threat = str(enemy.get("threat") or "unknown")
        health = int(metrics.get("health", 100) or 100)
        if bool(metrics.get("shootable")):
            selected = self._planner_skill_index("fire", controller, state, modules, directive)
            if selected is None:
                selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
            if selected is None:
                return None
            agent_pb2 = modules["agent_pb2"]
            index, skill = selected
            return index, skill, agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=4), {
                "source": "defensive_combat",
                "skill": "shootable_threat",
                "enemy": int(enemy.get("id", 0) or 0),
                "threat": threat,
                "dist": int(distance),
            }
        if health <= 65 and distance <= 80.0 and abs(turn) <= 12.0:
            selected = self._planner_skill_index("fire", controller, state, modules, directive)
            if selected is None:
                selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
            if selected is None:
                return None
            agent_pb2 = modules["agent_pb2"]
            index, skill = selected
            return index, skill, agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=4), {
                "source": "defensive_combat",
                "skill": "close_visible_threat_shot",
                "enemy": int(enemy.get("id", 0) or 0),
                "threat": threat,
                "dist": int(distance),
                "turn": round(turn, 1),
                "evidence": "visible_close",
            }
        if health > 55 and distance > 384.0 and self._cautious_recent_hit_window <= 0:
            return None
        if distance <= 384.0 and abs(turn) > 10.0:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
            if selected is None:
                return None
            agent_pb2 = modules["agent_pb2"]
            action_type = agent_pb2.ACTION_TURN_LEFT if turn > 0 else agent_pb2.ACTION_TURN_RIGHT
            index, skill = selected
            return index, skill, agent_pb2.PlayerAction(action=action_type, amount=max(6, min(28, int(abs(turn)))), duration_tics=4), {
                "source": "defensive_combat",
                "skill": "align_visible_threat",
                "enemy": int(enemy.get("id", 0) or 0),
                "threat": threat,
                "dist": int(distance),
                "turn": round(turn, 1),
            }
        return None

    def _urgent_hitscan_retaliation_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        constraints = directive.contract.constraints
        if directive.contract.objective != "complete_level":
            return None
        if (
            constraints.get("kill_budget") == 0
            or constraints.get("avoid_combat")
            or constraints.get("ammo_budget") == 0
            or constraints.get("weapon_policy") == "fist_only"
            or constraints.get("preserve_health")
            or self._fire_forbidden(directive, state)
        ):
            return None
        metrics = self._metrics(state)
        health = int(metrics.get("health", 100) or 100)
        if health <= ROUTE_CRITICAL_HEALTH_BREAKAWAY and (
            int(self._critical_turn_and_burn_steps) > 0
            or int(self._critical_turn_and_burn_handoff_steps) > 0
            or int(self._critical_turn_and_burn_deflect_steps) > 0
        ):
            return None
        enemy = self._nearest_enemy(state, prefer_visible=True)
        if enemy is None or not bool(enemy.get("visible")):
            return None
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        if distance > 128.0:
            return None
        threat = str(enemy.get("threat") or "unknown")
        if threat not in {"hitscan", "unknown", "projectile", "melee_rush", "melee"}:
            return None
        if bool(metrics.get("shootable")):
            selected = self._planner_skill_index("fire", controller, state, modules, directive)
            if selected is None:
                selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
            if selected is None:
                return None
            agent_pb2 = modules["agent_pb2"]
            index, skill = selected
            return index, skill, agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=3), {
                "source": "urgent_retaliation",
                "skill": "point_blank_hitscan_retaliation" if threat == "hitscan" else "point_blank_retaliation",
                "enemy": int(enemy.get("id", 0) or 0),
                "threat": threat,
                "dist": int(distance),
            }
        if self._cautious_recent_hit_window <= 0:
            return None
        cover = self._cautious_cover_action(
            state,
            directive,
            controller,
            modules,
            reason="recent_hit_point_blank_break_los",
            enemy=enemy,
            arm_commit=True,
        )
        if cover is None:
            return None
        index, skill, action, decision = cover
        decision = dict(decision)
        decision["source"] = "urgent_retaliation"
        decision["skill"] = str(decision.get("skill") or "point_blank_break_los")
        decision["reason"] = "recent_hit_point_blank_break_los"
        return index, skill, action, decision

    def _record_damage_reaction(
        self,
        directive: ObjectiveDirective,
        damage_delta: int,
        metrics_after: dict[str, Any],
    ) -> None:
        if damage_delta <= 0:
            return
        preserve_health = bool(directive.contract.constraints.get("preserve_health"))
        health_after = int(metrics_after.get("health", 100) or 100)
        survival_objective = directive.contract.objective in {"complete_level", "survive", "recover_health"}
        if not (preserve_health or survival_objective or health_after <= 65):
            return
        self._cautious_recent_hit_window = max(self._cautious_recent_hit_window, 30)
        self._cautious_threshold_cooldown = 0
        self._cautious_ambush_window = max(self._cautious_ambush_window, CAUTIOUS_COVER_AMBUSH_WINDOW)
        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 6)

    def _best_open_cover_side(self, navigation: Any) -> int:
        best_offset = 0
        best_distance = -1
        for probe in getattr(navigation, "direction_probes", []) or []:
            if not bool(getattr(probe, "open", False)):
                continue
            offset = int(getattr(probe, "angle_offset_degrees", 0) or 0)
            if abs(offset) < 45 or abs(offset) > 135:
                continue
            distance = int(getattr(probe, "block_distance_fp", 0) or 0)
            if distance > best_distance:
                best_distance = distance
                best_offset = offset
        return best_offset

    def _raw_side_move_for_cover_side(self, side: int, amount: int) -> int:
        return int(amount) if int(side) < 0 else -int(amount)

    def _raw_steer_turn_units(self, delta: float) -> int:
        return max(
            -int(MACRO_RAW_STEER_TURN_CAP),
            min(int(MACRO_RAW_STEER_TURN_CAP), int(float(delta) * MACRO_RAW_STEER_TURN_SCALE)),
        )

    def _hard_escape_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        """Universal unstick. Fires when the body has physically not moved for many steps
        (regardless of which skill loops — door open/close/backup, barrel wedge, corner).
        Commits to a decisive maneuver: turn a big fixed angle toward the MOST-open probe,
        then bull forward+strafe with large amounts/long durations. Holds the commitment for
        several steps so one gentle nudge doesn't hand back to the planner that re-wedges."""
        HARD_WEDGE_TRIGGER = 6
        if self._hard_escape_active <= 0 and self._hard_wedge_steps < HARD_WEDGE_TRIGGER:
            return None
        selected = (
            self._planner_skill_index("recover_stuck", controller, state, modules, directive)
            or self._planner_skill_index("route_progression", controller, state, modules, directive)
        )
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        # A wedge against a CLOSED DOOR isn't geometry to slide around — it's a door to
        # OPEN. Try USE on the nearest door line first; only bull-rush past if none is in
        # reach (real wall/barrel/corner). This is the fix for "walks into the door and
        # does nothing" across every goal (see wall: door USE never fired from hunt/route).
        if self._planner is not None:
            door_use = self._planner.wedge_door_use_action(state, agent_pb2, self._door_memory, wedge_steps=int(self._hard_wedge_steps))
            if door_use is not None:
                index, skill = selected
                decision = dict(door_use.detail)
                decision["source"] = "hard_escape"
                if door_use.door_line_id is not None:
                    decision["line_id"] = int(door_use.door_line_id)
                self._last_plan = {"status": "active", "skill": skill, "planner_skill": "recover_stuck", "kind": "wedge_door_use"}
                return index, skill, door_use.action, decision
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        navigation = getattr(state, "navigation", None)
        probes = [p for p in getattr(navigation, "direction_probes", []) or [] if bool(getattr(p, "open", False))]

        # Start a fresh escape: choose the turn direction ONCE toward the deepest-open probe
        # (or flip 135° if nothing's open — we're boxed, spin to find a way out).
        if self._hard_escape_active <= 0:
            self._hard_escape_active = 8  # commit for 8 steps
            if probes:
                deepest = max(probes, key=lambda it: int(getattr(it, "block_distance_fp", 0) or 0))
                off = float(getattr(deepest, "angle_offset_degrees", 0) or 0)
                self._hard_escape_dir = 1 if off > 0 else -1
            else:
                self._hard_escape_dir = 1 if (self._hard_escape_dir <= 0) else -1  # alternate hard spins
        self._hard_escape_active -= 1

        # Big turn toward the escape direction + drive forward + a shove sideways to slip off
        # whatever we're caught on. All large + long so it physically clears geometry.
        turn_deg = 40.0 * self._hard_escape_dir
        if raw_cls is not None:
            action = agent_pb2.PlayerAction(
                duration_tics=8,
                raw=raw_cls(
                    angle_turn=self._raw_steer_turn_units(turn_deg),
                    forward_move=50,
                    side_move=40 * self._hard_escape_dir,
                ),
            )
        else:
            # No raw ticcmd: alternate turn then forward across the committed steps.
            if self._hard_escape_active % 2 == 0:
                at = agent_pb2.ACTION_TURN_RIGHT if self._hard_escape_dir > 0 else agent_pb2.ACTION_TURN_LEFT
                action = agent_pb2.PlayerAction(action=at, amount=45, duration_tics=6)
            else:
                action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=50, duration_tics=12)
        index, skill = selected
        decision = {
            "source": "hard_escape",
            "skill": "hard_unstick",
            "action": "turn_and_bull",
            "wedge_steps": int(self._hard_wedge_steps),
            "escape_left": int(self._hard_escape_active),
            "dir": int(self._hard_escape_dir),
        }
        self._last_plan = {"status": "active", "skill": skill, "planner_skill": "recover_stuck", "kind": "hard_unstick"}
        return index, skill, action, decision

    def _stuck_recovery_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        navigation = getattr(state, "navigation", None)
        probes = [
            probe for probe in getattr(navigation, "direction_probes", []) or []
            if bool(getattr(probe, "open", False))
        ]
        if probes:
            if self._recovery_repeat_count >= 4:
                if bool(getattr(navigation, "back_open", False)):
                    action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=36, duration_tics=10)
                    decision = {
                        "source": "stuck_recovery",
                        "skill": "live_probe_escape_backoff",
                        "action": "backoff",
                        "repeat": int(self._recovery_repeat_count),
                    }
                    index, selected_skill = selected
                    self._last_plan = {"status": "active", "skill": selected_skill, "planner_skill": "recover_stuck", "kind": decision["skill"]}
                    return index, selected_skill, action, decision
                probe = max(probes, key=lambda item: int(getattr(item, "block_distance_fp", 0) or 0))
                offset = float(getattr(probe, "angle_offset_degrees", 0) or 0)
                action_type = agent_pb2.ACTION_TURN_RIGHT if offset > 0 else agent_pb2.ACTION_TURN_LEFT
                action = agent_pb2.PlayerAction(action=action_type, amount=48, duration_tics=6)
                decision = {
                    "source": "stuck_recovery",
                    "skill": "live_probe_escape_turn",
                    "probe": round(offset, 1),
                    "repeat": int(self._recovery_repeat_count),
                }
                index, selected_skill = selected
                self._last_plan = {"status": "active", "skill": selected_skill, "planner_skill": "recover_stuck", "kind": decision["skill"]}
                return index, selected_skill, action, decision
            lateral_probes = [
                probe for probe in probes
                if 45 <= abs(float(getattr(probe, "angle_offset_degrees", 0) or 0)) <= 135
            ]
            if self._recovery_forward_stalls >= 2 and lateral_probes:
                probe = max(lateral_probes, key=lambda item: int(getattr(item, "block_distance_fp", 0) or 0))
                offset = float(getattr(probe, "angle_offset_degrees", 0) or 0)
                action_type = agent_pb2.ACTION_STRAFE_LEFT if offset > 0 else agent_pb2.ACTION_STRAFE_RIGHT
                action = agent_pb2.PlayerAction(action=action_type, amount=34, duration_tics=10)
                decision = {
                    "source": "stuck_recovery",
                    "skill": "live_probe_escape_strafe",
                    "action": "strafe",
                    "probe": round(offset, 1),
                    "prior_forward_stalls": int(self._recovery_forward_stalls),
                }
            else:
                probe = max(probes, key=lambda item: int(getattr(item, "block_distance_fp", 0) or 0))
                offset = float(getattr(probe, "angle_offset_degrees", 0) or 0)
                if abs(offset) >= 45:
                    action_type = agent_pb2.ACTION_STRAFE_LEFT if offset > 0 else agent_pb2.ACTION_STRAFE_RIGHT
                    action = agent_pb2.PlayerAction(action=action_type, amount=28, duration_tics=10)
                    decision = {
                        "source": "stuck_recovery",
                        "skill": "live_probe_strafe",
                        "action": "strafe",
                        "probe": round(offset, 1),
                    }
                elif abs(offset) <= 12:
                    action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=42, duration_tics=12)
                    decision = {"source": "stuck_recovery", "skill": "live_probe_forward", "probe": round(offset, 1)}
                else:
                    action_type = agent_pb2.ACTION_TURN_LEFT if offset > 0 else agent_pb2.ACTION_TURN_RIGHT
                    action = agent_pb2.PlayerAction(action=action_type, amount=max(8, min(28, int(abs(offset)))), duration_tics=4)
                    decision = {"source": "stuck_recovery", "skill": "live_probe_turn", "probe": round(offset, 1)}
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_TURN_LEFT, amount=24, duration_tics=6)
            decision = {"source": "stuck_recovery", "skill": "scan_turn"}
        index, selected_skill = selected
        self._last_plan = {"status": "active", "skill": selected_skill, "planner_skill": "recover_stuck", "kind": decision["skill"]}
        return index, selected_skill, action, decision

    def _movement_stalled(self, action: Any, step_moved: float, modules: dict[str, Any]) -> bool:
        action_type = int(getattr(action, "action", 0) or 0)
        agent_pb2 = modules["agent_pb2"]
        movement_actions = {
            int(agent_pb2.ACTION_FORWARD),
            int(agent_pb2.ACTION_BACKWARD),
            int(agent_pb2.ACTION_STRAFE_LEFT),
            int(agent_pb2.ACTION_STRAFE_RIGHT),
        }
        if action_type in movement_actions:
            return float(step_moved) < 6.0
        raw = getattr(action, "raw", None)
        if raw is None:
            return False
        raw_forward = int(getattr(raw, "forward_move", 0) or 0)
        raw_side = int(getattr(raw, "side_move", 0) or 0)
        return bool(raw_forward or raw_side) and float(step_moved) < 6.0

    def _record_recovery_outcome(
        self,
        action: Any,
        decision: dict[str, Any],
        step_moved: float,
        modules: dict[str, Any],
    ) -> None:
        if str(decision.get("source", "")) != "stuck_recovery":
            if not self._movement_stalled(action, step_moved, modules):
                self._recovery_forward_stalls = 0
                if float(step_moved) >= 48.0:
                    self._recovery_repeat_key = None
                    self._recovery_repeat_count = 0
            return
        skill_name = str(decision.get("skill", ""))
        try:
            probe = round(float(decision.get("probe", 0.0) or 0.0), 1)
        except Exception:
            probe = 0.0
        key = (skill_name, probe)
        if key == self._recovery_repeat_key:
            self._recovery_repeat_count += 1
        else:
            self._recovery_repeat_key = key
            self._recovery_repeat_count = 1
        if str(decision.get("skill", "")) == "live_probe_forward" and self._movement_stalled(action, step_moved, modules):
            self._recovery_forward_stalls += 1
            if self._recovery_forward_stalls >= 2:
                self._charge_recovery_loop_to_last_planner_line(status="repeated_recovery_forward_stall")
            return
        if not self._movement_stalled(action, step_moved, modules):
            self._recovery_forward_stalls = 0
        if self._recovery_repeat_count >= 3 and skill_name in {
            "live_probe_turn",
            "live_probe_escape_turn",
            "live_probe_strafe",
            "live_probe_escape_strafe",
            "live_probe_escape_backoff",
            "scan_turn",
        }:
            self._charge_recovery_loop_to_last_planner_line(status="repeated_recovery_turn")

    def _charge_recovery_loop_to_last_planner_line(self, *, status: str) -> bool:
        line_id = self._last_spatial_route_line_id
        if line_id is None:
            return False
        line = self._planner._line_by_id(int(line_id)) if self._planner is not None else None
        try:
            special = int(getattr(line, "special", 0) or 0)
            tag = int(getattr(line, "tag", 0) or 0)
        except Exception:
            special = 0
            tag = 0
        self._door_memory.observe_line(
            int(line_id),
            special=special,
            tag=tag,
            exit_line=bool(getattr(line, "exit", False)),
        )
        if bool(getattr(line, "passable", False) or getattr(line, "door", False) or getattr(line, "use_trigger", False)):
            record_route_contact = getattr(self._door_memory, "record_route_contact", None)
            if callable(record_route_contact):
                record_route_contact(int(line_id))
            else:
                self._door_memory.record_failure(int(line_id), status=status)
        else:
            self._door_memory.record_failure(int(line_id), status=status)
        self._last_spatial_route_line_id = None
        self._recovery_repeat_key = None
        self._recovery_repeat_count = 0
        return True

    def _maybe_request_vision(
        self,
        directive: ObjectiveDirective,
        previous: Any,
        current_state: Any,
        action: Any,
        decision: dict[str, Any],
        modules: dict[str, Any],
        *,
        step: int,
        stuck_steps: int,
        step_moved: float,
        previous_metrics: dict[str, Any],
        current_metrics: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            trigger = self._vision_trigger(
                directive,
                current_state,
                action,
                decision,
                modules,
                stuck_steps=stuck_steps,
                step_moved=step_moved,
                previous_metrics=previous_metrics,
                current_metrics=current_metrics,
            )
        except Exception:
            return None
        if trigger is None:
            return None
        try:
            context = self._vision_context(
                directive,
                current_state,
                action,
                decision,
                modules,
                step=step,
                stuck_steps=stuck_steps,
                step_moved=step_moved,
                previous_metrics=previous_metrics,
                current_metrics=current_metrics,
            )
            return self._vision.request(trigger, context)
        except Exception:
            return None

    def _vision_trigger(
        self,
        directive: ObjectiveDirective,
        state: Any,
        action: Any,
        decision: dict[str, Any],
        modules: dict[str, Any],
        *,
        stuck_steps: int,
        step_moved: float,
        previous_metrics: dict[str, Any],
        current_metrics: dict[str, Any],
    ) -> str | None:
        constraints = directive.contract.constraints
        rules = set(directive.rules)
        health_drop = int(current_metrics.get("health", 0)) < int(previous_metrics.get("health", 0))
        no_kill_exit = (
            directive.contract.style == "speedrun"
            and constraints.get("kill_budget") == 0
            and bool({"exit", "complete_level"} & rules)
        )
        if no_kill_exit and health_drop:
            return "no_kill_speedrun_damage"

        combat_contact = bool(
            previous_metrics.get("visible_enemy")
            or previous_metrics.get("shootable")
            or current_metrics.get("visible_enemy")
            or current_metrics.get("shootable")
        )
        if combat_contact:
            return None

        line_id = decision.get("line_id")
        if line_id is None:
            line_id = decision.get("line")
        line_status = self._door_memory.last_status_for(line_id) if line_id is not None else ""
        action_type = int(getattr(action, "action", 0) or 0)
        agent_pb2 = modules["agent_pb2"]
        is_use = action_type == int(agent_pb2.ACTION_USE)
        is_stalled = self._movement_stalled(action, step_moved, modules)
        planner_skill = str(decision.get("planner_skill") or decision.get("skill") or "")

        if line_id is not None and (is_use or "use" in planner_skill or "door" in planner_skill):
            if (
                (is_use and float(step_moved) < 6.0 and self._door_memory.attempts_for(line_id) >= 2)
                or line_status in {"no_progress_after_use", "assumed_opening", "stale_open_blocked", "route_contact_blocked"}
            ):
                return "repeated_failed_use"

        if is_stalled and str(decision.get("source", "")) == "spatial_planner":
            nav = getattr(state, "navigation", None)
            route_says_open = (
                bool(getattr(nav, "forward_open", False))
                or self._door_memory.is_open(line_id)
                or planner_skill.startswith("sector_route")
                or planner_skill.startswith("route")
            )
            if route_says_open:
                return "route_open_but_blocked"

        if stuck_steps >= 3 and is_stalled:
            return "stuck_same_coordinates"

        if {"exit", "complete_level"} & rules:
            near_exit = 0 < int(current_metrics.get("exit_dist", 0) or 0) <= 160
            exit_unconfirmed = not bool(current_metrics.get("exit_line", False))
            if near_exit and exit_unconfirmed and (is_use or is_stalled or "exit" in planner_skill):
                return "exit_target_ambiguous"
        return None

    def _vision_context(
        self,
        directive: ObjectiveDirective,
        state: Any,
        action: Any,
        decision: dict[str, Any],
        modules: dict[str, Any],
        *,
        step: int,
        stuck_steps: int,
        step_moved: float,
        previous_metrics: dict[str, Any],
        current_metrics: dict[str, Any],
    ) -> dict[str, Any]:
        nav = getattr(state, "navigation", None)
        try:
            action_summary = modules["summarize_action"](action) if "summarize_action" in modules else {}
        except Exception:
            action_summary = {}
        line_id = decision.get("line_id", decision.get("line"))
        return {
            "objective": directive.contract.objective,
            "style": directive.contract.style,
            "rules": list(directive.rules)[:6],
            "step": int(step),
            "skill": str(decision.get("planner_skill") or decision.get("skill") or "")[:48],
            "action": int(action_summary.get("action", getattr(action, "action", 0)) or 0),
            "line": int(line_id) if line_id is not None else None,
            "line_state": self._door_memory.state_for(line_id),
            "line_status": self._door_memory.last_status_for(line_id),
            "moved": round(float(step_moved), 2),
            "stuck": int(stuck_steps),
            "hp_delta": int(current_metrics.get("health", 0)) - int(previous_metrics.get("health", 0)),
            "exit_dist": int(current_metrics.get("exit_dist", 0) or 0),
            "forward_open": bool(getattr(nav, "forward_open", False)),
            "front_dist": int((getattr(nav, "front_block_distance_fp", 0) or 0) / FP_UNIT),
        }

    def _route_recovery_allowed_under_contact(self, directive: ObjectiveDirective) -> bool:
        rules = set(directive.rules)
        constraints = directive.contract.constraints
        return (
            directive.contract.style == "speedrun"
            and constraints.get("kill_budget") == 0
            and bool({"exit", "complete_level"} & rules)
        )

    def _update_world_model(self, state: Any) -> None:
        planner = self._planner
        if planner is None:
            return
        self._reset_episode_memory_if_needed(state)
        navigation = getattr(state, "navigation", None)
        for line in getattr(navigation, "use_lines", []) or []:
            line_id = int(getattr(line, "line_id", -1))
            if line_id >= 0:
                special = int(getattr(line, "special", 0))
                tag = int(getattr(line, "tag", 0))
                self._door_memory.observe_line(line_id, special=special, tag=tag, exit_line=special in {11, 51})
        self._world_memory.update(state, planner)
        # Repair EVERY held color each update, not just newly-acquired deltas: a door can be
        # flagged requires_key AFTER its key was already picked up (E1M2 line 527 regression).
        for color in sorted(set(getattr(self._world_memory, "acquired_keys", set()) or set())):
            self._door_memory.mark_key_acquired(color)
        self._last_probes = self._probe_batcher.snapshot(state, planner)

    def _reset_episode_memory_if_needed(self, state: Any) -> None:
        level = getattr(state, "level", None)
        key = (int(getattr(level, "episode", 1) or 1), int(getattr(level, "map", 1) or 1))
        tick = int(getattr(state, "tick", 0) or 0)
        if self._last_level_key is not None and (key != self._last_level_key or tick < int(self._last_tick or 0)):
            self._door_memory = DoorMemory()
            self._world_memory = WorldMemory()
            self._combat_state = CombatState()
            self._last_plan = None
            self._last_probes = None
            self._frontier_route_repeat_counts = {}
            self._planner_probe_escape_steps = 0
        self._last_level_key = key
        self._last_tick = tick

    def _select_skill(self, controller: Any, state: Any, directive: ObjectiveDirective, modules: dict[str, Any]) -> tuple[int, str]:
        actions = list(modules["SKILL_ACTIONS"])
        mask = list(controller.action_mask(state))
        heuristic = controller.heuristic_action_index(state)
        if 0 <= heuristic < len(actions) and mask[heuristic] and actions[heuristic] in directive.allowed_skills:
            return heuristic, actions[heuristic]
        for skill in directive.allowed_skills:
            if skill in actions:
                index = actions.index(skill)
                if index < len(mask) and mask[index]:
                    return index, skill
        for fallback in ("recover_stuck", "retreat", "route_progression"):
            if fallback in directive.allowed_skills and fallback in actions:
                index = actions.index(fallback)
                if index < len(mask) and mask[index]:
                    return index, fallback
        # The controller's heuristic may be an unsafe combat action when all contract-safe
        # skills are currently masked. Preserve the objective contract and let action_for
        # run a deterministic safe primitive instead of falling through to fire/engage.
        for fallback in ("recover_stuck", "retreat", "route_progression", "open_use_line", "press_exit", "seek_enemy"):
            if fallback in directive.allowed_skills and fallback in actions:
                return actions.index(fallback), fallback
        if 0 <= heuristic < len(actions) and actions[heuristic] in directive.allowed_skills:
            return heuristic, actions[heuristic]
        return 0, actions[0]

    def _metrics(self, state: Any) -> dict[str, Any]:
        player = getattr(state, "player", None)
        level = getattr(state, "level", None)
        combat = getattr(state, "combat", None)
        obj = getattr(player, "object", None)
        pos = getattr(obj, "position", None)
        ammo = getattr(player, "ammo", None)
        enemies = list(getattr(state, "enemies", []) or [])
        weapon = self._weapon_id(state)
        bullets = int(getattr(ammo, "bullets", 0))
        shells = int(getattr(ammo, "shells", 0))
        rockets = int(getattr(ammo, "rockets", 0))
        cells = int(getattr(ammo, "cells", 0))
        exit_ready, exit_distance = self._exit_affordance(state)
        nearest_enemy = self._nearest_enemy(state, prefer_visible=True)
        nearest_enemy_dist = int(float(nearest_enemy.get("distance", 0.0))) if nearest_enemy is not None else 0
        return {
            "tick": int(getattr(state, "tick", 0)),
            "episode": int(getattr(level, "episode", 0) or 0),
            "map": int(getattr(level, "map", 0) or 0),
            "x": int(getattr(pos, "x_fp", 0)),
            "y": int(getattr(pos, "y_fp", 0)),
            "angle": float(getattr(obj, "angle_degrees", 0) or 0),
            "health": int(getattr(player, "health", 0)),
            "kills": int(getattr(player, "kills", 0)),
            "total_kills": int(getattr(level, "total_kills", 0)),
            "bullets": bullets,
            "shells": shells,
            "rockets": rockets,
            "cells": cells,
            "ammo_total": bullets + shells + rockets + cells,
            "weapon": weapon,
            "enemy_count": len(enemies),
            "nearest_enemy_dist": nearest_enemy_dist,
            "visible_enemy": any(bool(getattr(enemy, "line_of_sight", False)) for enemy in enemies),
            "shootable": bool(getattr(combat, "has_shootable_target", False) and getattr(combat, "target_is_enemy", False)),
            "exit_line": exit_ready,
            "exit_dist": exit_distance,
        }

    def _navigation_metrics(self, state: Any) -> dict[str, Any]:
        navigation = getattr(state, "navigation", None)
        side = []
        for probe in getattr(navigation, "direction_probes", []) or []:
            offset = int(getattr(probe, "angle_offset_degrees", 0) or 0)
            if abs(offset) in {45, 90, 135}:
                side.append(
                    [
                        offset,
                        int(bool(getattr(probe, "open", False))),
                        int((getattr(probe, "block_distance_fp", 0) or 0) / FP_UNIT),
                    ]
                )
        return {
            "fo": int(bool(getattr(navigation, "forward_open", False))),
            "bo": int(bool(getattr(navigation, "back_open", False))),
            "fd": int((getattr(navigation, "front_block_distance_fp", 0) or 0) / FP_UNIT),
            "side": side[:4],
        }

    def _weapon_id(self, state: Any) -> int:
        player = getattr(state, "player", None)
        raw = getattr(player, "ready_weapon", 0)
        if isinstance(raw, int):
            return int(raw)
        text = str(raw or "").upper()
        names = {
            "WEAPON_FIST": 0,
            "FIST": 0,
            "WP_FIST": 0,
            "WEAPON_PISTOL": 1,
            "PISTOL": 1,
            "WEAPON_SHOTGUN": 2,
            "SHOTGUN": 2,
            "WEAPON_CHAINGUN": 3,
            "CHAINGUN": 3,
            "WEAPON_ROCKET": 4,
            "WEAPON_MISSILE": 4,
            "ROCKET": 4,
            "WEAPON_PLASMA": 5,
            "PLASMA": 5,
            "WEAPON_BFG": 6,
            "BFG": 6,
            "WEAPON_CHAINSAW": 7,
            "CHAINSAW": 7,
        }
        return names.get(text, 0 if "FIST" in text else -1)

    def _nearest_enemy(self, state: Any, *, prefer_visible: bool = False) -> dict[str, Any] | None:
        player = getattr(state, "player", None)
        pobj = getattr(player, "object", None)
        ppos = getattr(pobj, "position", None)
        px = int(getattr(ppos, "x_fp", 0))
        py = int(getattr(ppos, "y_fp", 0))
        angle = float(getattr(pobj, "angle_degrees", 0) or 0)
        enemies: list[dict[str, Any]] = []
        combat = getattr(state, "combat", None)
        combat_target = int(getattr(combat, "target_id", 0) or 0)
        has_shootable_target = bool(getattr(combat, "has_shootable_target", False) and getattr(combat, "target_is_enemy", False))
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            pos = getattr(obj, "position", None)
            if obj is None or pos is None or int(getattr(obj, "health", 0) or 0) <= 0:
                continue
            ex = int(getattr(pos, "x_fp", 0))
            ey = int(getattr(pos, "y_fp", 0))
            dx = (ex - px) / FP_UNIT
            dy = (ey - py) / FP_UNIT
            dist = math.hypot(dx, dy)
            bearing = math.degrees(math.atan2(dy, dx)) % 360.0 if dist else angle
            turn = ((bearing - angle + 540.0) % 360.0) - 180.0
            type_id = int(getattr(obj, "type_id", 0) or 0)
            enemy_id = int(getattr(obj, "id", 0) or 0)
            enemies.append({
                "id": enemy_id,
                "type_id": type_id,
                "threat": classify_enemy(enemy),
                "distance": dist,
                "turn": turn,
                "visible": bool(getattr(enemy, "line_of_sight", False)),
                "shootable_target": bool(has_shootable_target and combat_target == enemy_id),
                "health": int(getattr(obj, "health", 0) or 0),
            })
        if not enemies:
            return None
        return min(enemies, key=lambda item: (not item["shootable_target"], 0 if item["visible"] and prefer_visible else 1, item["distance"]))

    def _exit_affordance(self, state: Any) -> tuple[bool, int]:
        navigation = getattr(state, "navigation", None)
        best: int | None = None
        for line in getattr(navigation, "use_lines", []) or []:
            if int(getattr(line, "special", 0)) in {11, 51}:
                distance = int(getattr(line, "nearest_distance_fp", 0) or getattr(line, "distance_fp", 0) or 0)
                if distance > 0:
                    best = distance if best is None else min(best, distance)
        waypoint = getattr(navigation, "route_waypoint", None)
        if bool(getattr(waypoint, "exit", False)):
            line = getattr(waypoint, "line", None)
            distance = int(getattr(line, "nearest_distance_fp", 0) or getattr(line, "distance_fp", 0) or 0)
            if distance > 0:
                best = distance if best is None else min(best, distance)
        if best is None:
            return False, 0
        return best <= 96 * FP_UNIT, int(best / FP_UNIT)

    def _exit_line_visible(self, state: Any) -> bool:
        return self._exit_affordance(state)[0]

    def _action_fired(self, action_summary: dict[str, Any], agent_pb2: Any | None = None) -> bool:
        if agent_pb2 is not None and int(action_summary.get("action", 0)) == int(agent_pb2.ACTION_SHOOT):
            return True
        raw = action_summary.get("raw") or {}
        if bool(int(raw.get("buttons", 0)) & 1):
            return True
        mouse = action_summary.get("mouse") or {}
        if bool(int(mouse.get("buttons", 0)) & 1):
            return True
        return False

    def _protobuf_action_fired(self, action: Any, agent_pb2: Any) -> bool:
        if int(getattr(action, "action", 0) or 0) == int(agent_pb2.ACTION_SHOOT):
            return True
        raw = getattr(action, "raw", None)
        if raw is not None and bool(int(getattr(raw, "buttons", 0) or 0) & 1):
            return True
        mouse = getattr(action, "mouse", None)
        if mouse is not None and bool(int(getattr(mouse, "buttons", 0) or 0) & 1):
            return True
        return False

    def _human_active(self) -> bool:
        if self._ignore_human_interrupt:
            return False
        now = time.monotonic()
        if now - self._human_check_at < 0.25:
            return self._human_check_active
        try:
            with urllib.request.urlopen(COPLAY_STATE_URL, timeout=0.5) as resp:
                active = (json.loads(resp.read() or b"{}")).get("owner") == "human"
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            active = False
        self._human_check_at = now
        self._human_check_active = active
        return active


RUNTIME = BrainRuntime()


def drive_objective(body: dict[str, Any] | None = None) -> dict[str, Any]:
    return RUNTIME.drive(body or {})


def drive_ticks(body: dict[str, Any] | None = None) -> dict[str, Any]:
    return RUNTIME.drive_ticks(body or {})


def drive_goal(body: dict[str, Any] | None = None) -> dict[str, Any]:
    return RUNTIME.drive_goal(body or {})


def brain_status() -> dict[str, Any]:
    return RUNTIME.status()


def brain_memory() -> dict[str, Any]:
    return RUNTIME.memory()


def map_status() -> dict[str, Any]:
    return RUNTIME.map_status()


def vision_status() -> dict[str, Any]:
    return RUNTIME.vision_status()


def tactical_status() -> dict[str, Any]:
    return RUNTIME.tactical_status()


def reset_brain_episode() -> dict[str, Any]:
    return RUNTIME.reset_episode()
