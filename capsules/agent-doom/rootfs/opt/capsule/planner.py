#!/usr/bin/env python3.11
"""Capsule-local spatial planner for Agent DOOM objectives."""

from __future__ import annotations

import math
from typing import Any, Iterable

from planner_model import (
    BARREL_ALIGN_DEGREES,
    BARREL_BLAST_UNITS,
    BARREL_MAX_SHOT_UNITS,
    BARREL_SAFE_UNITS,
    BARREL_SELF_CHAIN_GUARD_UNITS,
    BARREL_THING_TYPES,
    BarrelRuntime,
    CELL_UNITS,
    COMBAT_STALEMATE_STEPS,
    FP_UNIT,
    HEALTH_SEEK_MAX_TARGET_THREAT,
    HEALTH_SEEK_RELEASE,
    HEALTH_SEEK_TRIGGER,
    HEALTH_THING_TYPES,
    HealthItemRuntime,
    KEY_THING_TYPES,
    KeyItemRuntime,
    LIFT_SPECIALS,
    LIFT_WALK_SPECIALS,
    MAX_GRID_NODES,
    MAX_STEP_UP_FP,
    MELEE_RUSH_THREATS,
    MapLineRuntime,
    NO_KILL_CLOSE_BLOCKER_HEALTH,
    NO_KILL_LOS_BREAK_HEALTH,
    NavCellRuntime,
    PlanAction,
    Point,
    PortalEdge,
    RAW_STEER_TURN_CAP,
    RAW_STEER_TURN_SCALE,
    RAW_TURN_CAP,
    RAW_TURN_SCALE,
    REMEMBERED_PROBE_CONTACT_UNITS,
    REMEMBERED_PROBE_CROSSFIRE_DEGREES,
    REMEMBERED_PROBE_CROSSFIRE_UNITS,
    REMEMBERED_PROBE_DANGER_HEALTH,
    REMEMBERED_PROBE_SHOOTABLE_HEALTH,
    REMEMBERED_PROGRESSION_PROBE_MAX_UNITS,
    ROUTE_THREAT_REFUSE_MULT,
    Route,
    RouteStep,
    SectorRuntime,
    THREAT_ROUTE_CONFIDENT_LOW_TIER_CAP,
    USE_DISTANCE_FP,
    _angle_delta,
    _bearing,
    _dist,
    _same_point,
    _segments_intersect,
)
from planner_threat import ThreatPricingMixin
from progression_lines import ProgressionLinesMixin
from sector_routes import SectorRoutingMixin
from threat_model import classify_enemy

