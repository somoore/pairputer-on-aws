"""Health-seek routing and hazard-sector escape for Agent DOOM's spatial planner."""

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
    HEALTH_SEEK_MAX_TARGET_THREAT,
    HEALTH_SEEK_RELEASE,
    HEALTH_SEEK_TRIGGER,
    HealthItemRuntime,
    Point,
    PortalEdge,
    Route,
    RouteStep,
    SpatialPlanner,
)
from world_memory import WorldMemory  # noqa: E402

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


class TestAgentDoomPlannerHealthHazard(unittest.TestCase):
    def test_hazard_escape_routes_from_damaging_sector_to_safe_neighbor(self):
        snap = snapshot(
            [
                vertex(0, 0, -64),
                vertex(1, 0, 64),
                vertex(2, 128, 64),
                vertex(3, 128, -64),
                vertex(4, 256, 64),
                vertex(5, 256, -64),
            ],
            [
                line(10, 0, 1, front_sector=1),
                line(11, 1, 2, front_sector=1),
                line(12, 2, 3, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(13, 3, 0, front_sector=1),
                line(14, 2, 4, front_sector=2),
                line(15, 4, 5, front_sector=2),
                line(16, 5, 3, front_sector=2),
            ],
            sectors=[sector(1, damaging=True), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)

        plan = planner.hazard_escape_action(state(64, 0, 0), FakeAgentPb2, DoorMemory())

        self.assertIsNotNone(plan)
        self.assertEqual(plan.skill, "route_progression")
        # Escape sprints for the NEAREST standable ground (nav cells only exist on safe
        # floors) — the old sector route was traced marching ACROSS a nukage pool to a
        # far safe sector (hp 90->75 wading). Sector routing remains only as fallback.
        self.assertEqual(plan.detail["skill"], "hazard_nearest_ground_escape")
        self.assertEqual(plan.detail["hazard_sector"], 1)
    def test_hazard_escape_route_does_not_backtrack_when_low_health_under_los(self):
        snap = snapshot(
            [
                vertex(0, 0, -64),
                vertex(1, 0, 64),
                vertex(2, 128, 64),
                vertex(3, 128, -64),
                vertex(4, 256, 64),
                vertex(5, 256, -64),
            ],
            [
                line(10, 0, 1, front_sector=1),
                line(11, 1, 2, front_sector=1),
                line(12, 2, 3, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(13, 3, 0, front_sector=1),
                line(14, 2, 4, front_sector=2),
                line(15, 4, 5, front_sector=2),
                line(16, 5, 3, front_sector=2),
            ],
            sectors=[sector(1, damaging=True), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(64, 0, 180, [{"id": 7, "pos": (64, 128), "los": True}])
        st.player.health = 10

        plan = planner.hazard_escape_action(st, FakeRawAgentPb2, DoorMemory())

        self.assertIsNotNone(plan)
        # Same safety intent as before: escaping acid at low health under enemy LOS must
        # never turn into a break-LOS backtrack — the nearest-ground sprint always
        # TRANSLATES toward standable floor.
        self.assertEqual(plan.detail["skill"], "hazard_nearest_ground_escape")
        self.assertNotEqual(plan.detail.get("action"), "break_los_low_health")
        self.assertGreater(int(plan.action.raw.forward_move), 0, "escape must translate, not turn in place")
    def test_hazard_escape_ignores_safe_sector(self):
        snap = snapshot(
            [
                vertex(0, 0, -64),
                vertex(1, 0, 64),
                vertex(2, 128, 64),
                vertex(3, 128, -64),
            ],
            [line(10, 0, 1, front_sector=1), line(11, 1, 2, front_sector=1), line(12, 2, 3, front_sector=1), line(13, 3, 0, front_sector=1)],
            sectors=[sector(1)],
        )
        planner = SpatialPlanner(snap, cell_units=96)

        self.assertIsNone(planner.hazard_escape_action(state(64, 0, 0), FakeAgentPb2, DoorMemory()))
    def test_low_health_passable_backtrack_evades_instead_of_turning_around_under_los(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        route = [PortalEdge(74, 78, 313, Point(fp(-192), fp(0)), 0, True, False, False, False, False, 1.0)]
        st = state(0, 0, 0, enemy=(48, 0))
        st.player.health = 50
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            forward_open=False,
            front_block_distance_fp=fp(48),
            back_open=True,
            direction_probes=[
                probe(-90, open=True, distance=96),
                probe(90, open=True, distance=96),
            ],
        )
        planner._edge = lambda _a, _b, _door_memory: None  # type: ignore[method-assign]
        planner._route = lambda _start, _targets, _door_memory: Route([RouteStep(Point(fp(-96), fp(0)))], cost=1.0)  # type: ignore[method-assign]
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
        self.assertEqual(action.detail["avoid"], "low_health_backtrack")
        self.assertEqual(action.door_line_id, 313)
        self.assertNotEqual(action.detail.get("skill"), "navcell_to_portal")
    def test_low_health_complete_level_routes_to_health_before_progression(self):
        snap = snapshot(
            [vertex(0, 256, -64), vertex(1, 256, 64)],
            [line(330, 0, 1, special=11, exit=True)],
            things=[thing(11, 2012, 192, 0)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.player.health = HEALTH_SEEK_TRIGGER - 1

        action = planner.objective_action(st, ("complete_level", "exit", "use", "explore"), FakeAgentPb2, DoorMemory(), WorldMemory())

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertEqual(action.detail["skill"], "route_to_health")
        self.assertEqual(action.detail["health_item"], "medkit")
        self.assertEqual(action.detail["thing"], 11)
        self.assertLess(action.detail["target_threat"], 30.0)
    def test_critical_health_route_breaks_los_before_exposed_pickup_run(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(11, 2012, 192, 0)])
        planner = SpatialPlanner(snap, cell_units=96)
        planner._route = lambda *_args, **_kwargs: Route([RouteStep(Point(fp(128), fp(0)))], cost=1.0)  # type: ignore[method-assign]
        planner._route_endpoint_near = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
        planner.has_line_of_sight = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
        st = state(0, 0, 0, enemy=(512, 0))
        st.player.health = HEALTH_SEEK_TRIGGER - 1
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(direction_probes=[probe(90, open=True, distance=256)])

        action = planner.objective_action(
            st,
            ("complete_level", "exit", "use", "explore"),
            FakeAgentPb2,
            DoorMemory(),
            WorldMemory(),
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "retreat")
        self.assertEqual(action.detail["skill"], "health_route_break_los")
        self.assertEqual(action.detail["blocked_skill"], "route_to_health")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
    def test_health_seek_releases_above_high_watermark(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], [], things=[thing(11, 2012, 192, 0)]), cell_units=96)
        planner._health_seek_active = True
        st = state(0, 0, 0)
        st.player.health = HEALTH_SEEK_RELEASE

        action = planner._needed_health_action(st, planner._player(st), FakeAgentPb2, DoorMemory(), WorldMemory())

        self.assertIsNone(action)
        self.assertFalse(planner._health_seek_active)
    def test_health_route_skips_pickup_inside_hitscan_threat(self):
        snap = snapshot(
            [vertex(0, 0, 0)],
            [],
            things=[
                thing(11, 2012, 96, 0),
                thing(12, 2011, 96, 192),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.player.health = HEALTH_SEEK_TRIGGER - 1
        st.enemies = [
            SimpleNamespace(
                object=SimpleNamespace(
                    id=77,
                    type_id=3004,
                    health=20,
                    distance_fp=fp(96),
                    position=SimpleNamespace(x_fp=fp(96), y_fp=0, z_fp=0),
                ),
                line_of_sight=True,
            )
        ]
        planner.has_line_of_sight = lambda point, _enemy: int(point.y) == 0  # type: ignore[method-assign]

        action = planner.objective_action(st, ("complete_level", "exit", "use", "explore"), FakeAgentPb2, DoorMemory(), WorldMemory())

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "route_to_health")
        self.assertEqual(action.detail["thing"], 12)
        self.assertEqual(action.detail["health_item"], "stimpack")
        self.assertLess(action.detail["target_threat"], 30.0)
    def test_health_seek_rejects_sector_route_with_lethal_next_portal(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner.sectors = {
            1: SimpleNamespace(center=Point(fp(0), fp(0)), damaging=False),
            2: SimpleNamespace(center=Point(fp(256), fp(0)), damaging=False),
        }
        planner._portal_graph = {
            1: [PortalEdge(1, 2, 340, Point(fp(64), fp(0)), 0, True, False, False, False, False, 1.0)]
        }
        planner.health_items = [HealthItemRuntime(11, 2012, "medkit", 25, Point(fp(256), fp(0)), 2)]
        planner._route = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        planner._threats = [
            {"id": 77 + idx, "point": Point(fp(64), fp(0)), "threat": "projectile", "type_id": 3001}
            for idx in range(4)
        ]
        planner.has_line_of_sight = lambda _point, _target: False  # type: ignore[method-assign]
        st = state(0, 0, 0)
        st.player.health = HEALTH_SEEK_TRIGGER - 1

        action = planner._needed_health_action(st, planner._player(st), FakeAgentPb2, DoorMemory(), WorldMemory())

        self.assertIsNone(action)
        self.assertEqual(planner._last_status, "no_safe_health_route")
        self.assertGreaterEqual(planner._portal_threat_multiplier(planner._portal_graph[1][0]), HEALTH_SEEK_MAX_TARGET_THREAT)
    def test_health_seek_chooses_safe_sector_route_over_short_hot_route(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner.sectors = {
            1: SimpleNamespace(center=Point(fp(0), fp(0)), damaging=False),
            2: SimpleNamespace(center=Point(fp(256), fp(0)), damaging=False),
            3: SimpleNamespace(center=Point(fp(256), fp(800)), damaging=False),
        }
        planner._portal_graph = {
            1: [
                PortalEdge(1, 2, 340, Point(fp(64), fp(0)), 0, True, False, False, False, False, 1.0),
                PortalEdge(1, 3, 520, Point(fp(64), fp(800)), 0, True, False, False, False, False, 6.0),
            ]
        }
        planner.health_items = [
            HealthItemRuntime(11, 2012, "medkit", 25, Point(fp(256), fp(0)), 2),
            HealthItemRuntime(12, 2011, "stimpack", 10, Point(fp(256), fp(800)), 3),
        ]
        planner._route = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        planner._threats = [
            {"id": 77 + idx, "point": Point(fp(64), fp(0)), "threat": "projectile", "type_id": 3001}
            for idx in range(4)
        ]
        planner.has_line_of_sight = lambda _point, _target: False  # type: ignore[method-assign]
        st = state(0, 0, 0)
        st.player.health = HEALTH_SEEK_TRIGGER - 1

        action = planner._needed_health_action(st, planner._player(st), FakeRawAgentPb2, DoorMemory(), WorldMemory())

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "sector_route_to_health")
        self.assertEqual(action.detail["thing"], 12)
        self.assertEqual(action.detail["line"], 520)
        self.assertLess(action.detail["route_step_threat_mult"], HEALTH_SEEK_MAX_TARGET_THREAT)
    def test_health_memory_does_not_consume_nearby_pickup_until_health_increases(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(11, 2012, 48, 0)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.player.health = 20
        memory = WorldMemory()

        memory.update(st, planner)

        self.assertNotIn(11, memory.consumed_health_items)

        st.player.health = 45
        memory.update(st, planner)

        self.assertIn(11, memory.consumed_health_items)


if __name__ == "__main__":
    unittest.main()
