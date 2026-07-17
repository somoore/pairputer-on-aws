"""Live-navigation doors, walk-triggers, key acquisition, frontier exploration, and WAD/door-memory for Agent DOOM's spatial planner."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))

from door_memory import DoorMemory  # noqa: E402
from map_cache import MapCache  # noqa: E402
from planner import (  # noqa: E402
    FP_UNIT,
    Point,
    Route,
    RouteStep,
    SpatialPlanner,
)
from probe_runtime import ProbeBatcher  # noqa: E402
from wad_map import WadMapLoader  # noqa: E402
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
    _write_minimal_wad,
)


class TestAgentDoomPlannerNavKeysFrontier(unittest.TestCase):
    def _required_red_key_fixture(self) -> tuple[SpatialPlanner, DoorMemory, WorldMemory]:
        snap = snapshot(
            [
                vertex(0, 0, -64),
                vertex(1, 0, 64),
                vertex(2, 128, -64),
                vertex(3, 128, 64),
                vertex(4, 256, -64),
                vertex(5, 256, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=-1, two_sided=False, blocking=True),
                line(11, 2, 3, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(12, 4, 5, front_sector=2, back_sector=-1, two_sided=False, blocking=True),
            ],
            sectors=[sector(1), sector(2)],
            things=[thing(41, 13, 224, 0)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(527, special=28)
        memory.record_failure(527, status="key_required")
        world = WorldMemory()
        return planner, memory, world

    def test_enemy_objective_routes_to_visibility_cell(self):
        snap = snapshot(
            [
                vertex(0, 128, -128),
                vertex(1, 128, 128),
            ],
            [line(10, 0, 1)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        action = planner.objective_action(
            state(0, 0, 0, enemy=(256, 0)),
            ("find_enemy", "shoot"),
            FakeAgentPb2,
            DoorMemory(),
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "close_visible_contact")
        self.assertEqual(action.detail["skill"], "planner_route_to_los")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertGreater(planner.summary()["route"], 0)
    def test_dynamic_tag_gate_is_closed_until_matching_tag_opens(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 128, -64),
                vertex(3, 128, 64),
            ],
            [
                line(20, 0, 1, special=103, tag=12, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, door=True, use_trigger=True),
                line(30, 2, 3, front_sector=2, back_sector=3, two_sided=True, passable=False, blocking=False),
            ],
            sectors=[sector(1), sector(2), sector(3, tag=12, ceiling=0)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()

        self.assertIsNone(planner._sector_route(1, [3], memory))

        memory.observe_line(20, special=103, tag=12)
        memory.record_attempt(20, status="sector_portal_use")
        route = planner._sector_route(1, [3], memory)

        self.assertIsNotNone(route)
        self.assertEqual([edge.line_id for edge in route], [20, 30])
        self.assertEqual(route[-1].tag_gate, 12)
    def test_dynamic_tag_gate_portal_action_crosses_without_use(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
            ],
            [
                line(30, 0, 1, front_sector=2, back_sector=3, two_sided=True, passable=False, blocking=False),
            ],
            sectors=[sector(2), sector(3, tag=12, ceiling=0)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(20, special=103, tag=12)
        memory.record_attempt(20, status="sector_portal_use")
        route = planner._sector_route(2, [3], memory)
        self.assertIsNotNone(route)

        action = planner._portal_route_action(
            {"point": Point(fp(48), fp(0)), "angle": 0},
            route,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "sector_route_to_use_line", "route": len(route)},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.door_line_id, 30)
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
    def test_nav_cells_keep_sector_identity(self):
        snap = snapshot(
            [
                vertex(0, 0, 0),
                vertex(1, 128, 0),
                vertex(2, 128, 128),
                vertex(3, 0, 128),
            ],
            [
                line(1, 0, 1, front_sector=7),
                line(2, 1, 2, front_sector=7),
                line(3, 2, 3, front_sector=7),
                line(4, 3, 0, front_sector=7),
            ],
            sectors=[sector(7)],
        )
        planner = SpatialPlanner(snap, cell_units=64)
        self.assertGreater(planner.summary()["cells"], 0)
        self.assertTrue(all(cell.sector_id == 7 for cell in planner.nav_cells))
    def test_explore_probe_uses_live_forward_probe_when_graph_route_missing(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._route = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=True,
            direction_probes=[
                probe(-30, open=True, distance=96),
                probe(0, open=True, distance=96),
                probe(30, open=True, distance=96),
            ],
        )

        action = planner.objective_action(st, ("explore",), FakeAgentPb2, DoorMemory())

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "planner_probe_explore")
        self.assertEqual(action.detail["action"], "forward")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
    def test_objective_uses_stale_live_use_line_before_blind_explore(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
            ],
            [
                line(389, 0, 1, special=103, front_sector=1, back_sector=2, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(389, special=103)
        for _ in range(3):
            memory.record_attempt(389, status="planner_live_use")
        memory.record_stale_open(389)
        self.assertTrue(planner._line_retry_exhausted(memory, 389, special=103))
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=389,
                    special=103,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=0),
                    nearest_distance_fp=fp(19),
                )
            ],
            route_waypoint=None,
        )

        action = planner.objective_action(st, ("complete_level", "exit", "use", "explore"), FakeRawAgentPb2, memory)

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 389)
        self.assertEqual(action.detail["skill"], "last_chance_live_use_line")
        self.assertEqual(action.detail["action"], "use")

        memory.record_stale_open(389)
        follow = planner.objective_action(st, ("complete_level", "exit", "use", "explore"), FakeRawAgentPb2, memory)

        self.assertIsNotNone(follow)
        self.assertEqual(follow.skill, "open_use_line")
        self.assertEqual(follow.door_line_id, 389)
        self.assertEqual(follow.detail["skill"], "last_chance_live_use_line")
        self.assertEqual(follow.detail["action"], "force_follow_live_use_line")
        self.assertIsNotNone(follow.action.raw)
        self.assertGreater(follow.action.raw.forward_move, 0)

        memory.record_force_follow_stalled(389)
        self.assertTrue(memory.live_line_suppressed(389))
        memory.record_route_contact(389)
        self.assertTrue(memory.live_line_suppressed(389))
        suppressed = planner._last_chance_live_use_line_action(st, {"point": Point(0, 0), "angle": 0}, FakeRawAgentPb2, memory)
        self.assertIsNone(suppressed)
        live = planner._navigation_use_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeRawAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "planner_live_use_line"},
            include_open=True,
        )
        self.assertIsNone(live)

        resync = planner._last_chance_live_use_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeRawAgentPb2,
            memory,
            allow_suppressed=True,
        )
        self.assertIsNotNone(resync)
        self.assertEqual(resync.detail["resync"], "suppressed_live_use")
        self.assertEqual(resync.door_line_id, 389)

        memory.record_force_follow_stalled(389)
        memory.record_force_follow_stalled(389)
        capped = planner._last_chance_live_use_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeRawAgentPb2,
            memory,
            allow_suppressed=True,
        )
        self.assertIsNone(capped)
    def test_hidden_melee_rush_does_not_spam_walk_trigger_route(self):
        snap = snapshot(
            [
                vertex(0, 96, 96),
                vertex(1, 96, 160),
                vertex(2, 64, -64),
                vertex(3, 64, 64),
            ],
            [
                line(988, 0, 1, special=97, walk_trigger=True, passable=True, blocking=False, sight_blocking=False),
                line(1, 2, 3, blocking=True, sight_blocking=True),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = SimpleNamespace(
            player=SimpleNamespace(
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0, z_fp=0), angle_degrees=0)
            ),
            enemies=[
                SimpleNamespace(
                    object=SimpleNamespace(
                        id=77,
                        type_id=3002,
                        health=150,
                        distance_fp=fp(128),
                        position=SimpleNamespace(x_fp=fp(128), y_fp=0, z_fp=0),
                    ),
                    line_of_sight=False,
                )
            ],
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=fp(256),
                back_open=True,
                direction_probes=[],
                use_lines=[
                    SimpleNamespace(
                        line_id=988,
                        special=97,
                        nearest_point=SimpleNamespace(x_fp=fp(96), y_fp=fp(128)),
                        midpoint=SimpleNamespace(x_fp=fp(96), y_fp=fp(128)),
                        nearest_distance_fp=fp(80),
                        distance_fp=fp(80),
                    )
                ],
            ),
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
        )

        action = planner.objective_action(st, ["find_enemy", "attack"], FakeRawAgentPb2, DoorMemory())

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "hidden_melee_rush_evasion")
        self.assertEqual(action.detail["action"], "kite_back_hidden")
        self.assertNotEqual(action.detail.get("line"), 988)
        self.assertLess(action.action.raw.forward_move, 0)
    def test_walk_trigger_contact_route_respects_retry_exhaustion(self):
        snap = snapshot(
            [vertex(0, 96, 96), vertex(1, 96, 160)],
            [line(988, 0, 1, special=2, tag=12, walk_trigger=True, passable=True, blocking=False, sight_blocking=False)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(988, special=2, tag=12)
        for _ in range(3):
            memory.record_route_abandoned(988, passable=True)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            route_waypoint=SimpleNamespace(
                walk_trigger=True,
                exit=False,
                line=SimpleNamespace(
                    line_id=988,
                    special=2,
                    tag=12,
                    nearest_point=SimpleNamespace(x_fp=fp(96), y_fp=fp(128)),
                    midpoint=SimpleNamespace(x_fp=fp(96), y_fp=fp(128)),
                    nearest_distance_fp=fp(80),
                    distance_fp=fp(80),
                ),
            ),
            use_lines=[],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}

        action = planner._navigation_walk_trigger_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_walk_trigger_for_contact", "enemy": 77},
        )

        self.assertIsNone(action)
    def test_remembered_exit_line_probes_before_blind_explore(self):
        snap = snapshot(
            [
                vertex(0, 768, -64),
                vertex(1, 768, 64),
            ],
            [
                line(873, 0, 1, special=11, exit=True, use_trigger=True),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        planner._sector_route_to_line_action = lambda *_args, **_kw: None  # type: ignore[method-assign]
        planner._route = lambda *_args, **_kw: None  # type: ignore[method-assign]
        memory = DoorMemory()
        memory.observe_line(873, special=11, exit_line=True)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(forward_open=True, route_waypoint=None)

        action = planner.objective_action(st, ("exit", "use", "explore"), FakeAgentPb2, memory)

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertEqual(action.door_line_id, 873)
        self.assertEqual(action.detail["skill"], "remembered_exit_probe")
        self.assertNotEqual(action.detail.get("skill"), "planner_probe_explore")
    def test_remembered_progression_line_routes_before_blind_explore(self):
        snap = snapshot(
            [
                vertex(0, 512, -64),
                vertex(1, 512, 64),
            ],
            [
                line(326, 0, 1, special=1, door=True, use_trigger=True),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        planner._line_objective_action = lambda *_args, **_kw: None  # type: ignore[method-assign]
        planner._route_waypoint_action = lambda *_args, **_kw: None  # type: ignore[method-assign]
        planner._last_chance_live_use_line_action = lambda *_args, **_kw: None  # type: ignore[method-assign]
        planner._sector_route_to_line_action = lambda *_args, **_kw: None  # type: ignore[method-assign]
        planner._route = lambda *_args, **_kw: None  # type: ignore[method-assign]
        memory = DoorMemory()
        memory.observe_line(326, special=1)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(forward_open=True, route_waypoint=None)

        action = planner.objective_action(st, ("exit", "use", "explore"), FakeAgentPb2, memory)

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertEqual(action.door_line_id, 326)
        self.assertEqual(action.detail["skill"], "remembered_progression_probe")
    def test_remembered_line_candidates_exclude_key_and_blocked_lines(self):
        memory = DoorMemory(max_failures=2)
        memory.observe_line(11, special=1)
        memory.observe_line(26, special=26)
        memory.record_failure(26, status="requires_key")
        memory.observe_line(33, special=0)
        memory.record_route_abandoned(33)

        rows = memory.candidate_lines()

        line_ids = {row["line_id"] for row in rows}
        self.assertIn(11, line_ids)
        self.assertNotIn(26, line_ids)
        self.assertNotIn(33, line_ids)
    def test_probe_batcher_returns_compact_probe_groups(self):
        st = enemy_state(0, 0, 0, [{"id": 2, "pos": (128, 0), "los": True}])
        st.navigation = SimpleNamespace(
            front_block_distance_fp=fp(32),
            forward_open=False,
            back_open=True,
            left_open=True,
            right_open=False,
            direction_probes=[SimpleNamespace(angle_offset_degrees=90, open=True, block_distance_fp=fp(256), use_line_ahead=False)],
            use_lines=[SimpleNamespace(line_id=9, special=1, tag=0, nearest_distance_fp=fp(32), distance_fp=fp(64))],
        )
        st.combat = SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=2, target_health=20, target_distance_fp=fp(128))
        probes = ProbeBatcher().snapshot(st, None)
        self.assertEqual(sorted(probes), ["cmb", "mov", "use", "vis"])
        self.assertEqual(probes["cmb"]["shootable"], 1)
        self.assertEqual(probes["use"][0][:2], [9, 1])
    def test_door_memory_blocks_repeated_failed_use(self):
        memory = DoorMemory(max_failures=3)
        for _ in range(3):
            memory.record_attempt(5)
            memory.record_failure(5, status="no_progress_after_use")
        self.assertTrue(memory.is_blocked(5))
        summary = memory.summary()
        self.assertIn(5, summary["blocked"])
    def test_regular_door_failure_is_treated_as_opening(self):
        memory = DoorMemory(max_failures=3)
        memory.observe_line(151, special=1)
        memory.record_attempt(151, status="sector_portal_use")
        self.assertEqual(memory.state_for(151), "opening")
        memory.record_failure(151, status="no_progress_after_use")
        self.assertTrue(memory.is_open(151))
        self.assertFalse(memory.is_blocked(151))
        self.assertEqual(memory.state_for(151), "opening")
    def test_static_use_skips_opening_non_exit_door(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(151, 0, 1, special=1, door=True, use_trigger=True)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(151, special=1)
        memory.record_attempt(151, status="planner_use")
        line_rt = planner._line_by_id(151)
        self.assertIsNotNone(line_rt)

        action = planner._static_use_line_action(
            {"point": Point(fp(0), fp(0)), "angle": 0},
            line_rt,
            FakeAgentPb2,
            memory,
            exit_only=False,
            detail={"skill": "planner_use_line"},
        )

        self.assertIsNone(action)
    def test_live_navigation_opening_door_pushes_through_instead_of_reusing(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(151, special=1)
        memory.record_attempt(151, status="planner_live_use")
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=151,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(16), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(16), y_fp=fp(0)),
                    nearest_distance_fp=fp(16),
                    distance_fp=fp(16),
                )
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._navigation_use_line_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_use_line_for_contact"},
            include_open=True,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "follow_opening")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
    def test_live_navigation_routes_to_far_blocked_use_line(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(386, special=1)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=False,
            front_block_distance_fp=fp(32),
            use_lines=[
                SimpleNamespace(
                    line_id=386,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(256), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(256), y_fp=fp(0)),
                    nearest_distance_fp=fp(256),
                    distance_fp=fp(256),
                )
            ],
        )
        planner._nearest_nodes = lambda _point, limit=10: [Point(fp(256), fp(0))]  # type: ignore[method-assign]
        planner._route = lambda _start, _targets, _memory: Route([RouteStep(Point(fp(256), fp(0)))], cost=1)  # type: ignore[method-assign]
        player = {"point": Point(fp(0), fp(0)), "angle": 90}

        action = planner._navigation_use_line_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "planner_live_use_line"},
            max_distance_fp=384 * FP_UNIT,
            include_open=True,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "route_to_live_use_line")
        self.assertEqual(action.detail["live_line"], 386)
    def test_live_navigation_rejects_route_that_does_not_reach_far_use_line(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(389, special=103)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=False,
            front_block_distance_fp=fp(32),
            use_lines=[
                SimpleNamespace(
                    line_id=389,
                    special=103,
                    nearest_point=SimpleNamespace(x_fp=fp(320), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(320), y_fp=fp(0)),
                    nearest_distance_fp=fp(320),
                    distance_fp=fp(320),
                )
            ],
        )
        planner._nearest_nodes = lambda _point, limit=10: [Point(fp(0), fp(64))]  # type: ignore[method-assign]
        planner._route = lambda _start, _targets, _memory: Route([RouteStep(Point(fp(0), fp(64)))], cost=1)  # type: ignore[method-assign]
        player = {"point": Point(fp(0), fp(0)), "angle": 90}

        action = planner._navigation_use_line_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "planner_live_use_line"},
            max_distance_fp=384 * FP_UNIT,
            include_open=True,
        )

        self.assertIsNone(action)
        self.assertEqual(memory.state_for(389), "congested")
    def test_live_navigation_uses_close_congested_line(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(151, special=1)
        for _ in range(memory.max_failures + 1):
            memory.record_route_contact(151)
        self.assertEqual(memory.state_for(151), "congested")
        self.assertFalse(memory.can_retry(151))
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=151,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(56), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(56), y_fp=fp(0)),
                    nearest_distance_fp=fp(56),
                    distance_fp=fp(56),
                )
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}

        action = planner._navigation_use_line_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_use_line_for_contact"},
            include_open=True,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.detail["action"], "use")
    def test_live_navigation_opened_door_pushes_through_instead_of_reusing(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(325, special=1)
        memory.record_success(325)
        st = state(0, 0, 0)
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
            FakeAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "final_corridor_use_line"},
            include_open=True,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "follow_opening")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
    def test_live_navigation_opened_door_targets_far_side_when_static_line_exists(self):
        snap = snapshot(
            [vertex(0, 40, -64), vertex(1, 40, 64)],
            [line(325, 0, 1, special=1, door=True, use_trigger=True, two_sided=True, front_sector=1, back_sector=2)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(325, special=1)
        memory.record_success(325)
        st = state(80, 0, 180)
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
        player = {"point": Point(fp(80), fp(0)), "angle": 180}

        action = planner._navigation_use_line_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "planner_live_use_line"},
            include_open=True,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "follow_opening")
        self.assertEqual(action.detail["target"], "through_opening")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
    def test_live_navigation_uses_linked_open_door_face(self):
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
        memory = DoorMemory()
        memory.record_route_contact(326)
        memory.record_success(329)
        st = state(80, 0, 180)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=326,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=0),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=0),
                    nearest_distance_fp=fp(16),
                    distance_fp=fp(16),
                ),
                SimpleNamespace(
                    line_id=329,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(48), y_fp=0),
                    midpoint=SimpleNamespace(x_fp=fp(48), y_fp=0),
                    nearest_distance_fp=fp(32),
                    distance_fp=fp(32),
                ),
            ],
        )

        action = planner._navigation_use_line_action(
            st,
            {"point": Point(fp(80), fp(0)), "angle": 180},
            FakeAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "planner_live_use_line"},
            include_open=True,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.door_line_id, 326)
        self.assertEqual(action.detail["action"], "follow_opening")
        self.assertEqual(action.detail["target"], "through_opening")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
    def test_live_navigation_skips_repeated_stale_opening_use_line(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(389, special=103)
        for _ in range(3):
            memory.record_attempt(389, status="planner_live_use")
        memory.record_stale_open(389)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=389,
                    special=103,
                    nearest_point=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(48),
                )
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._navigation_use_line_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="open_use_line",
            detail={"skill": "planner_live_use_line"},
            include_open=True,
        )
        self.assertIsNone(action)
    def test_live_navigation_keeps_retrying_stale_exit_line(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(873, special=11, exit_line=True)
        for _ in range(3):
            memory.record_attempt(873, status="exit_use")
        memory.record_stale_open(873)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=873,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(48),
                )
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._navigation_use_line_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="press_exit",
            detail={"skill": "live_exit_line"},
            include_open=True,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.door_line_id, 873)
    def test_contact_route_ignores_use_line_behind_enemy_vector(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(151, special=1)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=151,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(-48), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(-48), y_fp=fp(0)),
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(48),
                )
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._navigation_use_line_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_use_line_for_contact"},
            include_open=True,
            target_point=Point(fp(512), fp(0)),
            max_target_delta_degrees=100.0,
        )
        self.assertIsNone(action)
    def test_contact_route_keeps_use_line_toward_enemy_vector(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(151, special=1)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=151,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(48),
                )
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._navigation_use_line_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_use_line_for_contact"},
            include_open=True,
            target_point=Point(fp(512), fp(0)),
            max_target_delta_degrees=100.0,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["line"], 151)
    def test_blocked_enemy_contact_uses_walk_trigger_toward_target(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=False,
            front_block_distance_fp=fp(32),
            use_lines=[
                SimpleNamespace(
                    line_id=195,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(64),
                    distance_fp=fp(64),
                )
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._navigation_walk_trigger_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_walk_trigger_for_contact"},
            target_point=Point(fp(512), fp(0)),
            max_target_delta_degrees=100.0,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["line"], 195)
        self.assertEqual(action.detail["action"], "cross_walk_trigger")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
    def test_walk_trigger_nearby_drives_through_line_not_to_line(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, 64, -64), vertex(1, 64, 64)],
                [line(288, 0, 1, special=88, passable=True, blocking=False, walk_trigger=True)],
            ),
            cell_units=96,
        )
        memory = DoorMemory()
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=True,
            front_block_distance_fp=fp(512),
            use_lines=[
                SimpleNamespace(
                    line_id=288,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(64),
                    distance_fp=fp(64),
                )
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}

        action = planner._navigation_walk_trigger_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_walk_trigger_for_contact"},
            target_point=Point(fp(512), fp(0)),
            max_target_delta_degrees=100.0,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["line"], 288)
        self.assertEqual(action.detail["action"], "cross_walk_trigger")
        self.assertEqual(action.detail["mt"], [fp(160), fp(0)])
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
    def test_walk_trigger_approach_targets_far_side_before_cross_radius(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, -256, 448), vertex(1, -128, 448)],
                [line(288, 0, 1, special=88, passable=True, blocking=False, walk_trigger=True)],
            ),
            cell_units=96,
        )
        memory = DoorMemory()
        st = state(-240, 336, 90)
        st.navigation = SimpleNamespace(
            forward_open=True,
            front_block_distance_fp=fp(512),
            use_lines=[
                SimpleNamespace(
                    line_id=288,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(-240), y_fp=fp(448)),
                    midpoint=SimpleNamespace(x_fp=fp(-192), y_fp=fp(448)),
                    nearest_distance_fp=fp(112),
                    distance_fp=fp(112),
                )
            ],
        )
        player = {"point": Point(fp(-240), fp(336)), "angle": 90}

        action = planner._navigation_walk_trigger_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_walk_trigger_for_contact"},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["line"], 288)
        self.assertEqual(action.detail["mt"], [fp(-240), fp(544)])
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
    def test_walk_trigger_candidate_avoids_congested_line(self):
        planner = SpatialPlanner(
            snapshot(
                [
                    vertex(0, 96, -64),
                    vertex(1, 96, 64),
                    vertex(2, 160, -64),
                    vertex(3, 160, 64),
                ],
                [
                    line(288, 0, 1, special=88, passable=True, blocking=False, walk_trigger=True),
                    line(289, 2, 3, special=88, passable=True, blocking=False, walk_trigger=True),
                ],
            ),
            cell_units=96,
        )
        memory = DoorMemory()
        memory.observe_line(288, special=88, tag=4)
        memory.record_route_abandoned(288, passable=True)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            forward_open=True,
            front_block_distance_fp=fp(512),
            use_lines=[
                SimpleNamespace(
                    line_id=288,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(96), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(96), y_fp=fp(0)),
                    nearest_distance_fp=fp(96),
                    distance_fp=fp(96),
                ),
                SimpleNamespace(
                    line_id=289,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(160), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(160), y_fp=fp(0)),
                    nearest_distance_fp=fp(160),
                    distance_fp=fp(160),
                ),
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}

        action = planner._navigation_walk_trigger_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_walk_trigger_for_contact"},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["line"], 289)
    def test_walk_trigger_planning_does_not_mark_lift_open_before_crossing(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, -128, 256), vertex(1, -128, 320)],
                [line(289, 0, 1, special=88, tag=4, passable=True, blocking=False, walk_trigger=True)],
            ),
            cell_units=96,
        )
        memory = DoorMemory()
        memory.observe_line(289, special=88, tag=4)
        st = state(-96, 288, 180)
        st.navigation = SimpleNamespace(
            forward_open=True,
            front_block_distance_fp=fp(512),
            use_lines=[
                SimpleNamespace(
                    line_id=289,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(-128), y_fp=fp(288)),
                    midpoint=SimpleNamespace(x_fp=fp(-128), y_fp=fp(288)),
                    nearest_distance_fp=fp(32),
                    distance_fp=fp(32),
                )
            ],
        )
        player = {"point": Point(fp(-96), fp(288)), "angle": 180}

        action = planner._navigation_walk_trigger_action(
            st,
            player,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_walk_trigger_for_contact"},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "cross_walk_trigger")
        self.assertEqual(memory.state_for(289), "closed")
        self.assertEqual(memory.last_status_for(289), "walk_trigger_cross_attempt")
    def test_remembered_progression_skips_settling_walk_lift_trigger(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, -128, 256), vertex(1, -128, 320)],
                [line(289, 0, 1, special=88, tag=4, passable=True, blocking=False, walk_trigger=True)],
            ),
            cell_units=96,
        )
        memory = DoorMemory()
        memory.observe_line(289, special=88, tag=4)
        memory.record_progress(289)
        st = state(-96, 288, 180)
        player = {"point": Point(fp(-96), fp(288)), "angle": 180}

        action = planner._remembered_progression_line_action(st, player, FakeAgentPb2, memory, prefer_exit=False)

        self.assertIsNone(action)
    def test_remembered_progression_skips_congested_walk_lift_trigger(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, -256, 448), vertex(1, -128, 448)],
                [line(288, 0, 1, special=88, tag=4, passable=True, blocking=False, walk_trigger=True)],
            ),
            cell_units=96,
        )
        memory = DoorMemory()
        memory.observe_line(288, special=88, tag=4)
        memory.record_route_abandoned(288, passable=True)
        st = state(-192, 320, 90)
        player = {"point": Point(fp(-192), fp(320)), "angle": 90}

        action = planner._remembered_progression_line_action(st, player, FakeAgentPb2, memory, prefer_exit=False)

        self.assertIsNone(action)
    def test_stale_open_gives_normal_door_opening_grace(self):
        memory = DoorMemory(max_failures=2)
        memory.observe_line(151, special=1)
        memory.record_failure(151, status="no_progress_after_use")
        self.assertTrue(memory.is_open(151))
        memory.record_stale_open(151)
        self.assertTrue(memory.is_open(151))
        self.assertEqual(memory.state_for(151), "opening")
        for _ in range(5):
            memory.record_stale_open(151)
        self.assertFalse(memory.is_blocked(151))
        self.assertTrue(memory.can_retry(151))
        self.assertEqual(memory.state_for(151), "closed")
    def test_key_acquisition_repairs_matching_key_doors(self):
        memory = DoorMemory()
        memory.observe_line(527, special=28)
        memory.record_failure(527, status="key_required")
        memory.observe_line(528, special=26)
        memory.record_failure(528, status="key_required")
        self.assertEqual(memory.required_key_colors(), {"red", "blue"})

        repaired = memory.mark_key_acquired("red")

        self.assertEqual(repaired, 1)
        self.assertEqual(memory.state_for(527), "closed")
        self.assertTrue(memory.can_retry(527))
        self.assertEqual(memory.state_for(528), "requires_key")
    def test_explore_uses_current_frontier_door_when_graph_route_is_exhausted(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
            ],
            [
                line(
                    291,
                    0,
                    1,
                    front_sector=76,
                    back_sector=78,
                    two_sided=True,
                    door=True,
                    special=1,
                    blocking=True,
                    sight_blocking=True,
                )
            ],
            sectors=[sector(76), sector(78)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        planner._world_frontier = [78]
        memory = DoorMemory()
        memory.observe_line(291, special=1)
        for _ in range(memory.max_failures + 1):
            memory.record_route_contact(291)
        self.assertIsNone(planner._sector_route(76, [78], memory))
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(current_sector=SimpleNamespace(sector_id=76))

        action = planner._explore_action(st, {"point": Point(0, 0), "angle": 0}, FakeAgentPb2, memory)

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.door_line_id, 291)
        self.assertEqual(action.detail["skill"], "frontier_escape_use_line")
        self.assertEqual(action.detail["action"], "use")
    def test_explore_frontier_escape_ignores_far_remembered_doors(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 512, -64),
                vertex(3, 512, 64),
            ],
            [
                line(
                    291,
                    0,
                    1,
                    front_sector=76,
                    back_sector=78,
                    two_sided=True,
                    door=True,
                    special=1,
                    blocking=True,
                    sight_blocking=True,
                ),
                line(
                    378,
                    2,
                    3,
                    front_sector=38,
                    back_sector=116,
                    two_sided=True,
                    door=True,
                    special=1,
                    blocking=True,
                    sight_blocking=True,
                ),
            ],
            sectors=[sector(76), sector(78), sector(38), sector(116)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        planner._world_frontier = [78]
        memory = DoorMemory()
        memory.observe_line(291, special=1)
        memory.observe_line(378, special=1)
        for _ in range(memory.max_failures + 1):
            memory.record_route_contact(291)
            memory.record_route_contact(378)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(current_sector=SimpleNamespace(sector_id=76))

        action = planner._explore_action(st, {"point": Point(0, 0), "angle": 0}, FakeAgentPb2, memory)

        self.assertIsNotNone(action)
        self.assertEqual(action.door_line_id, 291)
    def test_explore_routes_to_reachable_boundary_door_when_frontier_route_is_exhausted(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 192, -64),
                vertex(3, 192, 64),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(
                    378,
                    2,
                    3,
                    front_sector=2,
                    back_sector=3,
                    two_sided=True,
                    door=True,
                    special=1,
                    blocking=True,
                    sight_blocking=True,
                ),
            ],
            sectors=[sector(1), sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        planner._world_frontier = [3]
        planner.sector_for_point_fp = lambda *_args: 99  # type: ignore[method-assign]
        memory = DoorMemory()
        memory.observe_line(378, special=1)
        for _ in range(memory.max_failures + 1):
            memory.record_route_contact(378)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(current_sector=SimpleNamespace(sector_id=1))

        action = planner._explore_action(st, {"point": Point(0, 0), "angle": 0}, FakeAgentPb2, memory)

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertEqual(action.detail["skill"], "frontier_boundary_escape")
        self.assertEqual(action.detail["line"], 10)
        self.assertEqual(action.detail["boundary_line"], 378)
        self.assertEqual(action.door_line_id, 10)
    def test_frontier_escape_probe_aborts_under_low_health_fire(self):
        snap = snapshot(
            [
                vertex(0, 512, -64),
                vertex(1, 512, 64),
            ],
            [
                line(
                    329,
                    0,
                    1,
                    front_sector=1,
                    back_sector=2,
                    two_sided=True,
                    door=True,
                    special=1,
                    blocking=True,
                    sight_blocking=True,
                ),
            ],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(329, special=1)
        st = state(0, 0, 0, enemy=(256, 0), shootable=True)
        st.player.health = 52
        st.navigation = SimpleNamespace(
            current_sector=SimpleNamespace(sector_id=1),
            direction_probes=[probe(90, open=True, distance=256)],
        )

        action = planner._frontier_escape_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeAgentPb2,
            memory,
            {2},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "retreat")
        self.assertEqual(action.detail["skill"], "frontier_probe_escape")
        self.assertEqual(action.detail["reason"], "shootable_low_health")
        self.assertNotEqual(action.detail.get("skill"), "frontier_escape_probe")
    def test_explore_uses_reachable_boundary_door_from_source_sector(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
            ],
            [
                line(
                    378,
                    0,
                    1,
                    front_sector=2,
                    back_sector=3,
                    two_sided=True,
                    door=True,
                    special=1,
                    blocking=True,
                    sight_blocking=True,
                ),
            ],
            sectors=[sector(2), sector(3)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        planner._world_frontier = [3]
        planner.sector_for_point_fp = lambda *_args: 2  # type: ignore[method-assign]
        memory = DoorMemory()
        memory.observe_line(378, special=1)
        for _ in range(memory.max_failures + 1):
            memory.record_route_contact(378)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(current_sector=SimpleNamespace(sector_id=2))

        action = planner._explore_action(st, {"point": Point(0, 0), "angle": 0}, FakeAgentPb2, memory)

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.door_line_id, 378)
        self.assertEqual(action.detail["skill"], "frontier_escape_use_line")
        self.assertEqual(action.detail["action"], "use")
    def test_reachable_boundary_escape_prefers_clean_route_over_congested_passable_portal(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 192, -64),
                vertex(3, 192, 64),
                vertex(4, 64, 160),
                vertex(5, 192, 160),
                vertex(6, 320, 160),
                vertex(7, 320, 224),
            ],
            [
                line(10, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(20, 2, 3, front_sector=2, back_sector=3, two_sided=True, door=True, special=1, blocking=True),
                line(11, 4, 5, front_sector=1, back_sector=4, two_sided=True, passable=True, blocking=False, sight_blocking=False),
                line(30, 6, 7, front_sector=4, back_sector=5, two_sided=True, door=True, special=1, blocking=True),
            ],
            sectors=[sector(1), sector(2), sector(3), sector(4), sector(5)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.record_route_contact(10)
        memory.observe_line(20, special=1)
        memory.observe_line(30, special=1)
        for _ in range(memory.max_failures + 1):
            memory.record_route_contact(20)
            memory.record_route_contact(30)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(current_sector=SimpleNamespace(sector_id=1))

        action = planner._reachable_boundary_escape_action(st, {"point": Point(0, 0), "angle": 90}, FakeAgentPb2, memory)

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "center_passable_portal")
        self.assertEqual(action.detail["boundary_line"], 30)
        self.assertEqual(action.door_line_id, 11)
    def test_complete_level_routes_to_required_key_before_frontier(self):
        planner, memory, world = self._required_red_key_fixture()

        action = planner.objective_action(state(0, 0, 0), ("complete_level", "exit", "explore"), FakeAgentPb2, memory, world)

        self.assertIsNotNone(action)
        self.assertIn(action.detail["skill"], {"route_to_key", "sector_route_to_key", "route_to_key_probe"})
        self.assertEqual(action.detail["key"], "red")
    def test_required_key_suppresses_complete_level_combat_fire(self):
        planner, memory, world = self._required_red_key_fixture()

        action = planner.objective_action(
            state(0, 0, 0, shootable=True),
            ("complete_level", "exit", "use", "explore"),
            FakeAgentPb2,
            memory,
            world,
        )

        self.assertIsNotNone(action)
        self.assertIn(action.detail["skill"], {"route_to_key", "sector_route_to_key", "route_to_key_probe"})
        self.assertEqual(action.detail["key"], "red")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_SHOOT)
    def test_required_key_suppresses_route_waypoint_detour(self):
        planner, memory, world = self._required_red_key_fixture()
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            route_waypoint=SimpleNamespace(
                walk_trigger=True,
                exit=False,
                line=SimpleNamespace(
                    line_id=195,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(64),
                    distance_fp=fp(64),
                ),
            )
        )

        action = planner.objective_action(st, ("complete_level", "exit", "use", "explore"), FakeAgentPb2, memory, world)

        self.assertIsNotNone(action)
        self.assertIn(action.detail["skill"], {"route_to_key", "sector_route_to_key", "route_to_key_probe"})
        self.assertEqual(action.detail["key"], "red")
        self.assertNotEqual(action.detail.get("line"), 195)
    def test_stale_open_repairs_door_hypothesis(self):
        memory = DoorMemory(max_failures=3)
        memory.record_failure(151, status="no_progress_after_use")
        memory.record_stale_open(151)
        self.assertFalse(memory.is_blocked(151))
        self.assertTrue(memory.can_retry(151))
        self.assertEqual(memory.state_for(151), "closed")
    def test_exit_line_failures_stay_retryable(self):
        memory = DoorMemory(max_failures=2)
        memory.observe_line(330, special=11, exit_line=True)
        for _ in range(4):
            memory.record_attempt(330, status="exit_use")
            memory.record_failure(330, status="no_progress_after_use")
        self.assertFalse(memory.is_blocked(330))
        self.assertTrue(memory.can_retry(330))
        self.assertEqual(memory.state_for(330), "exit")
    def test_route_action_looks_ahead_on_clear_path(self):
        snap = snapshot([vertex(0, 0, -64), vertex(1, 0, 64)], [])
        planner = SpatialPlanner(snap, cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        route = Route(
            points=[
                RouteStep(Point(fp(20), fp(0))),
                RouteStep(Point(fp(96), fp(0))),
                RouteStep(Point(fp(192), fp(0))),
                RouteStep(Point(fp(384), fp(0))),
            ]
        )
        action = planner._route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="route_progression",
            detail={"skill": "planner_route_to_los"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
        self.assertGreaterEqual(action.detail["dist"], 300)
    def test_route_action_can_look_past_opening_door(self):
        snap = snapshot([vertex(0, 96, -64), vertex(1, 96, 64)], [line(8, 0, 1, special=1, door=True, use_trigger=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        memory = DoorMemory()
        memory.observe_line(8, special=1)
        memory.record_failure(8, status="no_progress_after_use")
        route = Route(
            points=[
                RouteStep(Point(fp(96), fp(0)), line_id=8, use_line=True),
                RouteStep(Point(fp(384), fp(0))),
            ]
        )
        action = planner._route_action(
            player,
            route,
            FakeAgentPb2,
            memory,
            skill="route_progression",
            detail={"skill": "planner_route_to_los"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
        self.assertGreaterEqual(action.detail["dist"], 300)
        self.assertIsNone(action.door_line_id)
    def test_map_cache_status_is_compact_counts_not_geometry(self):
        cache = MapCache()
        cache._cached = SimpleNamespace(
            key=(1, 1, 123),
            fetched_at=0,
            snapshot=snapshot([vertex(0, 0, 0)], [line(1, 0, 0)]),
        )
        status = cache.summary()
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["v"], 1)
        self.assertEqual(status["l"], 1)
        self.assertNotIn("vertices", status)
        self.assertNotIn("lines", status)
    def test_wad_loader_reads_doom_map_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mini.wad"
            _write_minimal_wad(path)
            snap = WadMapLoader(path).load(1, 1)
        self.assertEqual(snap.source, "wad")
        self.assertEqual(len(snap.vertices), 2)
        self.assertEqual(len(snap.lines), 1)
        self.assertTrue(snap.lines[0].door)
        self.assertTrue(snap.lines[0].use_trigger)
        self.assertFalse(snap.lines[0].passable)
        self.assertEqual(len(snap.sectors), 1)
        self.assertEqual(len(snap.things), 1)
        self.assertTrue(snap.things[0].player_start)
    def test_wad_line_special_activation_classification(self):
        # Regression for E1M2 line 389: special 103 (S1 Door Open Stay) is a SWITCH you press
        # USE on, not a walk-over. Per the doomwiki linedef type table: D/S-type = use_trigger,
        # W-type = walk_trigger, gun (46 GR Door) and scrollers (48) are neither.
        cases = {
            103: "use",  # S1 Door Open Stay
            62: "use",  # SR Lift
            18: "use",  # S1 Floor To Higher Adjacent Floor
            20: "use",  # S1 Floor To Higher Floor Change Texture
            23: "use",  # S1 Floor To Lowest Adjacent Floor
            88: "walk",  # WR Lift Also Monsters
            97: "walk",  # WR Teleport
            2: "walk",  # W1 Door Stay Open
            46: "none",  # GR Door (gun-activated)
            48: "none",  # Scrolling Wall (no trigger)
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mini.wad"
            _write_minimal_wad(path, specials=tuple(cases))
            snap = WadMapLoader(path).load(1, 1)
        self.assertEqual(len(snap.lines), len(cases))
        for line_obj, (special, kind) in zip(snap.lines, cases.items()):
            self.assertEqual(line_obj.special, special)
            self.assertEqual(line_obj.use_trigger, kind == "use", f"special {special} use_trigger")
            self.assertEqual(line_obj.walk_trigger, kind == "walk", f"special {special} walk_trigger")


if __name__ == "__main__":
    unittest.main()
