#!/usr/bin/env python3.11
"""Threat pricing and no-kill evasion for the Agent DOOM spatial planner.

Owns how the planner prices enemy threat into routes and how it evades
instead of shooting: point/sector/portal threat multipliers with their caps,
threat metadata and per-step route detail, probe-threat summaries, and the
no-kill route evasion (LOS-break, exit-commit, lateral-probe) machinery.
Extracted verbatim from planner.SpatialPlanner.

ThreatPricingMixin is a mixin over SpatialPlanner state: every method runs on
the SpatialPlanner instance, reads/writes attributes initialized in
SpatialPlanner.__init__, and calls shared SpatialPlanner helpers via self. It
holds no state of its own. This module must not import planner at runtime.
"""

from __future__ import annotations

from typing import Any, Iterable

from planner_model import (
    EXIT_SPECIALS,
    FP_UNIT,
    LOW_TIER_HITSCAN_TYPE_IDS,
    NO_KILL_CLOSE_BLOCKER_HEALTH,
    NO_KILL_CLOSE_BLOCKER_UNITS,
    NO_KILL_EXIT_COMMIT_UNITS,
    NO_KILL_FORWARD_CLEAR_UNITS,
    NO_KILL_LOS_BREAK_HEALTH,
    PlanAction,
    Point,
    PortalEdge,
    RETRIED_DOOR_RETREAT_HEALTH,
    THREAT_ROUTE_CONFIDENT_LOW_TIER_CAP,
    THREAT_ROUTE_NORMAL_CAP,
    THREAT_ROUTE_NORMAL_TARGET_CAP,
    THREAT_ROUTE_NO_KILL_CAP,
    THREAT_ROUTE_NO_KILL_TARGET_CAP,
    _angle_delta,
    _bearing,
    _dist,
)
from threat_model import classify_enemy, object_type_id


