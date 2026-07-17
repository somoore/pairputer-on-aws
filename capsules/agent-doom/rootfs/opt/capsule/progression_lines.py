#!/usr/bin/env python3.11
"""Progression-line traversal for the Agent DOOM spatial planner.

Owns how the planner gets through the map's gated progression: use-line and
walk-trigger navigation, live/static exit lines, remembered progression
lines, door state (opening/confirmed-open/linked doors, door-leaf sectors,
final-door commits), retried-line exhaustion, key acquisition, tag-gate
switch hunting, and the E1M1 final-corridor special cases. Extracted verbatim
from planner.SpatialPlanner.

ProgressionLinesMixin is a mixin over SpatialPlanner state: every method runs
on the SpatialPlanner instance, reads/writes attributes initialized in
SpatialPlanner.__init__, and calls shared SpatialPlanner helpers via self. It
holds no state of its own. This module must not import planner at runtime.
"""

from __future__ import annotations

from typing import Any, Iterable

from planner_model import (
    DOOR_SPECIALS,
    EXIT_SPECIALS,
    FAR_STALE_NORMAL_DOOR_SPECIALS,
    FINAL_DOOR_COMMIT_ROUTE,
    FINAL_DOOR_HARD_BLOCK_UNITS,
    FP_UNIT,
    LIFT_WALK_SPECIALS,
    MapLineRuntime,
    PROGRESSION_SPECIALS,
    PlanAction,
    Point,
    PortalEdge,
    REMEMBERED_EXIT_PROBE_MAX_UNITS,
    REMEMBERED_PROGRESSION_PROBE_MAX_UNITS,
    USE_DISTANCE_FP,
    USE_TRIGGER_SPECIALS,
    WALK_TRIGGER_SPECIALS,
    _angle_delta,
    _bearing,
    _dist,
)


