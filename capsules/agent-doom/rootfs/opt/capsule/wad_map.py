#!/usr/bin/env python3.11
"""Minimal Doom-format WAD map reader for Agent DOOM planning."""

from __future__ import annotations

import glob
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

FP_UNIT = 65536
ML_BLOCKING = 0x0001
ML_TWOSIDED = 0x0004
NO_SIDEDEF = 0xFFFF
PLAYER_START_TYPES = {1, 2, 3, 4}
ENEMY_TYPES = {9, 58, 3001, 3002, 3003, 3004, 3005, 3006}
BARREL_THING_TYPES = {2035}
# Vanilla Doom/Doom II linedef activation types 1-141, transcribed from the doomwiki.org
# "Linedef type" table. D = USE opens the door line itself (no tag); S = tagged switch
# (S1 once / SR repeatable, USE); W = walk-over (W1/WR, fires on crossing); G = gun (fires
# when shot). 48 (scrolling wall) and the unassigned 78/85 trigger nothing.
# MUST STAY IN SYNC with the copies in planner.py and door_memory.py — the capsule deploys
# these modules as standalone files, so the sets are duplicated instead of imported.
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
G_SPECIALS = frozenset({24, 46, 47})
# Lifts/platforms change sector floor height at runtime. S-type lift specials are activated
# with USE on the line (like a switch); W-type fire when the player walks across the line.
LIFT_USE_SPECIALS = {21, 62, 122, 123}
LIFT_WALK_SPECIALS = {10, 88, 120, 121}
LIFT_SPECIALS = LIFT_USE_SPECIALS | LIFT_WALK_SPECIALS
USE_TRIGGER_SPECIALS = D_SPECIALS | S_SPECIALS
# ponytail: 125/126 (W1/WR teleport, monsters only) are W-type but the player cannot fire
# them; if a map ever needs the agent to avoid chasing one, subtract them here.
WALK_TRIGGER_SPECIALS = W_SPECIALS
# Door actions that OPEN passage (any activation type). Close-only door actions
# (3, 42, 50, 75, 107, 110, 113, 116) stay out: triggering them never opens a route.
DOOR_SPECIALS = {
    1, 2, 4, 16, 26, 27, 28, 29, 31, 32, 33, 34, 46, 61, 63, 76, 86, 90, 99, 103,
    105, 106, 108, 109, 111, 112, 114, 115, 117, 118, 133, 134, 135, 136, 137,
}
EXIT_SPECIALS = {11, 51, 52, 124, 197}


@dataclass(frozen=True)
class Lump:
    name: str
    offset: int
    size: int


def find_wad_path() -> Path | None:
    explicit = os.environ.get("PAIRPUTER_DOOM_WAD") or os.environ.get("PAIRPUTER_DOOM_WAD_PATH")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    for pattern in ("/home/app/app/*.WAD", "/home/app/app/*.wad", "/opt/capsule/app/*.WAD", "/opt/capsule/app/*.wad"):
        matches = sorted(glob.glob(pattern))
        if matches:
            return Path(matches[0])
    return None