class SpatialPlanner(ThreatPricingMixin, SectorRoutingMixin, ProgressionLinesMixin):
    """Builds a small graph from Doom map geometry and selects objective actions."""

    def __init__(self, snapshot: Any, *, cell_units: int = CELL_UNITS, max_nodes: int = MAX_GRID_NODES) -> None:
        self.snapshot = snapshot
        self.cell_fp = int(cell_units * FP_UNIT)
        self.max_nodes = int(max_nodes)
        self.vertices = self._vertices(snapshot)
        self.lines = self._lines(snapshot)
        self._sight_blocking_lines = [line for line in self.lines if line.sight_blocking]
        self._trigger_lines_by_tag: dict[int, list[MapLineRuntime]] = {}
        for line in self.lines:
            if line.special > 0 and line.tag > 0:
                self._trigger_lines_by_tag.setdefault(line.tag, []).append(line)
        self.sectors = self._sectors(snapshot)
        self._bounds_cache = self._bounds()
        self.portal_edges = self._portal_edges()
        self._portal_graph: dict[int, list[PortalEdge]] = {}
        for edge in self.portal_edges:
            self._portal_graph.setdefault(edge.src, []).append(edge)
        self.key_items = self._key_items(snapshot)
        self.health_items = self._health_items(snapshot)
        self.barrel_items = self._barrel_items(snapshot)
        self.nav_cells = self._sample_nav_cells()
        self.nodes = [cell.point for cell in self.nav_cells]
        self._node_key: dict[tuple[int, int], int] = {self._grid_key(point): i for i, point in enumerate(self.nodes)}
        self._blockmap: dict[tuple[int, int], list[int]] = {}
        for cell in self.nav_cells:
            self._blockmap.setdefault(cell.block, []).append(cell.id)
        self._edge_crossing_cache: dict[tuple[int, int, int, int], tuple[bool, tuple[int, ...]]] = {}
        # ponytail: per-plan memo caches -- threats and line geometry are fixed within one
        # objective_action call, so multiplier/LOS results repeat heavily during Dijkstra
        # (every node is re-scored per incoming edge). Cleared each plan; ceiling is memory
        # per plan (~thousands of small entries), upgrade path is a real spatial index.
        self._threat_mult_cache: dict[tuple[int, int, bool, str, bool], float] = {}
        self._los_cache: dict[tuple[int, int, int, int], bool] = {}
        self._last_route_len = 0
        self._last_status = "ready"
        self._wedge_use_counts: dict[int, int] = {}  # per-line USE presses from wedge_door_use_action; caps dead-door hammering
        self._health_route_best: dict[int, tuple[int, int]] = {}  # item id -> (best_dist_units, picks_without_progress)
        self._health_unreachable: set[int] = set()  # items whose route never converged — objective resumes instead
        self._avoid_sector_ids: set[int] = set()
        self._threats: list[dict[str, Any]] = []
        self._threat_cost_mode = "route"
        self._preserve_health = False
        self._route_confident = False
        self._no_kill_evasion_streak = 0
        self._no_kill_evasion_cooldown = 0
        self._health_seek_active = False
        self._attempted_barrel_shots: set[int] = set()
        # Stalemate break-off: how many consecutive steps we've engaged the SAME enemy
        # without it dying. A single fight dragging is the "stuck" the traces showed
        # (DoomGuy orbiting one guy 9+ steps on the weak pistol). After the cap we
        # advance PAST it toward the next target / exit so we never freeze on one enemy.
        self._engaged_enemy_id = 0
        self._engaged_steps = 0
        self._engaged_enemy_hp = 0
        self._rule_set: set[str] = set()
        self._last_los_route_endpoint_key: tuple[int, int] | None = None
        self._blocked_los_route_counts: dict[tuple[int, int], int] = {}
        self._blocked_los_route_keys: set[tuple[int, int]] = set()

    def summary(self) -> dict[str, Any]:
        return {
            "status": self._last_status,
            "nodes": len(self.nodes),
            "cells": len(self.nav_cells),
            "sec": len(self.sectors),
            "ports": len(self.portal_edges),
            "route": self._last_route_len,
            "digest": int(getattr(self.snapshot, "digest", 0)),
        }

    def objective_action(
        self,
        state: Any,
        rules: Iterable[str],
        agent_pb2: Any,
        door_memory: Any,
        world_memory: Any | None = None,
    ) -> PlanAction | None:
        rule_set = set(rules)
        self._rule_set = rule_set  # so combat helpers (stalemate break-off) can read the objective's rules
        player = self._player(state)
        if player is None:
            self._last_status = "no_player"
            return None
        player_state = getattr(state, "player", None)
        try:
            health = int(getattr(player_state, "health", 100) or 100)
        except (TypeError, ValueError):
            health = 100
        ammo = getattr(player_state, "ammo", None)
        ammo_total = (
            int(getattr(ammo, "bullets", 0) or 0)
            + int(getattr(ammo, "shells", 0) or 0)
            + int(getattr(ammo, "rockets", 0) or 0)
            + int(getattr(ammo, "cells", 0) or 0)
        )
        try:
            ready_weapon = int(getattr(player_state, "ready_weapon", 0) or 0)
        except (TypeError, ValueError):
            ready_weapon = -1
        self._world_frontier = world_memory.frontier_targets(self) if world_memory is not None else []
        self._avoid_sector_ids = self._enemy_sector_ids(state) if {"avoid_combat", "no_kills"} & rule_set else set()
        no_kill_rules = {"avoid_combat", "no_kills", "cease_fire"}
        threat_rules = {"complete_level", "exit", "use", "avoid_combat", "no_kills", "avoid_damage", "preserve_health"}
        self._threat_cost_mode = "no_kill" if no_kill_rules & rule_set else "route"
        self._preserve_health = bool({"avoid_damage", "preserve_health"} & rule_set)
        self._route_confident = bool(
            self._threat_cost_mode == "route"
            and "complete_level" in rule_set
            and health > 50
            and ready_weapon > 0
            and ammo_total > 0
        )
        self._threats = self._threat_metadata(state, player) if threat_rules & rule_set else []
        self._threat_mult_cache.clear()
        self._los_cache.clear()

        if self._low_health(state) and "exit" not in rule_set:
            return self._retreat_action(state, player, agent_pb2, detail={"skill": "combat_retreat_low_health"})

        if "exit" in rule_set and {"avoid_combat", "no_kills"} & rule_set:
            blocking_only = "speedrun" in rule_set
            if self._no_kill_evasion_cooldown > 0:
                self._no_kill_evasion_cooldown -= 1
                planned = self._no_kill_route_evasion(state, player, agent_pb2, blocking_only=blocking_only)
                if planned is not None:
                    planned_action = planned.detail.get("action")
                    if planned_action in {"break_los_low_health", "panic_run_past", "panic_escape_side"}:
                        self._no_kill_evasion_streak = 0
                        self._no_kill_evasion_cooldown = 0
                        return planned
                    if planned_action == "bait_back":
                        return planned
            else:
                planned = self._no_kill_route_evasion(state, player, agent_pb2, blocking_only=blocking_only)
                if planned is not None:
                    if planned.detail.get("action") in {"break_los_low_health", "panic_run_past", "panic_escape_side"}:
                        self._no_kill_evasion_streak = 0
                        self._no_kill_evasion_cooldown = 0
                        return planned
                    self._no_kill_evasion_streak += 1
                    if self._no_kill_evasion_streak >= 3:
                        self._no_kill_evasion_streak = 0
                        self._no_kill_evasion_cooldown = 5
                    return planned
            self._no_kill_evasion_streak = 0

        health_focus_rules = {"exit", "complete_level", "use", "explore", "recover_health"}
        if health_focus_rules & rule_set:
            planned = self._needed_health_action(state, player, agent_pb2, door_memory, world_memory)
            if planned is not None:
                return planned

        key_focus_rules = {"exit", "complete_level", "use", "explore"}
        if key_focus_rules & rule_set:
            planned = self._needed_key_action(state, player, agent_pb2, door_memory, world_memory)
            if planned is not None:
                return planned

        # Barrel play stays allowed under preserve_health: banning it was tried
        # and adjudicated at 2/20 vs the 30% baseline — detonations convert
        # kills, and slower kills bleed more hitscan damage than the blast risk
        # costs. The self-splash mechanism (traced -51hp) is handled by the
        # chain guard inside _barrel_shot_action instead.
        barrel_allowed = bool(
            ("complete_level" in rule_set or "attack" in rule_set)
            and not {"no_kills", "avoid_combat", "cease_fire"} & rule_set
            and ready_weapon > 0
            and ammo_total > 0
        )
        if barrel_allowed:
            planned = self._barrel_shot_action(
                state,
                player,
                agent_pb2,
                force=bool("attack" in rule_set and "complete_level" not in rule_set),
            )
            if planned is not None:
                return planned

        route_combat_allowed = bool(
            "complete_level" in rule_set
            and health > 55
            and not {"no_kills", "avoid_combat", "cease_fire"} & rule_set
        )
        if bool(self._shootable(state)) and ({"shoot", "attack"} & rule_set or route_combat_allowed):
            return PlanAction(
                skill="fire",
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=8),
                detail={
                    "skill": "combat_fire_burst" if {"shoot", "attack"} & rule_set else "complete_level_fire_burst",
                    "evidence": "combat_probe",
                },
            )

        # DoomGuy "clear the map WHILE advancing to the exit": when the objective
        # wants both combat and exit, hunt down every enemy first and only let the
        # exit path pull us once the map is clear. Without this gate the exit route
        # beelines the door in ~26 steps and leaves 5-7 enemies alive (kills 1).
        # The exit stays the through-line: when no enemy is known, we fall straight
        # into exit routing below.
        hunt_and_advance = bool({"find_enemy", "shoot", "attack"} & rule_set)
        enemies_remain = hunt_and_advance and self._enemies_remain(state)

        if {"find_enemy", "shoot", "attack"} & rule_set:
            planned = self._enemy_action(state, player, agent_pb2, door_memory, shoot=bool({"shoot", "attack"} & rule_set))
            if planned is not None:
                return planned
            # Enemy known but not directly engageable (no LOS/route): push toward it
            # via frontier exploration rather than surrendering to the exit route.
            if enemies_remain and {"exit", "complete_level", "use", "explore"} & rule_set:
                hunt_move = self._explore_action(state, player, agent_pb2, door_memory)
                if hunt_move is not None:
                    hunt_detail = dict(hunt_move.detail or {})
                    hunt_detail["skill"] = "hunt_advance_" + str(hunt_detail.get("skill", "explore"))
                    return PlanAction(skill=hunt_move.skill, action=hunt_move.action, door_line_id=hunt_move.door_line_id, detail=hunt_detail)

        if "exit" in rule_set and not enemies_remain:
            planned = self._live_exit_line_action(state, player, agent_pb2, door_memory)
            if planned is not None:
                return planned
            planned = self._line_objective_action(player, agent_pb2, door_memory, exit_only=True, state=state)
            if planned is not None:
                return planned
            planned = self._route_waypoint_action(state, player, agent_pb2, door_memory)
            if planned is not None:
                return planned
            planned = self._needed_key_action(state, player, agent_pb2, door_memory, world_memory)
            if planned is not None:
                return planned
            exit_sectors = {
                sector
                for line in self.lines
                if line.exit
                for sector in (line.front_sector, line.back_sector)
                if sector >= 0
            }
            if exit_sectors:
                planned = self._tag_gate_switch_action(state, player, agent_pb2, door_memory, exit_sectors)
                if planned is not None:
                    return planned
            if "complete_level" in rule_set:
                planned = self._e1m1_final_corridor_override(state, player, agent_pb2, door_memory)
                if planned is not None:
                    return planned
            planned = self._remembered_progression_line_action(state, player, agent_pb2, door_memory, prefer_exit=True)
            if planned is not None:
                return planned

        if "complete_level" in rule_set:
            planned = self._e1m1_final_corridor_override(state, player, agent_pb2, door_memory)
            if planned is not None:
                return planned

        generic_use_allowed = "use" in rule_set and not ("complete_level" in rule_set and "exit" in rule_set)
        if generic_use_allowed:
            planned = self._route_waypoint_action(state, player, agent_pb2, door_memory)
            if planned is not None:
                return planned
            planned = self._line_objective_action(player, agent_pb2, door_memory, exit_only=False, state=state)
            if planned is not None:
                return planned
            planned = self._remembered_progression_line_action(state, player, agent_pb2, door_memory, prefer_exit=False)
            if planned is not None:
                return planned

        if "explore" in rule_set:
            live_use_allowed = bool({"use", "exit", "complete_level"} & rule_set)
            live_use_repair_only = "complete_level" in rule_set and "exit" in rule_set
            if live_use_allowed:
                planned = self._last_chance_live_use_line_action(
                    state,
                    player,
                    agent_pb2,
                    door_memory,
                    repair_only=live_use_repair_only,
                )
                if planned is not None:
                    return planned
                planned = self._needed_key_action(state, player, agent_pb2, door_memory, world_memory)
                if planned is not None:
                    return planned
                planned = self._remembered_progression_line_action(state, player, agent_pb2, door_memory, prefer_exit="exit" in rule_set)
                if planned is not None:
                    return planned
            planned = self._explore_action(state, player, agent_pb2, door_memory)
            if planned is not None:
                if (
                    live_use_allowed
                    and planned.detail.get("skill") == "planner_probe_explore"
                ):
                    live_resync = self._last_chance_live_use_line_action(
                        state,
                        player,
                        agent_pb2,
                        door_memory,
                        allow_suppressed=True,
                        repair_only=live_use_repair_only,
                        max_distance_fp=REMEMBERED_PROGRESSION_PROBE_MAX_UNITS * FP_UNIT,
                    )
                    if live_resync is not None:
                        return live_resync
                return planned
        return None

    def _needed_health_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
        world_memory: Any | None,
    ) -> PlanAction | None:
        player_state = getattr(state, "player", None)
        health = int(getattr(player_state, "health", 100) or 100)
        if health >= HEALTH_SEEK_RELEASE:
            self._health_seek_active = False
            return None
        if health < HEALTH_SEEK_TRIGGER:
            self._health_seek_active = True
        elif not self._health_seek_active:
            return None
        consumed = {int(item) for item in getattr(world_memory, "consumed_health_items", set()) or set()}
        candidates = [
            item for item in self.health_items
            if int(item.id) not in consumed and int(item.id) not in self._health_unreachable
        ]
        if not candidates:
            self._last_status = "no_health_items"
            return None
        best_plan: tuple[float, PlanAction] | None = None
        for item in candidates:
            distance = _dist(player["point"], item.point)
            target_threat = self._point_threat_multiplier(item.point, target=False)
            if target_threat >= HEALTH_SEEK_MAX_TARGET_THREAT:
                continue
            detail = {
                "skill": "route_to_health",
                "thing": int(item.id),
                "health_item": item.kind,
                "heal": int(item.value),
                "hp": int(health),
                "dist": int(distance / FP_UNIT),
                "target_threat": round(float(target_threat), 2),
            }
            if item.sector_id is not None:
                detail["sector"] = item.sector_id
            targets = self._nearest_nodes(item.point, limit=4)
            route = self._route(player["point"], targets, door_memory)
            if route is not None and self._route_endpoint_near(route, item.point, max_distance_fp=144 * FP_UNIT):
                route_cost = float(route.cost) + distance / FP_UNIT * 0.05 - min(int(item.value), 100) * 0.1
                planned = self._route_action(
                    player,
                    route,
                    agent_pb2,
                    door_memory,
                    skill="route_progression",
                    detail={**detail, "route": len(route.points)},
                )
                if self._health_route_step_allowed(planned) and (best_plan is None or route_cost < best_plan[0]):
                    best_plan = (route_cost, planned)
                continue
            if item.sector_id is not None:
                route_edges = self._sector_route_from_player(player, [item.sector_id], door_memory)
                if route_edges:
                    next_edge = self._select_portal_route_edge(player, route_edges)
                    next_threat = self._portal_threat_multiplier(next_edge, target=False)
                    if next_threat >= HEALTH_SEEK_MAX_TARGET_THREAT:
                        continue
                    route_cost = self._route_edge_cost(route_edges, door_memory) + distance / FP_UNIT * 0.05 - min(int(item.value), 100) * 0.1
                    planned = self._portal_route_action(
                        player,
                        route_edges,
                        agent_pb2,
                        door_memory,
                        skill="route_progression",
                        detail={**detail, "skill": "sector_route_to_health", "route": len(route_edges)},
                        state=state,
                    )
                    if self._health_route_step_allowed(planned) and (best_plan is None or route_cost < best_plan[0]):
                        best_plan = (route_cost, planned)
                    continue
            if self.has_line_of_sight(player["point"], item.point):
                planned = self._turn_or_forward(
                    player,
                    item.point,
                    agent_pb2,
                    skill="route_progression",
                    detail={**detail, "skill": "visible_health_probe"},
                )
                route_cost = distance / FP_UNIT + 80.0 - min(int(item.value), 100) * 0.1
                if best_plan is None or route_cost < best_plan[0]:
                    best_plan = (route_cost, planned)
        if best_plan is None:
            self._last_status = "no_safe_health_route"
            return None
        # Opportunistic, not obsessive (the demo contract): if routing to this item has not
        # actually CLOSED the distance across many picks, the item is unreachable in practice
        # (ledge, oscillating route, gated) — blacklist it and let the objective resume. The
        # failure this kills was measured live: 18 straight autopilot bursts (~2 min) ground
        # at one spot in route_to_health because a health pickup could never be reached.
        item_id = int(best_plan[1].detail.get("thing", -1) or -1)
        if item_id >= 0:
            dist_units = int(best_plan[1].detail.get("dist", 0) or 0)
            best_dist, stale = self._health_route_best.get(item_id, (1 << 30, 0))
            if dist_units + 24 < best_dist:
                self._health_route_best[item_id] = (dist_units, 0)
            else:
                stale += 1
                self._health_route_best[item_id] = (best_dist, stale)
                if stale >= 40:
                    self._health_unreachable.add(item_id)
                    self._last_status = "health_item_unreachable"
                    return None
        under_fire = self._health_route_under_fire_escape(state, player, agent_pb2, best_plan[1])
        if under_fire is not None:
            return under_fire
        self._last_route_len = int(best_plan[1].detail.get("route", self._last_route_len) or self._last_route_len)
        self._last_status = "route_to_health"
        return best_plan[1]

    def _health_route_under_fire_escape(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        plan: PlanAction,
    ) -> PlanAction | None:
        player_state = getattr(state, "player", None)
        try:
            health = int(getattr(player_state, "health", 100) or 100)
        except (TypeError, ValueError):
            health = 100
        if health >= HEALTH_SEEK_TRIGGER:
            return None
        try:
            health_distance = int(plan.detail.get("dist", 9999) or 9999)
        except (TypeError, ValueError):
            health_distance = 9999
        if health_distance <= 64:
            return None
        skill = str(plan.detail.get("skill") or "")
        if skill not in {"route_to_health", "sector_route_to_health", "visible_health_probe"}:
            return None
        raw = getattr(plan.action, "raw", None)
        raw_forward = int(getattr(raw, "forward_move", 0) or 0) if raw is not None else 0
        action_type = int(getattr(plan.action, "action", 0) or 0)
        if action_type != int(getattr(agent_pb2, "ACTION_FORWARD", -1)) and raw_forward <= 0:
            return None
        enemy = self._nearest_enemy(state, player)
        if enemy is None or not bool(enemy.get("line_of_sight")):
            return None
        try:
            enemy_distance = int(float(enemy.get("distance_fp", 0) or 0) / FP_UNIT)
        except (TypeError, ValueError):
            enemy_distance = 9999
        if enemy_distance > 1400 and not bool(self._shootable(state)):
            return None
        detail = dict(plan.detail)
        detail.update(
            {
                "skill": "health_route_break_los",
                "blocked_skill": skill,
                "enemy": int(enemy.get("id", 0) or 0),
                "enemy_dist": enemy_distance,
                "hp": health,
            }
        )
        return self._retreat_action(state, player, agent_pb2, detail=detail)

    def _health_route_step_allowed(self, plan: PlanAction | None) -> bool:
        if plan is None:
            return False
        try:
            threat_mult = float(plan.detail.get("route_step_threat_mult", 1.0) or 1.0)
        except Exception:
            threat_mult = 1.0
        return threat_mult < HEALTH_SEEK_MAX_TARGET_THREAT

    def _barrel_chain_reaches_player(self, target: BarrelRuntime, player_point: Point) -> bool:
        """BFS the detonation chain from `target`: barrels ignite each other within
        blast radius (LOS-limited). True if any chained barrel can blast the player."""
        frontier = [target]
        seen = {int(target.id)}
        while frontier:
            cur = frontier.pop()
            if (
                _dist(player_point, cur.point) / FP_UNIT < BARREL_SELF_CHAIN_GUARD_UNITS
                and self.has_line_of_sight(player_point, cur.point)
            ):
                return True
            for other in self.barrel_items:
                if int(other.id) in seen:
                    continue
                if (
                    _dist(cur.point, other.point) / FP_UNIT <= BARREL_BLAST_UNITS
                    and self.has_line_of_sight(cur.point, other.point)
                ):
                    seen.add(int(other.id))
                    frontier.append(other)
        return False

    def _barrel_shot_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        *,
        force: bool = False,
    ) -> PlanAction | None:
        if not self.barrel_items:
            return None
        player_state = getattr(state, "player", None)
        try:
            health = int(getattr(player_state, "health", 100) or 100)
        except (TypeError, ValueError):
            health = 100
        pressure = self._point_threat_multiplier(player["point"], target=False)
        if not force and health > 70 and pressure < ROUTE_THREAT_REFUSE_MULT:
            return None
        enemies: list[tuple[Point, str, int]] = []
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            pos = getattr(obj, "position", None)
            if obj is None or pos is None or int(getattr(obj, "health", 0) or 0) <= 0:
                continue
            if not bool(getattr(enemy, "line_of_sight", False)):
                continue
            enemies.append(
                (
                    Point(int(getattr(pos, "x_fp", 0) or 0), int(getattr(pos, "y_fp", 0) or 0)),
                    classify_enemy(enemy),
                    int(getattr(obj, "id", 0) or 0),
                )
            )
        if not enemies:
            return None

        best: tuple[tuple[float, float, float, float], BarrelRuntime, list[tuple[Point, str, int]], float, float] | None = None
        for barrel in self.barrel_items:
            if int(barrel.id) in self._attempted_barrel_shots:
                continue
            player_dist = _dist(player["point"], barrel.point) / FP_UNIT
            if player_dist < BARREL_SAFE_UNITS or player_dist > BARREL_MAX_SHOT_UNITS:
                continue
            if not self.has_line_of_sight(player["point"], barrel.point):
                continue
            # ponytail: barrels chain-detonate (blast is LOS-limited). Refuse a
            # candidate only when its ACTUAL chain reaches blast range of the
            # player (traced -51hp) — a blanket "no barrels near me" guard was
            # adjudicated at 1/10 vs the 30% baseline because the camp spot sits
            # near the cluster and barrel kills are load-bearing.
            if self._barrel_chain_reaches_player(barrel, player["point"]):
                continue
            nearby: list[tuple[Point, str, int]] = []
            threat_score = 0.0
            for enemy_point, threat, enemy_id in enemies:
                if _dist(enemy_point, barrel.point) / FP_UNIT > BARREL_BLAST_UNITS:
                    continue
                nearby.append((enemy_point, threat, enemy_id))
                threat_score += {"hitscan": 3.0, "projectile": 2.0, "melee_rush": 1.5, "melee": 1.5}.get(threat, 1.0)
            if not nearby:
                continue
            bearing = _bearing(player["point"], barrel.point)
            turn = _angle_delta(bearing, player["angle"])
            score = (-float(len(nearby)), -threat_score, abs(turn), player_dist)
            candidate = (score, barrel, nearby, turn, player_dist)
            if best is None or candidate[0] < best[0]:
                best = candidate
        if best is None:
            return None

        _score, barrel, nearby, turn, player_dist = best
        detail = {
            "skill": "barrel_shot",
            "barrel": int(barrel.id),
            "thing": int(barrel.id),
            "enemies_near": len(nearby),
            "enemy_ids": [int(item[2]) for item in nearby[:4]],
            "dist": int(player_dist),
            "turn": round(turn, 1),
        }
        if barrel.sector_id is not None:
            detail["sector"] = barrel.sector_id
        if abs(turn) > BARREL_ALIGN_DEGREES:
            return None
        self._attempted_barrel_shots.add(int(barrel.id))
        return PlanAction(
            skill="fire",
            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=4),
            detail={**detail, "action": "fire"},
        )

    def _enemy_action(self, state: Any, player: dict[str, Any], agent_pb2: Any, door_memory: Any, *, shoot: bool) -> PlanAction | None:
        enemy = self._nearest_enemy(state, player)
        if enemy is None:
            self._engaged_enemy_id = 0
            self._engaged_steps = 0
            self._last_status = "no_enemy"
            return None
        # Stalemate tracking: count consecutive steps on the SAME enemy. Reset the
        # counter whenever we switch targets OR the enemy's health drops (we're making
        # progress — keep fighting). It only escalates when a fight is genuinely dragging.
        eid = int(enemy.get("id", 0) or 0)
        ehp = self._enemy_health(state, eid)
        if eid != self._engaged_enemy_id:
            self._engaged_enemy_id = eid
            self._engaged_steps = 0
            self._engaged_enemy_hp = ehp
        else:
            if ehp < self._engaged_enemy_hp:      # landing damage -> not stuck, reset
                self._engaged_steps = 0
            self._engaged_enemy_hp = min(self._engaged_enemy_hp, ehp) if ehp else self._engaged_enemy_hp
            self._engaged_steps += 1
        # Break off a dragging fight only when we have somewhere to go AND aren't clearly
        # winning: advance past this enemy toward the next target / exit (never freeze on one).
        if self._engaged_steps >= COMBAT_STALEMATE_STEPS and {"exit", "complete_level", "use", "explore"} & self._rule_set:
            advance = self._explore_action(state, player, agent_pb2, door_memory)
            if advance is not None:
                self._engaged_steps = 0  # give the new position a fresh engagement window
                d = dict(advance.detail or {})
                d["skill"] = "stalemate_advance_" + str(d.get("skill", "explore"))
                d["broke_off_enemy"] = eid
                return PlanAction(skill=advance.skill, action=advance.action, door_line_id=advance.door_line_id, detail=d)
        enemy_point = enemy["point"]
        map_los = self.has_line_of_sight(player["point"], enemy_point)
        engine_los = bool(enemy.get("line_of_sight"))
        if map_los or engine_los:
            # ponytail: the direct close is a straight-line march at the enemy —
            # zero angular displacement for his aim, the losing duel shape at
            # range. Under preserve_health vs a far hitscanner that beeline was
            # THE first-contact damage source (traced: 47-step zero-shot march
            # across 1000u of open ground, -9/-15). Route instead: the sector
            # router pays 50x for LOS-exposed cells and approaches via cover.
            if (
                self._preserve_health
                and str(enemy.get("threat") or "unknown") in {"hitscan", "unknown"}
                and _dist(player["point"], enemy_point) / FP_UNIT > 512.0
            ):
                covered = self._sector_route_to_enemy_action(
                    state, player, enemy_point, agent_pb2, door_memory, shoot=shoot, enemy_id=enemy["id"]
                )
                if covered is not None:
                    return covered
            return self._visible_enemy_action(
                state,
                player,
                enemy,
                agent_pb2,
                skill="close_visible_contact" if shoot else "seek_enemy",
                detail={"skill": "combat_reposition_visible", "enemy": enemy["id"], "los": engine_los or map_los},
            )

        hidden_melee = self._hidden_melee_rush_action(state, player, enemy, agent_pb2, shoot=shoot)
        if hidden_melee is not None:
            return hidden_melee

        walk_trigger = self._navigation_walk_trigger_action(
            state,
            player,
            agent_pb2,
            door_memory,
            skill="route_progression",
            detail={
                "skill": "planner_route_walk_trigger_for_contact",
                "enemy": enemy["id"],
                "threat": enemy.get("threat", "unknown"),
            },
        )
        if walk_trigger is not None:
            return walk_trigger

        contact_door = self._navigation_use_line_action(
            state,
            player,
            agent_pb2,
            door_memory,
            skill="route_progression",
            detail={"skill": "planner_route_use_line_for_contact", "enemy": enemy["id"]},
            max_distance_fp=1920 * FP_UNIT,
            include_open=True,
            target_point=enemy_point,
            max_target_delta_degrees=100.0,
        )
        if contact_door is not None:
            return contact_door

        blocked_use = self._blocked_use_line_action(
            state,
            player,
            agent_pb2,
            door_memory,
            skill="open_use_line",
            detail={"skill": "planner_unblock_adjacent_use_line", "enemy": enemy["id"]},
        )
        if blocked_use is not None:
            return blocked_use
        if self._route_blocked_now(state):
            self._mark_last_los_route_blocked()
            self._last_status = "live_probe_blocked_route"
            return self._local_probe_action(
                state,
                player,
                enemy_point,
                agent_pb2,
                skill="close_visible_contact" if shoot else "seek_enemy",
                detail={"skill": "planner_probe_blocked_route", "enemy": enemy["id"]},
            )

        targets = self._vantage_nodes(enemy_point, player["point"])
        route = self._route(player["point"], targets, door_memory)
        if route is None:
            sector_plan = self._sector_route_to_enemy_action(state, player, enemy_point, agent_pb2, door_memory, shoot=shoot, enemy_id=enemy["id"])
            if sector_plan is not None:
                return sector_plan
            near_hidden = self._near_hidden_enemy_action(state, player, enemy, agent_pb2, shoot=shoot)
            if near_hidden is not None:
                return near_hidden
            use_plan = self._navigation_use_line_action(
                state,
                player,
                agent_pb2,
                door_memory,
                skill="route_progression",
                detail={"skill": "planner_route_live_use_line", "enemy": enemy["id"], "reason": "no_graph_route"},
                target_point=enemy_point,
                max_target_delta_degrees=100.0,
            )
            if use_plan is not None:
                return use_plan
            self._last_status = "local_probe_no_enemy_route"
            return self._local_probe_action(
                state,
                player,
                enemy_point,
                agent_pb2,
                skill="close_visible_contact" if shoot else "seek_enemy",
                detail={"skill": "planner_probe_to_contact", "enemy": enemy["id"], "reason": "no_graph_route"},
            )
        self._last_route_len = len(route.points)
        return self._route_action(
            player,
            route,
            agent_pb2,
            door_memory,
            skill="close_visible_contact" if shoot else "seek_enemy",
            detail={"skill": "planner_route_to_los", "enemy": enemy["id"], "route": len(route.points)},
        )

    def _near_hidden_enemy_action(self, state: Any, player: dict[str, Any], enemy: dict[str, Any], agent_pb2: Any, *, shoot: bool) -> PlanAction | None:
        distance_units = int(float(enemy.get("distance_fp", _dist(player["point"], enemy["point"]))) / FP_UNIT)
        if distance_units > 1200:
            return None
        navigation = getattr(state, "navigation", None)
        if not bool(getattr(navigation, "forward_open", False)):
            return None
        delta = _angle_delta(_bearing(player["point"], enemy["point"]), player["angle"])
        detail = {"skill": "planner_align_near_hidden_enemy", "enemy": enemy["id"], "dist": distance_units}
        skill = "close_visible_contact" if shoot else "seek_enemy"
        if abs(delta) > 18.0:
            return self._turn(player, delta, agent_pb2, skill, detail)
        merged = dict(detail)
        merged["action"] = "forward"
        return PlanAction(
            skill=skill,
            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=34, duration_tics=12),
            detail=merged,
        )

    def _hidden_melee_rush_action(self, state: Any, player: dict[str, Any], enemy: dict[str, Any], agent_pb2: Any, *, shoot: bool) -> PlanAction | None:
        if str(enemy.get("threat") or "unknown") not in MELEE_RUSH_THREATS:
            return None
        distance_units = int(float(enemy.get("distance_fp", _dist(player["point"], enemy["point"]))) / FP_UNIT)
        if distance_units > 384:
            return None
        navigation = getattr(state, "navigation", None)
        delta = _angle_delta(_bearing(player["point"], enemy["point"]), player["angle"])
        skill = "close_visible_contact" if shoot else "seek_enemy"
        detail = {
            "skill": "hidden_melee_rush_evasion",
            "enemy": enemy["id"],
            "dist": distance_units,
            "turn": round(delta, 1),
            "threat": enemy.get("threat", "unknown"),
        }
        if abs(delta) > 90.0:
            return self._turn(player, delta, agent_pb2, skill, {**detail, "action": "face_hidden"})
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if bool(getattr(navigation, "back_open", False)):
            if raw_cls is not None:
                return PlanAction(
                    skill=skill,
                    action=agent_pb2.PlayerAction(
                        duration_tics=8,
                        raw=raw_cls(forward_move=-46, angle_turn=self._raw_steer_turn_units(delta)),
                    ),
                    detail={**detail, "action": "kite_back_hidden"},
                )
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=42, duration_tics=8),
                detail={**detail, "action": "kite_back_hidden"},
            )
        side = self._best_lateral_probe(navigation)
        if side:
            if raw_cls is not None:
                return PlanAction(
                    skill=skill,
                    action=agent_pb2.PlayerAction(
                        duration_tics=8,
                        raw=raw_cls(side_move=54 if side > 0 else -54, angle_turn=self._raw_steer_turn_units(delta)),
                    ),
                    detail={**detail, "action": "slip_side_hidden", "side": "left" if side > 0 else "right"},
                )
            action_type = agent_pb2.ACTION_STRAFE_LEFT if side > 0 else agent_pb2.ACTION_STRAFE_RIGHT
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=action_type, amount=42, duration_tics=8),
                detail={**detail, "action": "slip_side_hidden", "side": "left" if side > 0 else "right"},
            )
        if abs(delta) > 18.0:
            return self._turn(player, delta, agent_pb2, skill, {**detail, "action": "face_hidden"})
        if bool(getattr(navigation, "forward_open", False)) and distance_units > 160:
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=26, duration_tics=6),
                detail={**detail, "action": "cautious_close_hidden"},
            )
        return None

    def _probe_escape_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        *,
        detail: dict[str, Any],
    ) -> PlanAction | None:
        player_state = getattr(state, "player", None)
        health = int(getattr(player_state, "health", 100) or 100)
        shootable = bool(self._shootable(state))
        threats = self._current_probe_threats(state, player)
        close_pressure = any(item["distance_units"] <= REMEMBERED_PROBE_CONTACT_UNITS for item in threats)
        los_pressure = any(bool(item["los"]) for item in threats)
        crossfire_spread = self._threat_bearing_spread(
            [
                item["bearing"]
                for item in threats
                if item["distance_units"] <= REMEMBERED_PROBE_CROSSFIRE_UNITS and (item["los"] or item["distance_units"] <= 640)
            ]
        )
        crossfire = crossfire_spread >= REMEMBERED_PROBE_CROSSFIRE_DEGREES
        if shootable and health <= REMEMBERED_PROBE_SHOOTABLE_HEALTH:
            reason = "shootable_low_health"
        elif crossfire and health <= REMEMBERED_PROBE_SHOOTABLE_HEALTH:
            reason = "crossfire"
        elif health <= REMEMBERED_PROBE_DANGER_HEALTH and (los_pressure or close_pressure):
            reason = "low_health_pressure"
        elif health <= NO_KILL_CLOSE_BLOCKER_HEALTH and threats:
            reason = "critical_health_threat"
        else:
            return None
        merged = dict(detail)
        merged.update(
            {
                "reason": reason,
                "hp": health,
                "threats": len(threats),
                "crossfire": round(crossfire_spread, 1),
            }
        )
        return self._retreat_action(state, player, agent_pb2, detail=merged)

    def _explore_action(self, state: Any, player: dict[str, Any], agent_pb2: Any, door_memory: Any) -> PlanAction | None:
        frontier = getattr(self, "_world_frontier", None)
        if frontier:
            route = self._sector_route_from_player(player, frontier, door_memory)
            if route:
                return self._portal_route_action(
                    player,
                    route,
                    agent_pb2,
                    door_memory,
                    skill="route_progression",
                    detail={"skill": "frontier_sector_route", "route": len(route)},
                )
            planned = self._frontier_escape_line_action(state, player, agent_pb2, door_memory, set(frontier))
            if planned is not None:
                return planned
            planned = self._reachable_boundary_escape_action(state, player, agent_pb2, door_memory)
            if planned is not None:
                return planned
        if not self.nodes:
            return None
        targets = sorted(range(len(self.nodes)), key=lambda idx: _dist(player["point"], self.nodes[idx]), reverse=True)[:24]
        route = self._route(player["point"], targets, door_memory)
        if route is None:
            self._last_status = "local_probe_no_explore_route"
            return self._local_probe_action(
                state,
                player,
                Point(player["point"].x + int(math.cos(math.radians(player["angle"])) * self.cell_fp),
                      player["point"].y + int(math.sin(math.radians(player["angle"])) * self.cell_fp)),
                agent_pb2,
                skill="route_progression",
                detail={"skill": "planner_probe_explore", "reason": "no_graph_route"},
            )
        self._last_route_len = len(route.points)
        return self._route_action(
            player,
            route,
            agent_pb2,
            door_memory,
            skill="route_progression",
            detail={"skill": "planner_explore", "route": len(route.points)},
        )

    def _frontier_escape_line_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
        frontier: set[int],
    ) -> PlanAction | None:
        sector = self.sector_for_player(state, player)
        if sector is None:
            return None
        candidates: list[tuple[int, float, PortalEdge, MapLineRuntime]] = []
        for edge in self._portal_graph.get(int(sector), []):
            if edge.passable or edge.exit or self.sector_is_damaging(edge.dst):
                continue
            if not (edge.door or edge.use_line or edge.walk_trigger):
                continue
            state_name = str(door_memory.state_for(edge.line_id)) if hasattr(door_memory, "state_for") else "unknown"
            if state_name in {"blocked", "requires_key", "requires_switch"}:
                continue
            if door_memory.is_blocked(edge.line_id):
                continue
            line = self._line_by_id(edge.line_id)
            if line is None:
                continue
            target = self._nearest_point_on_line(player["point"], line)
            distance = _dist(player["point"], target)
            frontier_rank = 0 if edge.dst in frontier else 1
            state_rank = 0 if state_name in {"opening", "opened"} else 1
            candidates.append((frontier_rank * 10 + state_rank, distance, edge, line))
        if not candidates:
            return None
        _rank, distance, edge, line = min(candidates, key=lambda item: (item[0], item[1], item[2].line_id))
        state_name = str(door_memory.state_for(edge.line_id)) if hasattr(door_memory, "state_for") else "unknown"
        if door_memory.is_open(edge.line_id):
            target = self._line_use_target(player["point"], line)
            return self._turn_or_forward(
                player,
                target,
                agent_pb2,
                skill="route_progression",
                detail={
                    "skill": "frontier_escape_follow_opening",
                    "line": edge.line_id,
                    "sector": edge.dst,
                    "state": state_name,
                    "dist": int(distance / FP_UNIT),
                },
                door_line_id=edge.line_id,
            )
        target = self._nearest_point_on_line(player["point"], line)
        delta = _angle_delta(_bearing(player["point"], target), player["angle"])
        detail = {
            "skill": "frontier_escape_use_line",
            "line": edge.line_id,
            "special": edge.special,
            "sector": edge.dst,
            "state": state_name,
            "dist": int(distance / FP_UNIT),
            "turn": round(delta, 1),
        }
        if distance <= USE_DISTANCE_FP:
            if abs(delta) <= 26.0:
                door_memory.record_attempt(edge.line_id, status="frontier_escape_use")
                return PlanAction(
                    skill="open_use_line",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
                    door_line_id=edge.line_id,
                    detail={**detail, "action": "use"},
                )
            return self._turn(player, delta, agent_pb2, "open_use_line", {**detail, "action": "turn"}, door_line_id=edge.line_id)
        escape = self._probe_escape_action(
            state,
            player,
            agent_pb2,
            detail={**detail, "skill": "frontier_probe_escape"},
        )
        if escape is not None:
            return escape
        return self._turn_or_forward(
            player,
            target,
            agent_pb2,
            skill="route_progression",
            detail={**detail, "skill": "frontier_escape_probe", "action": "approach"},
            door_line_id=edge.line_id,
        )

    def _reachable_boundary_escape_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
    ) -> PlanAction | None:
        sector = self.sector_for_player(state, player)
        if sector is None:
            return None
        reachable = self._sector_reachable_set(int(sector), door_memory)
        if not reachable:
            return None
        candidates: list[tuple[float, PortalEdge, MapLineRuntime, list[PortalEdge]]] = []
        for src in reachable:
            for edge in self._portal_graph.get(src, []):
                if edge.dst in reachable:
                    continue
                if edge.passable or edge.exit or self.sector_is_damaging(edge.dst):
                    continue
                if not (edge.door or edge.use_line or edge.walk_trigger):
                    continue
                state_name = str(door_memory.state_for(edge.line_id)) if hasattr(door_memory, "state_for") else "unknown"
                if state_name in {"blocked", "requires_key", "requires_switch"}:
                    continue
                if door_memory.is_blocked(edge.line_id):
                    continue
                line = self._line_by_id(edge.line_id)
                if line is None:
                    continue
                route = self._sector_route(int(sector), [edge.src], door_memory)
                if route is None:
                    continue
                distance = _dist(player["point"], self._nearest_point_on_line(player["point"], line))
                route_cost = self._route_edge_cost(route, door_memory)
                candidates.append((route_cost + (distance / FP_UNIT) + (0.0 if state_name in {"opening", "opened"} else 256.0), edge, line, route))
        if not candidates:
            return None
        _score, edge, line, route = min(candidates, key=lambda item: (item[0], item[1].line_id))
        detail = {
            "skill": "frontier_boundary_escape",
            "line": edge.line_id,
            "boundary_line": edge.line_id,
            "special": edge.special,
            "sector": edge.dst,
            "src_sector": edge.src,
            "state": str(door_memory.state_for(edge.line_id)) if hasattr(door_memory, "state_for") else "unknown",
        }
        if route:
            self._last_status = "frontier_boundary_escape_route"
            self._last_route_len = len(route)
            return self._portal_route_action(
                player,
                route,
                agent_pb2,
                door_memory,
                skill="route_progression",
                detail={**detail, "route": len(route)},
                state=state,
            )
        if route is None:
            return None
        line_action = self._static_use_line_action(
            player,
            line,
            agent_pb2,
            door_memory,
            exit_only=False,
            detail=detail,
            max_distance_fp=USE_DISTANCE_FP,
        )
        if line_action is not None:
            return line_action
        target = self._nearest_point_on_line(player["point"], line)
        return self._turn_or_forward(
            player,
            target,
            agent_pb2,
            skill="route_progression",
            detail={**detail, "skill": "frontier_boundary_probe", "dist": int(_dist(player["point"], target) / FP_UNIT)},
            door_line_id=edge.line_id,
        )

    def _turn_or_forward(
        self,
        player: dict[str, Any],
        target: Point,
        agent_pb2: Any,
        *,
        skill: str,
        detail: dict[str, Any],
        door_line_id: int | None = None,
    ) -> PlanAction:
        delta = _angle_delta(_bearing(player["point"], target), player["angle"])
        distance_units = int(_dist(player["point"], target) / FP_UNIT)
        route_line = self._line_by_id(door_line_id)
        route_line_is_door = bool(route_line and (route_line.door or route_line.use_trigger))
        if not route_line_is_door:
            # Macro segment hint: this movement follows a known-passable route line, so the drive
            # loop may keep driving toward this exact point in repeated bursts INSIDE the capsule
            # (re-steering only) instead of returning to the full plan loop after every 6-14 tics.
            # Guards in the brain hand control back on blocked/damaged/threat-change.
            detail = dict(detail)
            detail["mt"] = [int(target.x), int(target.y)]
        allow_reverse = (
            str(detail.get("skill", "")).startswith("sector_route")
            and str(detail.get("skill", "")) != "sector_route_to_exit_line"
            and not route_line_is_door
        )
        if abs(delta) >= 150.0 and distance_units <= 192 and allow_reverse:
            merged = dict(detail)
            merged.update({"action": "reverse", "dist": distance_units, "turn": round(delta, 1)})
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=34, duration_tics=8),
                door_line_id=door_line_id,
                detail=merged,
            )
        route_skill = str(detail.get("skill", ""))
        is_sector_route = route_skill.startswith("sector_route")
        is_navcell_portal = route_skill == "navcell_to_portal"
        guided_route = is_sector_route or is_navcell_portal
        align_tolerance = 5.0 if guided_route and distance_units <= 420 else 12.0
        if abs(delta) > align_tolerance:
            if guided_route:
                steered = self._raw_steer_forward(
                    player,
                    delta,
                    distance_units,
                    agent_pb2,
                    skill,
                    detail,
                    door_line_id,
                    force=is_navcell_portal and distance_units <= 96,
                )
                if steered is not None:
                    return steered
            return self._turn(player, delta, agent_pb2, skill, detail, door_line_id=door_line_id)
        if guided_route and distance_units <= 420:
            steered = self._raw_steer_forward(
                player,
                delta,
                distance_units,
                agent_pb2,
                skill,
                detail,
                door_line_id,
                force=True,
            )
            if steered is not None:
                return steered
        merged = dict(detail)
        merged.update({"action": "forward", "dist": distance_units})
        return PlanAction(
            skill=skill,
            action=agent_pb2.PlayerAction(
                action=agent_pb2.ACTION_FORWARD,
                amount=42,
                duration_tics=max(2, min(14, max(1, distance_units) // 8)),
            ),
            door_line_id=door_line_id,
            detail=merged,
        )

    def _raw_steer_forward(
        self,
        player: dict[str, Any],
        delta: float,
        distance_units: int,
        agent_pb2: Any,
        skill: str,
        detail: dict[str, Any],
        door_line_id: int | None,
        *,
        force: bool = False,
    ) -> PlanAction | None:
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is None:
            return None
        abs_delta = abs(float(delta))
        if abs_delta > 88.0 or (distance_units < 72 and not force):
            return None
        turn = self._raw_steer_turn_units(delta)
        forward = 50 if abs_delta <= 38.0 else 40
        duration = 8 if abs_delta <= 38.0 else 6
        merged = dict(detail)
        merged.update({"action": "steer_forward", "dist": distance_units, "turn": round(delta, 1)})
        return PlanAction(
            skill=skill,
            action=agent_pb2.PlayerAction(
                duration_tics=duration,
                raw=raw_cls(forward_move=forward, angle_turn=turn),
            ),
            door_line_id=door_line_id,
            detail=merged,
        )

    def _visible_enemy_action(
        self,
        state: Any,
        player: dict[str, Any],
        enemy: dict[str, Any],
        agent_pb2: Any,
        *,
        skill: str,
        detail: dict[str, Any],
    ) -> PlanAction:
        target = enemy["point"]
        delta = _angle_delta(_bearing(player["point"], target), player["angle"])
        distance_units = int(float(enemy.get("distance_fp", _dist(player["point"], target))) / FP_UNIT)
        merged = dict(detail)
        merged.update({"turn": round(delta, 1), "dist": distance_units})
        navigation = getattr(state, "navigation", None)
        shoot = "seek_enemy" not in skill  # seek_enemy is the no-shoot find-only variant
        engine_shootable = bool(enemy.get("shootable_target"))
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        # DoomGuy kill-conversion: the old path aimed to 2.5 deg then strafed/nudged
        # the crosshair OFF target and fired only via the next-frame _shootable check
        # -- so shots landed ~2 dmg/shot (traced 40 dmg / 23 fires / 1 kill). Fire
        # instead as a RawTiccmd that HOLDS attack while continuously correcting aim
        # (angle_turn), so the burst tracks the enemy instead of spraying past it.
        # The engine's shootable_target OR a tight cone both open the fire window;
        # the live angle_turn snaps the residual delta closed as the burst runs.
        forward_open = bool(getattr(navigation, "forward_open", False))
        # DoomGuy is a rusher: the pistol barely lands past ~250u, and traces show
        # the agent orbiting enemies at 400-900u trading ineffective fire (kills 1
        # then drifts to exit). So CLOSE the gap while firing until we are at
        # reliable-kill range, only planting for a stationary burst when close.
        close_range = 250
        fire_cone = 18.0 if distance_units <= 256 else (11.0 if distance_units <= 700 else 6.0)
        wants_fire = shoot and raw_cls is not None and (engine_shootable or abs(delta) <= fire_cone)
        # Hazard-aware combat: never step OFF safe floor INTO nukage/lava to fight — the
        # toxic-slime death loop (hazard escape walks out, blind combat charges back in,
        # repeat until dead). Bullets cross slime fine: fight from the walkway. When we're
        # ALREADY in slime, don't gate — the hazard override owns getting us out.
        px, py = int(player["point"].x), int(player["point"].y)
        on_safe_floor = not self.point_is_damaging_fp(px, py)

        def _lands_in_hazard(bearing_deg: float) -> bool:
            # Sample several distances: on E1M1's zigzag a single 72u probe lands on the
            # FAR walkway while the path in between crosses slime (traced: rush_fire
            # charged straight back into the pool one step after escaping it).
            rad = math.radians(bearing_deg % 360.0)
            return any(
                self.point_is_damaging_fp(
                    px + int(math.cos(rad) * units * FP_UNIT),
                    py + int(math.sin(rad) * units * FP_UNIT),
                )
                for units in (36, 72, 108)
            )

        enemy_bearing = _bearing(player["point"], target)
        hazard_toward_enemy = on_safe_floor and _lands_in_hazard(enemy_bearing)
        if wants_fire and distance_units > close_range and forward_open and not hazard_toward_enemy:
            # Charge in WHILE firing and steering onto the target.
            merged["action"] = "rush_fire"
            merged["converged"] = engine_shootable
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(
                    duration_tics=6,
                    raw=raw_cls(
                        buttons=1,  # BT_ATTACK
                        angle_turn=self._raw_turn_units(delta, cap=384),
                        forward_move=50,
                    ),
                ),
                detail=merged,
            )
        if wants_fire:
            # NEVER plant: fire WHILE moving (the "never stop" contract). Classic DOOM
            # circle-strafe — sidestep across the target while shooting so we're a moving
            # gun, not a standing one (traces showed 9-step stationary fire loops). Pick a
            # strafe side from the open lateral probe; edge forward too when there's room to
            # keep pressing. Aim stays live via angle_turn.
            side = self._best_lateral_probe(navigation)  # +left / -right / 0
            side_move = 40 if side > 0 else (-40 if side < 0 else 30)  # always some lateral motion
            fwd = 20 if (distance_units > 120 and forward_open) else 0
            if on_safe_floor:
                # Keep the circle-strafe ON the walkway: engine sidemove>0 strafes RIGHT
                # (world bearing angle-90, DOOM angles are CCW). Flip to the safe side, or
                # plant for this burst if slime flanks both sides — a standing burst beats
                # shedding health in nukage.
                right_haz = _lands_in_hazard(player["angle"] - 90.0)
                left_haz = _lands_in_hazard(player["angle"] + 90.0)
                if (side_move > 0 and right_haz) or (side_move < 0 and left_haz):
                    side_move = -side_move if not (left_haz if side_move > 0 else right_haz) else 0
                if fwd and _lands_in_hazard(player["angle"]):
                    fwd = 0
            merged["action"] = "strafe_fire"
            merged["converged"] = engine_shootable
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(
                    duration_tics=6,
                    raw=raw_cls(
                        buttons=1,  # BT_ATTACK
                        angle_turn=self._raw_turn_units(delta, cap=384),
                        side_move=side_move,
                        forward_move=fwd,
                    ),
                ),
                detail=merged,
            )
        aim_tolerance = 2.5 if distance_units <= 384 else 8.0
        if abs(delta) > aim_tolerance:
            merged["action"] = "aim"
            return self._turn(player, delta, agent_pb2, skill, merged)
        if distance_units > close_range and forward_open and not hazard_toward_enemy:
            merged["action"] = "close"
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=50, duration_tics=12),
                detail=merged,
            )
        strafe = self._combat_strafe(state, agent_pb2, merged)
        if strafe is not None:
            return strafe
        if hazard_toward_enemy:
            # Nudging forward would step into slime: hold ground and fire instead.
            merged["action"] = "hold_fire_at_hazard_edge"
            if wants_fire:
                return PlanAction(
                    skill=skill,
                    action=agent_pb2.PlayerAction(
                        duration_tics=6,
                        raw=raw_cls(buttons=1, angle_turn=self._raw_turn_units(delta, cap=384)),
                    ),
                    detail=merged,
                )
            return self._turn(player, delta, agent_pb2, skill, merged)
        merged["action"] = "nudge"
        return PlanAction(
            skill=skill,
            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=32, duration_tics=8),
            detail=merged,
        )

    def _combat_strafe(self, state: Any, agent_pb2: Any, detail: dict[str, Any]) -> PlanAction | None:
        navigation = getattr(state, "navigation", None)
        offset = self._best_lateral_probe(navigation)
        if not offset:
            return None
        merged = dict(detail)
        merged["action"] = "strafe"
        merged["probe"] = int(offset)
        action_type = agent_pb2.ACTION_STRAFE_LEFT if offset > 0 else agent_pb2.ACTION_STRAFE_RIGHT
        return PlanAction(
            skill="close_visible_contact",
            action=agent_pb2.PlayerAction(action=action_type, amount=24, duration_tics=8),
            detail=merged,
        )

    def _turn(
        self,
        player: dict[str, Any],
        delta: float,
        agent_pb2: Any,
        skill: str,
        detail: dict[str, Any],
        *,
        door_line_id: int | None = None,
    ) -> PlanAction:
        desired = min(45.0, abs(float(delta)))
        duration = 3 if desired > 28.0 else (2 if desired > 14.0 else 1)
        amount = max(6, min(26, int(desired / max(1, duration) * 2.2)))
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is not None:
            action = agent_pb2.PlayerAction(
                duration_tics=duration,
                raw=raw_cls(angle_turn=self._raw_turn_units(delta)),
            )
        else:
            action_type = agent_pb2.ACTION_TURN_LEFT if delta > 0 else agent_pb2.ACTION_TURN_RIGHT
            action = agent_pb2.PlayerAction(action=action_type, amount=amount, duration_tics=duration)
        merged = dict(detail)
        merged.update({"action": "turn", "turn": round(delta, 1)})
        return PlanAction(
            skill=skill,
            action=action,
            door_line_id=door_line_id,
            detail=merged,
        )

    def _raw_turn_units(self, delta: float, *, cap: int = RAW_TURN_CAP) -> int:
        return max(-int(cap), min(int(cap), int(float(delta) * RAW_TURN_SCALE)))

    def _raw_steer_turn_units(self, delta: float) -> int:
        return max(
            -int(RAW_STEER_TURN_CAP),
            min(int(RAW_STEER_TURN_CAP), int(float(delta) * RAW_STEER_TURN_SCALE)),
        )

    def _side_probe_open(self, state: Any, *, left: bool) -> bool:
        navigation = getattr(state, "navigation", None)
        wanted_sign = 1 if left else -1
        for probe in getattr(navigation, "direction_probes", []) or []:
            offset = int(getattr(probe, "angle_offset_degrees", 0) or 0)
            if offset == 0 or (1 if offset > 0 else -1) != wanted_sign:
                continue
            if 45 <= abs(offset) <= 135 and bool(getattr(probe, "open", False)):
                return True
        return False

    def _side_probe_distance(self, state: Any, *, left: bool) -> int:
        navigation = getattr(state, "navigation", None)
        wanted_sign = 1 if left else -1
        best = 0
        for probe in getattr(navigation, "direction_probes", []) or []:
            offset = int(getattr(probe, "angle_offset_degrees", 0) or 0)
            if offset == 0 or (1 if offset > 0 else -1) != wanted_sign:
                continue
            if 45 <= abs(offset) <= 135 and bool(getattr(probe, "open", False)):
                best = max(best, int(getattr(probe, "block_distance_fp", 0) or 0))
        return best

    def _line_point(self, line: Any) -> Point | None:
        for attr in ("nearest_point", "midpoint"):
            pos = getattr(line, attr, None)
            if pos is not None:
                return Point(int(getattr(pos, "x_fp", 0)), int(getattr(pos, "y_fp", 0)))
        return None

    def _line_midpoint(self, line: Any) -> Point | None:
        pos = getattr(line, "midpoint", None)
        if pos is not None:
            return Point(int(getattr(pos, "x_fp", 0)), int(getattr(pos, "y_fp", 0)))
        return None

    def _nearest_point_on_line(self, point: Point, line: MapLineRuntime) -> Point:
        ax = float(line.a.x)
        ay = float(line.a.y)
        bx = float(line.b.x)
        by = float(line.b.y)
        dx = bx - ax
        dy = by - ay
        length_sq = dx * dx + dy * dy
        if length_sq <= 1.0:
            return line.midpoint
        t = ((float(point.x) - ax) * dx + (float(point.y) - ay) * dy) / length_sq
        t = max(0.0, min(1.0, t))
        return Point(int(ax + dx * t), int(ay + dy * t))

    def _line_use_target(self, point: Point, line: MapLineRuntime) -> Point:
        nearest = self._nearest_point_on_line(point, line)
        dx = float(line.b.x - line.a.x)
        dy = float(line.b.y - line.a.y)
        length = math.hypot(dx, dy)
        if length <= 1.0:
            return nearest
        nx = -dy / length
        ny = dx / length
        px = float(point.x - nearest.x)
        py = float(point.y - nearest.y)
        sign = -1.0 if (px * nx + py * ny) > 0.0 else 1.0
        push = 96 * FP_UNIT
        return Point(
            int(nearest.x + nx * sign * push),
            int(nearest.y + ny * sign * push),
        )

    def _line_projection_fraction(self, point: Point, line: MapLineRuntime) -> float:
        ax = float(line.a.x)
        ay = float(line.a.y)
        bx = float(line.b.x)
        by = float(line.b.y)
        dx = bx - ax
        dy = by - ay
        length_sq = dx * dx + dy * dy
        if length_sq <= 1.0:
            return 0.5
        return max(0.0, min(1.0, ((float(point.x) - ax) * dx + (float(point.y) - ay) * dy) / length_sq))

    def _sector_route_to_enemy_action(
        self,
        state: Any,
        player: dict[str, Any],
        enemy_point: Point,
        agent_pb2: Any,
        door_memory: Any,
        *,
        shoot: bool,
        enemy_id: int,
    ) -> PlanAction | None:
        target_sectors: list[int] = []
        enemy_sector = self.sector_for_point_fp(enemy_point.x, enemy_point.y)
        for sector in self.sectors.values():
            distance = _dist(sector.center, enemy_point) / FP_UNIT
            if 96 <= distance <= 1400 and self.has_line_of_sight(sector.center, enemy_point):
                target_sectors.append(sector.id)
        if enemy_sector is not None and enemy_sector not in target_sectors:
            target_sectors.append(enemy_sector)
        route = self._sector_route_from_player(player, target_sectors, door_memory)
        if not route:
            return None
        self._last_status = "sector_route_to_enemy"
        self._last_route_len = len(route)
        return self._portal_route_action(
            player,
            route,
            agent_pb2,
            door_memory,
            skill="close_visible_contact" if shoot else "seek_enemy",
            detail={"skill": "sector_route_to_los", "enemy": enemy_id, "route": len(route)},
            state=state,
        )

    def _low_health_backtrack_evasion(
        self,
        state: Any | None,
        player: dict[str, Any],
        edge: PortalEdge,
        target: Point,
        agent_pb2: Any,
        route_remaining: int,
    ) -> PlanAction | None:
        if state is None or not edge.passable or route_remaining > 6:
            return None
        health = int(getattr(getattr(state, "player", None), "health", 100) or 100)
        if health > NO_KILL_LOS_BREAK_HEALTH:
            return None
        enemy = self._nearest_enemy(state, player)
        if enemy is None or not bool(enemy.get("line_of_sight")):
            return None
        delta = _angle_delta(_bearing(player["point"], target), player["angle"])
        if abs(delta) < 120.0:
            return None
        evasion = self._no_kill_route_evasion(state, player, agent_pb2, blocking_only=False)
        if evasion is None:
            return None
        detail = dict(evasion.detail)
        detail.update(
            {
                "avoid": "low_health_backtrack",
                "portal_line": edge.line_id,
                "route": route_remaining,
                "portal_turn": round(delta, 1),
            }
        )
        return PlanAction(
            skill=evasion.skill,
            action=evasion.action,
            door_line_id=edge.line_id,
            detail=detail,
        )

    def player_from_state(self, state: Any) -> dict[str, Any] | None:
        return self._player(state)

    def sector_for_player(self, state: Any, player: dict[str, Any] | None = None) -> int | None:
        navigation = getattr(state, "navigation", None)
        current = getattr(navigation, "current_sector", None)
        sector_id = getattr(current, "sector_id", None)
        if sector_id is not None:
            try:
                return int(sector_id)
            except Exception:
                pass
        if player is None:
            player = self._player(state)
        if player is None:
            return None
        return self.sector_for_point_fp(player["point"].x, player["point"].y)

    def hazard_escape_action(self, state: Any, agent_pb2: Any, door_memory: Any) -> PlanAction | None:
        player = self._player(state)
        if player is None:
            return None
        current_sector = self.sector_for_player(state, player)
        if current_sector is None or not self.sector_is_damaging(int(current_sector)):
            return None
        # Nearest SAFE STANDABLE ground first: nav cells are sampled only on non-damaging
        # floors, so the closest node IS the shortest way out of the acid. The sector-route
        # fallback below picks safe sectors by route cost and was traced marching DoomGuy
        # ACROSS the nukage pool to a far edge (hp 90->75 wading, then wedged). Sprint
        # toward the nearest node instead — translating every step; acid ticks while turning.
        # CLIMBABLE only: DOOM's step-up limit is 24 units. E1M1's pool floor sits well
        # below the raised walkway, so the geometrically-nearest node can be an unclimbable
        # lip — traced as oscillating at the pool wall bleeding hp 83->73. Filter targets to
        # floors within step-up range of the current sector.
        cur = self.sectors.get(int(current_sector))
        cur_floor = int(getattr(cur, "floor_height_fp", 0) or 0)
        climbable_pts = []
        for cell in self.nav_cells:
            if cell.sector_id is None:
                continue
            cell_sector = self.sectors.get(int(cell.sector_id))
            if cell_sector is None:
                continue
            if int(getattr(cell_sector, "floor_height_fp", 0) or 0) - cur_floor <= 24 * FP_UNIT:
                climbable_pts.append(cell.point)
        if climbable_pts:
            nearest = min(climbable_pts, key=lambda pt: _dist(player["point"], pt))
            near_dist = _dist(player["point"], nearest)
            if near_dist <= 600 * FP_UNIT:
                self._last_status = "hazard_floor_escape"
                delta = _angle_delta(_bearing(player["point"], nearest), player["angle"])
                detail = {
                    "skill": "hazard_nearest_ground_escape",
                    "hazard_sector": int(current_sector),
                    "dist": int(near_dist / FP_UNIT),
                }
                raw_cls = getattr(agent_pb2, "RawTiccmd", None)
                if raw_cls is not None:
                    return PlanAction(
                        skill="route_progression",
                        action=agent_pb2.PlayerAction(
                            duration_tics=8,
                            raw=raw_cls(forward_move=56, angle_turn=self._raw_steer_turn_units(delta)),
                        ),
                        detail=detail,
                    )
                return self._turn_or_forward(player, nearest, agent_pb2, skill="route_progression", detail=detail)
        safe_targets = [
            sector_id
            for sector_id, sector in self.sectors.items()
            if int(sector_id) != int(current_sector) and not bool(sector.damaging)
        ]
        route = self._sector_route(int(current_sector), safe_targets, door_memory)
        if route:
            self._last_status = "hazard_floor_escape"
            self._last_route_len = len(route)
            return self._portal_route_action(
                player,
                route,
                agent_pb2,
                door_memory,
                skill="route_progression",
                detail={
                    "skill": "sector_route_hazard_escape",
                    "hazard_sector": int(current_sector),
                    "route": len(route),
                },
                state=state,
            )
        return self._hazard_local_escape_action(state, player, agent_pb2, current_sector=int(current_sector))

    def _hazard_local_escape_action(self, state: Any, player: dict[str, Any], agent_pb2: Any, *, current_sector: int) -> PlanAction:
        navigation = getattr(state, "navigation", None)
        probes = [
            probe
            for probe in getattr(navigation, "direction_probes", []) or []
            if bool(getattr(probe, "open", False))
        ]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if probes:
            probe = max(probes, key=lambda item: int(getattr(item, "block_distance_fp", 0) or 0))
            offset = float(getattr(probe, "angle_offset_degrees", 0) or 0.0)
        elif bool(getattr(navigation, "forward_open", False)):
            offset = 0.0
        elif bool(getattr(navigation, "back_open", False)):
            offset = 180.0
        else:
            offset = 90.0
        detail = {
            "skill": "hazard_floor_escape",
            "action": "hazard_probe_escape",
            "hazard_sector": int(current_sector),
            "probe": round(offset, 1),
        }
        if raw_cls is not None:
            side = 0
            if abs(offset) >= 45.0 and abs(offset) <= 135.0:
                side = 54 if offset > 0 else -54
            forward = -48 if abs(offset) > 135.0 else 56
            return PlanAction(
                skill="route_progression",
                action=agent_pb2.PlayerAction(
                    duration_tics=8,
                    raw=raw_cls(
                        forward_move=forward,
                        side_move=side,
                        angle_turn=self._raw_steer_turn_units(offset),
                    ),
                ),
                detail=detail,
            )
        if abs(offset) <= 20.0:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=48, duration_tics=8)
        elif abs(offset) >= 135.0:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=42, duration_tics=8)
        elif abs(offset) >= 45.0:
            action_type = agent_pb2.ACTION_STRAFE_LEFT if offset > 0 else agent_pb2.ACTION_STRAFE_RIGHT
            action = agent_pb2.PlayerAction(action=action_type, amount=44, duration_tics=8)
        else:
            action = self._turn(player, offset, agent_pb2, "route_progression", detail).action
        return PlanAction(skill="route_progression", action=action, detail=detail)

    def sector_for_point_fp(self, x_fp: int, y_fp: int) -> int | None:
        point = Point(int(x_fp), int(y_fp))
        containing: list[tuple[float, int]] = []
        for sector_id in self.sectors:
            if self._point_in_sector(point, sector_id):
                containing.append((_dist(point, self.sectors[sector_id].center), sector_id))
        if containing:
            return min(containing)[1]
        if not self.sectors:
            return None
        return min(self.sectors, key=lambda sector_id: _dist(point, self.sectors[sector_id].center))

    def _sector_containing_point(self, point: Point) -> int | None:
        containing: list[tuple[float, int]] = []
        for sector_id in self.sectors:
            if self._point_in_sector(point, sector_id):
                containing.append((_dist(point, self.sectors[sector_id].center), sector_id))
        if not containing:
            return None
        return min(containing)[1]

    def neighbor_sector_ids(self, sector_id: int) -> set[int]:
        return {edge.dst for edge in self._portal_graph.get(int(sector_id), [])}

    def point_is_damaging_fp(self, x_fp: int, y_fp: int) -> bool:
        """True when the map point sits in a damaging (nukage/lava) sector."""
        sector_id = self.sector_for_point_fp(int(x_fp), int(y_fp))
        return sector_id is not None and self.sector_is_damaging(int(sector_id))

    def sector_is_damaging(self, sector_id: int) -> bool:
        sector = self.sectors.get(int(sector_id))
        return bool(sector and sector.damaging)

    def _point_in_sector(self, point: Point, sector_id: int) -> bool:
        crossings = 0
        for line in self.lines:
            if line.front_sector != sector_id and line.back_sector != sector_id:
                continue
            a, b = line.a, line.b
            if (a.y > point.y) == (b.y > point.y):
                continue
            denom = b.y - a.y
            if denom == 0:
                continue
            x_cross = a.x + (point.y - a.y) * (b.x - a.x) / denom
            if x_cross > point.x:
                crossings += 1
        return crossings % 2 == 1

    def _local_probe_action(
        self,
        state: Any | None,
        player: dict[str, Any],
        target: Point,
        agent_pb2: Any,
        *,
        skill: str,
        detail: dict[str, Any],
    ) -> PlanAction:
        desired = _angle_delta(_bearing(player["point"], target), player["angle"])
        probes = []
        navigation = getattr(state, "navigation", None) if state is not None else None
        for probe in getattr(navigation, "direction_probes", []) or []:
            if not bool(getattr(probe, "open", False)):
                continue
            offset = float(getattr(probe, "angle_offset_degrees", 0))
            distance = float(getattr(probe, "block_distance_fp", 0) or 0) / FP_UNIT
            probes.append((abs(offset - desired), -distance, offset))
        if probes:
            offset = min(probes)[2]
            merged = dict(detail)
            merged.update({"probe": round(offset, 1), "desired": round(desired, 1)})
            if abs(offset) <= 10.0:
                merged["action"] = "forward"
                return PlanAction(
                    skill=skill,
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=42, duration_tics=14),
                    detail=merged,
                )
            if abs(offset) >= 45.0:
                merged["action"] = "probe_strafe"
                action_type = agent_pb2.ACTION_STRAFE_LEFT if offset > 0 else agent_pb2.ACTION_STRAFE_RIGHT
                return PlanAction(
                    skill=skill,
                    action=agent_pb2.PlayerAction(action=action_type, amount=32, duration_tics=10),
                    detail=merged,
                )
            return self._turn(player, offset, agent_pb2, skill, merged)
        if bool(getattr(navigation, "forward_open", False)):
            merged = dict(detail)
            merged["action"] = "forward"
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=34, duration_tics=10),
                detail=merged,
            )
        if bool(getattr(navigation, "back_open", False)):
            merged = dict(detail)
            merged["action"] = "probe_backoff"
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=34, duration_tics=10),
                detail=merged,
            )
        turn = desired if abs(desired) > 8.0 else 30.0
        merged = dict(detail)
        merged["action"] = "scan"
        return self._turn(player, turn, agent_pb2, "seek_enemy" if skill != "route_progression" else skill, merged)

    def _low_health(self, state: Any) -> bool:
        player = getattr(state, "player", None)
        return int(getattr(player, "health", 100) or 0) <= 30

    def _retreat_action(self, state: Any, player: dict[str, Any], agent_pb2: Any, *, detail: dict[str, Any]) -> PlanAction:
        navigation = getattr(state, "navigation", None)
        probes = [
            probe for probe in getattr(navigation, "direction_probes", []) or []
            if bool(getattr(probe, "open", False)) and abs(int(getattr(probe, "angle_offset_degrees", 0))) >= 60
        ]
        if probes:
            probe = max(probes, key=lambda item: int(getattr(item, "block_distance_fp", 0) or 0))
            offset = float(getattr(probe, "angle_offset_degrees", 0))
            if abs(offset) <= 100:
                action_type = agent_pb2.ACTION_STRAFE_LEFT if offset > 0 else agent_pb2.ACTION_STRAFE_RIGHT
                merged = dict(detail)
                merged.update({"action": "strafe", "probe": round(offset, 1)})
                return PlanAction(
                    skill="retreat",
                    action=agent_pb2.PlayerAction(action=action_type, amount=22, duration_tics=8),
                    detail=merged,
                )
        merged = dict(detail)
        merged["action"] = "back"
        return PlanAction(
            skill="retreat",
            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=20, duration_tics=8),
            detail=merged,
        )

    def _floor_fp(self, sector_id: int) -> int | None:
        sector = self.sectors.get(int(sector_id))
        return None if sector is None else int(sector.floor_height_fp)

    def _step_up_fp(self, line: MapLineRuntime, src_point: Point) -> int:
        """Floor rise when crossing `line` away from src_point (0 when flat/downhill/unknown)."""
        if not line.two_sided:
            return 0
        front = self._floor_fp(line.front_sector)
        back = self._floor_fp(line.back_sector)
        if front is None or back is None or front == back:
            return 0
        # Doom convention: the front sidedef sits on the right of the v1->v2 vector.
        cross = (line.b.x - line.a.x) * (src_point.y - line.a.y) - (line.b.y - line.a.y) * (src_point.x - line.a.x)
        src_floor, dst_floor = (back, front) if cross > 0 else (front, back)
        return max(0, dst_floor - src_floor)

    def has_line_of_sight(self, a: Point, b: Point) -> bool:
        key = (a.x, a.y, b.x, b.y)
        cached = self._los_cache.get(key)
        if cached is not None:
            return cached
        result = True
        for line in self._sight_blocking_lines:
            if self._line_crosses_segment(line, a, b):
                result = False
                break
        self._los_cache[key] = result
        return result

    def _line_crosses_segment(self, line: MapLineRuntime, a: Point, b: Point) -> bool:
        if _same_point(a, line.a) or _same_point(a, line.b) or _same_point(b, line.a) or _same_point(b, line.b):
            return False
        return _segments_intersect(a, b, line.a, line.b)

    def _vantage_nodes(self, enemy: Point, start: Point) -> list[int]:
        candidates: list[tuple[float, int]] = []
        for idx, node in enumerate(self.nodes):
            if self._grid_key(node) in self._blocked_los_route_keys:
                continue
            d_enemy = _dist(node, enemy) / FP_UNIT
            if d_enemy < 96 or d_enemy > 1400:
                continue
            if self.has_line_of_sight(node, enemy):
                score = (_dist(start, node) / FP_UNIT) + d_enemy * 0.25
                candidates.append((score, idx))
        if not candidates:
            return self._nearest_nodes(enemy, limit=16)
        candidates.sort()
        return [idx for _, idx in candidates[:24]]

    def _nearest_nodes(self, point: Point, *, limit: int) -> list[int]:
        scored = sorted(((_dist(point, node), idx) for idx, node in enumerate(self.nodes)), key=lambda item: item[0])
        return [idx for _, idx in scored[:limit]]

    def _neighbors(self, idx: int) -> Iterable[int]:
        if idx < len(self.nav_cells):
            x_key, y_key = self.nav_cells[idx].block
        else:
            x_key, y_key = self._grid_key(self.nodes[idx])
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                for nxt in self._blockmap.get((x_key + dx, y_key + dy), []):
                    if nxt != idx:
                        yield nxt

    def _sample_nav_cells(self) -> list[NavCellRuntime]:
        left, right, bottom, top = self._bounds_cache
        cell = self.cell_fp
        while True:
            cols = max(1, int((right - left) // cell) + 1)
            rows = max(1, int((top - bottom) // cell) + 1)
            if cols * rows <= self.max_nodes:
                break
            cell = int(cell * 1.35)
        self.cell_fp = cell
        cells: list[NavCellRuntime] = []
        y = bottom + cell // 2
        while y <= top:
            x = left + cell // 2
            while x <= right:
                point = Point(int(x), int(y))
                sector_id = self._sector_containing_point(point)
                if not self.sectors or sector_id is not None:
                    if sector_id is None or not self.sector_is_damaging(sector_id):
                        cells.append(
                            NavCellRuntime(
                                id=len(cells),
                                point=point,
                                sector_id=sector_id,
                                block=(int((point.x - left) // cell), int((point.y - bottom) // cell)),
                            )
                        )
                x += cell
            y += cell
        if cells:
            return cells
        return [
            NavCellRuntime(
                id=0,
                point=Point((left + right) // 2, (bottom + top) // 2),
                sector_id=None,
                block=(0, 0),
            )
        ]

    def _sample_nodes(self) -> list[Point]:
        return [cell.point for cell in self._sample_nav_cells()]

    def _bounds(self) -> tuple[int, int, int, int]:
        left = int(getattr(self.snapshot, "bbox_left_fp", 0) or 0)
        right = int(getattr(self.snapshot, "bbox_right_fp", 0) or 0)
        top = int(getattr(self.snapshot, "bbox_top_fp", 0) or 0)
        bottom = int(getattr(self.snapshot, "bbox_bottom_fp", 0) or 0)
        if right <= left or top <= bottom:
            xs = [point.x for point in self.vertices]
            ys = [point.y for point in self.vertices]
            left, right = min(xs), max(xs)
            bottom, top = min(ys), max(ys)
        pad = int(32 * FP_UNIT)
        return left - pad, right + pad, bottom - pad, top + pad

    def _grid_key(self, point: Point) -> tuple[int, int]:
        left, _, bottom, _ = getattr(self, "_bounds_cache", self._bounds())
        return (int((point.x - left) // self.cell_fp), int((point.y - bottom) // self.cell_fp))

    def _vertices(self, snapshot: Any) -> list[Point]:
        vertices = []
        for vertex in getattr(snapshot, "vertices", []) or []:
            vertices.append(Point(int(getattr(vertex, "x_fp", 0)), int(getattr(vertex, "y_fp", 0))))
        if not vertices:
            vertices.append(Point(0, 0))
        return vertices

    def _sectors(self, snapshot: Any) -> dict[int, SectorRuntime]:
        raw_sectors = {int(getattr(sector, "id", idx)): sector for idx, sector in enumerate(getattr(snapshot, "sectors", []) or [])}
        points_by_sector: dict[int, list[Point]] = {sector_id: [] for sector_id in raw_sectors}
        for line in self.lines:
            for sector_id in (line.front_sector, line.back_sector):
                if sector_id >= 0:
                    points_by_sector.setdefault(sector_id, []).append(line.midpoint)
        sectors: dict[int, SectorRuntime] = {}
        for sector_id, raw in raw_sectors.items():
            points = points_by_sector.get(sector_id) or [Point(0, 0)]
            center = Point(
                int(sum(point.x for point in points) / len(points)),
                int(sum(point.y for point in points) / len(points)),
            )
            sectors[sector_id] = SectorRuntime(
                id=sector_id,
                center=center,
                tag=int(getattr(raw, "tag", 0) or 0),
                floor_height_fp=int(getattr(raw, "floor_height_fp", 0) or 0),
                ceiling_height_fp=int(getattr(raw, "ceiling_height_fp", 0) or 0),
                damaging=bool(getattr(raw, "damaging", False)),
                exit_damage=bool(getattr(raw, "exit_damage", False)),
            )
        for sector_id, points in points_by_sector.items():
            if sector_id in sectors:
                continue
            center = Point(
                int(sum(point.x for point in points) / len(points)),
                int(sum(point.y for point in points) / len(points)),
            )
            sectors[sector_id] = SectorRuntime(id=sector_id, center=center)
        return sectors

    def _portal_edges(self) -> list[PortalEdge]:
        edges: list[PortalEdge] = []
        for line in self.lines:
            if line.front_sector < 0 or line.back_sector < 0 or line.front_sector == line.back_sector:
                continue
            front = self.sectors.get(line.front_sector)
            back = self.sectors.get(line.back_sector)
            front_tag = 0 if front is None else int(front.tag)
            back_tag = 0 if back is None else int(back.tag)
            tag_gate = self._dynamic_tag_gate_for_line(line, front_tag, back_tag)
            if not (line.passable or line.door or line.use_trigger or line.walk_trigger or line.exit or tag_gate):
                continue
            use_line = bool((line.door or line.use_trigger) and not line.passable)
            for src, dst in ((line.front_sector, line.back_sector), (line.back_sector, line.front_sector)):
                src_floor = self._floor_fp(src)
                dst_floor = self._floor_fp(dst)
                step_up = 0
                if src_floor is not None and dst_floor is not None:
                    step_up = max(0, dst_floor - src_floor)
                # A step-up taller than 24 units is unwalkable against STATIC floors, but only the
                # engine knows dynamic heights (lifts, stair builders, floor raisers). USE-able
                # lines become door-like portals in that direction; everything else stays routable
                # as a last-resort probe edge with a steep cost, and live probes + door memory
                # abandon it if it really is a wall. Downhill stays cheap (drops are always legal).
                over_step = step_up > MAX_STEP_UP_FP
                dir_use_line = use_line or bool((line.door or line.use_trigger) and over_step)
                src_center = self.sectors.get(src, SectorRuntime(src, line.midpoint)).center
                dst_center = self.sectors.get(dst, SectorRuntime(dst, line.midpoint)).center
                cost = (_dist(src_center, line.midpoint) + _dist(line.midpoint, dst_center)) / FP_UNIT
                if dir_use_line:
                    cost += 64.0
                if over_step and not dir_use_line:
                    cost += 768.0
                if tag_gate:
                    cost += 48.0
                if line.exit:
                    cost -= 16.0
                edges.append(
                    PortalEdge(
                        src=src,
                        dst=dst,
                        line_id=line.id,
                        point=line.midpoint,
                        special=line.special,
                        passable=line.passable and step_up <= MAX_STEP_UP_FP,
                        use_line=dir_use_line,
                        door=line.door,
                        exit=line.exit,
                        walk_trigger=line.walk_trigger,
                        cost=max(1.0, cost),
                        tag_gate=tag_gate,
                    )
                )
        return edges

    def _dynamic_tag_gate_for_line(self, line: MapLineRuntime, front_tag: int, back_tag: int) -> int:
        if line.special != 0 or line.passable or line.blocking or not line.two_sided:
            return 0
        if front_tag > 0 and back_tag == front_tag:
            return front_tag
        if front_tag > 0 and back_tag == 0:
            return front_tag
        if back_tag > 0 and front_tag == 0:
            return back_tag
        return 0

    def _lines(self, snapshot: Any) -> list[MapLineRuntime]:
        lines: list[MapLineRuntime] = []
        for raw in getattr(snapshot, "lines", []) or []:
            v1 = int(getattr(raw, "v1", -1))
            v2 = int(getattr(raw, "v2", -1))
            if v1 < 0 or v2 < 0 or v1 >= len(self.vertices) or v2 >= len(self.vertices):
                continue
            a = self.vertices[v1]
            b = self.vertices[v2]
            lines.append(
                MapLineRuntime(
                    id=int(getattr(raw, "id", len(lines))),
                    v1=v1,
                    v2=v2,
                    a=a,
                    b=b,
                    special=int(getattr(raw, "special", 0)),
                    tag=int(getattr(raw, "tag", 0)),
                    front_sector=int(getattr(raw, "front_sector", -1)),
                    back_sector=int(getattr(raw, "back_sector", -1)),
                    two_sided=bool(getattr(raw, "two_sided", False)),
                    passable=bool(getattr(raw, "passable", False)),
                    blocking=bool(getattr(raw, "blocking", False)),
                    sight_blocking=bool(getattr(raw, "sight_blocking", False)),
                    door=bool(getattr(raw, "door", False)),
                    use_trigger=bool(getattr(raw, "use_trigger", False)),
                    walk_trigger=bool(getattr(raw, "walk_trigger", False)),
                    exit=bool(getattr(raw, "exit", False)),
                    midpoint=Point((a.x + b.x) // 2, (a.y + b.y) // 2),
                    lift=bool(getattr(raw, "lift", False)) or int(getattr(raw, "special", 0)) in LIFT_SPECIALS,
                )
            )
        return lines

    def _key_items(self, snapshot: Any) -> list[KeyItemRuntime]:
        items: list[KeyItemRuntime] = []
        for raw in getattr(snapshot, "things", []) or []:
            type_id = int(getattr(raw, "type_id", 0) or 0)
            color = KEY_THING_TYPES.get(type_id)
            if not color:
                continue
            pos = getattr(raw, "position", None)
            if pos is None:
                continue
            point = Point(int(getattr(pos, "x_fp", 0) or 0), int(getattr(pos, "y_fp", 0) or 0))
            items.append(
                KeyItemRuntime(
                    id=int(getattr(raw, "id", len(items)) or 0),
                    type_id=type_id,
                    color=color,
                    point=point,
                    sector_id=self.sector_for_point_fp(point.x, point.y),
                )
            )
        return items

    def _health_items(self, snapshot: Any) -> list[HealthItemRuntime]:
        items: list[HealthItemRuntime] = []
        for raw in getattr(snapshot, "things", []) or []:
            type_id = int(getattr(raw, "type_id", 0) or 0)
            health = HEALTH_THING_TYPES.get(type_id)
            if health is None:
                continue
            kind, value = health
            pos = getattr(raw, "position", None)
            if pos is None:
                continue
            point = Point(int(getattr(pos, "x_fp", 0) or 0), int(getattr(pos, "y_fp", 0) or 0))
            items.append(
                HealthItemRuntime(
                    id=int(getattr(raw, "id", len(items)) or 0),
                    type_id=type_id,
                    kind=kind,
                    value=int(value),
                    point=point,
                    sector_id=self.sector_for_point_fp(point.x, point.y),
                )
            )
        return items

    def _barrel_items(self, snapshot: Any) -> list[BarrelRuntime]:
        items: list[BarrelRuntime] = []
        for raw in getattr(snapshot, "things", []) or []:
            type_id = int(getattr(raw, "type_id", 0) or 0)
            if type_id not in BARREL_THING_TYPES:
                continue
            pos = getattr(raw, "position", None)
            if pos is None:
                continue
            point = Point(int(getattr(pos, "x_fp", 0) or 0), int(getattr(pos, "y_fp", 0) or 0))
            items.append(
                BarrelRuntime(
                    id=int(getattr(raw, "id", len(items)) or 0),
                    type_id=type_id,
                    point=point,
                    sector_id=self.sector_for_point_fp(point.x, point.y),
                )
            )
        return items

    def _line_by_id(self, line_id: int | None) -> MapLineRuntime | None:
        if line_id is None:
            return None
        for line in self.lines:
            if line.id == int(line_id):
                return line
        return None

    def _player(self, state: Any) -> dict[str, Any] | None:
        player = getattr(state, "player", None)
        obj = getattr(player, "object", None)
        pos = getattr(obj, "position", None)
        if pos is None:
            return None
        return {
            "point": Point(int(getattr(pos, "x_fp", 0)), int(getattr(pos, "y_fp", 0))),
            "angle": int(getattr(obj, "angle_degrees", 0)) % 360,
        }

    def _shootable(self, state: Any) -> bool:
        combat = getattr(state, "combat", None)
        return bool(getattr(combat, "has_shootable_target", False) and getattr(combat, "target_is_enemy", False))

    def _enemy_health(self, state: Any, enemy_id: int) -> int:
        """Current health of a specific enemy id (0 if gone) — for stalemate detection."""
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            if obj is not None and int(getattr(obj, "id", 0) or 0) == int(enemy_id):
                return int(getattr(obj, "health", 0) or 0)
        return 0

    def _enemies_remain(self, state: Any) -> bool:
        """True if any live enemy (health > 0) is in the current observation — the
        'clear the map' gate: keep hunting while any remain, only surrender to the
        exit route once the observed set is empty. Bounded to what the engine reports
        this frame (DOOM gives no global census), which is the right scope: an enemy
        we can't see yet becomes visible as we advance toward the exit through-line."""
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            if obj is not None and int(getattr(obj, "health", 0) or 0) > 0:
                return True
        return False

    def _nearest_enemy(self, state: Any, player: dict[str, Any]) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        combat = getattr(state, "combat", None)
        combat_target = int(getattr(combat, "target_id", 0) or 0)
        has_shootable_target = bool(getattr(combat, "has_shootable_target", False) and getattr(combat, "target_is_enemy", False))
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            pos = getattr(obj, "position", None)
            if obj is None or pos is None or int(getattr(obj, "health", 0)) <= 0:
                continue
            point = Point(int(getattr(pos, "x_fp", 0)), int(getattr(pos, "y_fp", 0)))
            distance = int(getattr(obj, "distance_fp", 0) or _dist(player["point"], point))
            enemy_id = int(getattr(obj, "id", 0))
            line_of_sight = bool(getattr(enemy, "line_of_sight", False))
            type_id = int(getattr(obj, "type_id", 0) or 0)
            candidates.append(
                {
                    "id": enemy_id,
                    "type_id": type_id,
                    "threat": classify_enemy(enemy),
                    "point": point,
                    "distance_fp": distance,
                    "line_of_sight": line_of_sight,
                    "shootable_target": bool(has_shootable_target and combat_target == enemy_id),
                }
            )
        if not candidates:
            return None
        return min(candidates, key=lambda item: (not item["shootable_target"], not item["line_of_sight"], item["distance_fp"]))

    def _enemy_sector_ids(self, state: Any) -> set[int]:
        sectors: set[int] = set()
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            pos = getattr(obj, "position", None)
            if obj is None or pos is None or int(getattr(obj, "health", 0) or 0) <= 0:
                continue
            sector_id = self.sector_for_point_fp(int(getattr(pos, "x_fp", 0)), int(getattr(pos, "y_fp", 0)))
            if sector_id is not None:
                sectors.add(int(sector_id))
        return sectors
