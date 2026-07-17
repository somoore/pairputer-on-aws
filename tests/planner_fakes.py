"""Shared hermetic fixtures for Agent DOOM spatial-planner tests: fake protobuf enums and geometry builders."""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))

from planner import FP_UNIT  # noqa: E402


@dataclass
class FakeAction:
    action: int = 0
    amount: int = 0
    duration_tics: int = 1
    raw: object | None = None


@dataclass
class FakeRawTiccmd:
    forward_move: int = 0
    side_move: int = 0
    angle_turn: int = 0
    buttons: int = 0


class FakeAgentPb2:
    ACTION_FORWARD = 1
    ACTION_BACKWARD = 2
    ACTION_TURN_LEFT = 3
    ACTION_TURN_RIGHT = 4
    ACTION_STRAFE_LEFT = 5
    ACTION_STRAFE_RIGHT = 6
    ACTION_SHOOT = 7
    ACTION_USE = 8
    PlayerAction = FakeAction


class FakeRawAgentPb2(FakeAgentPb2):
    RawTiccmd = FakeRawTiccmd


def fp(units: int) -> int:
    return int(units * FP_UNIT)


def vertex(idx: int, x: int, y: int) -> SimpleNamespace:
    return SimpleNamespace(id=idx, x_fp=fp(x), y_fp=fp(y))


def line(idx: int, v1: int, v2: int, **kw) -> SimpleNamespace:
    defaults = {
        "special": 0,
        "tag": 0,
        "front_sector": -1,
        "back_sector": -1,
        "two_sided": False,
        "passable": False,
        "blocking": True,
        "sight_blocking": True,
        "door": False,
        "use_trigger": False,
        "walk_trigger": False,
        "exit": False,
    }
    defaults.update(kw)
    return SimpleNamespace(id=idx, v1=v1, v2=v2, **defaults)


def sector(idx: int, *, damaging: bool = False, tag: int = 0, floor: int = 0, ceiling: int = 128) -> SimpleNamespace:
    return SimpleNamespace(
        id=idx,
        damaging=damaging,
        exit_damage=False,
        tag=tag,
        floor_height_fp=fp(floor),
        ceiling_height_fp=fp(ceiling),
    )


def snapshot(vertices, lines, sectors=None, things=None) -> SimpleNamespace:
    return SimpleNamespace(
        episode=1,
        map=1,
        digest=123,
        vertices=vertices,
        lines=lines,
        sectors=sectors or [],
        things=things or [],
        truncated=False,
        bbox_left_fp=fp(-64),
        bbox_right_fp=fp(384),
        bbox_bottom_fp=fp(-256),
        bbox_top_fp=fp(256),
    )


def state(x: int, y: int, angle: int, *, enemy: tuple[int, int] | None = None, shootable: bool = False) -> SimpleNamespace:
    enemies = []
    if enemy is not None:
        ex, ey = enemy
        enemies.append(
            SimpleNamespace(
                object=SimpleNamespace(
                    id=77,
                    health=20,
                    distance_fp=fp(abs(ex - x) + abs(ey - y)),
                    position=SimpleNamespace(x_fp=fp(ex), y_fp=fp(ey), z_fp=0),
                ),
                line_of_sight=False,
            )
        )
    return SimpleNamespace(
        player=SimpleNamespace(
            object=SimpleNamespace(position=SimpleNamespace(x_fp=fp(x), y_fp=fp(y), z_fp=0), angle_degrees=angle)
        ),
        enemies=enemies,
        combat=SimpleNamespace(has_shootable_target=shootable, target_is_enemy=shootable),
    )


def thing(idx: int, type_id: int, x: int, y: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=idx,
        type_id=type_id,
        position=SimpleNamespace(x_fp=fp(x), y_fp=fp(y), z_fp=0),
    )


def enemy_state(x: int, y: int, angle: int, enemies: list[dict]) -> SimpleNamespace:
    raw_enemies = []
    for item in enemies:
        ex, ey = item["pos"]
        raw_enemies.append(
            SimpleNamespace(
                object=SimpleNamespace(
                    id=item["id"],
                    health=item.get("health", 20),
                    distance_fp=fp(abs(ex - x) + abs(ey - y)),
                    position=SimpleNamespace(x_fp=fp(ex), y_fp=fp(ey), z_fp=0),
                ),
                line_of_sight=bool(item.get("los", False)),
            )
        )
    return SimpleNamespace(
        player=SimpleNamespace(
            object=SimpleNamespace(position=SimpleNamespace(x_fp=fp(x), y_fp=fp(y), z_fp=0), angle_degrees=angle)
        ),
        enemies=raw_enemies,
        combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
    )


def probe(offset: int, *, open: bool = True, distance: int = 256) -> SimpleNamespace:
    return SimpleNamespace(angle_offset_degrees=offset, open=open, block_distance_fp=fp(distance))


def _write_minimal_wad(path: Path, *, specials: tuple[int, ...] = (1,)) -> None:
    linedefs = b"".join(struct.pack("<HHHHHHH", 0, 1, 1, special, 7, 0, 0xFFFF) for special in specials)
    lumps = [
        ("E1M1", b""),
        ("THINGS", struct.pack("<hhHHH", 0, 0, 0, 1, 7)),
        ("LINEDEFS", linedefs),
        ("SIDEDEFS", struct.pack("<hh8s8s8sH", 0, 0, b"-", b"-", b"-", 0)),
        ("VERTEXES", struct.pack("<hhhh", 0, -64, 0, 64)),
        ("SEGS", b""),
        ("SSECTORS", b""),
        ("NODES", b""),
        ("SECTORS", struct.pack("<hh8s8shhh", 0, 128, b"FLOOR0_1", b"CEIL1_1", 160, 0, 0)),
        ("REJECT", b""),
        ("BLOCKMAP", b""),
    ]
    data = bytearray()
    directory = []
    for name, payload in lumps:
        directory.append((len(data), len(payload), name))
        data.extend(payload)
    directory_offset = 12 + len(data)
    body = bytearray(struct.pack("<4sii", b"IWAD", len(lumps), directory_offset))
    body.extend(data)
    for offset, size, name in directory:
        body.extend(struct.pack("<ii8s", 12 + offset, size, name.encode("ascii").ljust(8, b"\0")))
    path.write_bytes(body)
