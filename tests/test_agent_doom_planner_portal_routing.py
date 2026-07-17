"""Portal/sector/passable-portal route geometry and door-crossing steering for Agent DOOM's spatial planner."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))

from door_memory import DoorMemory  # noqa: E402
from planner import (  # noqa: E402
    FP_UNIT,
    PlanAction,
    Point,
    PortalEdge,
    Route,
    RouteStep,
    SpatialPlanner,
)

from planner_fakes import (  # noqa: E402
    FakeAgentPb2,
    FakeRawAgentPb2,
    fp,
    vertex,
    line,
    sector,
    snapshot,
    state,
    thing,
    enemy_state,
    probe,
)


class TestAgentDoomPlannerPortalRouting(unittest.TestCase):
    def test_sector_portal_graph_routes_through_door_edge(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
            ],
            [line(20, 0, 1, special=1, front_sector=1, back_sector=2, two_sided=True, door=True, use_trigger=True)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        self.assertEqual(route[0].line_id, 20)
        self.assertTrue(route[0].use_line)
        self.assertGreaterEqual(planner.summary()["ports"], 2)
    def test_sector_portal_graph_ignores_blocking_two_sided_wall(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(20, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=False, blocking=True),
                line(21, 2, 3, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
            ],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        self.assertEqual(route[0].line_id, 21)
    def test_portal_route_approaches_closed_use_line_before_crossing(self):
        snap = snapshot(
            [
                vertex(0, 128, -64),
                vertex(1, 128, 64),
                vertex(2, 0, -128),
                vertex(3, 128, -128),
                vertex(4, 128, 128),
                vertex(5, 0, 128),
                vertex(6, 256, -128),
                vertex(7, 256, 128),
            ],
            [
                line(20, 0, 1, special=1, front_sector=1, back_sector=2, two_sided=True, door=True, use_trigger=True),
                line(21, 2, 3, front_sector=1),
                line(22, 3, 4, front_sector=1),
                line(23, 4, 5, front_sector=1),
                line(24, 5, 2, front_sector=1),
                line(25, 0, 6, front_sector=2),
                line(26, 6, 7, front_sector=2),
                line(27, 7, 1, front_sector=2),
            ],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
    def test_portal_use_faces_nearest_line_point_not_midpoint(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(20, 0, 1, special=1, front_sector=1, back_sector=2, two_sided=True, door=True, use_trigger=True)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        player = {"point": Point(fp(48), fp(0)), "angle": 350}
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 20)
    def test_portal_route_preopens_immediate_upcoming_use_line(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        player = {"point": Point(fp(48), fp(0)), "angle": 0}
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 11)
        self.assertEqual(action.detail["skill"], "preopen_upcoming_use_line")
    def test_portal_route_retries_unconfirmed_upcoming_opening_door(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        memory.record_attempt(11, status="preopen_upcoming_use")
        player = {"point": Point(fp(48), fp(0)), "angle": 0}
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 11)
    def test_portal_route_retries_opening_upcoming_door_under_enemy_los(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        memory.record_attempt(11, status="preopen_upcoming_use")
        player = {"point": Point(fp(48), fp(0)), "angle": 0}
        st = enemy_state(48, 0, 0, [{"id": 77, "pos": (48, 256), "los": True}])
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 11)
        self.assertEqual(action.detail["skill"], "pressure_reopen_upcoming_use_line")
    def test_portal_route_pushes_through_opening_upcoming_door_under_pressure_after_retries(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        for _ in range(3):
            memory.record_attempt(11, status="pressure_reopen_upcoming_use")
        player = {"point": Point(fp(48), fp(0)), "angle": 0}
        st = enemy_state(48, 0, 0, [{"id": 77, "pos": (48, 256), "los": True}])
        st.navigation = SimpleNamespace(forward_open=True, front_block_distance_fp=fp(96))
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
        self.assertEqual(action.door_line_id, 11)
        self.assertEqual(action.detail["action"], "pressure_follow_opening")
    def test_portal_route_retreats_from_retried_upcoming_door_when_hurt_and_blocked(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        for _ in range(3):
            memory.record_attempt(11, status="pressure_reopen_upcoming_use")
        player = {"point": Point(fp(48), fp(0)), "angle": 0}
        st = enemy_state(48, 0, 0, [{"id": 77, "pos": (48, 256), "los": True}])
        st.player.health = 50
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(16), back_open=True)
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(action.door_line_id, 11)
        self.assertEqual(action.detail["action"], "retreat_pressure_upcoming_use_line")
        self.assertEqual(action.detail["hp"], 50)
    def test_portal_route_retreats_from_retried_upcoming_door_when_still_blocked(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        for _ in range(3):
            memory.record_attempt(11, status="pressure_reopen_upcoming_use")
        player = {"point": Point(fp(48), fp(0)), "angle": 0}
        st = enemy_state(48, 0, 0, [{"id": 77, "pos": (48, 256), "los": True}])
        st.player.health = 100
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(16), back_open=True)
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(action.door_line_id, 11)
        self.assertEqual(action.detail["action"], "retreat_pressure_upcoming_use_line")
        self.assertEqual(action.detail["attempts"], 3)
    def test_portal_route_stops_reusing_current_door_after_retries(self):
        snap = snapshot(
            [
                vertex(0, 96, -64),
                vertex(1, 96, 64),
            ],
            [
                line(11, 0, 1, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(2, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        for _ in range(3):
            memory.record_attempt(11, status="sector_portal_use")
        player = {"point": Point(fp(88), fp(0)), "angle": 0}
        st = enemy_state(88, 0, 0, [{"id": 77, "pos": (88, 256), "los": True}])
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(16), back_open=True)
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.detail["action"], "commit_retried_use_line")
        self.assertEqual(action.door_line_id, 11)
    def test_portal_route_retreats_from_retried_current_door_when_hurt_and_blocked(self):
        snap = snapshot(
            [
                vertex(0, 96, -64),
                vertex(1, 96, 64),
            ],
            [
                line(11, 0, 1, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(2, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        for _ in range(3):
            memory.record_attempt(11, status="sector_portal_use")
        player = {"point": Point(fp(88), fp(0)), "angle": 0}
        st = enemy_state(88, 0, 0, [{"id": 77, "pos": (88, 256), "los": True}])
        st.player.health = 50
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(16), back_open=True)
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.detail["action"], "commit_retried_use_line")
        self.assertEqual(action.detail["hp"], 50)
    def test_portal_route_commits_to_final_retried_current_door_before_critical_health(self):
        snap = snapshot(
            [
                vertex(0, 96, -64),
                vertex(1, 96, 64),
            ],
            [
                line(11, 0, 1, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(2, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        for _ in range(3):
            memory.record_attempt(11, status="sector_portal_use")
        player = {"point": Point(fp(88), fp(0)), "angle": 270}
        st = enemy_state(88, 0, 270, [{"id": 77, "pos": (88, 256), "los": True}])
        st.player.health = 71
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(73), back_open=True)
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line", "route": 3},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.detail["action"], "force_follow_retried_use_line")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
    def test_portal_route_retreats_from_critical_retried_current_door_when_hard_blocked(self):
        snap = snapshot(
            [
                vertex(0, 96, -64),
                vertex(1, 96, 64),
            ],
            [
                line(11, 0, 1, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(2, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        for _ in range(3):
            memory.record_attempt(11, status="sector_portal_use")
        player = {"point": Point(fp(88), fp(0)), "angle": 0}
        st = enemy_state(88, 0, 0, [{"id": 77, "pos": (88, 256), "los": True}])
        st.player.health = 20
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(16), back_open=True)
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line", "route": 3},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.detail["action"], "commit_retried_use_line")
        self.assertEqual(action.detail["hp"], 20)
    def test_portal_route_commits_to_final_upcoming_opening_door_before_critical_health(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        memory.record_attempt(11, status="preopen_upcoming_use")
        player = {"point": Point(fp(48), fp(0)), "angle": 0}
        st = enemy_state(48, 0, 0, [{"id": 77, "pos": (48, 256), "los": True}])
        st.player.health = 71
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(89), back_open=True)
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line", "route": 5},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.door_line_id, 11)
        self.assertEqual(action.detail["action"], "final_door_pressure_follow_opening")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
    def test_portal_route_retreats_from_critical_retried_upcoming_door_when_hard_blocked(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        for _ in range(3):
            memory.record_attempt(11, status="pressure_reopen_upcoming_use")
        player = {"point": Point(fp(48), fp(0)), "angle": 0}
        st = enemy_state(48, 0, 0, [{"id": 77, "pos": (48, 256), "los": True}])
        st.player.health = 20
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(16), back_open=True)
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line", "route": 4},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertEqual(action.door_line_id, 11)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(action.detail["action"], "retreat_pressure_upcoming_use_line")
        self.assertEqual(action.detail["hp"], 20)
    def test_portal_route_faces_upcoming_use_line_before_using(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        player = {"point": Point(fp(48), fp(0)), "angle": 70}
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 11)
    def test_portal_route_waits_at_jamb_for_opening_door_after_passable_portal(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        memory.record_attempt(11, status="preopen_upcoming_use")
        st = state(48, 0, 0)
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(24))
        action = planner._portal_route_action(
            {"point": Point(fp(48), fp(0)), "angle": 0},
            route,
            FakeAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "wait_upcoming_door_jamb")
        self.assertEqual(action.door_line_id, 11)
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
    def test_final_route_does_not_wait_at_jamb_for_opening_upcoming_door(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        memory.record_attempt(11, status="preopen_upcoming_use")
        st = state(48, 0, 0)
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(89))
        action = planner._portal_route_action(
            {"point": Point(fp(48), fp(0)), "angle": 0},
            route,
            FakeRawAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line", "route": 5},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "final_door_wait_commit")
        self.assertEqual(action.door_line_id, 11)
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
    def test_portal_route_stops_preopening_when_opening_door_probe_is_clear(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 96, -64),
                vertex(3, 96, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(11, 2, 3, special=1, front_sector=2, back_sector=3, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(11, special=1)
        memory.record_attempt(11, status="preopen_upcoming_use")
        st = state(48, 0, 0)
        st.navigation = SimpleNamespace(forward_open=True, front_block_distance_fp=fp(160))
        action = planner._portal_route_action(
            {"point": Point(fp(48), fp(0)), "angle": 0},
            route,
            FakeAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertNotEqual(action.detail["skill"], "preopen_upcoming_use_line")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
    def test_portal_route_crosses_door_that_is_opening(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 0, -128),
                vertex(3, 64, -128),
                vertex(4, 64, 128),
                vertex(5, 0, 128),
                vertex(6, 192, -128),
                vertex(7, 192, 128),
            ],
            [
                line(20, 0, 1, special=1, front_sector=1, back_sector=2, two_sided=True, door=True, use_trigger=True),
                line(21, 2, 3, front_sector=1),
                line(22, 3, 4, front_sector=1),
                line(23, 4, 5, front_sector=1),
                line(24, 5, 2, front_sector=1),
                line(25, 0, 6, front_sector=2),
                line(26, 6, 7, front_sector=2),
                line(27, 7, 1, front_sector=2),
            ],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(20, special=1)
        memory.record_failure(20, status="no_progress_after_use")
        player = {"point": Point(fp(48), fp(0)), "angle": 180}
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
        self.assertEqual(action.detail["action"], "forward")
        self.assertEqual(action.door_line_id, 20)
        self.assertEqual(action.door_line_id, 20)
    def test_final_route_commits_through_current_opening_door_instead_of_strafing(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 0, -128),
                vertex(3, 64, -128),
                vertex(4, 64, 128),
                vertex(5, 0, 128),
                vertex(6, 192, -128),
                vertex(7, 192, 128),
            ],
            [
                line(20, 0, 1, special=1, front_sector=1, back_sector=2, two_sided=True, door=True, use_trigger=True),
                line(21, 2, 3, front_sector=1),
                line(22, 3, 4, front_sector=1),
                line(23, 4, 5, front_sector=1),
                line(24, 5, 2, front_sector=1),
                line(25, 0, 6, front_sector=2),
                line(26, 6, 7, front_sector=2),
                line(27, 7, 1, front_sector=2),
            ],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(20, special=1)
        memory.record_failure(20, status="no_progress_after_use")
        st = state(48, 0, 180)
        st.navigation = SimpleNamespace(forward_open=True, front_block_distance_fp=fp(96))
        action = planner._portal_route_action(
            {"point": Point(fp(48), fp(0)), "angle": 180},
            route,
            FakeRawAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line", "route": 5},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "final_door_follow_opening_commit")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
    def test_recover_stuck_final_route_uses_probe_instead_of_large_turn(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(315, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=True,
            front_block_distance_fp=fp(96),
            direction_probes=[
                probe(-90, open=False, distance=0),
                probe(90, open=True, distance=192),
            ],
        )
        action = planner._portal_route_action(
            {"point": Point(fp(0), fp(0)), "angle": 0},
            route,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="recover_stuck",
            detail={"skill": "sector_route_to_exit_line", "route": 5},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "recover_stuck")
        self.assertEqual(action.detail["action"], "final_route_recovery_probe")
        self.assertEqual(action.door_line_id, 315)
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertGreater(action.action.raw.side_move, 0)
    def test_press_exit_final_route_still_uses_normal_turn_alignment(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(315, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=True,
            front_block_distance_fp=fp(96),
            direction_probes=[
                probe(-90, open=False, distance=0),
                probe(90, open=True, distance=192),
            ],
        )
        action = planner._portal_route_action(
            {"point": Point(fp(0), fp(0)), "angle": 0},
            route,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line", "route": 5},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertNotEqual(action.detail.get("action"), "final_route_recovery_probe")
    def test_close_passable_sector_portal_targets_destination_center(self):
        snap = snapshot(
            [
                vertex(0, 64, 0),
                vertex(1, -64, 0),
                vertex(2, -128, -128),
                vertex(3, 128, -128),
                vertex(4, 128, 128),
                vertex(5, -128, 128),
                vertex(6, 128, -128),
                vertex(7, 128, 128),
            ],
            [
                line(20, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(21, 2, 3, front_sector=1),
                line(22, 3, 4, front_sector=1),
                line(23, 4, 5, front_sector=1),
                line(24, 5, 2, front_sector=1),
                line(25, 0, 6, front_sector=2),
                line(26, 6, 7, front_sector=2),
                line(27, 7, 1, front_sector=2),
            ],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        player = {"point": Point(fp(0), fp(6)), "angle": 270}
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_use_line"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
        self.assertEqual(action.detail["action"], "forward")
    def test_centered_blocked_passable_portal_side_squeezes_when_side_room_exists(self):
        snap = snapshot(
            [vertex(0, 64, 0), vertex(1, -64, 0)],
            [line(310, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        st = state(0, 6, 270)
        st.navigation = SimpleNamespace(
            forward_open=False,
            front_block_distance_fp=fp(16),
            direction_probes=[
                probe(-90, open=True, distance=96),
                probe(90, open=True, distance=192),
            ],
        )
        player = {"point": Point(fp(0), fp(6)), "angle": 270}
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "blocked_portal_side_squeeze")
        self.assertEqual(action.door_line_id, 310)
        self.assertIsNotNone(action.action.raw)
        self.assertIn("mt", action.detail)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertNotEqual(action.action.raw.side_move, 0)
    def test_open_centered_passable_portal_commits_forward(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(335, 0, 1, front_sector=100, back_sector=38, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            sectors=[sector(100), sector(38)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(100, [38], DoorMemory())
        self.assertIsNotNone(route)
        st = state(56, 0, 180)
        st.navigation = SimpleNamespace(forward_open=True, front_block_distance_fp=fp(96), direction_probes=[])
        player = {"point": Point(fp(56), fp(0)), "angle": 180}
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="route_progression",
            detail={"skill": "frontier_sector_route", "route": 5},
            state=st,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "cross_passable_portal")
        self.assertEqual(action.door_line_id, 335)
        self.assertIsNotNone(action.action.raw)
        self.assertIn("mt", action.detail)
        self.assertGreater(action.action.raw.forward_move, 0)
    def test_sector_portal_uses_navcell_route_when_direct_segment_is_blocked(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 90}
        route = [PortalEdge(1, 2, 385, Point(fp(256), fp(0)), 0, True, False, False, False, False, 1.0)]

        def fake_edge(a, b, door_memory):
            if b == route[0].point:
                return None
            return (1.0, None, False)

        def fake_route(start, targets, door_memory):
            return Route([RouteStep(Point(fp(0), fp(96)))], cost=1.0)

        planner._edge = fake_edge  # type: ignore[method-assign]
        planner._route = fake_route  # type: ignore[method-assign]
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "navcell_to_portal")
        self.assertEqual(action.detail["portal_line"], 385)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
    def test_passable_portal_squeeze_uses_raw_forward_and_side_move(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(310, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        st = state(48, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=False,
            front_block_distance_fp=fp(16),
            direction_probes=[
                probe(-90, open=True, distance=96),
                probe(90, open=True, distance=192),
            ],
        )
        player = {"point": Point(fp(48), fp(-36)), "angle": 180}
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "squeeze_passable_portal")
        self.assertEqual(action.door_line_id, 310)
        self.assertIsNotNone(action.action.raw)
        self.assertIn("mt", action.detail)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertGreater(action.action.raw.side_move, 0)
    def test_final_corridor_nearby_use_line_wins_over_passable_portal_squeeze(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(346, 0, 1, front_sector=78, back_sector=80, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            sectors=[sector(78), sector(80)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = [
            PortalEdge(78, 80, 346, Point(fp(0), fp(0)), 0, True, False, False, False, False, 1.0)
        ]
        st = state(0, 0, 270)
        st.navigation = SimpleNamespace(
            forward_open=False,
            front_block_distance_fp=fp(16),
            use_lines=[
                SimpleNamespace(
                    line_id=325,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(0), y_fp=fp(-48)),
                )
            ],
            direction_probes=[
                probe(-90, open=False, distance=0),
                probe(90, open=True, distance=96),
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 270}
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line", "route": 4},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "final_corridor_use_line")
        self.assertEqual(action.door_line_id, 325)
        self.assertNotEqual(action.detail["action"], "squeeze_passable_portal")
    def test_final_corridor_ignores_e1m1_side_door_live_use_line(self):
        snap = snapshot(
            [
                vertex(0, 3072, -4000),
                vertex(1, 2944, -4000),
                vertex(2, 2912, -3776),
                vertex(3, 2912, -3904),
            ],
            [
                line(310, 0, 1, front_sector=56, back_sector=75, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(248, 2, 3, special=1, front_sector=67, back_sector=68, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(56), sector(67), sector(68), sector(75)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = [
            PortalEdge(56, 75, 310, Point(fp(3008), fp(-4000)), 0, True, False, False, False, False, 1.0)
        ]
        st = state(2960, -3820, 180)
        st.level = SimpleNamespace(episode=1, map=1)
        st.navigation = SimpleNamespace(
            forward_open=False,
            front_block_distance_fp=fp(16),
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=248,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(2912), y_fp=fp(-3840)),
                    midpoint=SimpleNamespace(x_fp=fp(2912), y_fp=fp(-3840)),
                    nearest_distance_fp=fp(52),
                    distance_fp=fp(52),
                )
            ],
            direction_probes=[
                probe(-90, open=True, distance=96),
                probe(90, open=True, distance=96),
            ],
        )

        action = planner._portal_route_action(
            {"point": Point(fp(2960), fp(-3820)), "angle": 180},
            route,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="open_use_line",
            detail={"skill": "sector_route_to_use_line", "route": 1},
            state=st,
        )

        self.assertIsNotNone(action)
        self.assertNotEqual(action.door_line_id, 248)
        self.assertNotEqual(action.detail.get("skill"), "final_corridor_use_line")
    def test_final_corridor_side_doors_skipped_by_live_use_selectors(self):
        snap = snapshot(
            [vertex(0, 2912, -3776), vertex(1, 2912, -3904)],
            [line(248, 0, 1, special=1, front_sector=67, back_sector=68, two_sided=True, door=True, use_trigger=True)],
            sectors=[sector(67), sector(68)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(2960, -3820, 180)
        st.level = SimpleNamespace(episode=1, map=1)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=248,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(2912), y_fp=fp(-3840)),
                    midpoint=SimpleNamespace(x_fp=fp(2912), y_fp=fp(-3840)),
                    nearest_distance_fp=fp(52),
                    distance_fp=fp(52),
                )
            ],
            forward_open=True,
            front_block_distance_fp=fp(160),
        )
        player = {"point": Point(fp(2960), fp(-3820)), "angle": 180}

        last_chance = planner._last_chance_live_use_line_action(
            st,
            player,
            FakeRawAgentPb2,
            DoorMemory(),
            max_distance_fp=512 * FP_UNIT,
        )
        live = planner._navigation_use_line_action(
            st,
            player,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="open_use_line",
            detail={"skill": "planner_live_use_line"},
            max_distance_fp=512 * FP_UNIT,
            include_open=True,
        )

        self.assertIsNone(last_chance)
        self.assertIsNone(live)
    def test_final_corridor_side_doors_skipped_by_static_and_sector_use_routes(self):
        snap = snapshot(
            [vertex(0, 2912, -3776), vertex(1, 2912, -3904)],
            [line(247, 0, 1, special=1, front_sector=67, back_sector=68, two_sided=True, door=True, use_trigger=True)],
            sectors=[sector(67), sector(68)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(2960, -3820, 180)
        st.level = SimpleNamespace(episode=1, map=1)
        st.navigation = SimpleNamespace(use_lines=[])
        player = {"point": Point(fp(2960), fp(-3820)), "angle": 180}
        line_rt = planner._line_by_id(247)
        self.assertIsNotNone(line_rt)

        static = planner._line_objective_action(player, FakeRawAgentPb2, DoorMemory(), exit_only=False, state=st)
        sector_route = planner._sector_route_to_line_action(
            player,
            line_rt,
            FakeRawAgentPb2,
            DoorMemory(),
            exit_only=False,
            state=st,
        )

        self.assertIsNone(static)
        self.assertIsNone(sector_route)
    def test_passable_portal_squeeze_centers_before_endpoint_crossing(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(385, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        st = state(48, 112, 270)
        st.navigation = SimpleNamespace(
            forward_open=False,
            front_block_distance_fp=fp(16),
            direction_probes=[probe(90, open=True, distance=192)],
        )
        player = {"point": Point(fp(48), fp(112)), "angle": 270}
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "center_passable_portal")
        self.assertEqual(action.door_line_id, 385)
        self.assertIn("mt", action.detail)
    def test_late_route_passable_portal_centers_endpoint_before_blocking_probe(self):
        snap = snapshot(
            [vertex(0, -64, 0), vertex(1, 64, 0)],
            [line(315, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        st = state(-88, 176, 286)
        st.navigation = SimpleNamespace(
            forward_open=True,
            front_block_distance_fp=fp(96),
            direction_probes=[probe(90, open=True, distance=192)],
        )
        player = {"point": Point(fp(-88), fp(176)), "angle": 286}
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line", "route": 5},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "center_passable_portal")
        self.assertEqual(action.door_line_id, 315)
        self.assertIsNotNone(action.action.raw)
        self.assertIn("mt", action.detail)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertGreater(action.action.raw.side_move, 0)
    def test_early_route_passable_portal_does_not_endpoint_center(self):
        snap = snapshot(
            [vertex(0, -64, 0), vertex(1, 64, 0)],
            [line(40, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(1, [2], DoorMemory())
        self.assertIsNotNone(route)
        st = state(-88, 176, 286)
        st.navigation = SimpleNamespace(
            forward_open=True,
            front_block_distance_fp=fp(96),
            direction_probes=[probe(90, open=True, distance=192)],
        )
        player = {"point": Point(fp(-88), fp(176)), "angle": 286}
        action = planner._portal_route_action(
            player,
            route,
            FakeRawAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line", "route": 21},
            state=st,
        )
        self.assertIsNotNone(action)
        self.assertNotEqual(action.detail.get("skill"), "center_passable_portal")
    def test_portal_route_skips_close_passable_backtrack(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        route = [
            PortalEdge(1, 2, 10, Point(fp(-48), fp(0)), 0, True, False, False, False, False, 1.0),
            PortalEdge(2, 3, 11, Point(fp(320), fp(0)), 0, True, False, False, False, False, 1.0),
        ]
        action = planner._portal_route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["line"], 11)
        self.assertEqual(action.detail["skipped_line"], 10)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
    def test_static_health_items_are_available_from_wad_things(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(11, 2011, 96, 0), thing(12, 2012, 192, 0)])
        planner = SpatialPlanner(snap, cell_units=96)

        self.assertEqual([item.kind for item in planner.health_items], ["stimpack", "medkit"])
        self.assertEqual([item.value for item in planner.health_items], [10, 25])
    def test_centered_blocked_passable_portal_backs_out_instead_of_crossing_in_place(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        edge = PortalEdge(
            src=1,
            dst=2,
            line_id=10,
            point=Point(fp(64), fp(0)),
            special=0,
            passable=True,
            use_line=False,
            door=False,
            walk_trigger=False,
            exit=False,
            cost=1.0,
        )
        st = state(64, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=False,
            front_block_distance_fp=fp(16),
            back_open=True,
            direction_probes=[
                probe(-90, open=False, distance=0),
                probe(90, open=False, distance=0),
            ],
        )
        action = planner._passable_portal_squeeze_action(
            st,
            {"point": Point(fp(64), fp(0)), "angle": 0},
            edge,
            Point(fp(96), fp(0)),
            FakeRawAgentPb2,
            "press_exit",
            {"skill": "sector_route_to_exit_line", "route": 10},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(action.detail["action"], "reset_blocked_passable_portal")
    def test_sector_route_uses_raw_forward_steering_when_available(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._turn_or_forward(
            player,
            Point(fp(256), fp(128)),
            FakeRawAgentPb2,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertEqual(action.detail["action"], "steer_forward")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertGreater(action.action.raw.angle_turn, 0)
        self.assertLessEqual(abs(action.action.raw.angle_turn), 1536)
    def test_close_sector_portal_steers_on_small_angle_error(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._turn_or_forward(
            player,
            Point(fp(240), fp(48)),
            FakeRawAgentPb2,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertEqual(action.detail["action"], "steer_forward")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.angle_turn, 0)
    def test_close_navcell_to_portal_steers_forward_instead_of_orbiting_waypoint(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 333}
        action = planner._turn_or_forward(
            player,
            Point(fp(24), fp(12)),
            FakeRawAgentPb2,
            skill="route_progression",
            detail={"skill": "navcell_to_portal", "portal_line": 385},
            door_line_id=385,
        )
        self.assertEqual(action.detail["action"], "steer_forward")
        self.assertEqual(action.door_line_id, 385)
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertNotEqual(action.action.raw.angle_turn, 0)
    def test_sector_route_door_line_turns_instead_of_reversing_through_leaf(self):
        snap = snapshot(
            [vertex(0, 96, -64), vertex(1, 96, 64)],
            [line(326, 0, 1, special=1, front_sector=4, back_sector=26, two_sided=True, door=True, use_trigger=True)],
            sectors=[sector(4), sector(26)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        player = {"point": Point(fp(80), fp(0)), "angle": 0}
        action = planner._turn_or_forward(
            player,
            Point(fp(-32), fp(0)),
            FakeRawAgentPb2,
            skill="open_use_line",
            detail={"skill": "sector_route_to_use_line"},
            door_line_id=326,
        )

        self.assertEqual(action.detail["action"], "turn")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_BACKWARD)
    def test_short_forward_nudge_scales_duration_by_distance(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._turn_or_forward(
            player,
            Point(fp(48), fp(0)),
            FakeAgentPb2,
            skill="press_exit",
            detail={"skill": "live_exit_line"},
        )
        self.assertEqual(action.detail["action"], "forward")
        self.assertLess(action.action.duration_tics, 14)
        self.assertGreaterEqual(action.action.duration_tics, 2)
    def test_exit_sector_route_behind_player_turns_instead_of_reversing(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, -64), vertex(1, 0, 64)], []), cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._turn_or_forward(
            player,
            Point(fp(-48), fp(0)),
            FakeAgentPb2,
            skill="press_exit",
            detail={"skill": "sector_route_to_exit_line"},
        )
        self.assertIn(action.action.action, {FakeAgentPb2.ACTION_TURN_LEFT, FakeAgentPb2.ACTION_TURN_RIGHT})
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_BACKWARD)
    def test_large_turns_are_capped_to_avoid_overshoot_cycles(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._turn(player, 179.0, FakeAgentPb2, "press_exit", {"skill": "sector_route_to_exit_line"})
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_TURN_LEFT)
        self.assertLessEqual(action.action.amount, 26)
        self.assertLessEqual(action.action.duration_tics, 3)
    def test_local_blocked_route_probe_uses_lateral_space(self):
        planner = SpatialPlanner(snapshot([], []), cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=False,
            direction_probes=[
                SimpleNamespace(angle_offset_degrees=-90, open=True, block_distance_fp=fp(256)),
                SimpleNamespace(angle_offset_degrees=90, open=False, block_distance_fp=fp(64)),
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}

        action = planner._local_probe_action(
            st,
            player,
            Point(fp(128), fp(0)),
            FakeAgentPb2,
            skill="seek_enemy",
            detail={"skill": "planner_probe_blocked_route"},
        )

        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_STRAFE_RIGHT)
        self.assertEqual(action.detail["action"], "probe_strafe")
    def test_local_blocked_route_probe_backs_out_when_only_back_is_open(self):
        planner = SpatialPlanner(snapshot([], []), cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=False,
            back_open=True,
            direction_probes=[
                SimpleNamespace(angle_offset_degrees=-90, open=False, block_distance_fp=fp(96)),
                SimpleNamespace(angle_offset_degrees=90, open=False, block_distance_fp=fp(96)),
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}

        action = planner._local_probe_action(
            st,
            player,
            Point(fp(128), fp(0)),
            FakeAgentPb2,
            skill="seek_enemy",
            detail={"skill": "planner_probe_blocked_route"},
        )

        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(action.detail["action"], "probe_backoff")
    def test_vantage_nodes_skip_repeatedly_blocked_los_route_endpoint(self):
        planner = SpatialPlanner(snapshot([], []), cell_units=96)
        bad = Point(fp(128), fp(0))
        alternate = Point(fp(256), fp(0))
        planner.nodes = [bad, alternate]
        planner._last_los_route_endpoint_key = planner._grid_key(bad)

        planner._mark_last_los_route_blocked()
        planner._mark_last_los_route_blocked()
        result = planner._vantage_nodes(Point(fp(512), fp(0)), Point(fp(0), fp(0)))

        self.assertEqual(result[0], 1)
        self.assertNotIn(0, result)
    def test_complete_level_exit_does_not_detour_to_generic_use_line(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
            ],
            [line(151, 0, 1, special=1, door=True, use_trigger=True)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=151,
                    special=1,
                    tag=0,
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(64),
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                )
            ]
        )

        action = planner.objective_action(st, ("complete_level", "exit", "use", "explore"), FakeAgentPb2, DoorMemory())

        self.assertTrue(action is None or action.door_line_id != 151)
        if action is not None:
            self.assertNotIn(
                action.detail.get("skill"),
                {"planner_use_line", "last_chance_live_use_line", "sector_route_to_use_line"},
            )
    def test_portal_route_treats_linked_open_door_face_as_open(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 48, 64),
                vertex(3, 48, -64),
            ],
            [
                line(326, 0, 1, special=1, front_sector=4, back_sector=26, two_sided=True, door=True, use_trigger=True),
                line(329, 2, 3, special=1, front_sector=25, back_sector=26, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(4), sector(25), sector(26)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(4, [25], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.record_route_contact(326)
        memory.record_success(329)

        action = planner._portal_route_action(
            {"point": Point(fp(80), fp(0)), "angle": 180},
            route,
            FakeRawAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "sector_route_to_use_line", "route": len(route)},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.door_line_id, 326)
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertNotIn(action.detail.get("action"), {"retreat_retried_use_line", "force_follow_retried_use_line"})
    def test_portal_route_reuses_current_face_when_linked_open_face_is_blocked(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 48, 64),
                vertex(3, 48, -64),
            ],
            [
                line(326, 0, 1, special=1, front_sector=4, back_sector=26, two_sided=True, door=True, use_trigger=True),
                line(329, 2, 3, special=1, front_sector=25, back_sector=26, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(4), sector(25), sector(26)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        route = planner._sector_route(25, [4], DoorMemory())
        self.assertIsNotNone(route)
        memory = DoorMemory()
        memory.observe_line(329, special=1)
        memory.record_route_contact(329)
        memory.record_success(326)
        st = state(32, 0, 0)
        st.navigation = SimpleNamespace(forward_open=False, front_block_distance_fp=fp(16), back_open=True)

        action = planner._portal_route_action(
            {"point": Point(fp(32), fp(0)), "angle": 0},
            route,
            FakeRawAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "sector_route_to_use_line", "route": len(route)},
            state=st,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.door_line_id, 329)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.detail["action"], "reuse_linked_blocked_face")
        self.assertEqual(memory.state_for(329), "opening")
    def test_low_health_final_corridor_opening_uses_raw_sprint(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(325, special=1)
        memory.record_success(325)
        st = state(0, 0, 0)
        st.player.health = 9
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=325,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(40), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(40), y_fp=fp(0)),
                    nearest_distance_fp=fp(40),
                    distance_fp=fp(40),
                )
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._navigation_use_line_action(
            st,
            player,
            FakeRawAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "final_corridor_use_line"},
            include_open=True,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "final_corridor_sprint_opening")
        self.assertEqual(action.action.raw.forward_move, 62)
        self.assertEqual(action.action.duration_tics, 10)
    def test_healthy_final_corridor_opening_uses_raw_sprint(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(325, special=1)
        memory.record_success(325)
        st = state(0, 0, 0)
        st.player.health = 53
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=325,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(40), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(40), y_fp=fp(0)),
                    nearest_distance_fp=fp(40),
                    distance_fp=fp(40),
                )
            ],
        )
        action = planner._navigation_use_line_action(
            st,
            {"point": Point(fp(0), fp(0)), "angle": 0},
            FakeRawAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "final_corridor_use_line"},
            include_open=True,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "final_corridor_sprint_opening")
        self.assertEqual(action.detail["hp"], 53)
        self.assertEqual(action.action.raw.forward_move, 62)
        self.assertEqual(action.action.duration_tics, 10)
    def test_e1m1_final_corridor_override_prefers_exit_corridor_door(self):
        snap = snapshot(
            [
                vertex(0, 3072, -4016),
                vertex(1, 2944, -4016),
                vertex(2, 2944, -4032),
                vertex(3, 3072, -4032),
                vertex(4, 2944, -3904),
                vertex(5, 2944, -3776),
            ],
            [
                line(340, 0, 1, special=1, door=True, use_trigger=True, two_sided=True, front_sector=75, back_sector=76),
                line(341, 2, 3, special=1, door=True, use_trigger=True, two_sided=True, front_sector=73, back_sector=76),
                line(247, 4, 5, special=1, door=True, use_trigger=True, two_sided=True, front_sector=56, back_sector=68),
            ],
            sectors=[sector(56), sector(68), sector(73), sector(75), sector(76)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        calls: list[int] = []

        def fake_sector_route(player, line_obj, agent_pb2, door_memory, *, exit_only, state=None):
            calls.append(line_obj.id)
            return PlanAction(
                skill="open_use_line",
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=1, duration_tics=1),
                door_line_id=line_obj.id,
                detail={"skill": "sector_route_to_use_line", "line": line_obj.id},
            )

        planner._sector_route_to_line_action = fake_sector_route  # type: ignore[method-assign]
        st = state(3000, -3933, 270)
        st.level = SimpleNamespace(episode=1, map=1)

        action = planner._e1m1_final_corridor_override(
            st,
            {"point": Point(fp(3000), fp(-3933)), "angle": 270},
            FakeAgentPb2,
            DoorMemory(),
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.door_line_id, 340)
        self.assertEqual(action.detail["skill"], "e1m1_final_corridor_override")
        self.assertEqual(action.detail["preferred_line"], 340)
        self.assertEqual(calls, [340])
    def test_sector_route_skips_exhausted_closed_door_edge(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
            ],
            [
                line(
                    151,
                    0,
                    1,
                    front_sector=1,
                    back_sector=2,
                    two_sided=True,
                    door=True,
                    special=1,
                    blocking=True,
                    sight_blocking=True,
                )
            ],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(151, special=1)
        for _ in range(memory.max_failures + 1):
            memory.record_route_contact(151)
        self.assertFalse(memory.can_retry(151))

        self.assertIsNone(planner._sector_route(1, [2], memory))
    def test_sector_route_keeps_exhausted_door_edge_after_open_evidence(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
            ],
            [
                line(
                    151,
                    0,
                    1,
                    front_sector=1,
                    back_sector=2,
                    two_sided=True,
                    door=True,
                    special=1,
                    blocking=True,
                    sight_blocking=True,
                )
            ],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(151, special=1)
        for _ in range(memory.max_failures + 1):
            memory.record_route_contact(151)
        memory.record_success(151)
        self.assertFalse(memory.can_retry(151))
        self.assertTrue(memory.is_open(151))

        route = planner._sector_route(1, [2], memory)

        self.assertIsNotNone(route)
        self.assertEqual(route[0].line_id, 151)


if __name__ == "__main__":
    unittest.main()
