#!/usr/bin/env python3.11
"""Portal/sector route construction and following for the Agent DOOM spatial planner.

Owns the planner's routing engine: grid A* routes and their edge costs,
sector-graph routes over portal edges, portal route execution (including the
passable-portal squeeze, upcoming use-edge and opening-door waits, and final
route-recovery probes), waypoint following, route-blocked bookkeeping, and
reachable-set computation. Extracted verbatim from planner.SpatialPlanner.

SectorRoutingMixin is a mixin over SpatialPlanner state: every method runs on
the SpatialPlanner instance, reads/writes attributes initialized in
SpatialPlanner.__init__, and calls shared SpatialPlanner helpers via self. It
holds no state of its own. This module must not import planner at runtime.
"""

from __future__ import annotations

import heapq
import math
from typing import Any, Iterable

from planner_model import (
    DOOR_SPECIALS,
    EXIT_SPECIALS,
    FINAL_DOOR_HARD_BLOCK_UNITS,
    FP_UNIT,
    MAX_STEP_UP_FP,
    MapLineRuntime,
    PlanAction,
    Point,
    PortalEdge,
    RETRIED_DOOR_RETREAT_HEALTH,
    Route,
    RouteStep,
    SectorRuntime,
    USE_DISTANCE_FP,
    USE_TRIGGER_SPECIALS,
    WALK_TRIGGER_SPECIALS,
    _angle_delta,
    _bearing,
    _dist,
)


