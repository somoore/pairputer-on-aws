"""wedge_door_use_action: the wedged-against-a-closed-door rescue.

When DoomGuy physically stalls against a door line, the normal use-line planner
suppresses lines it thinks it already tried, so it walks INTO the door forever
without pressing USE. wedge_door_use_action ignores that memory and presses USE
— but is capped so a door that won't open is abandoned (control falls back to
hard_unstick to route around it) instead of jackhammering USE indefinitely.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))

from door_memory import DoorMemory  # noqa: E402
from planner import SpatialPlanner  # noqa: E402
from planner_fakes import FakeAgentPb2, fp, vertex, line, snapshot, state  # noqa: E402


def _door_nav(dist_units: int = 48, x: int = 48, y: int = 0):
    # A normal manual door (special 1) sitting close in front of the player.
    return SimpleNamespace(
        back_open=True,
        use_lines=[
            SimpleNamespace(
                line_id=330,
                special=1,
                nearest_point=SimpleNamespace(x_fp=fp(x), y_fp=fp(y)),
                midpoint=SimpleNamespace(x_fp=fp(x), y_fp=fp(y)),
                nearest_distance_fp=fp(dist_units),
                distance_fp=fp(dist_units),
            )
        ],
        direction_probes=[],
    )


class TestWedgeDoorUse(unittest.TestCase):
    def _planner(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=1)])
        return SpatialPlanner(snap, cell_units=96)

    def test_presses_use_on_close_aligned_door(self):
        planner = self._planner()
        st = state(0, 0, 0)  # facing +x, door dead ahead
        st.navigation = _door_nav()
        action = planner.wedge_door_use_action(st, FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.detail["skill"], "wedge_door_use")

    def test_turns_first_when_not_facing_door(self):
        planner = self._planner()
        st = state(0, 0, 180)  # door is behind the facing angle
        st.navigation = _door_nav()
        action = planner.wedge_door_use_action(st, FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)  # a turn, not a USE yet

    def test_abandons_dead_door_after_cap(self):
        planner = self._planner()
        st = state(0, 0, 0)
        st.navigation = _door_nav()
        mem = DoorMemory()
        # Hammer the same never-opening door. After the cap, it must give up (None)
        # so hard_unstick can route around instead of freezing forever.
        results = [planner.wedge_door_use_action(st, FakeAgentPb2, mem) for _ in range(8)]
        uses = [a for a in results if a is not None and a.action.action == FakeAgentPb2.ACTION_USE]
        self.assertLessEqual(len(uses), 5, "must stop pressing USE on a dead door")
        self.assertIsNone(results[-1], "capped door yields to hard_unstick (None)")

    def test_no_door_in_reach_returns_none(self):
        planner = self._planner()
        st = state(0, 0, 0)
        st.navigation = _door_nav(dist_units=200)  # far outside 1.5x USE range (~96u)
        self.assertIsNone(planner.wedge_door_use_action(st, FakeAgentPb2, DoorMemory()))


if __name__ == "__main__":
    unittest.main()
