"""Hazard-aware combat: DoomGuy fights FROM the walkway, never charging into nukage.

The toxic-slime death loop: hazard escape walks him out of the slime, hazard-blind
combat (rush_fire forward 50 / strafe_fire / close) charges straight back in, repeat
until dead. Combat movement must refuse to step OFF safe floor INTO a damaging
sector — bullets cross slime fine.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from planner import SpatialPlanner, Point  # noqa: E402
from planner_fakes import FakeRawAgentPb2, fp, vertex, line, sector, snapshot, state  # noqa: E402


def _two_sector_map(*, slime: bool):
    # Sector 0 (walkway): x in [-128, 0]. Sector 1 (east): x in [0, 256]; damaging when slime.
    verts = [
        vertex(0, -128, -128), vertex(1, -128, 128), vertex(2, 0, 128),
        vertex(3, 0, -128), vertex(4, 256, 128), vertex(5, 256, -128),
    ]
    lines = [
        line(10, 0, 1, front_sector=0),                     # west wall
        line(11, 1, 2, front_sector=0),                     # north wall (s0)
        line(12, 2, 3, front_sector=0, back_sector=1, two_sided=True, passable=True, blocking=False, sight_blocking=False),  # shared edge
        line(13, 3, 0, front_sector=0),                     # south wall (s0)
        line(14, 2, 4, front_sector=1),                     # north wall (s1)
        line(15, 4, 5, front_sector=1),                     # east wall
        line(16, 5, 3, front_sector=1),                     # south wall (s1)
    ]
    sectors = [sector(0, damaging=False), sector(1, damaging=slime)]
    return snapshot(verts, lines, sectors)


def _combat_inputs():
    # Player on the walkway facing east (+x); enemy 256u away across the east sector.
    player = {"point": Point(fp(-64), fp(0)), "angle": 0}
    enemy = {"id": 7, "point": Point(fp(192), fp(0)), "distance_fp": fp(256), "shootable_target": True}
    st = state(-64, 0, 0)
    st.navigation = SimpleNamespace(
        forward_open=True,
        back_open=True,
        direction_probes=[
            SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=fp(96)),
            SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=fp(96)),
        ],
    )
    return st, player, enemy


class TestHazardAwareCombat(unittest.TestCase):
    def test_rush_fire_refuses_to_charge_into_slime(self):
        planner = SpatialPlanner(_two_sector_map(slime=True), cell_units=96)
        st, player, enemy = _combat_inputs()
        action = planner._visible_enemy_action(st, player, enemy, FakeRawAgentPb2, skill="combat", detail={})
        self.assertIsNotNone(action)
        # Must NOT be a forward charge into the nukage: strafe along the walkway instead.
        self.assertNotEqual(action.detail.get("action"), "rush_fire")
        raw = getattr(action.action, "raw", None)
        if raw is not None:
            self.assertEqual(int(getattr(raw, "forward_move", 0) or 0), 0, "no forward step into slime")

    def test_rush_fire_still_charges_on_safe_floor(self):
        planner = SpatialPlanner(_two_sector_map(slime=False), cell_units=96)
        st, player, enemy = _combat_inputs()
        action = planner._visible_enemy_action(st, player, enemy, FakeRawAgentPb2, skill="combat", detail={})
        self.assertIsNotNone(action)
        self.assertEqual(action.detail.get("action"), "rush_fire")
        self.assertEqual(int(action.action.raw.forward_move), 50)

    def test_point_is_damaging_fp(self):
        planner = SpatialPlanner(_two_sector_map(slime=True), cell_units=96)
        self.assertFalse(planner.point_is_damaging_fp(fp(-64), fp(0)))  # walkway
        self.assertTrue(planner.point_is_damaging_fp(fp(128), fp(0)))   # slime


if __name__ == "__main__":
    unittest.main()