class WadMapLoader:
    """Loads static map geometry from a Doom-format WAD."""

    def __init__(self, wad_path: str | os.PathLike[str]) -> None:
        self.path = Path(wad_path)
        self._data = self.path.read_bytes()
        self._lumps = self._read_directory()

    def load(self, episode: int, game_map: int) -> Any:
        marker = f"E{int(episode)}M{int(game_map)}"
        try:
            marker_index = next(i for i, lump in enumerate(self._lumps) if lump.name == marker)
        except StopIteration as exc:
            alt = f"MAP{int(game_map):02d}"
            try:
                marker_index = next(i for i, lump in enumerate(self._lumps) if lump.name == alt)
                marker = alt
            except StopIteration:
                raise KeyError(f"map {marker}/{alt} not found in {self.path}") from exc
        lumps = {lump.name: lump for lump in self._lumps[marker_index + 1: marker_index + 12]}
        vertices = self._vertices(lumps["VERTEXES"])
        sectors = self._sectors(lumps["SECTORS"])
        sidedefs = self._sidedefs(lumps["SIDEDEFS"])
        lines = self._lines(lumps["LINEDEFS"], vertices, sectors, sidedefs)
        things = self._things(lumps.get("THINGS"))
        xs = [int(getattr(v, "x_fp", 0)) for v in vertices] or [0]
        ys = [int(getattr(v, "y_fp", 0)) for v in vertices] or [0]
        digest = self._digest(marker, vertices, lines, sectors)
        return SimpleNamespace(
            schema="pairputer.wad_map.v1",
            source="wad",
            episode=int(episode),
            map=int(game_map),
            digest=digest,
            vertices=vertices,
            lines=lines,
            sectors=sectors,
            things=things,
            truncated=False,
            bbox_left_fp=min(xs),
            bbox_right_fp=max(xs),
            bbox_top_fp=max(ys),
            bbox_bottom_fp=min(ys),
        )

    def _read_directory(self) -> list[Lump]:
        if len(self._data) < 12 or self._data[:4] not in (b"IWAD", b"PWAD"):
            raise ValueError(f"{self.path} is not a Doom WAD")
        num_lumps, directory_offset = struct.unpack_from("<ii", self._data, 4)
        lumps: list[Lump] = []
        for i in range(num_lumps):
            offset, size, raw_name = struct.unpack_from("<ii8s", self._data, directory_offset + i * 16)
            name = raw_name.rstrip(b"\0").decode("ascii", "replace").upper()
            lumps.append(Lump(name=name, offset=offset, size=size))
        return lumps

    def _slice(self, lump: Lump | None) -> bytes:
        if lump is None:
            return b""
        return self._data[lump.offset: lump.offset + lump.size]

    def _vertices(self, lump: Lump) -> list[Any]:
        out = []
        raw = self._slice(lump)
        for idx in range(0, len(raw), 4):
            x, y = struct.unpack_from("<hh", raw, idx)
            out.append(SimpleNamespace(id=idx // 4, x_fp=int(x) * FP_UNIT, y_fp=int(y) * FP_UNIT))
        return out

    def _sectors(self, lump: Lump) -> list[Any]:
        out = []
        raw = self._slice(lump)
        for idx in range(0, len(raw), 26):
            floor, ceiling, _floorpic, _ceilpic, light, special, tag = struct.unpack_from("<hh8s8shhh", raw, idx)
            out.append(
                SimpleNamespace(
                    id=idx // 26,
                    floor_height_fp=int(floor) * FP_UNIT,
                    ceiling_height_fp=int(ceiling) * FP_UNIT,
                    light_level=int(light),
                    special=int(special),
                    tag=int(tag),
                    damaging=int(special) in {5, 7, 16},
                    exit_damage=int(special) in {11},
                    active_special=False,
                )
            )
        return out

    def _sidedefs(self, lump: Lump) -> list[int]:
        sectors = []
        raw = self._slice(lump)
        for idx in range(0, len(raw), 30):
            sector = struct.unpack_from("<H", raw, idx + 28)[0]
            sectors.append(int(sector))
        return sectors

    def _lines(self, lump: Lump, vertices: list[Any], sectors: list[Any], sidedefs: list[int]) -> list[Any]:
        out = []
        raw = self._slice(lump)
        for idx in range(0, len(raw), 14):
            v1, v2, flags, special, tag, right_side, left_side = struct.unpack_from("<HHHHHHH", raw, idx)
            front_sector = sidedefs[right_side] if right_side != NO_SIDEDEF and right_side < len(sidedefs) else -1
            back_sector = sidedefs[left_side] if left_side != NO_SIDEDEF and left_side < len(sidedefs) else -1
            two_sided = bool(flags & ML_TWOSIDED) and back_sector >= 0
            open_range = 0
            if two_sided and 0 <= front_sector < len(sectors) and 0 <= back_sector < len(sectors):
                front = sectors[front_sector]
                back = sectors[back_sector]
                top = min(int(front.ceiling_height_fp), int(back.ceiling_height_fp))
                bottom = max(int(front.floor_height_fp), int(back.floor_height_fp))
                open_range = top - bottom
            blocking = bool(flags & ML_BLOCKING) or not two_sided
            passable = not blocking and open_range >= 56 * FP_UNIT
            sight_blocking = (not two_sided) or open_range <= 0 or bool(flags & ML_BLOCKING)
            out.append(
                SimpleNamespace(
                    id=idx // 14,
                    v1=int(v1),
                    v2=int(v2),
                    flags=int(flags),
                    special=int(special),
                    tag=int(tag),
                    front_sector=int(front_sector),
                    back_sector=int(back_sector),
                    two_sided=two_sided,
                    blocking=blocking,
                    passable=passable,
                    sight_blocking=sight_blocking,
                    door=int(special) in DOOR_SPECIALS,
                    use_trigger=int(special) in USE_TRIGGER_SPECIALS,
                    walk_trigger=int(special) in WALK_TRIGGER_SPECIALS,
                    exit=int(special) in EXIT_SPECIALS,
                    lift=int(special) in LIFT_SPECIALS,
                    open_top_fp=0,
                    open_bottom_fp=0,
                    open_range_fp=open_range,
                )
            )
        return out

    def _things(self, lump: Lump | None) -> list[Any]:
        out = []
        raw = self._slice(lump)
        for idx in range(0, len(raw), 10):
            x, y, angle, type_id, options = struct.unpack_from("<hhHHH", raw, idx)
            out.append(
                SimpleNamespace(
                    id=idx // 10,
                    type_id=int(type_id),
                    position=SimpleNamespace(x_fp=int(x) * FP_UNIT, y_fp=int(y) * FP_UNIT, z_fp=0),
                    angle_degrees=int(angle),
                    options=int(options),
                    player_start=int(type_id) in PLAYER_START_TYPES,
                    enemy=int(type_id) in ENEMY_TYPES,
                    barrel=int(type_id) in BARREL_THING_TYPES,
                )
            )
        return out

    def _digest(self, marker: str, vertices: list[Any], lines: list[Any], sectors: list[Any]) -> int:
        digest = 1469598103934665603
        for value in (marker, len(vertices), len(lines), len(sectors)):
            digest = _mix(digest, value)
        for vertex in vertices[:128]:
            digest = _mix(digest, int(vertex.x_fp))
            digest = _mix(digest, int(vertex.y_fp))
        for line in lines[:256]:
            digest = _mix(digest, int(line.v1))
            digest = _mix(digest, int(line.v2))
            digest = _mix(digest, int(line.flags))
            digest = _mix(digest, int(line.special))
            digest = _mix(digest, int(line.tag))
        return digest & ((1 << 63) - 1)


def _mix(digest: int, value: Any) -> int:
    if isinstance(value, str):
        for ch in value.encode("utf-8"):
            digest = _mix(digest, ch)
        return digest
    digest ^= int(value) & 0xFFFFFFFFFFFFFFFF
    digest = (digest * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return digest
