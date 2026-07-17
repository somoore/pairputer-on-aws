"""Lift/step-height routing and tag-gate switch-hunting for Agent DOOM's spatial planner."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))

from door_memory import DoorMemory  # noqa: E402
from planner import (  # noqa: E402
    Point,
    SpatialPlanner,
)

from planner_fakes import (  # noqa: E402
    FakeAgentPb2,
    fp,
    vertex,
    line,
    sector,
    snapshot,
    state,
)


class TestLiftAndStepHeightRouting(unittest.TestCase):
    """Raised lifts/platforms: USE lines become door-like uphill, plain cliffs block uphill only."""

    def _lift_planner(self, *, special: int, use_trigger: bool, walk_trigger: bool = False):
        # Sector 1 (floor 0, west) meets sector 2 (floor 128, a raised lift, east) at x=64.
        # v1->v2 points +y, so the Doom front side (right of v1->v2) is east: front=2, back=1.
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [
                line(
                    20,
                    0,
                    1,
                    special=special,
                    tag=4,
                    front_sector=2,
                    back_sector=1,
                    two_sided=True,
                    passable=True,
                    blocking=False,
                    sight_blocking=False,
                    use_trigger=use_trigger,
                    walk_trigger=walk_trigger,
                )
            ],
            sectors=[sector(1, floor=0), sector(2, floor=128, ceiling=256, tag=4)],
        )
        return SpatialPlanner(snap, cell_units=96)

    def test_wad_classifies_lift_specials(self):
        import wad_map

        self.assertTrue(wad_map.LIFT_USE_SPECIALS <= wad_map.USE_TRIGGER_SPECIALS)
        self.assertIn(62, wad_map.USE_TRIGGER_SPECIALS)
        self.assertNotIn(88, wad_map.USE_TRIGGER_SPECIALS)
        self.assertIn(88, wad_map.LIFT_WALK_SPECIALS)

    def test_planner_marks_lift_lines_from_special(self):
        planner = self._lift_planner(special=62, use_trigger=True)
        self.assertTrue(planner.lines[0].lift)

    def test_raised_lift_portal_is_use_line_uphill_passable_downhill(self):
        planner = self._lift_planner(special=62, use_trigger=True)
        edges = {(edge.src, edge.dst): edge for edge in planner.portal_edges if edge.line_id == 20}
        uphill = edges[(1, 2)]
        downhill = edges[(2, 1)]
        self.assertTrue(uphill.use_line)
        self.assertFalse(uphill.passable)
        self.assertFalse(downhill.use_line)
        self.assertTrue(downhill.passable)

    def test_plain_tall_step_keeps_uphill_portal_as_costly_probe(self):
        # Stair builders / floor raisers change heights at runtime, so a statically impossible
        # step stays routable as a last-resort probe edge with a steep penalty (door memory
        # abandons it after real attempts), while downhill stays cheap.
        planner = self._lift_planner(special=0, use_trigger=False)
        edges = {(edge.src, edge.dst): edge for edge in planner.portal_edges if edge.line_id == 20}
        self.assertIn((1, 2), edges)
        self.assertIn((2, 1), edges)
        self.assertGreater(edges[(1, 2)].cost, edges[(2, 1)].cost + 700.0)
        self.assertFalse(edges[(1, 2)].passable)
        self.assertTrue(edges[(2, 1)].passable)

    def test_sector_route_crosses_raised_lift_via_use(self):
        planner = self._lift_planner(special=62, use_trigger=True)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        self.assertEqual(route[0].line_id, 20)
        self.assertTrue(route[0].use_line)

    def test_edge_crossings_gate_tall_step_by_direction_and_special(self):
        low = Point(fp(32), fp(0))
        high = Point(fp(96), fp(0))

        lift = self._lift_planner(special=62, use_trigger=True)
        blocked, door_lines = lift._edge_crossings(low, high)
        self.assertFalse(blocked)
        self.assertEqual(door_lines, (20,))
        blocked_down, door_lines_down = lift._edge_crossings(high, low)
        self.assertFalse(blocked_down)
        self.assertEqual(door_lines_down, ())

        cliff = self._lift_planner(special=0, use_trigger=False)
        blocked_up, probe_lines = cliff._edge_crossings(low, high)
        self.assertFalse(blocked_up)
        self.assertEqual(probe_lines, (20,))
        blocked_down, down_lines = cliff._edge_crossings(high, low)
        self.assertFalse(blocked_down)
        self.assertEqual(down_lines, ())

    def test_door_memory_treats_lift_use_as_opening_and_retriggerable(self):
        memory = DoorMemory()
        memory.observe_line(20, special=62, tag=4)
        memory.record_attempt(20, status="sector_portal_use")
        self.assertTrue(memory.is_open(20))
        self.assertEqual(memory.state_for(20), "opening")
        # Lift rose again before we crossed: stays retryable, does not hard-block.
        memory.record_stale_open(20)
        self.assertEqual(memory.state_for(20), "opening")
        self.assertTrue(memory.can_retry(20))
        memory.record_failure(20, status="use_no_progress")
        self.assertEqual(memory.state_for(20), "opening")
        self.assertTrue(memory.can_retry(20))


class TestTagGateSwitchHunting(unittest.TestCase):
    """Closed tag gates trigger a switch hunt, mirroring key hunting for locked doors."""

    def _gated_planner(self):
        # Sector 1 (player, with switch line 40 on its west wall) -> open portal (line 20)
        # -> sector 2 -> closed tag gate (line 30, sector 3 tagged 12) -> sector 3 (target).
        # Line 40 carries special 103 (S1 open door) with tag 12: the remote opener.
        snap = snapshot(
            [
                vertex(0, 96, -64),
                vertex(1, 96, 64),
                vertex(2, 160, -64),
                vertex(3, 160, 64),
                vertex(4, 16, -32),
                vertex(5, 16, 32),
            ],
            [
                line(20, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(30, 2, 3, front_sector=2, back_sector=3, two_sided=True, passable=False, blocking=False),
                line(40, 4, 5, special=103, tag=12, front_sector=1, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3, tag=12, ceiling=0)],
        )
        return SpatialPlanner(snap, cell_units=96)

    def test_switch_hunt_targets_trigger_then_route_opens(self):
        planner = self._gated_planner()
        memory = DoorMemory()
        player = {"point": Point(fp(48), fp(0)), "angle": 180}

        self.assertIsNone(planner._sector_route(1, [3], memory))

        action = planner._tag_gate_switch_action(state(48, 0, 180), player, FakeAgentPb2, memory, [3])
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "route_to_tag_switch")
        self.assertEqual(action.detail["gate"], 12)
        self.assertEqual(action.door_line_id, 40)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)

        # Pressing the switch recorded an opening tagged trigger -> the gate route now exists.
        self.assertTrue(memory.tag_is_open(12))
        self.assertIsNotNone(planner._sector_route(1, [3], memory))

    def test_switch_hunt_noop_when_gate_already_open(self):
        planner = self._gated_planner()
        memory = DoorMemory()
        memory.observe_line(40, special=103, tag=12)
        memory.record_attempt(40, status="planner_use")
        player = {"point": Point(fp(48), fp(0)), "angle": 180}
        self.assertIsNone(planner._tag_gate_switch_action(state(48, 0, 180), player, FakeAgentPb2, memory, [3]))

    def test_switch_hunt_gives_up_without_trigger_lines(self):
        planner = self._gated_planner()
        planner._trigger_lines_by_tag = {}
        memory = DoorMemory()
        player = {"point": Point(fp(48), fp(0)), "angle": 180}
        self.assertIsNone(planner._tag_gate_switch_action(state(48, 0, 180), player, FakeAgentPb2, memory, [3]))

    def test_route_abandoned_keeps_passable_boundaries_and_key_doors_recoverable(self):
        memory = DoorMemory()
        # Plain passable sector boundary: repeated no-cross is congestion, never a hard block —
        # it may be the single bridge into the rest of the map (E1M2 line 400 regression).
        memory.observe_line(400, special=0, tag=0)
        memory.record_route_abandoned(400, passable=True)
        self.assertEqual(memory.state_for(400), "congested")
        self.assertFalse(memory.is_blocked(400))
        # Key door abandoned by routing must flag requires_key so the key hunt engages and
        # mark_key_acquired can repair it (E1M2 line 527 regression).
        memory.observe_line(527, special=28, tag=0)
        memory.record_route_abandoned(527)
        self.assertEqual(memory.state_for(527), "requires_key")
        self.assertEqual(memory.required_key_colors(), {"red"})
        memory.mark_key_acquired("red")
        self.assertEqual(memory.state_for(527), "closed")
        # Non-passable no-special lines keep the old hard-block behavior.
        memory.observe_line(33, special=0, tag=0)
        memory.record_route_abandoned(33)
        self.assertTrue(memory.is_blocked(33))

    def test_route_abandoned_congests_any_passable_line_regardless_of_special(self):
        # E1M2 lines 288/289: passable special-88 walk-over lifts were hard-blocked, cutting
        # the only route into the map's east half. A passable line is a crossable bridge —
        # repeated no-cross is congestion, never proof of a wall.
        memory = DoorMemory()
        memory.observe_line(288, special=88, tag=6)
        memory.record_route_abandoned(288, passable=True)
        self.assertEqual(memory.state_for(288), "congested")
        self.assertFalse(memory.is_blocked(288))
        self.assertGreater(memory.route_penalty_for(288), 0)
        # Non-passable special-0 lines still hard-block.
        memory.observe_line(34, special=0, tag=0)
        memory.record_route_abandoned(34, passable=False)
        self.assertTrue(memory.is_blocked(34))

    def test_door_memory_tagged_one_shot_switch_use_opens_tag(self):
        # Special 23 (S1 lower floor) is not a door/lift, but a tagged USE must still
        # optimistically open its tag so routing proceeds; stale-open repair walks it back.
        memory = DoorMemory()
        memory.observe_line(50, special=23, tag=9)
        memory.record_attempt(50, status="planner_use")
        self.assertTrue(memory.tag_is_open(9))
        # Untagged unknown-special lines keep the old closed-after-attempt behavior.
        memory.observe_line(51, special=48, tag=0)
        memory.record_attempt(51, status="planner_use")
        self.assertFalse(memory.is_open(51))


if __name__ == "__main__":
    unittest.main()
