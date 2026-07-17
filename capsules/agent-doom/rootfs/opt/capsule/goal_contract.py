#!/usr/bin/env python3.11
"""Free-form goal compiler for the Agent DOOM capsule.

This is deliberately small and deterministic. Codex or another MCP host can
send natural language, but the in-capsule brain executes a structured contract
with explicit constraints and evidence requirements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

OBJECTIVE_ENUMS = {
    "exit_level",
    "complete_level",
    "rampage",
    "find_enemy",
    "kill_enemy",
    "clear_area",
    "explore",
    "survive",
    "recover_health",
    "preserve_health",
}


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().replace("'", "").split())


def _has(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _norm_token(value: Any) -> str:
    return "_".join(part for part in _norm(str(value)).replace("-", "_").split("_") if part)


def _apply_constraint_token(
    token: str,
    constraints: dict[str, Any],
    forbidden: list[str],
    failure: list[str],
    *,
    success: list[str] | None = None,
) -> None:
    token = _norm_token(token)
    if token in {"no_kill", "no_kills", "without_killing", "kill_budget_zero"}:
        constraints["kill_budget"] = 0
        forbidden.extend(["shoot_enemy", "melee_enemy", "attack_enemy"])
        failure.append("kill_delta")
        if success is not None:
            success.append("kills_unchanged")
    elif token in {"no_ammo", "dont_use_ammo", "do_not_use_ammo", "ammo_budget_zero"}:
        constraints["ammo_budget"] = 0
        forbidden.append("spend_ammo")
        failure.append("ammo_delta")
        if success is not None:
            success.append("ammo_unchanged")
    elif token in {"fist_only", "punch_only", "melee_only", "no_ranged"}:
        constraints["weapon_policy"] = "fist_only"
        forbidden.append("ranged_fire")
        failure.append("non_fist_attack")
    elif token in {"avoid_combat", "avoid_enemies"}:
        constraints["avoid_combat"] = True
    elif token in {"avoid_damage", "preserve_health", "safe"}:
        constraints["preserve_health"] = True
        failure.append("health_drop")
    elif token in {"speedrun", "fast"}:
        constraints["style_hint"] = "speedrun"


@dataclass(frozen=True)
class GoalContract:
    raw: str
    objective: str
    style: str = "balanced"
    constraints: dict[str, Any] = field(default_factory=dict)
    forbidden: tuple[str, ...] = ()
    success_evidence: tuple[str, ...] = ()
    failure_evidence: tuple[str, ...] = ()

    def compact(self) -> dict[str, Any]:
        """Small status form safe for normal MCP output."""
        constraints = []
        if self.constraints.get("kill_budget") == 0:
            constraints.append("no_kills")
        if self.constraints.get("ammo_budget") == 0:
            constraints.append("no_ammo")
        if self.constraints.get("weapon_policy"):
            constraints.append(str(self.constraints["weapon_policy"]))
        if self.constraints.get("avoid_combat"):
            constraints.append("avoid_combat")
        return {
            "obj": self.objective,
            "style": self.style,
            "rules": constraints[:4],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "objective": self.objective,
            "style": self.style,
            "constraints": dict(self.constraints),
            "forbidden": list(self.forbidden),
            "success_evidence": list(self.success_evidence),
            "failure_evidence": list(self.failure_evidence),
        }


def compile_goal_contract(goal: str, payload: dict[str, Any] | None = None) -> GoalContract:
    payload = dict(payload or {})
    raw = str(goal or payload.get("objective") or payload.get("goal") or "").strip()
    text = _norm(raw)

    wants_enemy = _has(text, "enemy", "enemies", "bad guy", "bad guys", "monster", "monsters",
                       "imp", "demon", "demons", "zombie", "zombies", "them", "everyone")
    # Combat VERBS — the natural way people say "go be aggressive". "fight demons",
    # "shoot the bad guys", "attack", "hunt", "wreck" all mean COMBAT, not explore.
    wants_fight = _has(text, "fight", "shoot", "attack", "hunt", "battle", "wreck", "destroy",
                       "blast", "gun down", "take out", "take down", "murder", "slay", "frag",
                       "engage", "combat", "aggressive", "go ham", "rip and tear")
    wants_kill = wants_fight or _has(
        text,
        "kill",
        "punch",
        "beat up",
        "down",
        "melee",
        "clear room",
        "clear the room",
        "clear this room",
        "clear area",
        "clear the area",
        "clear level",
    )
    wants_exit = _has(text, "exit", "finish", "complete", "beat the level", "next level", "end level", "escape", "leave the level")
    wants_find = _has(text, "find", "seek", "locate", "search")
    wants_health = _has(text, "health", "medkit", "stimpack", "recover")
    wants_survive = _has(text, "survive", "stay alive", "dont die", "do not die")
    wants_preserve_health = _has(
        text,
        "preserve health",
        "avoid damage",
        "dont get hurt",
        "do not get hurt",
        "stay healthy",
        "keep health",
        "no damage",
        "without damage",
        # Cautious phrasing also means preserve-health — fold it in HERE (not only at the
        # constraint line below) so the rampage gate sees it: "fight carefully",
        # "clear it safely" must stay cautious, not become an aggressive rampage.
        "safely",
        "safe route",
        "low damage",
        "careful",
        "carefully",
    )

    # RAMPAGE — the demo autopilot: hunt/kill enemies WHILE advancing toward the exit.
    # Must beat wants_exit (which would otherwise swallow any goal mentioning "exit" and
    # drop the combat half — "clear the map of enemies and reach the exit" was parsing to
    # plain exit_level, ignoring the enemies). Fires when a goal asks for BOTH combat and
    # map progress, or uses rampage phrasing directly.
    wants_map = _has(text, "the map", "whole map", "the level", "everything", "all the enemies", "all enemies", "through the level", "through the map", "map of enemies")
    wants_rampage = wants_fight or _has(text, "rampage", "hunt and", "fight through", "shoot your way", "clear the map", "clear the level", "kill everything")
    # Two DIFFERENT signals, don't conflate:
    #  - refuses_combat: explicitly won't fight ("don't fight", "no kills", "sneak past").
    #    This means NON-combat objectives (find/exit), never kill/clear/rampage.
    #  - wants_preserve_health ("safely", "carefully"): still FIGHTS, just cautiously —
    #    stays a clear/kill goal, only blocks the aggressive RAMPAGE default.
    refuses_combat = _has(
        text, "no kills", "no kill", "without killing", "dont kill", "do not kill",
        "kill nobody", "pacifist", "nonviolent", "avoid killing", "dont hurt anyone",
        "do not hurt anyone", "without a fight", "no fighting", "avoid combat",
        "avoid enemies", "avoid the enemies", "dont fight", "do not fight",
        "avoid fighting", "avoid the fight", "without fighting", "no combat",
        "dont engage", "do not engage", "sneak past", "run past", "skip the")
    forbids_combat = refuses_combat or wants_preserve_health  # gates the RAMPAGE default only
    # One-shot MANUAL commands (move left, shoot once, turn around, open door): a single
    # low-level action the user wants executed literally, NOT a strategy. These pass through
    # to a manual/explore objective; everything else that isn't an explicit non-combat goal
    # defaults to RAMPAGE — if someone's driving DOOM with an LLM, they want to see combat.
    manual_oneshot = _has(text, "move left", "move right", "move forward", "move back",
                          "move backward", "strafe left", "strafe right", "turn left",
                          "turn right", "turn around", "step forward", "step back",
                          "look left", "look right", "back up", "go left", "go right",
                          "go forward", "open the door", "open door", "open the next door",
                          "open the", "use the door", "use door", "press use", "shoot once",
                          "fire once", "one shot", "punch once")
    # A fists/melee goal at a SPECIFIC enemy ("punch the imp", "take down the imp with
    # fists") is a single kill, not a map rampage — keep it kill_enemy below.
    wants_melee_single = _has(text, "fist", "fists", "punch", "melee") and wants_enemy and not wants_map
    if manual_oneshot:
        objective = "explore"  # literal manual step; the planner just moves/acts as asked
    elif not forbids_combat and not wants_melee_single and (
        wants_rampage
        or (wants_kill and (wants_exit or wants_map))
        or (wants_enemy and wants_kill and wants_exit)
    ):
        objective = "rampage"
    elif wants_exit:
        objective = "complete_level" if _has(text, "beat", "complete", "finish", "next level") else "exit_level"
    elif wants_survive and not wants_enemy:
        objective = "survive"
    elif refuses_combat and wants_enemy:
        # "seek enemies but don't fight" — enemy interest is real but combat is refused
        # ("fight" here is inside "don't fight"): find/observe, don't kill.
        objective = "find_enemy"
    elif wants_kill and wants_enemy and not refuses_combat:
        objective = "kill_enemy"
    elif wants_kill and not refuses_combat:
        # "clear room, no damage"/"clear this room safely" is a CLEAR goal that wants
        # safety — clear_area (preserve_health rides along as a constraint), NOT the pure
        # preserve_health objective. refuses_combat (not forbids) so "safely" still clears.
        objective = "clear_area"
    elif _has(text, "wander", "explore", "look around", "walk around", "roam", "scout",
              "wander around", "have a look", "check out"):
        # Explicit exploration intent ("scout ahead safely") stays exploration — checked
        # BEFORE preserve_health so "safely" doesn't turn a scout into a survive/preserve goal.
        objective = "explore"
    elif wants_preserve_health:
        objective = "preserve_health"
    elif wants_health:
        objective = "recover_health"
    elif wants_find and wants_enemy:
        objective = "find_enemy"
    elif wants_survive:
        objective = "survive"
    else:
        # DEFAULT: assume they want to watch DOOM played — go aggressive.
        objective = "rampage"

    style = "balanced"
    if _has(text, "race", "fast", "fastest", "asap", "speedrun", "quick", "run for the exit", "make a run", "dash", "sprint", "rush", "book it"):
        style = "speedrun"
    if _has(text, "punch", "fist only", "fists only", "with fists", "use fists", "melee", "beat up", "beat him down", "beat them down"):
        style = "melee"
    if _has(text, "careful", "cautious", "safe"):
        style = "cautious"

    constraints: dict[str, Any] = {}
    forbidden: list[str] = []
    failure: list[str] = ["player_dead"]
    success: list[str] = []

    no_kills = _has(
        text,
        "no kills",
        "no kill",
        "without killing",
        "dont kill",
        "do not kill",
        "kill nobody",
        "pacifist",
        "nonviolent",
        "avoid killing",
        "dont hurt anyone",
        "do not hurt anyone",
    )
    no_ammo = _has(
        text,
        "no ammo",
        "without ammo",
        "without using ammo",
        "dont use ammo",
        "dont use any ammo",
        "do not use ammo",
        "do not use any ammo",
        "save ammo",
        "conserve ammo",
        "dont waste bullets",
        "do not waste bullets",
        "dont waste ammo",
        "do not waste ammo",
        "hold fire",
    )
    avoid_combat = no_kills or _has(text, "avoid combat", "avoid fighting", "without fighting", "dont fight", "do not fight")
    fist_only = style == "melee" or _has(text, "fist only", "fists only", "punch only")
    preserve_health = wants_preserve_health or _has(text, "safely", "safe route", "low damage", "careful", "carefully")

    if no_kills:
        _apply_constraint_token("no_kills", constraints, forbidden, failure)
    if no_ammo:
        _apply_constraint_token("no_ammo", constraints, forbidden, failure)
    if avoid_combat:
        _apply_constraint_token("avoid_combat", constraints, forbidden, failure)
    if preserve_health:
        _apply_constraint_token("preserve_health", constraints, forbidden, failure)
    if fist_only:
        _apply_constraint_token("fist_only", constraints, forbidden, failure)
    if objective == "clear_area":
        constraints.setdefault("kill_target", 3 if _has(text, "level") else 2)
    if objective == "rampage":
        # Clear the map: a high kill target so it keeps hunting rather than declaring
        # victory after one kill. Success is kills, NOT touching an exit line — the exit
        # is the through-line/direction, not the win condition (that false-terminated the
        # old exit_level parse at reached_exit with 6 enemies alive).
        constraints.setdefault("kill_target", 8)

    if objective in {"complete_level", "exit_level"}:
        success.append("level_transition" if objective == "complete_level" else "exit_reached")
    if objective == "rampage":
        success.append("kill_delta")
    if objective in {"kill_enemy", "clear_area"}:
        success.append("kill_delta")
    if objective == "find_enemy":
        success.append("enemy_visible")
    if objective == "recover_health":
        success.append("health_delta")
    if objective == "survive":
        success.append("survived_window")
    if objective == "preserve_health":
        success.append("survived_window")
        success.append("health_preserved")
    if objective == "explore":
        success.append("position_delta")
    if constraints.get("ammo_budget") == 0:
        success.append("ammo_unchanged")
    if constraints.get("kill_budget") == 0:
        success.append("kills_unchanged")

    overrides = payload.get("constraints")
    if isinstance(overrides, dict):
        constraints.update(overrides)
    elif isinstance(overrides, (list, tuple)):
        for token in overrides:
            _apply_constraint_token(str(token), constraints, forbidden, failure, success=success)
    if payload.get("style"):
        style = str(payload["style"]).strip().lower() or style
    if constraints.pop("style_hint", None) == "speedrun":
        style = "speedrun"
    explicit_objective = _norm_token(payload.get("objective_type") or "")
    if explicit_objective in OBJECTIVE_ENUMS:
        objective = explicit_objective

    return GoalContract(
        raw=raw,
        objective=objective,
        style=style,
        constraints=constraints,
        forbidden=tuple(dict.fromkeys(forbidden)),
        success_evidence=tuple(dict.fromkeys(success)),
        failure_evidence=tuple(dict.fromkeys(failure)),
    )


def contract_rules(contract: GoalContract) -> tuple[str, ...]:
    rules: list[str] = []
    if contract.objective in {"kill_enemy", "clear_area"}:
        rules.extend(["attack", "find_enemy"])
    if contract.objective == "rampage":
        # Hunt AND advance: combat rules first (the content) + exit/explore (the
        # direction). The planner's hunt-and-advance gate (_enemies_remain) keeps it
        # on the map while enemies live, then the exit rules pull it forward.
        rules.extend(["attack", "find_enemy", "exit", "use", "explore"])
    if contract.objective == "find_enemy":
        rules.append("find_enemy")
    if contract.objective in {"exit_level", "complete_level"}:
        rules.extend(["complete_level" if contract.objective == "complete_level" else "exit", "exit", "use", "explore"])
    if contract.objective == "recover_health":
        rules.extend(["survive", "explore"])
    if contract.objective in {"survive", "preserve_health"}:
        rules.append("survive")
    if contract.objective == "explore":
        rules.append("explore")
    if contract.style == "melee" and "attack" not in rules:
        rules.append("attack")
    return tuple(dict.fromkeys(rules or ["explore"]))


def filter_allowed_skills(skills: list[str], contract: GoalContract) -> list[str]:
    out = list(dict.fromkeys(skill for skill in skills if skill))
    constraints = contract.constraints
    if constraints.get("kill_budget") == 0:
        out = [s for s in out if s not in {"fire", "engage", "close_visible_contact"}]
    elif constraints.get("ammo_budget") == 0:
        out = [s for s in out if s not in {"fire", "engage"}]
    if contract.style == "speedrun":
        preferred = ["press_exit", "open_use_line", "route_progression", "recover_stuck", "retreat"]
        out = [s for s in preferred if s in out] + [s for s in out if s not in preferred]
        if constraints.get("kill_budget") == 0 and not constraints.get("preserve_health") and not constraints.get("avoid_combat"):
            out = [s for s in out if s != "retreat"]
    if contract.style == "melee":
        preferred = ["close_visible_contact", "seek_enemy", "route_progression", "recover_stuck", "retreat", "open_use_line"]
        out = [s for s in preferred if s in out] + [s for s in out if s not in preferred]
    return list(dict.fromkeys(out))
