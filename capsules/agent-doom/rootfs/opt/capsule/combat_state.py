#!/usr/bin/env python3.11
"""Small combat state machine for Agent DOOM."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CombatState:
    phase: str = "idle"
    target_id: int | None = None
    shots: int = 0
    lost: int = 0
    last_kills: int = 0

    def update(self, state: Any) -> None:
        player = getattr(state, "player", None)
        combat = getattr(state, "combat", None)
        kills = int(getattr(player, "kills", self.last_kills) or 0)
        if kills > self.last_kills:
            self.phase = "killed"
            self.shots = 0
        self.last_kills = kills

        if bool(getattr(combat, "has_shootable_target", False) and getattr(combat, "target_is_enemy", False)):
            self.phase = "shootable"
            self.target_id = int(getattr(combat, "target_id", self.target_id or 0) or 0)
            self.lost = 0
            return

        visible = False
        nearest_id = None
        nearest_distance = None
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            if obj is None or int(getattr(obj, "health", 0)) <= 0:
                continue
            distance = int(getattr(obj, "distance_fp", 0) or 0)
            if nearest_distance is None or distance < nearest_distance:
                nearest_distance = distance
                nearest_id = int(getattr(obj, "id", 0))
            visible = visible or bool(getattr(enemy, "line_of_sight", False))
        if visible:
            self.phase = "visible_reposition"
            self.target_id = nearest_id
            self.lost = 0
        elif nearest_id is not None:
            self.phase = "route_to_contact"
            self.target_id = nearest_id
            self.lost += 1
        else:
            self.phase = "search"
            self.target_id = None
            self.lost += 1

    def record_fire(self) -> None:
        self.shots += 1
        self.phase = "firing"

    def summary(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "shots": self.shots,
            "lost": self.lost,
            **({"target": int(self.target_id)} if self.target_id is not None else {}),
        }
