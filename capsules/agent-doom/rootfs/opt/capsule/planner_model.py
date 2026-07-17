#!/usr/bin/env python3.11
"""Data model for the Agent DOOM spatial planner.

Owns the planner's shared vocabulary: tuning constants (route/threat caps,
line-special sets, thing-type tables), the frozen map/route dataclasses
(Point, PlanAction, MapLineRuntime, SectorRuntime, PortalEdge, NavCellRuntime,
RouteStep, Route, KeyItemRuntime, HealthItemRuntime, BarrelRuntime), and the
pure point/segment geometry helpers. Extracted verbatim from planner.py,
which re-exports these names for existing importers. This module must not
import planner at runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

FP_UNIT = 65536.0
CELL_UNITS = 96
MAX_GRID_NODES = 1200
USE_DISTANCE_FP = int(64 * FP_UNIT)
RAW_TURN_SCALE = 64
RAW_TURN_CAP = 4096
RAW_STEER_TURN_SCALE = 24
RAW_STEER_TURN_CAP = 1536
MELEE_RUSH_THREATS = {"melee", "melee_rush"}
NO_KILL_LOS_BREAK_HEALTH = 70
NO_KILL_EXIT_COMMIT_UNITS = 384
NO_KILL_FORWARD_CLEAR_UNITS = 96
NO_KILL_CLOSE_BLOCKER_HEALTH = 45
NO_KILL_CLOSE_BLOCKER_UNITS = 96
RETRIED_DOOR_RETREAT_HEALTH = 80
FINAL_DOOR_COMMIT_ROUTE = 5
FINAL_DOOR_CRITICAL_HEALTH = 35
FINAL_DOOR_HARD_BLOCK_UNITS = 24
REMEMBERED_EXIT_PROBE_MAX_UNITS = 768
REMEMBERED_PROGRESSION_PROBE_MAX_UNITS = 640
REMEMBERED_PROBE_DANGER_HEALTH = 70
REMEMBERED_PROBE_SHOOTABLE_HEALTH = 85
REMEMBERED_PROBE_CONTACT_UNITS = 448
REMEMBERED_PROBE_CROSSFIRE_UNITS = 1024
REMEMBERED_PROBE_CROSSFIRE_DEGREES = 45.0
THREAT_ROUTE_NORMAL_CAP = 90.0
THREAT_ROUTE_NO_KILL_CAP = 250.0
THREAT_ROUTE_NORMAL_TARGET_CAP = 6.0
THREAT_ROUTE_NO_KILL_TARGET_CAP = 50.0
ROUTE_THREAT_REFUSE_MULT = 30.0
LOW_TIER_HITSCAN_TYPE_IDS = {3004}
THREAT_ROUTE_CONFIDENT_LOW_TIER_CAP = 24.0
# Break off a fight after this many consecutive steps on ONE enemy without killing it
# (and advance toward the next target / exit). Kills the "orbit one guy 9+ steps" stuck.
COMBAT_STALEMATE_STEPS = 6
HEALTH_SEEK_TRIGGER = 40
HEALTH_SEEK_RELEASE = 60
HEALTH_SEEK_MAX_TARGET_THREAT = ROUTE_THREAT_REFUSE_MULT
EXIT_SPECIALS = {11, 51, 52, 124, 197}
# Vanilla Doom/Doom II linedef activation types 1-141 (doomwiki.org "Linedef type" table):
# D = USE opens the door line itself, S = tagged switch (S1/SR, USE), W = walk-over (W1/WR),
# G = gun. MUST STAY IN SYNC with the copies in wad_map.py and door_memory.py — the capsule
# deploys these modules as standalone files, so the sets are duplicated instead of imported.
D_SPECIALS = frozenset({1, 26, 27, 28, 31, 32, 33, 34, 117, 118})
S_SPECIALS = frozenset({
    7, 9, 11, 14, 15, 18, 20, 21, 23, 29, 41, 42, 43, 45, 49, 50, 51, 55,
    60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 99, 101, 102, 103,
    111, 112, 113, 114, 115, 116, 122, 123, 127, 131, 132, 133, 134, 135, 136, 137,
    138, 139, 140,
})
W_SPECIALS = frozenset({
    2, 3, 4, 5, 6, 8, 10, 12, 13, 16, 17, 19, 22, 25, 30, 35, 36, 37, 38, 39, 40,
    44, 52, 53, 54, 56, 57, 58, 59, 72, 73, 74, 75, 76, 77, 79, 80, 81, 82, 83, 84,
    86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 100, 104, 105, 106, 107,
    108, 109, 110, 119, 120, 121, 124, 125, 126, 128, 129, 130, 141,
})
# Lifts/platforms: S-type specials are USE-activated switches on the line itself, W-type fire
# on walk-over. A raised lift reads as an impossible step in static WAD geometry, so routing
# treats its USE lines like doors: approach, press USE, cross once the floor lowers.
LIFT_USE_SPECIALS = {21, 62, 122, 123}
LIFT_WALK_SPECIALS = {10, 88, 120, 121}
LIFT_SPECIALS = LIFT_USE_SPECIALS | LIFT_WALK_SPECIALS
WALK_TRIGGER_SPECIALS = W_SPECIALS
USE_TRIGGER_SPECIALS = D_SPECIALS | S_SPECIALS
# Doom movement clips any step-up taller than 24 units; drops of any height are walkable.
MAX_STEP_UP_FP = 24 * 65536
KEY_THING_TYPES = {
    5: "blue",
    40: "blue",
    6: "yellow",
    39: "yellow",
    13: "red",
    38: "red",
}
HEALTH_THING_TYPES = {
    2011: ("stimpack", 10),
    2012: ("medkit", 25),
    2013: ("soulsphere", 100),
    2014: ("health_bonus", 1),
    2023: ("berserk", 100),
}
BARREL_THING_TYPES = {2035}
BARREL_BLAST_UNITS = 144
BARREL_SAFE_UNITS = 192
BARREL_MAX_SHOT_UNITS = 1024
BARREL_ALIGN_DEGREES = 8.0
# Blast radius (128) + margin: barrels chain-detonate and blast damage is
# LOS-limited, so barrel play is forbidden while the player stands within
# reach of ANY LOS-visible barrel — not just the one being shot.
BARREL_SELF_CHAIN_GUARD_UNITS = 160
# Door actions that OPEN passage (any activation type); close-only variants excluded.
# Same set as wad_map.DOOR_SPECIALS — keep in sync.
DOOR_SPECIALS = {
    1, 2, 4, 16, 26, 27, 28, 29, 31, 32, 33, 34, 46, 61, 63, 76, 86, 90, 99, 103,
    105, 106, 108, 109, 111, 112, 114, 115, 117, 118, 133, 134, 135, 136, 137,
}
# Far-stale skip list: congested doors not worth revisiting from afar. Key doors
# (26-28/32-34/99/133-137) and one-shot S1 doors (29/103/111/112) stay OUT so routing can
# come back to them once a key is found / for the single press they still owe (E1M2 line 389).
FAR_STALE_NORMAL_DOOR_SPECIALS = DOOR_SPECIALS - {
    26, 27, 28, 29, 32, 33, 34, 99, 103, 111, 112, 133, 134, 135, 136, 137
}
PROGRESSION_SPECIALS = DOOR_SPECIALS | USE_TRIGGER_SPECIALS | WALK_TRIGGER_SPECIALS | EXIT_SPECIALS




@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class PlanAction:
    skill: str
    action: Any
    detail: dict[str, Any]
    door_line_id: int | None = None


@dataclass(frozen=True)
class MapLineRuntime:
    id: int
    v1: int
    v2: int
    a: Point
    b: Point
    special: int
    tag: int
    front_sector: int
    back_sector: int
    two_sided: bool
    passable: bool
    blocking: bool
    sight_blocking: bool
    door: bool
    use_trigger: bool
    walk_trigger: bool
    exit: bool
    midpoint: Point
    lift: bool = False


@dataclass(frozen=True)
class SectorRuntime:
    id: int
    center: Point
    tag: int = 0
    floor_height_fp: int = 0
    ceiling_height_fp: int = 0
    damaging: bool = False
    exit_damage: bool = False


@dataclass(frozen=True)
class PortalEdge:
    src: int
    dst: int
    line_id: int
    point: Point
    special: int
    passable: bool
    use_line: bool
    door: bool
    exit: bool
    walk_trigger: bool
    cost: float
    tag_gate: int = 0


@dataclass(frozen=True)
class NavCellRuntime:
    id: int
    point: Point
    sector_id: int | None
    block: tuple[int, int]


@dataclass
class RouteStep:
    point: Point
    line_id: int | None = None
    use_line: bool = False
    sector_id: int | None = None


@dataclass
class Route:
    points: list[RouteStep] = field(default_factory=list)
    cost: float = 0.0


@dataclass(frozen=True)
class KeyItemRuntime:
    id: int
    type_id: int
    color: str
    point: Point
    sector_id: int | None


@dataclass(frozen=True)
class HealthItemRuntime:
    id: int
    type_id: int
    kind: str
    value: int
    point: Point
    sector_id: int | None


@dataclass(frozen=True)
class BarrelRuntime:
    id: int
    type_id: int
    point: Point
    sector_id: int | None




def _same_point(a: Point, b: Point) -> bool:
    return a.x == b.x and a.y == b.y


def _dist(a: Point, b: Point) -> float:
    return math.hypot(float(a.x - b.x), float(a.y - b.y))


def _bearing(a: Point, b: Point) -> float:
    return math.degrees(math.atan2(float(b.y - a.y), float(b.x - a.x))) % 360.0


def _angle_delta(target: float, current: float) -> float:
    return ((target - current + 540.0) % 360.0) - 180.0


def _orientation(a: Point, b: Point, c: Point) -> int:
    value = (b.y - a.y) * (c.x - b.x) - (b.x - a.x) * (c.y - b.y)
    if value == 0:
        return 0
    return 1 if value > 0 else 2


def _on_segment(a: Point, b: Point, c: Point) -> bool:
    return min(a.x, c.x) <= b.x <= max(a.x, c.x) and min(a.y, c.y) <= b.y <= max(a.y, c.y)


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    o1 = _orientation(a, b, c)
    o2 = _orientation(a, b, d)
    o3 = _orientation(c, d, a)
    o4 = _orientation(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and _on_segment(a, c, b):
        return True
    if o2 == 0 and _on_segment(a, d, b):
        return True
    if o3 == 0 and _on_segment(c, a, d):
        return True
    if o4 == 0 and _on_segment(c, b, d):
        return True
    return False