class ProgressionLinesMixin:
    """Progression-line traversal over SpatialPlanner state (see module docstring)."""

    def _line_objective_action(
        self,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
        *,
        exit_only: bool,
        state: Any | None = None,
    ) -> PlanAction | None:
        if not exit_only and state is not None:
            live_use = self._navigation_use_line_action(
                state,
                player,
                agent_pb2,
                door_memory,
                skill="open_use_line",
                detail={"skill": "planner_live_use_line"},
                max_distance_fp=384 * FP_UNIT,
                include_open=True,
            )
            if live_use is not None:
                return live_use
        candidates = []
        for line in self.lines:
            if exit_only:
                keep = line.exit or line.special in EXIT_SPECIALS or door_memory.state_for(line.id) == "exit"
            else:
                keep = line.exit or line.use_trigger or line.door
            if keep and not exit_only and state is not None and self._is_e1m1_final_corridor_side_door(state, line.id):
                keep = False
            if keep and not self._line_retry_exhausted(door_memory, line.id, special=line.special):
                candidates.append(line)
        candidates = [line for line in candidates if not door_memory.is_blocked(line.id) and door_memory.can_retry(line.id)]
        if not candidates:
            self._last_status = "no_use_line"
            return None
        candidates.sort(key=lambda line: _dist(player["point"], self._nearest_point_on_line(player["point"], line)))
        line = candidates[0]
        line_action = self._static_use_line_action(
            player,
            line,
            agent_pb2,
            door_memory,
            exit_only=exit_only,
            detail={"skill": "planner_use_line" if not exit_only else "static_exit_line"},
        )
        if line_action is not None:
            return line_action
        if exit_only:
            kwargs = {"state": state} if state is not None else {}
            sector_plan = self._sector_route_to_line_action(player, line, agent_pb2, door_memory, exit_only=exit_only, **kwargs)
            if sector_plan is not None:
                return sector_plan
        targets = self._nearest_nodes(line.midpoint, limit=12)
        route = self._route(player["point"], targets, door_memory)
        if route is None or not self._route_endpoint_near(route, line.midpoint, max_distance_fp=160 * FP_UNIT):
            kwargs = {"state": state} if state is not None else {}
            sector_plan = self._sector_route_to_line_action(player, line, agent_pb2, door_memory, exit_only=exit_only, **kwargs)
            if sector_plan is not None:
                return sector_plan
            self._last_status = "no_use_route"
            return None
        self._last_route_len = len(route.points)
        return self._route_action(
            player,
            route,
            agent_pb2,
            door_memory,
            skill="press_exit" if exit_only else "open_use_line",
            detail={"skill": "planner_route_to_use_line", "line": line.id, "route": len(route.points)},
        )

    def _navigation_use_line_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
        *,
        skill: str,
        detail: dict[str, Any],
        max_distance_fp: int | None = None,
        include_open: bool = False,
        target_point: Point | None = None,
        max_target_delta_degrees: float | None = None,
    ) -> PlanAction | None:
        navigation = getattr(state, "navigation", None)
        candidates = []
        for raw in getattr(navigation, "use_lines", []) or []:
            line_id = int(getattr(raw, "line_id", -1))
            special = int(getattr(raw, "special", 0))
            if line_id < 0 or door_memory.is_blocked(line_id):
                continue
            if self._is_e1m1_final_corridor_side_door(state, line_id):
                continue
            line_state = str(door_memory.state_for(line_id)) if hasattr(door_memory, "state_for") else "unknown"
            live_line_suppressed = getattr(door_memory, "live_line_suppressed", None)
            if callable(live_line_suppressed) and bool(live_line_suppressed(line_id)) and special not in EXIT_SPECIALS:
                continue
            can_retry = bool(door_memory.can_retry(line_id))
            if not can_retry and line_state != "congested":
                continue
            if self._line_retry_exhausted(door_memory, line_id, special=special):
                continue
            linked_open = self._door_any_open(door_memory, line_id)
            if linked_open and special not in EXIT_SPECIALS and not include_open:
                continue
            if special not in DOOR_SPECIALS and special not in USE_TRIGGER_SPECIALS and special not in EXIT_SPECIALS:
                continue
            point = self._line_point(raw)
            if point is None:
                continue
            distance = int(getattr(raw, "nearest_distance_fp", 0) or getattr(raw, "distance_fp", 0) or _dist(player["point"], point))
            if not can_retry and line_state == "congested" and distance > USE_DISTANCE_FP:
                continue
            if max_distance_fp is not None and distance > max_distance_fp:
                continue
            if target_point is not None:
                target_bearing = _bearing(player["point"], target_point)
                point_bearing = _bearing(player["point"], point)
                if max_target_delta_degrees is not None and abs(_angle_delta(point_bearing, target_bearing)) > float(max_target_delta_degrees):
                    continue
                to_line_x = float(point.x - player["point"].x)
                to_line_y = float(point.y - player["point"].y)
                to_target_x = float(target_point.x - player["point"].x)
                to_target_y = float(target_point.y - player["point"].y)
                if to_line_x * to_target_x + to_line_y * to_target_y <= 0:
                    continue
            candidates.append((distance, line_id, special, point))
        if not candidates:
            return None
        distance, line_id, special, point = min(candidates, key=lambda item: item[0])
        delta = _angle_delta(_bearing(player["point"], point), player["angle"])
        merged = dict(detail)
        merged.update({"line": line_id, "special": special, "dist": int(distance / FP_UNIT)})
        target = point
        linked_open = self._door_any_open(door_memory, line_id)
        if include_open and linked_open and special not in EXIT_SPECIALS:
            raw_cls = getattr(agent_pb2, "RawTiccmd", None)
            player_state = getattr(state, "player", None)
            health = int(getattr(player_state, "health", 100) or 100)
            final_corridor = detail.get("skill") == "final_corridor_use_line"
            static_line = self._line_by_id(line_id)
            target = self._line_use_target(player["point"], static_line) if static_line is not None else target
            delta = _angle_delta(_bearing(player["point"], target), player["angle"])
            merged["target"] = "through_opening" if static_line is not None else "line"
            if final_corridor and raw_cls is not None and abs(delta) <= 50.0:
                return PlanAction(
                    skill=skill,
                    action=agent_pb2.PlayerAction(
                        duration_tics=10,
                        raw=raw_cls(forward_move=62, angle_turn=self._raw_steer_turn_units(delta)),
                    ),
                    door_line_id=line_id,
                    detail={**merged, "action": "final_corridor_sprint_opening", "hp": health, "turn": round(delta, 1)},
                )
            if abs(delta) > 28.0:
                return self._turn(player, delta, agent_pb2, skill, merged, door_line_id=line_id)
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=42, duration_tics=8),
                door_line_id=line_id,
                detail={**merged, "action": "follow_opening"},
            )
        navigation = getattr(state, "navigation", None)
        front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
        blocked_ahead = (front_distance and front_distance <= 96 * FP_UNIT) or not bool(getattr(navigation, "forward_open", False))
        if distance > 160 * FP_UNIT and blocked_ahead:
            local_targets = self._nearest_nodes(target, limit=10)
            local_route = self._route(player["point"], local_targets, door_memory)
            if local_route is not None and self._route_endpoint_near(local_route, target, max_distance_fp=128 * FP_UNIT):
                plan = self._route_action(
                    player,
                    local_route,
                    agent_pb2,
                    door_memory,
                    skill=skill,
                    detail={
                        **merged,
                        "skill": "route_to_live_use_line",
                        "live_line": line_id,
                        "route": len(local_route.points),
                    },
                )
                if plan is not None:
                    return plan
            record_route_contact = getattr(door_memory, "record_route_contact", None)
            if callable(record_route_contact):
                record_route_contact(line_id)
            return None
        if distance <= USE_DISTANCE_FP:
            if abs(delta) <= 20.0:
                door_memory.record_attempt(line_id, status="planner_live_use")
                merged["action"] = "use"
                return PlanAction(
                    skill=skill,
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
                    door_line_id=line_id,
                    detail=merged,
                )
            return self._turn(player, delta, agent_pb2, skill, merged, door_line_id=line_id)
        return self._turn_or_forward(player, point, agent_pb2, skill=skill, detail=merged, door_line_id=line_id)

    def wedge_door_use_action(self, state: Any, agent_pb2: Any, door_memory: Any, *, wedge_steps: int = 0) -> PlanAction | None:
        """Wedged-against-a-door rescue: fire ACTION_USE at the nearest close door
        line REGARDLESS of retry/congestion/linked-open gating. A body pinned against a
        closed door repeatedly ""bumps"" it without ever pressing USE — the normal
        _navigation_use_line_action suppresses lines it thinks it already tried, so a
        wedge silently walks into the door forever. Here the physical wedge IS the
        evidence the door is still shut, so ignore memory: turn to face the nearest door
        line within ~1.5x use range and press USE. Returns None if no door is near enough.
        Not gated by objective/rules on purpose — a door blocks every goal.

        Capped at WEDGE_USE_MAX presses per line so a door that won't open (locked,
        key-gated, or a USE-inert line) is ABANDONED — control falls back to hard_unstick,
        which bull-rushes AROUND it. Without the cap this jackhammered USE forever on a
        dead door (measured: 246 frozen steps, 276 useless presses)."""
        WEDGE_USE_MAX = 5
        player = self._player(state)
        if player is None:
            return None
        navigation = getattr(state, "navigation", None)
        reach = int(USE_DISTANCE_FP * 1.5)
        best = None
        for raw in getattr(navigation, "use_lines", []) or []:
            line_id = int(getattr(raw, "line_id", -1))
            special = int(getattr(raw, "special", 0))
            if line_id < 0:
                continue
            # requires_key / requires_switch really can't open by USE — don't fake it.
            if door_memory.is_blocked(line_id):
                continue
            if special not in DOOR_SPECIALS and special not in USE_TRIGGER_SPECIALS and special not in EXIT_SPECIALS:
                continue
            # Already opened once, or we've hammered it past the cap without it opening:
            # this door is a dead end for USE — let hard_unstick route around it.
            # EXCEPT on a persistent wedge: DOOM doors auto-close after ~4s, so an "opened"
            # memory goes stale constantly. Still physically wedged after 10 steps right next
            # to an "open" door = the door re-closed; press USE anyway (a truly open door in
            # front of us would have let us through).
            if door_memory.is_open(line_id) and wedge_steps < 10:
                continue
            if self._wedge_use_counts.get(line_id, 0) >= WEDGE_USE_MAX:
                continue
            point = self._line_point(raw)
            if point is None:
                continue
            distance = int(getattr(raw, "nearest_distance_fp", 0) or getattr(raw, "distance_fp", 0) or _dist(player["point"], point))
            if distance > reach:
                continue
            if best is None or distance < best[0]:
                best = (distance, line_id, special, point)
        if best is None:
            return None
        distance, line_id, special, point = best
        delta = _angle_delta(_bearing(player["point"], point), player["angle"])
        detail = {"skill": "wedge_door_use", "line": line_id, "special": special, "dist": int(distance / FP_UNIT), "turn": round(delta, 1)}
        # Not yet facing the door: turn toward it (next wedge step presses USE).
        if abs(delta) > 30.0:
            return self._turn(player, delta, agent_pb2, "recover_stuck", detail, door_line_id=line_id)
        self._wedge_use_counts[line_id] = self._wedge_use_counts.get(line_id, 0) + 1
        door_memory.record_attempt(line_id, status="wedge_use")
        detail["action"] = "use"
        detail["tries"] = self._wedge_use_counts[line_id]
        return PlanAction(
            skill="recover_stuck",
            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
            door_line_id=line_id,
            detail=detail,
        )

    def _navigation_walk_trigger_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
        *,
        skill: str,
        detail: dict[str, Any],
        target_point: Point | None = None,
        max_target_delta_degrees: float | None = None,
        max_distance_fp: int = 768 * FP_UNIT,
    ) -> PlanAction | None:
        navigation = getattr(state, "navigation", None)
        candidates = []
        raw_lines = list(getattr(navigation, "use_lines", []) or [])
        waypoint = getattr(navigation, "route_waypoint", None)
        waypoint_line = getattr(waypoint, "line", None)
        if waypoint_line is not None and (
            bool(getattr(waypoint, "walk_trigger", False)) or int(getattr(waypoint_line, "special", 0) or 0) in WALK_TRIGGER_SPECIALS
        ):
            raw_lines.append(waypoint_line)
        for raw in raw_lines:
            line_id = int(getattr(raw, "line_id", -1))
            special = int(getattr(raw, "special", 0) or 0)
            if line_id < 0 or special not in WALK_TRIGGER_SPECIALS:
                continue
            if door_memory.is_blocked(line_id) or door_memory.is_open(line_id) or not door_memory.can_retry(line_id):
                continue
            point = self._line_point(raw)
            if point is None:
                point = self._line_midpoint(raw)
            if point is None:
                continue
            static_line = self._line_by_id(line_id)
            cross_target = self._line_use_target(player["point"], static_line) if static_line is not None else point
            distance = int(getattr(raw, "nearest_distance_fp", 0) or getattr(raw, "distance_fp", 0) or _dist(player["point"], point))
            if distance > int(max_distance_fp):
                continue
            if target_point is not None:
                target_bearing = _bearing(player["point"], target_point)
                point_bearing = _bearing(player["point"], point)
                if max_target_delta_degrees is not None and abs(_angle_delta(point_bearing, target_bearing)) > float(max_target_delta_degrees):
                    continue
                to_line_x = float(point.x - player["point"].x)
                to_line_y = float(point.y - player["point"].y)
                to_target_x = float(target_point.x - player["point"].x)
                to_target_y = float(target_point.y - player["point"].y)
                if to_line_x * to_target_x + to_line_y * to_target_y <= 0:
                    continue
            route_penalty_for = getattr(door_memory, "route_penalty_for", None)
            penalty = float(route_penalty_for(line_id)) if callable(route_penalty_for) else 0.0
            cost = (float(distance) / FP_UNIT) + penalty
            candidates.append((cost, distance, line_id, special, point, cross_target))
        if not candidates:
            return None
        _cost, distance, line_id, special, point, cross_target = min(candidates, key=lambda item: item[0])
        merged = dict(detail)
        merged.update({"line": line_id, "special": special, "dist": int(distance / FP_UNIT)})
        if distance <= 96 * FP_UNIT:
            # Walk-over triggers fire only after crossing the line. Aim at the far side instead
            # of the nearest point on the linedef; otherwise E1M2 special-88 lifts can be orbited
            # forever without actually triggering the lift.
            delta = _angle_delta(_bearing(player["point"], cross_target), player["angle"])
            if abs(delta) > 18.0:
                return self._turn(player, delta, agent_pb2, skill, merged, door_line_id=line_id)
            observe_line = getattr(door_memory, "observe_line", None)
            if callable(observe_line):
                observe_line(
                    line_id,
                    special=special,
                    tag=int(getattr(static_line, "tag", getattr(raw, "tag", 0)) or 0),
                )
            record_status = getattr(door_memory, "record_status", None)
            if callable(record_status):
                record_status(line_id, status="walk_trigger_cross_attempt")
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=58, duration_tics=12),
                door_line_id=line_id,
                detail={**merged, "action": "cross_walk_trigger", "tag": int(getattr(static_line, "tag", 0) or 0) if static_line is not None else int(getattr(raw, "tag", 0) or 0), "mt": [int(cross_target.x), int(cross_target.y)]},
            )
        return self._turn_or_forward(
            player,
            cross_target,
            agent_pb2,
            skill=skill,
            detail={**merged, "action": "approach_walk_trigger"},
            door_line_id=line_id,
        )

    def _live_exit_line_action(self, state: Any, player: dict[str, Any], agent_pb2: Any, door_memory: Any) -> PlanAction | None:
        navigation = getattr(state, "navigation", None)
        candidates = []
        for raw in getattr(navigation, "use_lines", []) or []:
            line_id = int(getattr(raw, "line_id", -1))
            special = int(getattr(raw, "special", 0) or 0)
            if line_id < 0 or special not in EXIT_SPECIALS:
                continue
            if door_memory.is_blocked(line_id) or not door_memory.can_retry(line_id):
                continue
            point = self._line_point(raw)
            if point is None:
                continue
            distance = int(getattr(raw, "nearest_distance_fp", 0) or getattr(raw, "distance_fp", 0) or _dist(player["point"], point))
            candidates.append((distance, line_id, special, point))
        if not candidates:
            return None
        distance, line_id, special, point = min(candidates, key=lambda item: item[0])
        static_line = self._line_by_id(line_id)
        if static_line is not None:
            target_sectors = {sector for sector in (static_line.front_sector, static_line.back_sector) if sector >= 0}
            containing = self._sector_containing_point(static_line.midpoint)
            if containing is not None:
                target_sectors.add(containing)
            current_sector = getattr(getattr(navigation, "current_sector", None), "sector_id", None)
            player_sector = int(current_sector) if current_sector is not None else self.sector_for_point_fp(player["point"].x, player["point"].y)
            if target_sectors and player_sector not in target_sectors:
                return None
        if distance > 384 * FP_UNIT:
            return None
        distance_units = int(distance / FP_UNIT)
        aim_point = self._line_use_target(player["point"], static_line) if static_line is not None else point
        delta = _angle_delta(_bearing(player["point"], aim_point), player["angle"])
        detail = {
            "skill": "live_exit_line",
            "line": line_id,
            "special": special,
            "dist": distance_units,
            "turn": round(delta, 1),
        }
        if distance <= USE_DISTANCE_FP:
            last_status_for = getattr(door_memory, "last_status_for", None)
            if callable(last_status_for) and str(last_status_for(line_id)) == "live_exit_use":
                record_status = getattr(door_memory, "record_status", None)
                if callable(record_status):
                    record_status(line_id, status="live_exit_release")
                raw_cls = getattr(agent_pb2, "RawTiccmd", None)
                action = (
                    agent_pb2.PlayerAction(duration_tics=2, raw=raw_cls())
                    if raw_cls is not None
                    else agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=0, duration_tics=2)
                )
                return PlanAction(
                    skill="press_exit",
                    action=action,
                    door_line_id=line_id,
                    detail={**detail, "action": "release_use"},
                )
            if static_line is not None:
                projection = self._line_projection_fraction(player["point"], static_line)
                midpoint_distance = _dist(player["point"], static_line.midpoint)
                if (projection < 0.18 or projection > 0.82) and midpoint_distance > 24 * FP_UNIT:
                    return self._turn_or_forward(
                        player,
                        static_line.midpoint,
                        agent_pb2,
                        skill="press_exit",
                        detail={**detail, "target": "center_exit_line"},
                        door_line_id=line_id,
                    )
            if abs(delta) > 16.0:
                return self._turn(player, delta, agent_pb2, "press_exit", detail, door_line_id=line_id)
            attempts = 0
            attempts_for = getattr(door_memory, "attempts_for", None)
            if callable(attempts_for):
                attempts = int(attempts_for(line_id))
            door_memory.record_attempt(line_id, status="live_exit_use")
            return PlanAction(
                skill="press_exit",
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=1),
                door_line_id=line_id,
                detail={**detail, "action": "use", "attempts": attempts},
            )
        if abs(delta) <= 36.0:
            return PlanAction(
                skill="press_exit",
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=46, duration_tics=10),
                door_line_id=line_id,
                detail={**detail, "action": "close_forward"},
            )
        if distance <= 256 * FP_UNIT and 55.0 <= abs(delta) <= 135.0 and self._side_probe_open(state, left=delta > 0):
            action_type = agent_pb2.ACTION_STRAFE_LEFT if delta > 0 else agent_pb2.ACTION_STRAFE_RIGHT
            return PlanAction(
                skill="press_exit",
                action=agent_pb2.PlayerAction(action=action_type, amount=34, duration_tics=8),
                door_line_id=line_id,
                detail={**detail, "action": "close_strafe"},
            )
        if distance <= 256 * FP_UNIT and abs(delta) >= 140.0 and bool(getattr(navigation, "back_open", False)):
            return PlanAction(
                skill="press_exit",
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=32, duration_tics=8),
                door_line_id=line_id,
                detail={**detail, "action": "close_reverse"},
            )
        return self._turn(player, delta, agent_pb2, "press_exit", detail, door_line_id=line_id)

    def _static_use_line_action(
        self,
        player: dict[str, Any],
        line: MapLineRuntime,
        agent_pb2: Any,
        door_memory: Any,
        *,
        exit_only: bool,
        detail: dict[str, Any],
        max_distance_fp: float | int | None = None,
    ) -> PlanAction | None:
        line_target = self._nearest_point_on_line(player["point"], line)
        distance = _dist(player["point"], line_target)
        limit = float(max_distance_fp if max_distance_fp is not None else USE_DISTANCE_FP)
        if distance > limit:
            return None
        if not exit_only and self._door_any_open(door_memory, line.id):
            return None
        distance_units = int(distance / FP_UNIT)
        delta = _angle_delta(_bearing(player["point"], line_target), player["angle"])
        skill = "press_exit" if exit_only else "open_use_line"
        merged = {
            **detail,
            "line": line.id,
            "special": line.special,
            "tag": line.tag,
            "dist": distance_units,
            "turn": round(delta, 1),
        }
        if line.use_trigger or line.door or line.exit:
            use_tolerance = 18.0 if exit_only else 18.0
            if exit_only and distance <= 96 * FP_UNIT:
                use_tolerance = 26.0
            if abs(delta) <= use_tolerance:
                door_memory.record_attempt(line.id, status="static_exit_use" if exit_only else "planner_use")
                return PlanAction(
                    skill=skill,
                    action=agent_pb2.PlayerAction(
                        action=agent_pb2.ACTION_USE,
                        amount=1,
                        duration_tics=1 if exit_only else 4,
                    ),
                    door_line_id=line.id,
                    detail={**merged, "action": "use"},
                )
        elif line.walk_trigger:
            # Walk-over triggers (e.g. WR lift lines) fire by CROSSING the line, not by USE.
            # Drive through to a point past the far side; turning in place would loop forever.
            through = self._line_use_target(player["point"], line)
            return self._turn_or_forward(
                player,
                through,
                agent_pb2,
                skill=skill,
                detail={**merged, "action": "cross_walk_trigger"},
                door_line_id=line.id,
            )
        turn = self._turn(player, delta, agent_pb2, skill, {**merged, "action": "turn"}, door_line_id=line.id)
        if exit_only:
            turn.action.amount = max(int(getattr(turn.action, "amount", 0) or 0), 48)
            turn.action.duration_tics = max(int(getattr(turn.action, "duration_tics", 1) or 1), 4)
        return turn

    def _last_chance_live_use_line_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
        *,
        allow_suppressed: bool = False,
        repair_only: bool = False,
        max_distance_fp: int = 192 * FP_UNIT,
    ) -> PlanAction | None:
        navigation = getattr(state, "navigation", None)
        candidates = []
        for raw in getattr(navigation, "use_lines", []) or []:
            line_id = int(getattr(raw, "line_id", -1))
            special = int(getattr(raw, "special", 0) or 0)
            if line_id < 0:
                continue
            if special not in DOOR_SPECIALS and special not in USE_TRIGGER_SPECIALS and special not in EXIT_SPECIALS:
                continue
            if self._is_e1m1_final_corridor_side_door(state, line_id):
                continue
            if repair_only and not self._line_retry_exhausted(door_memory, line_id, special=special):
                continue
            state_name = str(door_memory.state_for(line_id)) if hasattr(door_memory, "state_for") else "unknown"
            if state_name in {"requires_key", "requires_switch"}:
                continue
            live_line_suppressed = getattr(door_memory, "live_line_suppressed", None)
            suppressed = bool(callable(live_line_suppressed) and bool(live_line_suppressed(line_id)) and special not in EXIT_SPECIALS)
            if suppressed and not allow_suppressed:
                continue
            if door_memory.is_blocked(line_id) and special not in DOOR_SPECIALS:
                continue
            point = self._line_point(raw)
            if point is None:
                point = self._line_midpoint(raw)
            if point is None:
                continue
            distance = int(getattr(raw, "nearest_distance_fp", 0) or getattr(raw, "distance_fp", 0) or _dist(player["point"], point))
            if distance > max_distance_fp:
                continue
            status = str(door_memory.last_status_for(line_id)) if hasattr(door_memory, "last_status_for") else ""
            attempts_for = getattr(door_memory, "attempts_for", None)
            attempts = int(attempts_for(line_id)) if callable(attempts_for) else 0
            force_follow_failures_for = getattr(door_memory, "force_follow_failures_for", None)
            force_follow_failures = int(force_follow_failures_for(line_id)) if callable(force_follow_failures_for) else 0
            can_retry = getattr(door_memory, "can_retry", None)
            retryable = bool(can_retry(line_id)) if callable(can_retry) else True
            if (
                allow_suppressed
                and state_name == "congested"
                and not retryable
                and special in FAR_STALE_NORMAL_DOOR_SPECIALS
                and distance > 192 * FP_UNIT
            ):
                continue
            if suppressed and force_follow_failures >= 3 and distance <= 192 * FP_UNIT:
                continue
            rank = 0 if distance <= USE_DISTANCE_FP else 1
            if state_name in {"opening", "opened"}:
                rank -= 1
            if suppressed:
                rank += 3
            candidates.append((rank, distance, line_id, special, point, state_name, status, attempts, force_follow_failures))
        if not candidates:
            return None
        _rank, distance, line_id, special, point, state_name, status, attempts, force_follow_failures = min(
            candidates,
            key=lambda item: (item[0], item[1]),
        )
        static_line = self._line_by_id(line_id)
        follow_target = self._line_use_target(player["point"], static_line) if static_line is not None else point
        use_delta = _angle_delta(_bearing(player["point"], point), player["angle"])
        follow_delta = _angle_delta(_bearing(player["point"], follow_target), player["angle"])
        detail = {
            "skill": "last_chance_live_use_line",
            "line": line_id,
            "special": special,
            "dist": int(distance / FP_UNIT),
            "state": state_name,
            "status": status,
            "attempts": attempts,
            "force_follow_failures": force_follow_failures,
        }
        if suppressed:
            detail["resync"] = "suppressed_live_use"
        should_reuse = (
            state_name in {"closed", "congested"}
            or status in {"stale_open_blocked", "assumed_opening"}
        ) and attempts <= 3
        if distance <= USE_DISTANCE_FP and should_reuse:
            if abs(use_delta) <= 24.0:
                door_memory.record_attempt(line_id, status="last_chance_live_use")
                return PlanAction(
                    skill="open_use_line",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
                    door_line_id=line_id,
                    detail={**detail, "action": "use", "turn": round(use_delta, 1)},
                )
            return self._turn(player, use_delta, agent_pb2, "open_use_line", detail, door_line_id=line_id)
        if abs(follow_delta) > 36.0:
            return self._turn(player, follow_delta, agent_pb2, "open_use_line", detail, door_line_id=line_id)
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is not None:
            return PlanAction(
                skill="open_use_line",
                action=agent_pb2.PlayerAction(
                    duration_tics=10,
                    raw=raw_cls(forward_move=56, angle_turn=self._raw_steer_turn_units(follow_delta)),
                ),
                door_line_id=line_id,
                detail={**detail, "action": "force_follow_live_use_line", "turn": round(follow_delta, 1)},
            )
        return PlanAction(
            skill="open_use_line",
            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=48, duration_tics=10),
            door_line_id=line_id,
            detail={**detail, "action": "force_follow_live_use_line", "turn": round(follow_delta, 1)},
        )

    def _remembered_progression_line_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
        *,
        prefer_exit: bool,
    ) -> PlanAction | None:
        candidate_lines = getattr(door_memory, "candidate_lines", None)
        if not callable(candidate_lines):
            return None
        candidates: list[tuple[int, float, MapLineRuntime, dict[str, Any], bool]] = []
        for record in candidate_lines():
            try:
                line_id = int(record.get("line_id", -1))
            except Exception:
                continue
            line = self._line_by_id(line_id)
            if line is None:
                continue
            if self._is_e1m1_final_corridor_side_door(state, line_id):
                continue
            state_name = str(record.get("state") or door_memory.state_for(line_id))
            special = int(record.get("special") or line.special or 0)
            status = str(record.get("last_status") or "")
            if self._remembered_walk_lift_trigger_suppressed(line, state_name, special, status):
                continue
            is_exit = bool(line.exit or special in EXIT_SPECIALS or state_name == "exit")
            is_progression = bool(
                is_exit
                or line.door
                or line.use_trigger
                or line.walk_trigger
                or special in PROGRESSION_SPECIALS
                or state_name in {"opening", "opened", "closed", "congested"}
            )
            if not is_progression:
                continue
            if state_name in {"requires_key", "requires_switch"}:
                continue
            if door_memory.is_blocked(line_id) and not is_exit:
                continue
            force_follow_failures = int(record.get("force_follow_failures") or 0)
            if not is_exit and force_follow_failures >= 3:
                continue
            if self._line_retry_exhausted(door_memory, line_id, special=special) and not is_exit:
                continue
            if not door_memory.can_retry(line_id) and state_name not in {"opening", "opened", "congested", "exit"}:
                continue
            point = self._nearest_point_on_line(player["point"], line)
            distance = _dist(player["point"], point)
            if state_name == "congested" and not is_exit and distance > 192 * FP_UNIT:
                continue
            priority = self._remembered_line_priority(line, state_name, special, prefer_exit=prefer_exit)
            if priority is None:
                continue
            candidates.append((priority, distance, line, dict(record), is_exit))
        if not candidates:
            return None

        candidates.sort(key=lambda item: (item[0], item[1], int(item[2].id)))
        fallback: tuple[int, float, MapLineRuntime, dict[str, Any], bool] | None = None
        for priority, distance, line, record, is_exit in candidates[:8]:
            exit_only = bool(is_exit)
            skill = "press_exit" if exit_only else "open_use_line"
            detail = {
                "skill": "remembered_exit_line" if exit_only else "remembered_progression_line",
                "line": line.id,
                "special": line.special,
                "tag": line.tag,
                "state": str(record.get("state") or "unknown"),
                "status": str(record.get("last_status") or ""),
                "dist": int(distance / FP_UNIT),
                "priority": priority,
            }
            close = self._static_use_line_action(
                player,
                line,
                agent_pb2,
                door_memory,
                exit_only=exit_only,
                detail=detail,
                max_distance_fp=128 * FP_UNIT if exit_only else USE_DISTANCE_FP,
            )
            if close is not None:
                return close

            sector_plan = self._sector_route_to_line_action(
                player,
                line,
                agent_pb2,
                door_memory,
                exit_only=exit_only,
                state=state,
            )
            if sector_plan is not None:
                sector_plan.detail.setdefault("memory", "remembered_line")
                return sector_plan

            targets = self._nearest_nodes(line.midpoint, limit=12)
            route = self._route(player["point"], targets, door_memory)
            if route is not None and self._route_endpoint_near(route, line.midpoint, max_distance_fp=192 * FP_UNIT):
                self._last_route_len = len(route.points)
                return self._route_action(
                    player,
                    route,
                    agent_pb2,
                    door_memory,
                    skill=skill,
                    detail={**detail, "skill": "remembered_route_to_exit_line" if exit_only else "remembered_route_to_progression_line", "route": len(route.points)},
                )

            if fallback is None and not (
                (is_exit and distance > REMEMBERED_EXIT_PROBE_MAX_UNITS * FP_UNIT)
                or (not is_exit and distance > REMEMBERED_PROGRESSION_PROBE_MAX_UNITS * FP_UNIT)
            ):
                fallback = (priority, distance, line, record, is_exit)

        if fallback is None:
            return None
        priority, distance, line, record, is_exit = fallback
        target = self._line_use_target(player["point"], line)
        self._last_status = "remembered_line_probe"
        skill = "press_exit" if is_exit else "route_progression"
        if not is_exit:
            escape = self._probe_escape_action(
                state,
                player,
                agent_pb2,
                detail={
                    "skill": "remembered_probe_escape",
                    "line": line.id,
                    "special": line.special,
                    "tag": line.tag,
                    "state": str(record.get("state") or "unknown"),
                    "status": str(record.get("last_status") or ""),
                    "dist": int(distance / FP_UNIT),
                    "priority": priority,
                },
            )
            if escape is not None:
                return escape
        return self._turn_or_forward(
            player,
            target,
            agent_pb2,
            skill=skill,
            detail={
                "skill": "remembered_exit_probe" if is_exit else "remembered_progression_probe",
                "line": line.id,
                "special": line.special,
                "tag": line.tag,
                "state": str(record.get("state") or "unknown"),
                "status": str(record.get("last_status") or ""),
                "dist": int(distance / FP_UNIT),
                "priority": priority,
            },
            door_line_id=line.id,
        )

    def _remembered_line_priority(
        self,
        line: MapLineRuntime,
        state_name: str,
        special: int,
        *,
        prefer_exit: bool,
    ) -> int | None:
        is_exit = bool(line.exit or special in EXIT_SPECIALS or state_name == "exit")
        if is_exit:
            return 0 if prefer_exit else 1
        if not (line.door or line.use_trigger or line.walk_trigger or special in PROGRESSION_SPECIALS):
            return None
        if state_name in {"opened", "opening"}:
            return 1 if prefer_exit else 0
        if line.walk_trigger or special in WALK_TRIGGER_SPECIALS:
            return 2
        if state_name in {"closed", "unknown"}:
            return 3
        if state_name == "congested":
            return 4
        return 5

    def _remembered_walk_lift_trigger_suppressed(self, line: MapLineRuntime, state_name: str, special: int, status: str) -> bool:
        if not line.walk_trigger and special not in LIFT_WALK_SPECIALS:
            return False
        if int(line.tag or 0) <= 0:
            return False
        return state_name in {"closed", "unknown", "opening", "opened", "congested"} or status == "walk_lift_triggered"

    def _blocked_use_line_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
        *,
        skill: str,
        detail: dict[str, Any],
    ) -> PlanAction | None:
        navigation = getattr(state, "navigation", None)
        if navigation is None:
            return None
        front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
        blocked_ahead = (front_distance and front_distance <= 48 * FP_UNIT) or not bool(getattr(navigation, "forward_open", False))
        if not blocked_ahead:
            return None
        return self._navigation_use_line_action(
            state,
            player,
            agent_pb2,
            door_memory,
            skill=skill,
            detail=detail,
            max_distance_fp=USE_DISTANCE_FP,
            include_open=True,
        )

    def _route_local_door_action(
        self,
        state: Any,
        player: dict[str, Any],
        route_target: Point,
        agent_pb2: Any,
        door_memory: Any,
        *,
        base_detail: dict[str, Any],
    ) -> PlanAction | None:
        navigation = getattr(state, "navigation", None)
        candidates = []
        target_bearing = _bearing(player["point"], route_target)
        for raw in getattr(navigation, "use_lines", []) or []:
            line_id = int(getattr(raw, "line_id", -1))
            special = int(getattr(raw, "special", 0) or 0)
            if line_id < 0 or special in EXIT_SPECIALS:
                continue
            if special not in DOOR_SPECIALS and special not in USE_TRIGGER_SPECIALS:
                continue
            if door_memory.is_blocked(line_id) or not door_memory.can_retry(line_id):
                continue
            point = self._line_midpoint(raw)
            if point is None:
                point = self._line_point(raw)
            if point is None:
                continue
            static_line = self._line_by_id(line_id)
            if static_line is not None and not self._line_crosses_segment(static_line, player["point"], route_target):
                continue
            nearest_distance = int(getattr(raw, "nearest_distance_fp", 0) or getattr(raw, "distance_fp", 0) or _dist(player["point"], point))
            if nearest_distance > 192 * FP_UNIT:
                continue
            to_door_x = point.x - player["point"].x
            to_door_y = point.y - player["point"].y
            to_target_x = route_target.x - player["point"].x
            to_target_y = route_target.y - player["point"].y
            if to_door_x * to_target_x + to_door_y * to_target_y <= 0:
                continue
            bearing_delta = abs(_angle_delta(_bearing(player["point"], point), target_bearing))
            if bearing_delta > 75.0:
                continue
            state_rank = 1 if door_memory.is_open(line_id) else 0
            candidates.append((state_rank, nearest_distance, bearing_delta, line_id, special, point))
        if not candidates:
            return None
        _state_rank, nearest_distance, _bearing_delta, line_id, special, point = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        line_state = door_memory.state_for(line_id)
        detail = {
            **base_detail,
            "skill": "route_local_door",
            "line": line_id,
            "special": special,
            "door": line_state,
            "dist": int(nearest_distance / FP_UNIT),
        }
        delta = _angle_delta(_bearing(player["point"], point), player["angle"])
        if not door_memory.is_open(line_id) and nearest_distance <= USE_DISTANCE_FP:
            if abs(delta) <= 22.0:
                door_memory.record_attempt(line_id, status="route_local_door_use")
                return PlanAction(
                    skill="open_use_line",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
                    door_line_id=line_id,
                    detail={**detail, "action": "use"},
                )
            return self._turn(player, delta, agent_pb2, "open_use_line", detail, door_line_id=line_id)
        if abs(delta) > 16.0:
            return self._turn(player, delta, agent_pb2, "route_progression", detail, door_line_id=line_id)
        return PlanAction(
            skill="route_progression",
            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=46, duration_tics=14),
            door_line_id=line_id,
            detail={**detail, "action": "cross"},
        )

    def _door_opening(self, door_memory: Any, line_id: int | None) -> bool:
        if line_id is None:
            return False
        checker = getattr(door_memory, "is_opening", None)
        if callable(checker):
            if bool(checker(line_id)):
                return True
        if str(door_memory.state_for(line_id)) == "opening":
            return True
        return self._linked_door_state(door_memory, line_id, {"opening"})

    def _door_confirmed_open(self, door_memory: Any, line_id: int | None) -> bool:
        if line_id is None:
            return False
        checker = getattr(door_memory, "is_confirmed_open", None)
        if callable(checker):
            if bool(checker(line_id)):
                return True
        if str(door_memory.state_for(line_id)) == "opened":
            return True
        return self._linked_door_state(door_memory, line_id, {"opened"})

    def _door_any_open(self, door_memory: Any, line_id: int | None) -> bool:
        if line_id is None:
            return False
        return self._door_confirmed_open(door_memory, line_id) or self._door_opening(door_memory, line_id)

    def _door_direct_any_open(self, door_memory: Any, line_id: int | None) -> bool:
        if line_id is None:
            return False
        return str(door_memory.state_for(line_id)) in {"opening", "opened"}

    def _linked_open_face_blocked_action(
        self,
        state: Any | None,
        player: dict[str, Any],
        edge: PortalEdge,
        agent_pb2: Any,
        door_memory: Any,
        detail: dict[str, Any],
    ) -> PlanAction | None:
        if state is None or not edge.use_line:
            return None
        if self._door_direct_any_open(door_memory, edge.line_id):
            return None
        if not self._linked_door_state(door_memory, edge.line_id, {"opening", "opened"}):
            return None
        if not self._route_blocked_now(state):
            return None
        can_retry = getattr(door_memory, "can_retry", None)
        if callable(can_retry) and not bool(can_retry(edge.line_id)):
            return None
        line = self._line_by_id(edge.line_id)
        use_point = self._nearest_point_on_line(player["point"], line) if line is not None else edge.point
        distance = _dist(player["point"], use_point)
        if distance > USE_DISTANCE_FP:
            return None
        delta = _angle_delta(_bearing(player["point"], use_point), player["angle"])
        merged = {
            **detail,
            "action": "reuse_linked_blocked_face",
            "line": edge.line_id,
            "special": edge.special,
            "dist": int(distance / FP_UNIT),
            "turn": round(delta, 1),
        }
        if abs(delta) > 24.0:
            return self._turn(player, delta, agent_pb2, "open_use_line", merged, door_line_id=edge.line_id)
        door_memory.record_attempt(edge.line_id, status="linked_face_reuse")
        return PlanAction(
            skill="open_use_line",
            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
            door_line_id=edge.line_id,
            detail=merged,
        )

    def _linked_door_state(self, door_memory: Any, line_id: int | None, states: set[str]) -> bool:
        line = self._line_by_id(line_id)
        if line is None or not (line.door or line.use_trigger):
            return False
        for sector_id in (line.front_sector, line.back_sector):
            if sector_id < 0 or not self._sector_looks_like_door_leaf(sector_id, special=line.special):
                continue
            for other in self._door_lines_for_sector(sector_id, special=line.special):
                if other.id != line.id and str(door_memory.state_for(other.id)) in states:
                    return True
        return False

    def _sector_looks_like_door_leaf(self, sector_id: int, *, special: int) -> bool:
        incident = [
            line for line in self.lines
            if line.front_sector == sector_id or line.back_sector == sector_id
        ]
        traversable = [
            line for line in incident
            if line.passable or line.door or line.use_trigger or line.walk_trigger or line.exit
        ]
        door_lines = [
            line for line in traversable
            if (line.door or line.use_trigger) and line.special == special
        ]
        return 2 <= len(door_lines) <= 4 and len(traversable) <= 4

    def _door_lines_for_sector(self, sector_id: int, *, special: int) -> list[MapLineRuntime]:
        return [
            line for line in self.lines
            if (line.front_sector == sector_id or line.back_sector == sector_id)
            and (line.door or line.use_trigger)
            and line.special == special
        ]

    def _final_door_route_commit(self, route_remaining: int, front_distance: int) -> bool:
        hard_blocked = bool(front_distance) and front_distance <= FINAL_DOOR_HARD_BLOCK_UNITS * FP_UNIT
        return route_remaining <= FINAL_DOOR_COMMIT_ROUTE and not hard_blocked

    def _final_door_commit_action(
        self,
        player: dict[str, Any],
        target: Point,
        agent_pb2: Any,
        *,
        skill: str,
        detail: dict[str, Any],
        door_line_id: int | None,
        action_name: str,
        forward_move: int = 58,
        duration_tics: int = 10,
    ) -> PlanAction:
        delta = _angle_delta(_bearing(player["point"], target), player["angle"])
        merged = {**detail, "action": action_name, "turn": round(delta, 1)}
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is not None:
            side_move = 0
            if abs(delta) >= 45.0:
                side_move = 30 if delta > 0 else -30
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(
                    duration_tics=duration_tics,
                    raw=raw_cls(
                        forward_move=forward_move,
                        side_move=side_move,
                        angle_turn=self._raw_steer_turn_units(delta),
                    ),
                ),
                door_line_id=door_line_id,
                detail=merged,
            )
        if abs(delta) > 28.0:
            return self._turn(player, delta, agent_pb2, skill, merged, door_line_id=door_line_id)
        return PlanAction(
            skill=skill,
            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=50, duration_tics=duration_tics),
            door_line_id=door_line_id,
            detail=merged,
        )

    def _line_retry_exhausted(self, door_memory: Any, line_id: int | None, *, special: int = 0) -> bool:
        if line_id is None or int(special or 0) in EXIT_SPECIALS:
            return False
        attempts_for = getattr(door_memory, "attempts_for", None)
        last_status_for = getattr(door_memory, "last_status_for", None)
        state_for = getattr(door_memory, "state_for", None)
        attempts = int(attempts_for(line_id)) if callable(attempts_for) else 0
        last_status = str(last_status_for(line_id)) if callable(last_status_for) else ""
        state = str(state_for(line_id)) if callable(state_for) else ""
        return attempts >= 3 and last_status == "stale_open_blocked" and state in {"opening", "closed"}

    def _needed_key_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
        world_memory: Any | None,
    ) -> PlanAction | None:
        required_fn = getattr(door_memory, "required_key_colors", None)
        if not callable(required_fn):
            return None
        needed = {str(color) for color in required_fn()}
        if not needed:
            return None
        acquired = set(getattr(world_memory, "acquired_keys", set()) or set())
        needed.difference_update(acquired)
        if not needed:
            return None
        candidates = [item for item in self.key_items if item.color in needed]
        if not candidates:
            return None
        candidates.sort(key=lambda item: _dist(player["point"], item.point))
        for item in candidates:
            if _dist(player["point"], item.point) <= 72 * FP_UNIT:
                self._last_status = "key_pickup_reached"
                return None
            detail = {"skill": "route_to_key", "key": item.color, "thing": item.id}
            if item.sector_id is not None:
                detail["sector"] = item.sector_id
            targets = self._nearest_nodes(item.point, limit=12)
            route = self._route(player["point"], targets, door_memory)
            if route is not None and self._route_endpoint_near(route, item.point, max_distance_fp=144 * FP_UNIT):
                self._last_route_len = len(route.points)
                return self._route_action(
                    player,
                    route,
                    agent_pb2,
                    door_memory,
                    skill="route_progression",
                    detail={**detail, "route": len(route.points)},
                )
            if item.sector_id is not None:
                route_edges = self._sector_route_from_player(player, [item.sector_id], door_memory)
                if route_edges:
                    return self._portal_route_action(
                        player,
                        route_edges,
                        agent_pb2,
                        door_memory,
                        skill="route_progression",
                        detail={**detail, "skill": "sector_route_to_key", "route": len(route_edges)},
                        state=state,
                    )
            if self.has_line_of_sight(player["point"], item.point):
                return self._turn_or_forward(
                    player,
                    item.point,
                    agent_pb2,
                    skill="route_progression",
                    detail={**detail, "skill": "visible_key_probe"},
                )
        self._last_status = "no_key_route"
        return None

    def _tag_gate_switch_action(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
        target_sectors: Iterable[int],
    ) -> PlanAction | None:
        """Route to the trigger line (switch/walk-over) that opens a tag gate blocking progression.

        When the only path to the objective crosses a closed dynamic tag gate (a raised bridge,
        remote door, or floor moved by a tagged trigger elsewhere), find the map line carrying a
        special with that tag and go activate it, mirroring how key hunting swaps the objective.
        """
        start_sector = self.sector_for_point_fp(player["point"].x, player["point"].y)
        if start_sector is None or not self._trigger_lines_by_tag:
            return None
        if self._sector_route(int(start_sector), target_sectors, door_memory) is not None:
            return None
        relaxed = self._sector_route(int(start_sector), target_sectors, door_memory, relax_tag_gates=True)
        if not relaxed:
            return None
        tag_is_open = getattr(door_memory, "tag_is_open", None)
        for edge in relaxed:
            if not edge.tag_gate:
                continue
            if callable(tag_is_open) and tag_is_open(edge.tag_gate):
                continue
            triggers = [
                line
                for line in self._trigger_lines_by_tag.get(int(edge.tag_gate), [])
                if not door_memory.is_blocked(line.id) and door_memory.can_retry(line.id)
            ]
            if not triggers:
                door_memory.record_status(edge.line_id, status="tag_gate_no_trigger")
                return None
            triggers.sort(key=lambda line: _dist(player["point"], self._nearest_point_on_line(player["point"], line)))
            trigger = triggers[0]
            door_memory.observe_line(trigger.id, special=trigger.special, tag=trigger.tag)
            detail = {
                "skill": "route_to_tag_switch",
                "line": trigger.id,
                "special": trigger.special,
                "gate": int(edge.tag_gate),
            }
            close = self._static_use_line_action(
                player,
                trigger,
                agent_pb2,
                door_memory,
                exit_only=False,
                detail=detail,
            )
            if close is not None:
                return close
            sector_plan = self._sector_route_to_line_action(
                player, trigger, agent_pb2, door_memory, exit_only=False, state=state
            )
            if sector_plan is not None:
                sector_plan.detail.setdefault("gate", int(edge.tag_gate))
                return sector_plan
            self._last_status = "no_tag_switch_route"
            return None
        return None

    def _e1m1_final_corridor_override(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        door_memory: Any,
    ) -> PlanAction | None:
        level = getattr(state, "level", None)
        try:
            episode = int(getattr(level, "episode", 0) or 0)
            map_id = int(getattr(level, "map", 0) or 0)
        except (TypeError, ValueError):
            return None
        if episode != 1 or map_id != 1:
            return None
        x_units = int(player["point"].x / FP_UNIT)
        y_units = int(player["point"].y / FP_UNIT)
        if not (2860 <= x_units <= 3140 and -4080 <= y_units <= -3700):
            return None
        for line_id in (340, 341):
            line = self._line_by_id(line_id)
            if line is None:
                continue
            if door_memory.is_blocked(line_id) or not door_memory.can_retry(line_id):
                continue
            planned = self._sector_route_to_line_action(
                player,
                line,
                agent_pb2,
                door_memory,
                exit_only=False,
                state=state,
            )
            if planned is None:
                continue
            detail = dict(planned.detail)
            detail["skill"] = "e1m1_final_corridor_override"
            detail["preferred_line"] = line_id
            return PlanAction(
                skill=planned.skill,
                action=planned.action,
                detail=detail,
                door_line_id=planned.door_line_id,
            )
        return None

    def _is_e1m1_final_corridor_side_door(self, state: Any | None, line_id: int | None) -> bool:
        if line_id not in {247, 248}:
            return False
        level = getattr(state, "level", None)
        try:
            episode = int(getattr(level, "episode", 0) or 0)
            map_id = int(getattr(level, "map", 0) or 0)
        except (TypeError, ValueError):
            return False
        return episode == 1 and map_id == 1