class SectorRoutingMixin:
    """Portal/sector routing over SpatialPlanner state (see module docstring)."""

    def _portal_route_action(
        self,
        player: dict[str, Any],
        route: list[PortalEdge],
        agent_pb2: Any,
        door_memory: Any,
        *,
        skill: str,
        detail: dict[str, Any],
        state: Any | None = None,
    ) -> PlanAction | None:
        if not route:
            return None
        edge = self._select_portal_route_edge(player, route)
        route_remaining = int(detail.get("route", 99) or 99)
        merged = dict(detail)
        merged.update({"sector": edge.dst, "line": edge.line_id, "special": edge.special})
        merged = self._route_step_detail(
            merged,
            kind="portal",
            threat_multiplier=self._portal_threat_multiplier(edge, target=False),
            line_id=edge.line_id,
            sector=edge.dst,
            special=edge.special,
            route_remaining=route_remaining,
        )
        if edge is not route[0]:
            merged["skipped_line"] = route[0].line_id
        upcoming_use = self._upcoming_use_edge_action(player, route, agent_pb2, door_memory, merged, state=state)
        if upcoming_use is not None:
            return upcoming_use
        target = self._portal_entry_target(edge, state=state, player=player)
        recovery_probe = self._final_route_recovery_probe_action(
            state,
            player,
            target,
            agent_pb2,
            skill=skill,
            detail=merged,
            door_line_id=edge.line_id if edge.use_line or edge.door or edge.passable or edge.tag_gate else None,
            route_remaining=route_remaining,
        )
        if recovery_probe is not None:
            return recovery_probe
        linked_face_reuse = self._linked_open_face_blocked_action(state, player, edge, agent_pb2, door_memory, merged)
        if linked_face_reuse is not None:
            return linked_face_reuse
        if edge.use_line and not self._door_confirmed_open(door_memory, edge.line_id):
            line = self._line_by_id(edge.line_id)
            use_point = self._nearest_point_on_line(player["point"], line) if line is not None else edge.point
            distance = _dist(player["point"], use_point)
            attempts_for = getattr(door_memory, "attempts_for", None)
            attempts = int(attempts_for(edge.line_id)) if callable(attempts_for) else 0
            if edge.special in USE_TRIGGER_SPECIALS and attempts >= 3 and distance <= USE_DISTANCE_FP:
                target = self._portal_entry_target(edge, state=state, player=player)
                delta = _angle_delta(_bearing(player["point"], target), player["angle"])
                navigation = getattr(state, "navigation", None)
                front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
                forward_open = bool(getattr(navigation, "forward_open", False)) and (
                    not front_distance or front_distance > 32 * FP_UNIT
                )
                hard_blocked = bool(front_distance) and front_distance <= FINAL_DOOR_HARD_BLOCK_UNITS * FP_UNIT
                final_door_commit = self._final_door_route_commit(route_remaining, front_distance)
                if attempts >= 3 and distance <= USE_DISTANCE_FP and hard_blocked:
                    use_delta = _angle_delta(_bearing(player["point"], use_point), player["angle"])
                    detail_commit = {
                        **merged,
                        "action": "commit_retried_use_line",
                        "attempts": attempts,
                        "hp": int(getattr(getattr(state, "player", None), "health", 100) or 100),
                        "open": int(forward_open),
                        "dist": int(distance / FP_UNIT),
                        "turn": round(use_delta, 1),
                    }
                    if abs(use_delta) > 24.0:
                        return self._turn(
                            player,
                            use_delta,
                            agent_pb2,
                            "open_use_line",
                            {**detail_commit, "action": "align_retried_use_line"},
                            door_line_id=edge.line_id,
                        )
                    record_attempt = getattr(door_memory, "record_attempt", None)
                    if callable(record_attempt):
                        record_attempt(edge.line_id, status="sector_portal_commit_use")
                    return PlanAction(
                        skill="open_use_line",
                        action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=6),
                        door_line_id=edge.line_id,
                        detail=detail_commit,
                    )
                if (
                    not final_door_commit
                    and not forward_open
                    and bool(getattr(navigation, "back_open", False))
                ):
                    return PlanAction(
                        skill="route_progression",
                        action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=44, duration_tics=10),
                        door_line_id=edge.line_id,
                        detail={
                            **merged,
                            "action": "retreat_retried_use_line",
                            "attempts": attempts,
                            "hp": int(getattr(getattr(state, "player", None), "health", 100) or 100),
                            "open": int(forward_open),
                        },
                    )
                detail_push = {
                    **merged,
                    "action": "force_follow_retried_use_line",
                    "attempts": attempts,
                    "open": int(forward_open),
                    "turn": round(delta, 1),
                }
                raw_cls = getattr(agent_pb2, "RawTiccmd", None)
                if raw_cls is not None:
                    return PlanAction(
                        skill="open_use_line",
                        action=agent_pb2.PlayerAction(
                            duration_tics=10,
                            raw=raw_cls(
                                forward_move=56,
                                angle_turn=self._raw_steer_turn_units(delta),
                            ),
                        ),
                        door_line_id=edge.line_id,
                        detail=detail_push,
                    )
                return PlanAction(
                    skill="open_use_line",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=48, duration_tics=10),
                    door_line_id=edge.line_id,
                    detail=detail_push,
                )
            if self._door_opening(door_memory, edge.line_id):
                target = self._portal_entry_target(edge, state=state, player=player)
                if distance <= 176 * FP_UNIT:
                    delta = _angle_delta(_bearing(player["point"], target), player["angle"])
                    navigation = getattr(state, "navigation", None)
                    front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
                    if self._final_door_route_commit(route_remaining, front_distance):
                        return self._final_door_commit_action(
                            player,
                            target,
                            agent_pb2,
                            skill=skill,
                            detail=merged,
                            door_line_id=edge.line_id,
                            action_name="final_door_follow_opening_commit",
                            forward_move=60,
                            duration_tics=10,
                        )
                    if 55.0 <= abs(delta) <= 110.0:
                        action_type = agent_pb2.ACTION_STRAFE_LEFT if delta > 0 else agent_pb2.ACTION_STRAFE_RIGHT
                        return PlanAction(
                            skill=skill,
                            action=agent_pb2.PlayerAction(action=action_type, amount=38, duration_tics=10),
                            door_line_id=edge.line_id,
                            detail={**merged, "action": "follow_opening_strafe", "turn": round(delta, 1)},
                        )
                    return self._turn_or_forward(
                        player,
                        target,
                        agent_pb2,
                        skill=skill,
                        detail={**merged, "action": "follow_opening"},
                        door_line_id=edge.line_id,
                        )
            if distance <= USE_DISTANCE_FP:
                delta = _angle_delta(_bearing(player["point"], use_point), player["angle"])
                if abs(delta) <= 20.0:
                    door_memory.record_attempt(edge.line_id, status="sector_portal_use")
                    merged["action"] = "use"
                    return PlanAction(
                        skill="open_use_line",
                        action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
                        door_line_id=edge.line_id,
                        detail=merged,
                    )
                return self._turn(player, delta, agent_pb2, "open_use_line", merged, door_line_id=edge.line_id)
            return self._turn_or_forward(player, use_point, agent_pb2, skill="open_use_line", detail={**merged, "action": "approach_use"}, door_line_id=edge.line_id)
        line_id = edge.line_id if edge.use_line or edge.door or edge.passable or edge.tag_gate else None
        if edge.passable and route_remaining <= 4:
            local_use = self._navigation_use_line_action(
                state,
                player,
                agent_pb2,
                door_memory,
                skill="open_use_line",
                detail={**merged, "skill": "final_corridor_use_line"},
                max_distance_fp=128 * FP_UNIT,
                include_open=True,
            )
            if local_use is not None:
                if not self._is_e1m1_final_corridor_side_door(state, local_use.door_line_id):
                    return local_use
        squeeze = self._passable_portal_squeeze_action(state, player, edge, target, agent_pb2, skill, merged)
        if squeeze is not None:
            return squeeze
        if str(merged.get("skill", "")) != "sector_route_hazard_escape":
            threat_escape = self._low_health_backtrack_evasion(state, player, edge, target, agent_pb2, route_remaining)
            if threat_escape is not None:
                return threat_escape
        if _dist(player["point"], target) > 96 * FP_UNIT and not edge.tag_gate and self._edge(player["point"], target, door_memory) is None:
            local_targets = self._nearest_nodes(target, limit=10)
            local_route = self._route(player["point"], local_targets, door_memory)
            if local_route is not None and local_route.points:
                self._last_route_len = len(route) + len(local_route.points)
                return self._route_action(
                    player,
                    local_route,
                    agent_pb2,
                    door_memory,
                    skill=skill,
                    detail={
                        **merged,
                        "skill": "navcell_to_portal",
                        "portal_line": edge.line_id,
                        "local_route": len(local_route.points),
                    },
                )
        return self._turn_or_forward(player, target, agent_pb2, skill=skill, detail=merged, door_line_id=line_id)

    def _passable_portal_squeeze_action(
        self,
        state: Any | None,
        player: dict[str, Any],
        edge: PortalEdge,
        target: Point,
        agent_pb2: Any,
        skill: str,
        detail: dict[str, Any],
    ) -> PlanAction | None:
        if state is None or not edge.passable:
            return None
        line = self._line_by_id(edge.line_id)
        if line is None:
            return None
        line_point = self._nearest_point_on_line(player["point"], line)
        line_distance = _dist(player["point"], line_point)
        projection = self._line_projection_fraction(player["point"], line)
        midpoint_distance = _dist(player["point"], line.midpoint)
        route_remaining = int(detail.get("route", 99) or 99)
        if (
            route_remaining <= 8
            and line_distance <= 288 * FP_UNIT
            and (projection < 0.18 or projection > 0.82)
            and midpoint_distance > 160 * FP_UNIT
        ):
            raw_center = self._raw_center_passable_portal_action(
                state,
                player,
                edge,
                line,
                line.midpoint,
                agent_pb2,
                skill,
                {
                    **detail,
                    "skill": "center_passable_portal",
                    "dist": int(midpoint_distance / FP_UNIT),
                    "projection": round(projection, 2),
                },
            )
            if raw_center is not None:
                return raw_center
            return self._turn_or_forward(
                player,
                line.midpoint,
                agent_pb2,
                skill=skill,
                detail={
                    **detail,
                    "skill": "center_passable_portal",
                    "dist": int(midpoint_distance / FP_UNIT),
                    "projection": round(projection, 2),
                },
                door_line_id=edge.line_id,
            )
        if line_distance > 144 * FP_UNIT:
            return None
        navigation = getattr(state, "navigation", None)
        front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
        blocked_ahead = (front_distance and front_distance <= 72 * FP_UNIT) or not bool(getattr(navigation, "forward_open", False))
        delta = _angle_delta(_bearing(player["point"], target), player["angle"])
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        centered = 0.30 <= projection <= 0.70 and _dist(player["point"], line_point) <= 24 * FP_UNIT
        if not blocked_ahead and centered:
            merged = {
                **detail,
                "action": "cross_passable_portal",
                "dist": int(_dist(player["point"], line_point) / FP_UNIT),
                "turn": round(delta, 1),
                "projection": round(projection, 2),
                "mt": [int(target.x), int(target.y)],
            }
            if raw_cls is not None:
                return PlanAction(
                    skill=skill,
                    action=agent_pb2.PlayerAction(
                        duration_tics=14,
                        raw=raw_cls(
                            forward_move=58,
                            side_move=0,
                            angle_turn=self._raw_steer_turn_units(delta),
                        ),
                    ),
                    door_line_id=edge.line_id,
                    detail=merged,
                )
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=52, duration_tics=14),
                door_line_id=edge.line_id,
                detail=merged,
            )
        if not blocked_ahead:
            return None
        if (projection < 0.18 or projection > 0.82) and midpoint_distance > 48 * FP_UNIT:
            raw_center = self._raw_center_passable_portal_action(
                state,
                player,
                edge,
                line,
                line.midpoint,
                agent_pb2,
                skill,
                {
                    **detail,
                    "skill": "center_passable_portal",
                    "dist": int(midpoint_distance / FP_UNIT),
                    "projection": round(projection, 2),
                },
            )
            if raw_center is not None:
                return raw_center
            return self._turn_or_forward(
                player,
                line.midpoint,
                agent_pb2,
                skill=skill,
                detail={
                    **detail,
                    "skill": "center_passable_portal",
                    "dist": int(midpoint_distance / FP_UNIT),
                    "projection": round(projection, 2),
                },
                door_line_id=edge.line_id,
            )
        if abs(delta) > 70.0:
            return self._turn(player, delta, agent_pb2, skill, {**detail, "action": "face_passable_portal"}, door_line_id=edge.line_id)
        if centered:
            merged = {
                **detail,
                "action": "cross_passable_portal",
                "dist": int(_dist(player["point"], line_point) / FP_UNIT),
                "turn": round(delta, 1),
                "projection": round(projection, 2),
                "mt": [int(target.x), int(target.y)],
            }
            if blocked_ahead:
                left_score = self._side_probe_distance(state, left=True)
                right_score = self._side_probe_distance(state, left=False)
                if left_score or right_score:
                    go_left = left_score >= right_score
                    merged.update(
                        {
                            "action": "blocked_portal_side_squeeze",
                            "side": "left" if go_left else "right",
                        }
                    )
                    if raw_cls is not None:
                        return PlanAction(
                            skill=skill,
                            action=agent_pb2.PlayerAction(
                                duration_tics=10,
                                raw=raw_cls(
                                    forward_move=28,
                                    side_move=46 if go_left else -46,
                                    angle_turn=self._raw_steer_turn_units(delta),
                                ),
                            ),
                            door_line_id=edge.line_id,
                            detail=merged,
                        )
                    action_type = agent_pb2.ACTION_STRAFE_LEFT if go_left else agent_pb2.ACTION_STRAFE_RIGHT
                    return PlanAction(
                        skill=skill,
                        action=agent_pb2.PlayerAction(action=action_type, amount=36, duration_tics=10),
                        door_line_id=edge.line_id,
                        detail=merged,
                    )
                if bool(getattr(navigation, "back_open", False)):
                    return PlanAction(
                        skill=skill,
                        action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=28, duration_tics=8),
                        door_line_id=edge.line_id,
                        detail={**merged, "action": "reset_blocked_passable_portal"},
                    )
            if raw_cls is not None:
                return PlanAction(
                    skill=skill,
                    action=agent_pb2.PlayerAction(
                        duration_tics=14,
                        raw=raw_cls(
                            forward_move=58,
                            side_move=0,
                            angle_turn=self._raw_steer_turn_units(delta),
                        ),
                    ),
                    door_line_id=edge.line_id,
                    detail=merged,
                )
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=52, duration_tics=14),
                door_line_id=edge.line_id,
                detail=merged,
            )
        left_score = self._side_probe_distance(state, left=True)
        right_score = self._side_probe_distance(state, left=False)
        if left_score == right_score == 0:
            projection = self._line_projection_fraction(player["point"], line)
            go_left = projection >= 0.5
        else:
            go_left = left_score >= right_score
        merged = {
            **detail,
            "action": "squeeze_passable_portal",
            "dist": int(_dist(player["point"], line_point) / FP_UNIT),
            "turn": round(delta, 1),
            "side": "left" if go_left else "right",
            "mt": [int(target.x), int(target.y)],
        }
        if raw_cls is not None:
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(
                    duration_tics=10,
                    raw=raw_cls(
                        forward_move=54,
                        side_move=42 if go_left else -42,
                        angle_turn=self._raw_steer_turn_units(delta),
                    ),
                ),
                door_line_id=edge.line_id,
                detail=merged,
            )
        action_type = agent_pb2.ACTION_STRAFE_LEFT if go_left else agent_pb2.ACTION_STRAFE_RIGHT
        return PlanAction(
            skill=skill,
            action=agent_pb2.PlayerAction(action=action_type, amount=36, duration_tics=10),
            door_line_id=edge.line_id,
            detail=merged,
        )

    def _sector_route(
        self,
        start_sector: int,
        target_sectors: Iterable[int],
        door_memory: Any,
        *,
        relax_tag_gates: bool = False,
    ) -> list[PortalEdge] | None:
        targets = {int(sector) for sector in target_sectors if sector is not None}
        targets.discard(start_sector)
        if not targets:
            return []
        heap: list[tuple[float, int]] = [(0.0, start_sector)]
        best: dict[int, float] = {start_sector: 0.0}
        prev: dict[int, tuple[int, PortalEdge]] = {}
        found: int | None = None
        while heap:
            cost, sector = heapq.heappop(heap)
            if cost != best.get(sector):
                continue
            if sector in targets:
                found = sector
                break
            for edge in self._portal_graph.get(sector, []):
                if edge.tag_gate and not relax_tag_gates:
                    tag_is_open = getattr(door_memory, "tag_is_open", None)
                    if not (callable(tag_is_open) and tag_is_open(edge.tag_gate)):
                        continue
                if door_memory.is_blocked(edge.line_id) or self.sector_is_damaging(edge.dst):
                    continue
                can_retry = getattr(door_memory, "can_retry", None)
                if (
                    callable(can_retry)
                    and not bool(can_retry(edge.line_id))
                    and not edge.passable
                    and not edge.exit
                    and not door_memory.is_open(edge.line_id)
                ):
                    continue
                route_penalty_for = getattr(door_memory, "route_penalty_for", None)
                route_penalty = float(route_penalty_for(edge.line_id)) if callable(route_penalty_for) else 0.0
                edge_cost = edge.cost + route_penalty + (0.0 if edge.passable or door_memory.is_open(edge.line_id) else 64.0)
                edge_cost *= self._portal_threat_multiplier(edge, target=edge.dst in targets)
                if edge.dst in self._avoid_sector_ids and edge.dst not in targets:
                    edge_cost += 2400.0
                new_cost = cost + edge_cost
                if new_cost < best.get(edge.dst, float("inf")):
                    best[edge.dst] = new_cost
                    prev[edge.dst] = (sector, edge)
                    heapq.heappush(heap, (new_cost, edge.dst))
        if found is None:
            return None
        edges: list[PortalEdge] = []
        cursor = found
        while cursor != start_sector:
            parent, edge = prev[cursor]
            edges.append(edge)
            cursor = parent
        edges.reverse()
        return edges

    def _sector_route_from_player(self, player: dict[str, Any], target_sectors: Iterable[int], door_memory: Any) -> list[PortalEdge] | None:
        start_sector = self.sector_for_point_fp(player["point"].x, player["point"].y)
        if start_sector is None:
            return None
        return self._sector_route(int(start_sector), target_sectors, door_memory)

    def _route(self, start: Point, target_indices: Iterable[int], door_memory: Any) -> Route | None:
        targets = set(target_indices)
        if not targets:
            return None
        start_neighbors = self._nearest_nodes(start, limit=10)
        heap: list[tuple[float, int]] = []
        best: dict[int, float] = {}
        prev: dict[int, tuple[int | None, int | None, bool]] = {}
        for idx in start_neighbors:
            edge = self._edge(start, self.nodes[idx], door_memory)
            if edge is None:
                continue
            cost, line_id, use_line = edge
            cost *= self._point_threat_multiplier(self.nodes[idx], target=idx in targets)
            best[idx] = cost
            prev[idx] = (None, line_id, use_line)
            heapq.heappush(heap, (cost, idx))
        found: int | None = None
        while heap:
            cost, idx = heapq.heappop(heap)
            if cost != best.get(idx):
                continue
            if idx in targets:
                found = idx
                break
            for nxt in self._neighbors(idx):
                edge = self._edge(self.nodes[idx], self.nodes[nxt], door_memory)
                if edge is None:
                    continue
                step_cost, line_id, use_line = edge
                step_cost *= self._point_threat_multiplier(self.nodes[nxt], target=nxt in targets)
                new_cost = cost + step_cost
                if new_cost < best.get(nxt, float("inf")):
                    best[nxt] = new_cost
                    prev[nxt] = (idx, line_id, use_line)
                    heapq.heappush(heap, (new_cost, nxt))
        if found is None:
            return None
        indexes: list[int] = []
        cursor: int | None = found
        while cursor is not None:
            indexes.append(cursor)
            cursor = prev[cursor][0]
        indexes.reverse()
        steps: list[RouteStep] = []
        for idx in indexes:
            _, line_id, use_line = prev[idx]
            steps.append(RouteStep(point=self.nodes[idx], line_id=line_id, use_line=use_line))
        return Route(points=steps, cost=best[found])

    def _route_action(
        self,
        player: dict[str, Any],
        route: Route,
        agent_pb2: Any,
        door_memory: Any,
        *,
        skill: str,
        detail: dict[str, Any],
    ) -> PlanAction | None:
        if not route.points:
            return None
        if skill == "press_exit":
            line_id = detail.get("line")
            line = self._line_by_id(int(line_id)) if line_id is not None else None
            if line is not None and (line.exit or line.special in EXIT_SPECIALS) and len(route.points) <= 2:
                line_action = self._static_use_line_action(
                    player,
                    line,
                    agent_pb2,
                    door_memory,
                    exit_only=True,
                    detail={**detail, "skill": "static_exit_route_closeout"},
                    max_distance_fp=128 * FP_UNIT,
                )
                if line_action is not None:
                    return line_action
        if skill == "open_use_line":
            line_id = detail.get("line")
            line = self._line_by_id(int(line_id)) if line_id is not None else None
            if line is not None and (line.use_trigger or line.door) and len(route.points) <= 2:
                line_action = self._static_use_line_action(
                    player,
                    line,
                    agent_pb2,
                    door_memory,
                    exit_only=False,
                    detail={**detail, "skill": "static_use_route_closeout"},
                    max_distance_fp=128 * FP_UNIT,
                )
                if line_action is not None:
                    return line_action
        first = self._lookahead_route_step(player, route, door_memory)
        target = first.point
        route_step_threat = self._point_threat_multiplier(target, target=False)
        route_detail = self._route_step_detail(
            detail,
            kind="nav",
            threat_multiplier=route_step_threat,
            target=target,
            line_id=first.line_id,
            use_line=first.use_line,
        )
        if str(detail.get("skill", "")) == "planner_route_to_los":
            self._last_los_route_endpoint_key = self._grid_key(route.points[-1].point)
        if first.use_line:
            line = self._line_by_id(first.line_id)
            if line is not None and not self._door_any_open(door_memory, line.id):
                target = line.midpoint
                if _dist(player["point"], target) <= USE_DISTANCE_FP:
                    delta = _angle_delta(_bearing(player["point"], target), player["angle"])
                    if abs(delta) <= 20.0:
                        door_memory.record_attempt(line.id, status="planner_use")
                        merged = dict(detail)
                        merged.update({"line": line.id, "action": "use"})
                        return PlanAction(
                            skill="open_use_line",
                            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
                            door_line_id=line.id,
                            detail=merged,
                        )
                    return self._turn(player, delta, agent_pb2, "open_use_line", {"skill": "planner_face_door", "line": line.id})
        return self._turn_or_forward(player, target, agent_pb2, skill=skill, detail=route_detail, door_line_id=first.line_id)

    def _route_edge_cost(self, route: list[PortalEdge], door_memory: Any) -> float:
        route_penalty_for = getattr(door_memory, "route_penalty_for", None)
        total = 0.0
        for edge in route:
            penalty = float(route_penalty_for(edge.line_id)) if callable(route_penalty_for) else 0.0
            total += (float(edge.cost) + penalty) * self._portal_threat_multiplier(edge)
        return total

    def _route_waypoint_action(self, state: Any, player: dict[str, Any], agent_pb2: Any, door_memory: Any) -> PlanAction | None:
        navigation = getattr(state, "navigation", None)
        waypoint = getattr(navigation, "route_waypoint", None)
        raw_line = getattr(waypoint, "line", None)
        if raw_line is None:
            return None
        line_id = int(getattr(raw_line, "line_id", -1))
        special = int(getattr(raw_line, "special", 0) or 0)
        if line_id < 0 or door_memory.is_blocked(line_id) or not door_memory.can_retry(line_id):
            return None
        is_exit = bool(getattr(waypoint, "exit", False)) or special in EXIT_SPECIALS or door_memory.state_for(line_id) == "exit"
        is_walk = bool(getattr(waypoint, "walk_trigger", False)) or special in WALK_TRIGGER_SPECIALS
        is_use = special in USE_TRIGGER_SPECIALS or special in DOOR_SPECIALS
        if not (is_exit or is_walk or is_use):
            return None
        if is_walk and door_memory.is_open(line_id):
            return None
        point = self._line_point(raw_line)
        static_line = self._line_by_id(line_id)
        if point is None and static_line is not None:
            point = static_line.midpoint
        if point is None:
            return None
        distance = int(getattr(raw_line, "nearest_distance_fp", 0) or getattr(raw_line, "distance_fp", 0) or _dist(player["point"], point))
        skill = "press_exit" if is_exit else ("route_progression" if is_walk else "open_use_line")
        detail = {
            "skill": "route_waypoint_exit" if is_exit else ("route_waypoint_walk" if is_walk else "route_waypoint_use"),
            "line": line_id,
            "special": special,
            "tag": int(getattr(static_line, "tag", 0) or 0) if static_line is not None else int(getattr(raw_line, "tag", 0) or 0),
            "dist": int(distance / FP_UNIT),
        }
        if distance > 160 * FP_UNIT:
            local_door = self._route_local_door_action(state, player, point, agent_pb2, door_memory, base_detail=detail)
            if local_door is not None:
                return local_door
        if is_exit or is_use:
            if distance <= USE_DISTANCE_FP:
                delta = _angle_delta(_bearing(player["point"], point), player["angle"])
                if abs(delta) <= 20.0:
                    door_memory.record_attempt(line_id, status="route_waypoint_use")
                    merged = dict(detail)
                    merged["action"] = "use"
                    return PlanAction(
                        skill=skill,
                        action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
                        door_line_id=line_id,
                        detail=merged,
                    )
                return self._turn(player, delta, agent_pb2, skill, detail, door_line_id=line_id)
        if is_walk and distance <= 80 * FP_UNIT:
            cross_target = self._line_use_target(player["point"], static_line) if static_line is not None else point
            delta = _angle_delta(_bearing(player["point"], cross_target), player["angle"])
            if abs(delta) > 18.0:
                return self._turn(player, delta, agent_pb2, skill, detail, door_line_id=line_id)
            merged = dict(detail)
            merged["action"] = "cross"
            merged["mt"] = [int(cross_target.x), int(cross_target.y)]
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=58, duration_tics=14),
                door_line_id=line_id,
                detail=merged,
            )
        targets = self._nearest_nodes(point, limit=10)
        route = self._route(player["point"], targets, door_memory)
        if route is not None:
            self._last_route_len = len(route.points)
            plan = self._route_action(
                player,
                route,
                agent_pb2,
                door_memory,
                skill=skill,
                detail={**detail, "route": len(route.points)},
            )
            if is_walk and plan is not None and plan.door_line_id is None and distance <= 192 * FP_UNIT:
                return PlanAction(skill=plan.skill, action=plan.action, door_line_id=line_id, detail=plan.detail)
            return plan
        return self._turn_or_forward(player, point, agent_pb2, skill=skill, detail=detail, door_line_id=line_id)

    def _lookahead_route_step(self, player: dict[str, Any], route: Route, door_memory: Any) -> RouteStep:
        selected: RouteStep | None = None
        player_point = player["point"]
        for step in route.points[:6]:
            if step.use_line and not door_memory.is_open(step.line_id):
                return step
            distance = _dist(player_point, step.point) / FP_UNIT
            if distance < 40.0:
                selected = step
                continue
            edge = self._edge(player_point, step.point, door_memory)
            if edge is None:
                break
            _, crossed_line, crossed_use_line = edge
            if crossed_use_line and not door_memory.is_open(crossed_line):
                break
            selected = step
            if distance >= 360.0:
                break
        return selected or route.points[0]

    def _select_portal_route_edge(self, player: dict[str, Any], route: list[PortalEdge]) -> PortalEdge:
        edge = route[0]
        if len(route) < 2 or edge.use_line or edge.door or not edge.passable:
            return edge
        first_target = self._portal_entry_target(edge)
        first_distance = _dist(player["point"], first_target) / FP_UNIT
        first_delta = abs(_angle_delta(_bearing(player["point"], first_target), player["angle"]))
        if first_distance > 224.0 or first_delta < 75.0:
            return edge
        for candidate in route[1:4]:
            if candidate.use_line or candidate.door:
                return candidate
            candidate_target = self._portal_entry_target(candidate)
            candidate_delta = abs(_angle_delta(_bearing(player["point"], candidate_target), player["angle"]))
            if candidate_delta + 30.0 < first_delta:
                return candidate
        return edge

    def _portal_entry_target(self, edge: PortalEdge, *, state: Any | None = None, player: dict[str, Any] | None = None) -> Point:
        line = self._line_by_id(edge.line_id)
        if line is not None:
            dx = float(line.b.x - line.a.x)
            dy = float(line.b.y - line.a.y)
            if edge.dst == line.back_sector:
                nx, ny = -dy, dx
            elif edge.dst == line.front_sector:
                nx, ny = dy, -dx
            else:
                nx = ny = 0.0
            normal_len = math.hypot(nx, ny)
            if normal_len > 1.0:
                push = (96 if edge.passable else 112) * FP_UNIT
                return Point(
                    int(edge.point.x + nx / normal_len * push),
                    int(edge.point.y + ny / normal_len * push),
                )
        dst_center = self.sectors.get(edge.dst, SectorRuntime(edge.dst, edge.point)).center
        dx = float(dst_center.x - edge.point.x)
        dy = float(dst_center.y - edge.point.y)
        length = math.hypot(dx, dy)
        if length <= 1.0:
            return self._portal_target_avoiding_visible_enemy(edge, dst_center, line, state=state, player=player)
        push = min(192 * FP_UNIT, max(96 * FP_UNIT, length * 0.65))
        target = Point(
            int(edge.point.x + dx / length * push),
            int(edge.point.y + dy / length * push),
        )
        return self._portal_target_avoiding_visible_enemy(edge, target, line, state=state, player=player)

    def _portal_target_avoiding_visible_enemy(
        self,
        edge: PortalEdge,
        target: Point,
        line: MapLineRuntime | None,
        *,
        state: Any | None,
        player: dict[str, Any] | None,
    ) -> Point:
        if state is None or player is None or line is None or not getattr(self, "_avoid_sector_ids", set()):
            return target
        enemy = self._nearest_enemy(state, player)
        if enemy is None or not bool(enemy.get("line_of_sight")):
            return target
        enemy_point = enemy["point"]
        if min(_dist(enemy_point, edge.point), _dist(enemy_point, target)) > 128 * FP_UNIT:
            return target
        dx = float(line.b.x - line.a.x)
        dy = float(line.b.y - line.a.y)
        length = math.hypot(dx, dy)
        if length <= 1.0:
            return target
        tx = dx / length
        ty = dy / length
        offset = 96 * FP_UNIT
        left = Point(int(target.x - tx * offset), int(target.y - ty * offset))
        right = Point(int(target.x + tx * offset), int(target.y + ty * offset))
        return left if _dist(left, enemy_point) >= _dist(right, enemy_point) else right

    def _upcoming_use_edge_action(
        self,
        player: dict[str, Any],
        route: list[PortalEdge],
        agent_pb2: Any,
        door_memory: Any,
        detail: dict[str, Any],
        *,
        state: Any | None = None,
    ) -> PlanAction | None:
        if not route or route[0].use_line:
            return None
        for upcoming in route[1:4]:
            if (
                not upcoming.use_line
                or door_memory.is_confirmed_open(upcoming.line_id)
                or not door_memory.can_retry(upcoming.line_id)
            ):
                continue
            if door_memory.is_opening(upcoming.line_id) and route[0].passable:
                line = self._line_by_id(upcoming.line_id)
                enemy = self._nearest_enemy(state, player) if state is not None else None
                if line is not None and enemy is not None and bool(enemy.get("line_of_sight")):
                    target = self._nearest_point_on_line(player["point"], line)
                    distance = _dist(player["point"], target)
                    if distance <= USE_DISTANCE_FP:
                        delta = _angle_delta(_bearing(player["point"], target), player["angle"])
                        navigation = getattr(state, "navigation", None)
                        front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
                        forward_open = bool(getattr(navigation, "forward_open", False)) and (
                            not front_distance or front_distance > 32 * FP_UNIT
                        )
                        merged = {
                            **detail,
                            "skill": "pressure_reopen_upcoming_use_line",
                            "line": upcoming.line_id,
                            "special": upcoming.special,
                            "dist": int(distance / FP_UNIT),
                            "turn": round(delta, 1),
                        }
                        attempts_for = getattr(door_memory, "attempts_for", None)
                        attempts = int(attempts_for(upcoming.line_id)) if callable(attempts_for) else 0
                        health = int(getattr(getattr(state, "player", None), "health", 100) or 100)
                        route_remaining = int(detail.get("route", 99) or 99)
                        final_door_commit = self._final_door_route_commit(route_remaining, front_distance)
                        if final_door_commit:
                            route_target = self._portal_entry_target(upcoming, state=state, player=player)
                            return self._final_door_commit_action(
                                player,
                                route_target,
                                agent_pb2,
                                skill="open_use_line",
                                detail=merged,
                                door_line_id=upcoming.line_id,
                                action_name="final_door_pressure_follow_opening",
                                forward_move=60,
                                duration_tics=10,
                            )
                        if (
                            (health <= RETRIED_DOOR_RETREAT_HEALTH or attempts >= 3)
                            and not final_door_commit
                            and not forward_open
                            and bool(getattr(navigation, "back_open", False))
                        ):
                            return PlanAction(
                                skill="route_progression",
                                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=44, duration_tics=10),
                                door_line_id=upcoming.line_id,
                                detail={
                                    **merged,
                                    "action": "retreat_pressure_upcoming_use_line",
                                    "attempts": attempts,
                                    "hp": health,
                                    "open": int(forward_open),
                                },
                            )
                        if forward_open or attempts >= 3:
                            route_target = self._portal_entry_target(upcoming, state=state, player=player)
                            route_delta = _angle_delta(_bearing(player["point"], route_target), player["angle"])
                            raw_cls = getattr(agent_pb2, "RawTiccmd", None)
                            detail_push = {
                                **merged,
                                "action": "pressure_follow_opening",
                                "attempts": attempts,
                                "open": int(forward_open),
                                "turn": round(route_delta, 1),
                            }
                            if raw_cls is not None:
                                return PlanAction(
                                    skill="open_use_line",
                                    action=agent_pb2.PlayerAction(
                                        duration_tics=8,
                                        raw=raw_cls(
                                            forward_move=52,
                                            angle_turn=self._raw_steer_turn_units(route_delta),
                                        ),
                                    ),
                                    door_line_id=upcoming.line_id,
                                    detail=detail_push,
                                )
                            return PlanAction(
                                skill="open_use_line",
                                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=46, duration_tics=8),
                                door_line_id=upcoming.line_id,
                                detail=detail_push,
                            )
                        if abs(delta) <= 28.0:
                            door_memory.record_attempt(upcoming.line_id, status="pressure_reopen_upcoming_use")
                            return PlanAction(
                                skill="open_use_line",
                                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=2),
                                door_line_id=upcoming.line_id,
                                detail={**merged, "action": "use"},
                            )
                        return self._turn(player, delta, agent_pb2, "open_use_line", merged, door_line_id=upcoming.line_id)
                wait = self._upcoming_opening_door_wait_action(
                    state,
                    player,
                    route[0],
                    upcoming,
                    agent_pb2,
                    detail,
                )
                if wait is not None:
                    return wait
                if state is not None:
                    return None
            line = self._line_by_id(upcoming.line_id)
            target = line.midpoint if line is not None else upcoming.point
            distance = _dist(player["point"], target)
            if distance > 96 * FP_UNIT:
                continue
            delta = _angle_delta(_bearing(player["point"], target), player["angle"])
            merged = {
                **detail,
                "skill": "preopen_upcoming_use_line",
                "line": upcoming.line_id,
                "special": upcoming.special,
                "dist": int(distance / FP_UNIT),
                "turn": round(delta, 1),
            }
            if abs(delta) <= 20.0:
                door_memory.record_attempt(upcoming.line_id, status="preopen_upcoming_use")
                return PlanAction(
                    skill="open_use_line",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
                    door_line_id=upcoming.line_id,
                    detail={**merged, "action": "use"},
                )
            return self._turn(player, delta, agent_pb2, "open_use_line", merged, door_line_id=upcoming.line_id)
        return None

    def _upcoming_opening_door_wait_action(
        self,
        state: Any | None,
        player: dict[str, Any],
        portal: PortalEdge,
        upcoming: PortalEdge,
        agent_pb2: Any,
        detail: dict[str, Any],
    ) -> PlanAction | None:
        if state is None:
            return None
        navigation = getattr(state, "navigation", None)
        front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
        forward_open = bool(getattr(navigation, "forward_open", False)) and (not front_distance or front_distance > 72 * FP_UNIT)
        if forward_open:
            return None
        route_remaining = int(detail.get("route", 99) or 99)
        if self._final_door_route_commit(route_remaining, front_distance):
            target = self._portal_entry_target(upcoming, state=state, player=player)
            return self._final_door_commit_action(
                player,
                target,
                agent_pb2,
                skill="open_use_line",
                detail={**detail, "line": upcoming.line_id, "special": upcoming.special},
                door_line_id=upcoming.line_id,
                action_name="final_door_wait_commit",
                forward_move=58,
                duration_tics=10,
            )
        line = self._line_by_id(portal.line_id)
        if line is None:
            return None
        entry = self._portal_entry_target(portal, state=state, player=player)
        away_x = float(line.midpoint.x - entry.x)
        away_y = float(line.midpoint.y - entry.y)
        away_len = math.hypot(away_x, away_y)
        if away_len <= 1.0:
            away_x = float(player["point"].x - line.midpoint.x)
            away_y = float(player["point"].y - line.midpoint.y)
            away_len = math.hypot(away_x, away_y)
        if away_len <= 1.0:
            away_x, away_y, away_len = 0.0, 1.0, 1.0
        enemy = self._nearest_enemy(state, player)
        endpoints = [line.a, line.b]
        if enemy is not None and bool(enemy.get("line_of_sight")):
            endpoint = max(endpoints, key=lambda point: _dist(point, enemy["point"]))
        else:
            endpoint = min(endpoints, key=lambda point: _dist(point, player["point"]))
        target = Point(
            int(endpoint.x + away_x / away_len * 56 * FP_UNIT),
            int(endpoint.y + away_y / away_len * 56 * FP_UNIT),
        )
        merged = {
            **detail,
            "skill": "wait_upcoming_door_jamb",
            "line": upcoming.line_id,
            "portal_line": portal.line_id,
            "dist": int(_dist(player["point"], target) / FP_UNIT),
        }
        return self._turn_or_forward(
            player,
            target,
            agent_pb2,
            skill="route_progression",
            detail=merged,
            door_line_id=upcoming.line_id,
        )

    def _raw_center_passable_portal_action(
        self,
        state: Any | None,
        player: dict[str, Any],
        edge: PortalEdge,
        line: MapLineRuntime,
        target: Point,
        agent_pb2: Any,
        skill: str,
        detail: dict[str, Any],
    ) -> PlanAction | None:
        if state is None:
            return None
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is None:
            return None
        left_score = self._side_probe_distance(state, left=True)
        right_score = self._side_probe_distance(state, left=False)
        if not (left_score or right_score):
            return None
        delta = _angle_delta(_bearing(player["point"], target), player["angle"])
        if abs(delta) > 70.0:
            return None
        go_left = left_score >= right_score
        distance_units = int(_dist(player["point"], target) / FP_UNIT)
        side = 34 if distance_units <= 288 else 24
        forward = 46 if abs(delta) <= 35.0 else 36
        return PlanAction(
            skill=skill,
            action=agent_pb2.PlayerAction(
                duration_tics=12,
                raw=raw_cls(
                    forward_move=forward,
                    side_move=side if go_left else -side,
                    angle_turn=self._raw_steer_turn_units(delta),
                ),
            ),
            door_line_id=edge.line_id,
            detail={
                **detail,
                "action": "center_passable_portal_raw",
                "side": "left" if go_left else "right",
                "turn": round(delta, 1),
                "line": line.id,
                "mt": [int(target.x), int(target.y)],
            },
        )

    def _edge(self, a: Point, b: Point, door_memory: Any) -> tuple[float, int | None, bool] | None:
        blocked, door_lines = self._edge_crossings(a, b)
        if blocked:
            return None
        crossed_door: int | None = None
        for line_id in door_lines:
            if door_memory.is_open(line_id):
                continue
            if door_memory.is_blocked(line_id):
                return None
            crossed_door = line_id
        cost = _dist(a, b) / FP_UNIT
        if crossed_door is not None:
            cost += 80.0
        return cost, crossed_door, crossed_door is not None

    def _edge_crossings(self, a: Point, b: Point) -> tuple[bool, tuple[int, ...]]:
        key = (a.x, a.y, b.x, b.y)
        cached = self._edge_crossing_cache.get(key)
        if cached is not None:
            return cached
        door_lines: list[int] = []
        for line in self.lines:
            if not self._line_crosses_segment(line, a, b):
                continue
            if line.passable:
                # Static clearance is fine, but a raised lift/platform still walls off the
                # step-up direction. Route such crossings through the door machinery instead of
                # walking blind: USE-able lines get opened, and plain over-tall steps become
                # bounded probe attempts that door memory abandons if the floor never changed
                # (stair builders/floor raisers make static heights lie, so never hard-block).
                if self._step_up_fp(line, a) <= MAX_STEP_UP_FP:
                    continue
                door_lines.append(line.id)
                continue
            if line.door or line.use_trigger:
                door_lines.append(line.id)
                continue
            if line.blocking or line.sight_blocking or not line.passable:
                result = (True, ())
                self._edge_crossing_cache[key] = result
                return result
        result = (False, tuple(door_lines))
        self._edge_crossing_cache[key] = result
        return result

    def _sector_route_to_line_action(
        self,
        player: dict[str, Any],
        line: MapLineRuntime,
        agent_pb2: Any,
        door_memory: Any,
        *,
        exit_only: bool,
        state: Any | None = None,
    ) -> PlanAction | None:
        if not exit_only and self._is_e1m1_final_corridor_side_door(state, line.id):
            return None
        target_sectors = {sector for sector in (line.front_sector, line.back_sector) if sector >= 0}
        containing = self._sector_containing_point(line.midpoint)
        if containing is not None:
            target_sectors.add(containing)
        if not target_sectors:
            return None
        player_sector = self.sector_for_point_fp(player["point"].x, player["point"].y)
        skill = "press_exit" if exit_only else "open_use_line"
        detail = {
            "skill": "sector_route_to_exit_line" if exit_only else "sector_route_to_use_line",
            "line": line.id,
            "special": line.special,
            "tag": line.tag,
        }
        if player_sector in target_sectors:
            line_target = self._nearest_point_on_line(player["point"], line)
            if not exit_only and door_memory.is_open(line.id):
                through_target = self._line_use_target(player["point"], line)
                return self._turn_or_forward(
                    player,
                    through_target,
                    agent_pb2,
                    skill=skill,
                    detail={**detail, "action": "follow_opening", "state": door_memory.state_for(line.id)},
                    door_line_id=line.id,
                )
            if _dist(player["point"], line_target) <= USE_DISTANCE_FP:
                delta = _angle_delta(_bearing(player["point"], line_target), player["angle"])
                use_tolerance = 12.0 if exit_only else 20.0
                if abs(delta) <= use_tolerance:
                    door_memory.record_attempt(line.id, status="sector_line_use")
                    merged = dict(detail)
                    merged["action"] = "use"
                    return PlanAction(
                        skill=skill,
                        action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
                        door_line_id=line.id,
                        detail=merged,
                    )
                turn = self._turn(player, delta, agent_pb2, skill, detail, door_line_id=line.id)
                if exit_only:
                    turn.action.amount = max(int(getattr(turn.action, "amount", 0) or 0), 48)
                    turn.action.duration_tics = max(int(getattr(turn.action, "duration_tics", 1) or 1), 4)
                return turn
            return self._turn_or_forward(player, line_target, agent_pb2, skill=skill, detail=detail, door_line_id=line.id)
        route = self._sector_route_from_player(player, target_sectors, door_memory)
        if not route:
            return None
        self._last_status = "sector_route_to_exit_line" if exit_only else "sector_route_to_use_line"
        self._last_route_len = len(route)
        return self._portal_route_action(
            player,
            route,
            agent_pb2,
            door_memory,
            skill=skill,
            detail={**detail, "route": len(route)},
            state=state,
        )

    def _sector_reachable_set(self, start_sector: int, door_memory: Any) -> set[int]:
        reached: set[int] = set()
        heap: list[tuple[float, int]] = [(0.0, int(start_sector))]
        best: dict[int, float] = {int(start_sector): 0.0}
        while heap:
            cost, sector = heapq.heappop(heap)
            if cost != best.get(sector):
                continue
            reached.add(sector)
            for edge in self._portal_graph.get(sector, []):
                if edge.tag_gate:
                    tag_is_open = getattr(door_memory, "tag_is_open", None)
                    if not (callable(tag_is_open) and tag_is_open(edge.tag_gate)):
                        continue
                if door_memory.is_blocked(edge.line_id) or self.sector_is_damaging(edge.dst):
                    continue
                can_retry = getattr(door_memory, "can_retry", None)
                if (
                    callable(can_retry)
                    and not bool(can_retry(edge.line_id))
                    and not edge.passable
                    and not edge.exit
                    and not door_memory.is_open(edge.line_id)
                ):
                    continue
                new_cost = cost + edge.cost
                if new_cost < best.get(edge.dst, float("inf")):
                    best[edge.dst] = new_cost
                    heapq.heappush(heap, (new_cost, edge.dst))
        return set(best)

    def _route_endpoint_near(self, route: Route | None, target: Point, *, max_distance_fp: int) -> bool:
        if route is None or not route.points:
            return False
        return _dist(route.points[-1].point, target) <= int(max_distance_fp)

    def _mark_last_los_route_blocked(self) -> None:
        key = self._last_los_route_endpoint_key
        if key is None:
            return
        count = self._blocked_los_route_counts.get(key, 0) + 1
        self._blocked_los_route_counts[key] = count
        if count >= 2:
            self._blocked_los_route_keys.add(key)

    def _route_blocked_now(self, state: Any) -> bool:
        navigation = getattr(state, "navigation", None)
        if navigation is None:
            return False
        if bool(getattr(navigation, "forward_open", False)):
            return False
        front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
        return not front_distance or front_distance <= 80 * FP_UNIT

    def _final_route_recovery_probe_action(
        self,
        state: Any | None,
        player: dict[str, Any],
        target: Point,
        agent_pb2: Any,
        *,
        skill: str,
        detail: dict[str, Any],
        door_line_id: int | None,
        route_remaining: int,
    ) -> PlanAction | None:
        if skill != "recover_stuck" or state is None or route_remaining > 6:
            return None
        navigation = getattr(state, "navigation", None)
        if navigation is None:
            return None
        front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
        forward_open = bool(getattr(navigation, "forward_open", False)) and (
            not front_distance or front_distance > FINAL_DOOR_HARD_BLOCK_UNITS * FP_UNIT
        )
        left_score = self._side_probe_distance(state, left=True)
        right_score = self._side_probe_distance(state, left=False)
        if not forward_open and not (left_score or right_score):
            return None
        target_delta = _angle_delta(_bearing(player["point"], target), player["angle"])
        if abs(target_delta) <= 70.0 and forward_open:
            return None
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        go_left = left_score >= right_score
        side_move = 0
        if left_score or right_score:
            side_move = (28 if forward_open else 52) * (1 if go_left else -1)
        steer_delta = max(-25.0, min(25.0, target_delta))
        merged = {
            **detail,
            "action": "final_route_recovery_probe",
            "route": route_remaining,
            "turn": round(target_delta, 1),
            "steer": round(steer_delta, 1),
            "front": int(front_distance / FP_UNIT) if front_distance else 0,
            "side": "left" if side_move > 0 else ("right" if side_move < 0 else "none"),
        }
        if raw_cls is not None:
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(
                    duration_tics=10,
                    raw=raw_cls(
                        forward_move=52 if forward_open else 18,
                        side_move=side_move,
                        angle_turn=self._raw_steer_turn_units(steer_delta),
                    ),
                ),
                door_line_id=door_line_id,
                detail=merged,
            )
        if forward_open:
            return PlanAction(
                skill=skill,
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=42, duration_tics=10),
                door_line_id=door_line_id,
                detail=merged,
            )
        action_type = agent_pb2.ACTION_STRAFE_LEFT if side_move > 0 else agent_pb2.ACTION_STRAFE_RIGHT
        return PlanAction(
            skill=skill,
            action=agent_pb2.PlayerAction(action=action_type, amount=42, duration_tics=10),
            door_line_id=door_line_id,
            detail=merged,
        )
