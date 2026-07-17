#!/usr/bin/env python3.11
"""Compact world-state memory for the Agent DOOM capsule brain."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorldMemory:
    """Tracks level knowledge that should survive across objective-driver steps."""

    visited_sectors: set[int] = field(default_factory=set)
    visible_enemy_sectors: set[int] = field(default_factory=set)
    seen_use_lines: set[int] = field(default_factory=set)
    frontier_sectors: set[int] = field(default_factory=set)
    blocked_frontier_sectors: set[int] = field(default_factory=set)
    los_probe_targets: set[int] = field(default_factory=set)
    acquired_keys: set[str] = field(default_factory=set)
    consumed_health_items: set[int] = field(default_factory=set)
    last_sector: int | None = None
    last_health: int | None = None
    last_update: int = 0

    def update(self, state: Any, planner: Any | None) -> None:
        if planner is None:
            return
        player = planner.player_from_state(state)
        if player is None:
            return
        sector_id = planner.sector_for_player(state, player)
        if sector_id is not None and sector_id >= 0:
            self.last_sector = int(sector_id)
            self.visited_sectors.add(int(sector_id))
            self.frontier_sectors.update(planner.neighbor_sector_ids(int(sector_id)))
            self.frontier_sectors.difference_update(self.visited_sectors)
            self.frontier_sectors.difference_update(self.blocked_frontier_sectors)

        navigation = getattr(state, "navigation", None)
        for line in getattr(navigation, "use_lines", []) or []:
            line_id = int(getattr(line, "line_id", -1))
            if line_id >= 0:
                self.seen_use_lines.add(line_id)

        for enemy in getattr(state, "enemies", []) or []:
            if not bool(getattr(enemy, "line_of_sight", False)):
                continue
            obj = getattr(enemy, "object", None)
            pos = getattr(obj, "position", None)
            if pos is None:
                continue
            enemy_sector = planner.sector_for_point_fp(int(getattr(pos, "x_fp", 0)), int(getattr(pos, "y_fp", 0)))
            if enemy_sector is not None and enemy_sector >= 0:
                self.visible_enemy_sectors.add(int(enemy_sector))
        self.acquired_keys.update(_inventory_key_colors(getattr(state, "player", None)))
        self.acquired_keys.update(_nearby_key_colors(player, planner))
        player_state = getattr(state, "player", None)
        health = int(getattr(player_state, "health", 0) or 0)
        touching_health = _nearby_health_item_ids(player, planner, max_distance_fp=36 * 65536)
        if self.last_health is not None and health > self.last_health:
            nearby_health = _nearby_health_item_ids(player, planner, max_distance_fp=72 * 65536)
            self.consumed_health_items.update(nearby_health)
        elif touching_health and health < 100:
            self.consumed_health_items.update(touching_health)
        self.last_health = health
        self.last_update = int(time.time())

    def frontier_targets(self, planner: Any) -> list[int]:
        targets = [
            sector for sector in self.frontier_sectors
            if sector not in self.blocked_frontier_sectors and not planner.sector_is_damaging(sector)
        ]
        if targets:
            return sorted(targets)
        if self.last_sector is not None:
            return sorted(
                sector
                for sector in planner.neighbor_sector_ids(self.last_sector)
                if (
                    sector not in self.visited_sectors
                    and sector not in self.blocked_frontier_sectors
                    and not planner.sector_is_damaging(sector)
                )
            )
        return []

    def record_frontier_blocked(self, sector_id: int | None) -> None:
        if sector_id is None:
            return
        sector = int(sector_id)
        self.blocked_frontier_sectors.add(sector)
        self.frontier_sectors.discard(sector)

    def record_health_pickup_reached(self, thing_id: int | None) -> None:
        if thing_id is None:
            return
        self.consumed_health_items.add(int(thing_id))

    def claim_los_probe_fire(self, enemy_id: int) -> bool:
        enemy_id = int(enemy_id)
        if enemy_id in self.los_probe_targets:
            return False
        self.los_probe_targets.add(enemy_id)
        return True

    def summary(self) -> dict[str, Any]:
        return {
            "visited": len(self.visited_sectors),
            "frontier": len(self.frontier_sectors),
            "blocked_frontier": len(self.blocked_frontier_sectors),
            "seen_use": len(self.seen_use_lines),
            "enemy_sectors": len(self.visible_enemy_sectors),
            **({"keys": sorted(self.acquired_keys)} if self.acquired_keys else {}),
            **({"health_items": len(self.consumed_health_items)} if self.consumed_health_items else {}),
            **({"sector": int(self.last_sector)} if self.last_sector is not None else {}),
        }


def _inventory_key_colors(player: Any) -> set[str]:
    mask = int(getattr(player, "key_cards", 0) or 0)
    colors: set[str] = set()
    if mask & ((1 << 0) | (1 << 3)):
        colors.add("blue")
    if mask & ((1 << 1) | (1 << 4)):
        colors.add("yellow")
    if mask & ((1 << 2) | (1 << 5)):
        colors.add("red")
    return colors


def _nearby_key_colors(player: dict[str, Any], planner: Any) -> set[str]:
    point = player.get("point") if isinstance(player, dict) else None
    if point is None:
        return set()
    colors: set[str] = set()
    for item in getattr(planner, "key_items", []) or []:
        item_point = item.get("point") if isinstance(item, dict) else getattr(item, "point", None)
        color = item.get("color") if isinstance(item, dict) else getattr(item, "color", "")
        if item_point is None or not color:
            continue
        distance = ((int(point.x) - int(item_point.x)) ** 2 + (int(point.y) - int(item_point.y)) ** 2) ** 0.5
        if distance <= 72 * 65536:
            colors.add(str(color))
    return colors


def _nearby_health_item_ids(player: dict[str, Any], planner: Any, *, max_distance_fp: int) -> set[int]:
    point = player.get("point") if isinstance(player, dict) else None
    if point is None:
        return set()
    found: set[int] = set()
    for item in getattr(planner, "health_items", []) or []:
        item_point = item.get("point") if isinstance(item, dict) else getattr(item, "point", None)
        thing_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        if item_point is None or thing_id is None:
            continue
        distance = ((int(point.x) - int(item_point.x)) ** 2 + (int(point.y) - int(item_point.y)) ** 2) ** 0.5
        if distance <= int(max_distance_fp):
            found.add(int(thing_id))
    return found
