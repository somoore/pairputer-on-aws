#!/usr/bin/env python3.11
"""Compact batched probe facade for the Agent DOOM brain.

The engine already computes local navigation, combat, use-line, and LOS probes
inside each GameState. This module batches those signals into tiny summaries for
the capsule-local FSM without exposing raw map geometry through MCP.
"""

from __future__ import annotations

from typing import Any

FP_UNIT = 65536.0


class ProbeBatcher:
    """Builds compact probe batches from live engine state plus the map planner."""

    def snapshot(self, state: Any, planner: Any | None = None) -> dict[str, Any]:
        return {
            "vis": self.batch_visibility(state, planner),
            "mov": self.batch_movement(state),
            "cmb": self.batch_combat(state),
            "use": self.batch_use_line(state),
        }

    def batch_visibility(self, state: Any, planner: Any | None = None, *, limit: int = 6) -> list[list[Any]]:
        player = _player_point(state)
        rows: list[list[Any]] = []
        for enemy in list(getattr(state, "enemies", []) or [])[:limit]:
            obj = getattr(enemy, "object", None)
            pos = getattr(obj, "position", None)
            if obj is None or pos is None or int(getattr(obj, "health", 0) or 0) <= 0:
                continue
            enemy_id = int(getattr(obj, "id", 0) or 0)
            engine_los = bool(getattr(enemy, "line_of_sight", False))
            map_los = False
            if planner is not None and player is not None:
                try:
                    from planner import Point

                    point = Point(int(getattr(pos, "x_fp", 0)), int(getattr(pos, "y_fp", 0)))
                except Exception:
                    point = None
                if point is not None:
                    try:
                        map_los = bool(planner.has_line_of_sight(player, point))
                    except Exception:
                        map_los = False
            rows.append([enemy_id, int(engine_los), int(map_los)])
        return rows

    def batch_movement(self, state: Any) -> dict[str, Any]:
        nav = getattr(state, "navigation", None)
        probes = []
        for probe in getattr(nav, "direction_probes", []) or []:
            probes.append(
                [
                    int(getattr(probe, "angle_offset_degrees", 0) or 0),
                    int(bool(getattr(probe, "open", False))),
                    int(int(getattr(probe, "block_distance_fp", 0) or 0) / FP_UNIT),
                    int(bool(getattr(probe, "use_line_ahead", False))),
                ]
            )
        return {
            "open": "".join(k[0] for k in ("forward", "back", "left", "right") if bool(getattr(nav, f"{k}_open", False))),
            "front": int(int(getattr(nav, "front_block_distance_fp", 0) or 0) / FP_UNIT),
            "probe": probes[:9],
        }

    def batch_combat(self, state: Any) -> dict[str, Any]:
        combat = getattr(state, "combat", None)
        return {
            "shootable": int(bool(getattr(combat, "has_shootable_target", False) and getattr(combat, "target_is_enemy", False))),
            "target": int(getattr(combat, "target_id", 0) or 0),
            "hp": int(getattr(combat, "target_health", 0) or 0),
            "dist": int(int(getattr(combat, "target_distance_fp", 0) or 0) / FP_UNIT),
        }

    def batch_use_line(self, state: Any, *, limit: int = 4) -> list[list[int]]:
        nav = getattr(state, "navigation", None)
        rows = []
        for line in getattr(nav, "use_lines", []) or []:
            line_id = int(getattr(line, "line_id", -1) or -1)
            if line_id < 0:
                continue
            distance = int(getattr(line, "nearest_distance_fp", 0) or getattr(line, "distance_fp", 0) or 0)
            rows.append(
                [
                    line_id,
                    int(getattr(line, "special", 0) or 0),
                    int(getattr(line, "tag", 0) or 0),
                    int(distance / FP_UNIT),
                ]
            )
        rows.sort(key=lambda item: item[3])
        return rows[:limit]


def _player_point(state: Any) -> Any | None:
    player = getattr(state, "player", None)
    obj = getattr(player, "object", None)
    pos = getattr(obj, "position", None)
    if pos is None:
        return None
    try:
        from planner import Point

        return Point(int(getattr(pos, "x_fp", 0)), int(getattr(pos, "y_fp", 0)))
    except Exception:
        return None