class ThreatPricingMixin:
    """Threat pricing/no-kill evasion over SpatialPlanner state (see module docstring)."""

    def _point_threat_multiplier(self, point: Point, *, target: bool = False) -> float:
        if not self._threats:
            return 1.0
        mode = str(getattr(self, "_threat_cost_mode", "route") or "route")
        route_confident = bool(getattr(self, "_route_confident", False))
        key = (point.x, point.y, target, mode, route_confident)
        cached = self._threat_mult_cache.get(key)
        if cached is not None:
            return cached
        multiplier = 1.0
        uncapped_threat = False
        for threat in self._threats:
            enemy_point = threat["point"]
            distance_units = _dist(point, enemy_point) / FP_UNIT
            contribution = 0.0
            if distance_units <= 96.0:
                contribution = max(contribution, 49.0 if mode == "no_kill" else 9.0)
            elif distance_units <= 192.0:
                contribution = max(contribution, 24.0 if mode == "no_kill" else 5.0)
            elif distance_units <= 384.0:
                contribution = max(contribution, 11.0 if mode == "no_kill" else 2.0)
            elif distance_units <= 640.0:
                contribution = max(contribution, 4.0 if mode == "no_kill" else 0.75)
            if threat.get("threat") == "hitscan" and distance_units <= 1400.0 and self.has_line_of_sight(point, enemy_point):
                contribution = max(contribution, 99.0 if mode == "no_kill" else 49.0)
            if contribution > 0.0 and not (
                mode == "route"
                and route_confident
                and threat.get("threat") == "hitscan"
                and int(threat.get("type_id", 0) or 0) in LOW_TIER_HITSCAN_TYPE_IDS
            ):
                uncapped_threat = True
            multiplier += contribution
        result = min(multiplier, self._threat_target_cap()) if target else min(multiplier, self._threat_cap())
        if mode == "route" and route_confident and not target and not uncapped_threat:
            result = min(result, THREAT_ROUTE_CONFIDENT_LOW_TIER_CAP)
        self._threat_mult_cache[key] = result
        return result

    def _sector_threat_multiplier(self, sector_id: int, *, target: bool = False) -> float:
        sector = self.sectors.get(int(sector_id))
        if sector is None:
            return 1.0
        multiplier = self._point_threat_multiplier(sector.center, target=target)
        return min(multiplier, self._threat_target_cap()) if target else multiplier

    def _portal_threat_multiplier(self, edge: PortalEdge, *, target: bool = False) -> float:
        multiplier = self._sector_threat_multiplier(edge.dst, target=target)
        # Doorway clusters often sit on the portal linedef while the destination sector center
        # remains far away. Score the portal midpoint uncapped so A* treats crowded thresholds
        # as expensive instead of repeatedly steering into them.
        return max(multiplier, self._point_threat_multiplier(edge.point, target=False))

    def _threat_cap(self) -> float:
        return THREAT_ROUTE_NO_KILL_CAP if self._threat_cost_mode == "no_kill" else THREAT_ROUTE_NORMAL_CAP

    def _threat_target_cap(self) -> float:
        return THREAT_ROUTE_NO_KILL_TARGET_CAP if self._threat_cost_mode == "no_kill" else THREAT_ROUTE_NORMAL_TARGET_CAP

    def _threat_metadata(self, state: Any, player: dict[str, Any]) -> list[dict[str, Any]]:
        threats: list[dict[str, Any]] = []
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            pos = getattr(obj, "position", None)
            if obj is None or pos is None or int(getattr(obj, "health", 0) or 0) <= 0:
                continue
            point = Point(int(getattr(pos, "x_fp", 0)), int(getattr(pos, "y_fp", 0)))
            threat = classify_enemy(enemy)
            type_id = object_type_id(enemy)
            threats.append(
                {
                    "id": int(getattr(obj, "id", 0) or 0),
                    "point": point,
                    "sector": self.sector_for_point_fp(point.x, point.y),
                    "threat": threat,
                    "type_id": type_id,
                    "distance_fp": int(getattr(obj, "distance_fp", 0) or _dist(player["point"], point)),
                }
            )
        return threats

    def _route_step_detail(
        self,
        detail: dict[str, Any],
        *,
        kind: str,
        threat_multiplier: float,
        target: Point | None = None,
        line_id: int | None = None,
        use_line: bool | None = None,
        sector: int | None = None,
        special: int | None = None,
        route_remaining: int | None = None,
    ) -> dict[str, Any]:
        merged = {
            **detail,
            "route_step_kind": kind,
            "route_step_threat_mult": round(float(threat_multiplier), 2),
        }
        if target is not None:
            merged.update({"route_step_x": int(target.x), "route_step_y": int(target.y)})
        if line_id is not None:
            merged["route_step_line"] = int(line_id)
        if use_line is not None:
            merged["route_step_use_line"] = int(bool(use_line))
        if sector is not None:
            merged["route_step_sector"] = int(sector)
        if special is not None:
            merged["route_step_special"] = int(special)
        if route_remaining is not None:
            merged["route_remaining"] = int(route_remaining)
        return merged

    def _current_probe_threats(self, state: Any, player: dict[str, Any]) -> list[dict[str, Any]]:
        threats: list[dict[str, Any]] = []
        combat = getattr(state, "combat", None)
        combat_target = int(getattr(combat, "target_id", 0) or 0)
        shootable = bool(getattr(combat, "has_shootable_target", False) and getattr(combat, "target_is_enemy", False))
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            pos = getattr(obj, "position", None)
            if obj is None or pos is None or int(getattr(obj, "health", 0) or 0) <= 0:
                continue
            point = Point(int(getattr(pos, "x_fp", 0)), int(getattr(pos, "y_fp", 0)))
            enemy_id = int(getattr(obj, "id", 0) or 0)
            threats.append(
                {
                    "id": enemy_id,
                    "bearing": _bearing(player["point"], point),
                    "distance_units": int(float(getattr(obj, "distance_fp", 0) or _dist(player["point"], point)) / FP_UNIT),
                    "los": bool(getattr(enemy, "line_of_sight", False)) or bool(shootable and combat_target == enemy_id),
                    "threat": classify_enemy(enemy),
                }
            )
        return threats

    def _threat_bearing_spread(self, bearings: Iterable[float]) -> float:
        values = sorted(float(value) % 360.0 for value in bearings)
        if len(values) < 2:
            return 0.0
        gaps = [values[idx + 1] - values[idx] for idx in range(len(values) - 1)]
        gaps.append((values[0] + 360.0) - values[-1])
        return max(0.0, 360.0 - max(gaps))

    def _no_kill_route_evasion(
        self,
        state: Any,
        player: dict[str, Any],
        agent_pb2: Any,
        *,
        blocking_only: bool = False,
    ) -> PlanAction | None:
        enemy = self._nearest_enemy(state, player)
        if enemy is None or not bool(enemy.get("line_of_sight")):
            return None
        distance_units = int(float(enemy.get("distance_fp", 0) or 0) / FP_UNIT)
        delta = _angle_delta(_bearing(player["point"], enemy["point"]), player["angle"])
        navigation = getattr(state, "navigation", None)
        front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
        forward_clear = bool(getattr(navigation, "forward_open", False)) and (
            not front_distance or front_distance >= NO_KILL_FORWARD_CLEAR_UNITS * FP_UNIT
        )
        player_state = getattr(state, "player", None)
        health = int(getattr(player_state, "health", 100) or 100)
        nearest_exit_units = self._nearest_exit_distance_units(player)
        near_exit_commit = nearest_exit_units is not None and nearest_exit_units <= NO_KILL_EXIT_COMMIT_UNITS
        shootable_pressure = health <= RETRIED_DOOR_RETREAT_HEALTH and bool(self._shootable(state))
        los_break_health = NO_KILL_CLOSE_BLOCKER_HEALTH if blocking_only else NO_KILL_LOS_BREAK_HEALTH
        close_visible_pressure = health <= los_break_health and distance_units <= 384
        high_pressure_los_break = shootable_pressure or close_visible_pressure
        critical_los_break = high_pressure_los_break or (health <= los_break_health and (
            nearest_exit_units is None or not near_exit_commit
        ))
        if blocking_only and not critical_los_break and not (distance_units <= 56 and abs(delta) <= 50.0 and not forward_clear):
            return None
        if distance_units > 144:
            if (
                (not blocking_only or critical_los_break)
                and bool(enemy.get("line_of_sight"))
                and str(enemy.get("threat") or "unknown") in {"hitscan", "unknown"}
                and distance_units <= 1400
                and forward_clear
            ):
                left_open = self._side_probe_open(state, left=True)
                right_open = self._side_probe_open(state, left=False)
                if critical_los_break:
                    if left_open or right_open:
                        go_left = left_open and (not right_open or self._side_probe_distance(state, left=True) >= self._side_probe_distance(state, left=False))
                        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
                        detail = {
                            "skill": "no_kill_route_evasion",
                            "action": "break_los_low_health",
                            "enemy": enemy["id"],
                            "dist": distance_units,
                            "hp": health,
                            "side": "left" if go_left else "right",
                        }
                        if raw_cls is not None:
                            forward_move = 54
                            if blocking_only and not near_exit_commit:
                                forward_move = -26 if bool(getattr(navigation, "back_open", False)) else 0
                            return PlanAction(
                                skill="route_progression",
                                action=agent_pb2.PlayerAction(
                                    duration_tics=8,
                                    raw=raw_cls(forward_move=forward_move, side_move=48 if go_left else -48),
                                ),
                                detail={**detail, "mode": "low_health_forward" if forward_move > 0 else "low_health_lateral"},
                            )
                        action_type = agent_pb2.ACTION_STRAFE_LEFT if go_left else agent_pb2.ACTION_STRAFE_RIGHT
                        return PlanAction(
                            skill="route_progression",
                            action=agent_pb2.PlayerAction(action=action_type, amount=42, duration_tics=8),
                            detail=detail,
                        )
                    if bool(getattr(navigation, "back_open", False)):
                        return PlanAction(
                            skill="route_progression",
                            action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=42, duration_tics=8),
                            detail={
                                "skill": "no_kill_route_evasion",
                                "action": "break_los_low_health",
                                "enemy": enemy["id"],
                                "dist": distance_units,
                                "hp": health,
                                "mode": "low_health_back",
                            },
                        )
                if not (left_open or right_open):
                    return None
                if left_open and right_open:
                    left_score = self._side_probe_distance(state, left=True)
                    right_score = self._side_probe_distance(state, left=False)
                    go_left = left_score >= right_score if abs(delta) <= 8.0 else delta < 0
                else:
                    go_left = left_open
                raw_cls = getattr(agent_pb2, "RawTiccmd", None)
                detail = {
                    "skill": "no_kill_route_evasion",
                    "action": "exposure_sprint",
                    "enemy": enemy["id"],
                    "dist": distance_units,
                    "turn": round(delta, 1),
                    "side": "left" if go_left else "right",
                }
                if raw_cls is not None:
                    return PlanAction(
                        skill="route_progression",
                        action=agent_pb2.PlayerAction(
                            duration_tics=6,
                            raw=raw_cls(
                                forward_move=56,
                                side_move=34 if go_left else -34,
                                angle_turn=self._raw_steer_turn_units(delta),
                            ),
                        ),
                        detail={**detail, "mode": "sprint"},
                    )
                action_type = agent_pb2.ACTION_STRAFE_LEFT if go_left else agent_pb2.ACTION_STRAFE_RIGHT
                return PlanAction(
                    skill="route_progression",
                    action=agent_pb2.PlayerAction(action=action_type, amount=34, duration_tics=6),
                    detail={**detail, "mode": "sprint"},
                )
            return None
        if abs(delta) > 80.0 and distance_units > 80:
            return None
        if distance_units <= 56 and abs(delta) <= 45.0 and bool(getattr(navigation, "back_open", False)) and not forward_clear:
            return PlanAction(
                skill="route_progression",
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=46, duration_tics=10),
                detail={"skill": "no_kill_route_evasion", "action": "bait_back", "enemy": enemy["id"], "dist": distance_units},
            )
        left_open = self._side_probe_open(state, left=True)
        right_open = self._side_probe_open(state, left=False)
        if not (left_open or right_open):
            if forward_clear:
                raw_cls = getattr(agent_pb2, "RawTiccmd", None)
                detail = {
                    "skill": "no_kill_route_evasion",
                    "action": "run_past",
                    "enemy": enemy["id"],
                    "dist": distance_units,
                    "turn": round(delta, 1),
                    "mode": "forward_rush",
                }
                if raw_cls is not None:
                    return PlanAction(
                        skill="route_progression",
                        action=agent_pb2.PlayerAction(
                            duration_tics=6,
                            raw=raw_cls(forward_move=58, angle_turn=self._raw_steer_turn_units(delta)),
                        ),
                        detail=detail,
                    )
                return PlanAction(
                    skill="route_progression",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=48, duration_tics=6),
                    detail=detail,
                )
            if bool(getattr(navigation, "back_open", False)):
                return PlanAction(
                    skill="route_progression",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=42, duration_tics=10),
                    detail={"skill": "no_kill_route_evasion", "action": "back", "enemy": enemy["id"], "dist": distance_units},
                )
            return self._turn(
                player,
                -45.0 if delta >= 0 else 45.0,
                agent_pb2,
                "route_progression",
                {"skill": "no_kill_route_evasion", "action": "turn_away", "enemy": enemy["id"], "dist": distance_units},
            )
        if left_open and right_open:
            left_score = self._side_probe_distance(state, left=True)
            right_score = self._side_probe_distance(state, left=False)
            if abs(delta) <= 8.0:
                go_left = left_score >= right_score
            else:
                go_left = delta < 0
        else:
            go_left = left_open
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if health <= NO_KILL_CLOSE_BLOCKER_HEALTH and distance_units <= NO_KILL_CLOSE_BLOCKER_UNITS:
            detail = {
                "skill": "no_kill_route_evasion",
                "action": "panic_sidestep_close_blocker",
                "enemy": enemy["id"],
                "dist": distance_units,
                "turn": round(delta, 1),
                "side": "left" if go_left else "right",
            }
            if raw_cls is not None:
                forward_move = 50 if forward_clear else 0
                side_move = 54 if forward_clear else 62
                return PlanAction(
                    skill="route_progression",
                    action=agent_pb2.PlayerAction(
                        duration_tics=10,
                        raw=raw_cls(
                            forward_move=forward_move,
                            side_move=side_move if go_left else -side_move,
                            angle_turn=self._raw_steer_turn_units(delta),
                        ),
                    ),
                    detail={**detail, "mode": "close_lateral"},
                )
            action_type = agent_pb2.ACTION_STRAFE_LEFT if go_left else agent_pb2.ACTION_STRAFE_RIGHT
            return PlanAction(
                skill="route_progression",
                action=agent_pb2.PlayerAction(action=action_type, amount=54, duration_tics=10),
                detail={**detail, "mode": "close_lateral"},
            )
        if health <= NO_KILL_LOS_BREAK_HEALTH and forward_clear and (
            not blocking_only or near_exit_commit or not (left_open or right_open or bool(getattr(navigation, "back_open", False)))
        ):
            detail = {
                "skill": "no_kill_route_evasion",
                "action": "panic_run_past",
                "enemy": enemy["id"],
                "dist": distance_units,
                "turn": round(delta, 1),
                "side": "left" if go_left else "right",
            }
            if raw_cls is not None:
                return PlanAction(
                    skill="route_progression",
                    action=agent_pb2.PlayerAction(
                        duration_tics=10,
                        raw=raw_cls(
                            forward_move=62,
                            side_move=34 if go_left else -34,
                            angle_turn=self._raw_steer_turn_units(delta),
                        ),
                    ),
                    detail={**detail, "mode": "panic_forward"},
                )
            action_type = agent_pb2.ACTION_FORWARD
            return PlanAction(
                skill="route_progression",
                action=agent_pb2.PlayerAction(action=action_type, amount=54, duration_tics=10),
                detail={**detail, "mode": "panic_forward"},
            )
        if health <= NO_KILL_LOS_BREAK_HEALTH:
            detail = {
                "skill": "no_kill_route_evasion",
                "action": "panic_escape_side",
                "enemy": enemy["id"],
                "dist": distance_units,
                "turn": round(delta, 1),
                "side": "left" if go_left else "right",
            }
            if raw_cls is not None:
                return PlanAction(
                    skill="route_progression",
                    action=agent_pb2.PlayerAction(
                        duration_tics=10,
                        raw=raw_cls(
                            forward_move=-34 if bool(getattr(navigation, "back_open", False)) else 0,
                            side_move=62 if go_left else -62,
                            angle_turn=self._raw_steer_turn_units(delta),
                        ),
                    ),
                    detail={**detail, "mode": "panic_lateral"},
                )
            action_type = agent_pb2.ACTION_STRAFE_LEFT if go_left else agent_pb2.ACTION_STRAFE_RIGHT
            return PlanAction(
                skill="route_progression",
                action=agent_pb2.PlayerAction(action=action_type, amount=54, duration_tics=10),
                detail={**detail, "mode": "panic_lateral"},
            )
        detail = {
            "skill": "no_kill_route_evasion",
            "action": "strafe_past",
            "enemy": enemy["id"],
            "dist": distance_units,
            "turn": round(delta, 1),
            "side": "left" if go_left else "right",
        }
        if raw_cls is not None:
            forward_move = 50 if forward_clear else 0
            side_move = 50 if not forward_clear else 40
            return PlanAction(
                skill="route_progression",
                action=agent_pb2.PlayerAction(
                    duration_tics=8 if not forward_clear else 6,
                    raw=raw_cls(forward_move=forward_move, side_move=side_move if go_left else -side_move),
                ),
                detail={**detail, "mode": "side_step" if not forward_clear else "run_past"},
            )
        action_type = agent_pb2.ACTION_STRAFE_LEFT if go_left else agent_pb2.ACTION_STRAFE_RIGHT
        return PlanAction(
            skill="route_progression",
            action=agent_pb2.PlayerAction(action=action_type, amount=42 if not forward_clear else 34, duration_tics=8),
            detail={**detail, "mode": "side_step" if not forward_clear else "run_past"},
        )

    def _nearest_exit_distance_units(self, player: dict[str, Any]) -> int | None:
        distances: list[float] = []
        for line in self.lines:
            if not (line.exit or line.special in EXIT_SPECIALS):
                continue
            point = self._nearest_point_on_line(player["point"], line)
            distances.append(_dist(player["point"], point) / FP_UNIT)
        if not distances:
            return None
        return int(min(distances))

    def _best_lateral_probe(self, navigation: Any) -> int:
        candidates = []
        for probe in getattr(navigation, "direction_probes", []) or []:
            if not bool(getattr(probe, "open", False)):
                continue
            offset = int(getattr(probe, "angle_offset_degrees", 0) or 0)
            if abs(offset) < 45 or abs(offset) > 135:
                continue
            distance = int(getattr(probe, "block_distance_fp", 0) or 0)
            candidates.append((distance, offset))
        if not candidates:
            return 0
        _distance, offset = max(candidates)
        return int(offset)
