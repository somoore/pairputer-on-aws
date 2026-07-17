"""Exit and use-line objectives: live/static exit lines, exit waypoints, and remembered-line probing for Agent DOOM's spatial planner."""

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
    enemy_state,
    probe,
)


class TestAgentDoomPlannerExitUseObjectives(unittest.TestCase):
    def test_exit_objective_prefers_static_exit_line_before_generic_waypoint(self):
        snap = snapshot(
            [
                vertex(0, 0, -64),
                vertex(1, 0, 64),
                vertex(2, 256, -64),
                vertex(3, 256, 64),
            ],
            [
                line(195, 0, 1, special=88, walk_trigger=True, passable=True, blocking=False, sight_blocking=False),
                line(330, 2, 3, special=11, exit=True, use_trigger=True),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(-128, 0, 0)
        st.navigation = SimpleNamespace(
            route_waypoint=SimpleNamespace(
                walk_trigger=True,
                exit=False,
                line=SimpleNamespace(
                    line_id=195,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(0), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(0), y_fp=fp(0)),
                    nearest_distance_fp=fp(128),
                    distance_fp=fp(128),
                ),
            )
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertNotEqual(action.detail["skill"], "route_waypoint_walk")
        self.assertEqual(action.detail["line"], 330)
        memory = DoorMemory()
        memory.record_progress(195)
        progressed = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, memory)
        self.assertIsNotNone(progressed)
        self.assertNotEqual(progressed.detail.get("line"), 195)
    def test_exit_objective_uses_static_exit_line_without_live_waypoint(self):
        snap = snapshot(
            [
                vertex(0, 0, -64),
                vertex(1, 0, 64),
                vertex(2, 256, -64),
                vertex(3, 256, 64),
            ],
            [
                line(195, 0, 1, special=88, walk_trigger=True, passable=True, blocking=False, sight_blocking=False),
                line(330, 2, 3, special=11, exit=True, use_trigger=True),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        action = planner.objective_action(state(-128, 0, 0), ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertNotEqual(action.detail.get("line"), 195)
    def test_exit_line_prefers_sector_route_before_grid_route(self):
        snap = snapshot([vertex(0, 256, -64), vertex(1, 256, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        called = []

        def fake_sector_route(player, selected_line, agent_pb2, door_memory, *, exit_only):
            called.append(selected_line.id)
            return PlanAction(
                skill="press_exit",
                action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=1, duration_tics=1),
                detail={"skill": "sector_route_to_exit_line", "line": selected_line.id},
            )

        planner._sector_route_to_line_action = fake_sector_route  # type: ignore[method-assign]
        action = planner._line_objective_action(
            {"point": Point(fp(0), fp(0)), "angle": 0},
            FakeAgentPb2,
            DoorMemory(),
            exit_only=True,
        )
        self.assertIsNotNone(action)
        self.assertEqual(called, [330])
        self.assertEqual(action.detail["skill"], "sector_route_to_exit_line")
    def test_sector_line_closeout_follows_opening_non_exit_line_instead_of_reusing(self):
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
        planner.sector_for_point_fp = lambda *_args, **_kw: 1  # type: ignore[method-assign]
        memory = DoorMemory()
        memory.observe_line(389, special=103)
        memory.record_attempt(389, status="sector_line_use")
        line_rt = planner._line_by_id(389)
        self.assertIsNotNone(line_rt)

        action = planner._sector_route_to_line_action(
            {"point": Point(fp(48), fp(0)), "angle": 0},
            line_rt,
            FakeAgentPb2,
            memory,
            exit_only=False,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.door_line_id, 389)
        self.assertEqual(action.detail["state"], "opening")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
    def test_portal_route_commits_to_retried_current_door_when_exit_route_is_short_and_health_is_critical(self):
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
        st.player.health = 20
        st.navigation = SimpleNamespace(forward_open=True, front_block_distance_fp=fp(64), back_open=True)
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
    def test_portal_route_commits_to_retried_upcoming_door_when_exit_route_is_short_and_health_is_critical(self):
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
        st.navigation = SimpleNamespace(forward_open=True, front_block_distance_fp=fp(64), back_open=True)
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
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.door_line_id, 11)
        self.assertEqual(action.detail["action"], "final_door_pressure_follow_opening")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
    def test_exit_waypoint_uses_exit_switch_in_range(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            route_waypoint=SimpleNamespace(
                walk_trigger=False,
                exit=True,
                line=SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(64),
                    distance_fp=fp(64),
                ),
            )
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 330)
    def test_live_exit_line_close_and_aligned_uses_exit(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(64),
                )
            ]
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.detail["skill"], "live_exit_line")
    def test_live_exit_line_releases_after_use_attempt(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.record_attempt(330, status="live_exit_use")
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(64),
                )
            ],
        )
        action = planner.objective_action(st, ("exit", "complete_level", "no_kills"), FakeAgentPb2, memory)
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertEqual(action.detail["action"], "release_use")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
    def test_live_exit_line_uses_again_after_release(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.record_attempt(330, status="live_exit_use")
        memory.record_status(330, status="live_exit_release")
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(64),
                )
            ],
        )
        action = planner.objective_action(st, ("exit", "complete_level", "no_kills"), FakeAgentPb2, memory)
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.action.duration_tics, 1)
        self.assertEqual(action.detail["action"], "use")
    def test_live_exit_line_uses_semantic_use_action(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(64),
                )
            ],
        )
        action = planner.objective_action(st, ("exit", "complete_level"), FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeRawAgentPb2.ACTION_USE)
        self.assertEqual(action.action.duration_tics, 1)
    def test_no_route_resync_can_approach_farther_live_use_line(self):
        snap = snapshot(
            [
                vertex(0, 384, -64),
                vertex(1, 384, 64),
            ],
            [
                line(326, 0, 1, special=1, front_sector=1, back_sector=2, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=326,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(384), y_fp=0),
                    midpoint=SimpleNamespace(x_fp=fp(384), y_fp=0),
                    nearest_distance_fp=fp(384),
                    distance_fp=fp(384),
                )
            ],
            route_waypoint=None,
        )
        normal = planner._last_chance_live_use_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeRawAgentPb2,
            DoorMemory(),
        )
        self.assertIsNone(normal)

        resync = planner._last_chance_live_use_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeRawAgentPb2,
            DoorMemory(),
            allow_suppressed=True,
            max_distance_fp=512 * FP_UNIT,
        )
        self.assertIsNotNone(resync)
        self.assertEqual(resync.door_line_id, 326)
        self.assertIn(getattr(resync.action, "action", 0), {FakeRawAgentPb2.ACTION_FORWARD, 0})
    def test_no_route_resync_can_approach_far_suppressed_live_use_line(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(389, special=103)
        for _ in range(3):
            memory.record_force_follow_stalled(389, special=103, tag=5)
        self.assertTrue(memory.live_line_suppressed(389))
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=389,
                    special=103,
                    nearest_point=SimpleNamespace(x_fp=fp(591), y_fp=0),
                    midpoint=SimpleNamespace(x_fp=fp(591), y_fp=0),
                    nearest_distance_fp=fp(591),
                    distance_fp=fp(591),
                )
            ],
            route_waypoint=None,
        )

        action = planner._last_chance_live_use_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeRawAgentPb2,
            memory,
            allow_suppressed=True,
            max_distance_fp=640 * FP_UNIT,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.door_line_id, 389)
        self.assertEqual(action.detail["resync"], "suppressed_live_use")
        self.assertEqual(action.detail["action"], "force_follow_live_use_line")
    def test_no_route_resync_skips_far_exhausted_ordinary_door(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        memory = DoorMemory()
        memory.observe_line(291, special=1)
        for _ in range(memory.max_failures + 1):
            memory.record_route_contact(291)
        self.assertFalse(memory.can_retry(291))
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=291,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(432), y_fp=0),
                    midpoint=SimpleNamespace(x_fp=fp(432), y_fp=0),
                    nearest_distance_fp=fp(432),
                    distance_fp=fp(432),
                )
            ],
            route_waypoint=None,
        )

        action = planner._last_chance_live_use_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeRawAgentPb2,
            memory,
            allow_suppressed=True,
            max_distance_fp=640 * FP_UNIT,
        )

        self.assertIsNone(action)
    def test_complete_level_final_corridor_preempts_remembered_exit_probe(self):
        planner = SpatialPlanner(snapshot([], []), cell_units=96)
        final_action = PlanAction(
            skill="open_use_line",
            action=FakeAgentPb2.PlayerAction(action=FakeAgentPb2.ACTION_USE, amount=1, duration_tics=1),
            detail={"skill": "e1m1_final_corridor_override"},
            door_line_id=330,
        )
        remembered_probe = PlanAction(
            skill="press_exit",
            action=FakeAgentPb2.PlayerAction(action=FakeAgentPb2.ACTION_FORWARD, amount=48, duration_tics=8),
            detail={"skill": "remembered_exit_probe"},
            door_line_id=330,
        )
        planner._live_exit_line_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        planner._line_objective_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        planner._route_waypoint_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        planner._needed_key_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        planner._e1m1_final_corridor_override = lambda *_args, **_kwargs: final_action  # type: ignore[method-assign]
        planner._remembered_progression_line_action = lambda *_args, **_kwargs: remembered_probe  # type: ignore[method-assign]

        action = planner.objective_action(
            state(0, 0, 0),
            ("complete_level", "exit", "use", "explore"),
            FakeAgentPb2,
            DoorMemory(),
        )

        self.assertIs(action, final_action)
        self.assertEqual(action.detail["skill"], "e1m1_final_corridor_override")
    def test_far_unroutable_remembered_exit_does_not_become_blind_probe(self):
        snap = snapshot(
            [
                vertex(0, 2048, -64),
                vertex(1, 2048, 64),
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

        action = planner._remembered_progression_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeAgentPb2,
            memory,
            prefer_exit=True,
        )

        self.assertIsNone(action)
    def test_remembered_progression_skips_e1m1_final_corridor_side_doors(self):
        snap = snapshot(
            [
                vertex(0, 2912, -3776),
                vertex(1, 2912, -3904),
            ],
            [
                line(248, 0, 1, special=1, door=True, use_trigger=True),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(248, special=1)
        st = state(2960, -3820, 180)
        st.level = SimpleNamespace(episode=1, map=1)
        st.navigation = SimpleNamespace(forward_open=True, route_waypoint=None)

        action = planner._remembered_progression_line_action(
            st,
            {"point": Point(fp(2960), fp(-3820)), "angle": 180},
            FakeAgentPb2,
            memory,
            prefer_exit=False,
        )

        self.assertIsNone(action)
    def test_remembered_progression_probe_aborts_under_low_health_fire(self):
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
        planner._sector_route_to_line_action = lambda *_args, **_kw: None  # type: ignore[method-assign]
        planner._route = lambda *_args, **_kw: None  # type: ignore[method-assign]
        memory = DoorMemory()
        memory.observe_line(326, special=1)
        st = state(0, 0, 0, enemy=(256, 0), shootable=True)
        st.player.health = 52
        st.navigation = SimpleNamespace(
            forward_open=True,
            route_waypoint=None,
            direction_probes=[probe(90, open=True, distance=256)],
        )

        action = planner._remembered_progression_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeAgentPb2,
            memory,
            prefer_exit=False,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "retreat")
        self.assertEqual(action.detail["skill"], "remembered_probe_escape")
        self.assertEqual(action.detail["reason"], "shootable_low_health")
        self.assertNotEqual(action.detail.get("skill"), "remembered_progression_probe")
    def test_remembered_progression_probe_aborts_under_crossfire(self):
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
        planner._sector_route_to_line_action = lambda *_args, **_kw: None  # type: ignore[method-assign]
        planner._route = lambda *_args, **_kw: None  # type: ignore[method-assign]
        memory = DoorMemory()
        memory.observe_line(326, special=1)
        st = enemy_state(
            0,
            0,
            0,
            [
                {"id": 1, "pos": (512, 0), "los": True},
                {"id": 2, "pos": (0, 512), "los": True},
            ],
        )
        st.player.health = 80
        st.navigation = SimpleNamespace(
            forward_open=True,
            route_waypoint=None,
            direction_probes=[probe(-90, open=True, distance=160), probe(90, open=True, distance=256)],
        )

        action = planner._remembered_progression_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeAgentPb2,
            memory,
            prefer_exit=False,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "retreat")
        self.assertEqual(action.detail["skill"], "remembered_probe_escape")
        self.assertEqual(action.detail["reason"], "crossfire")
        self.assertGreaterEqual(action.detail["crossfire"], 45.0)
    def test_far_unroutable_remembered_progression_does_not_become_blind_probe(self):
        snap = snapshot(
            [
                vertex(0, 768, -64),
                vertex(1, 768, 64),
            ],
            [
                line(664, 0, 1, special=103, door=True, use_trigger=True),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        planner._sector_route_to_line_action = lambda *_args, **_kw: None  # type: ignore[method-assign]
        planner._route = lambda *_args, **_kw: None  # type: ignore[method-assign]
        memory = DoorMemory()
        memory.observe_line(664, special=103)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(forward_open=True, route_waypoint=None)

        action = planner._remembered_progression_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeAgentPb2,
            memory,
            prefer_exit=False,
        )

        self.assertIsNone(action)
    def test_far_congested_remembered_progression_is_suppressed(self):
        snap = snapshot(
            [
                vertex(0, 512, -64),
                vertex(1, 512, 64),
            ],
            [
                line(664, 0, 1, special=103, door=True, use_trigger=True),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        planner._sector_route_to_line_action = lambda *_args, **_kw: None  # type: ignore[method-assign]
        planner._route = lambda *_args, **_kw: None  # type: ignore[method-assign]
        memory = DoorMemory()
        memory.observe_line(664, special=103)
        memory.record_route_contact(664)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(forward_open=True, route_waypoint=None)

        action = planner._remembered_progression_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeAgentPb2,
            memory,
            prefer_exit=False,
        )

        self.assertIsNone(action)
    def test_remembered_progression_skips_repeated_force_follow_stalls_even_if_opening(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
            ],
            [
                line(389, 0, 1, special=103, tag=5, door=True, use_trigger=True),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(389, special=103, tag=5)
        for _ in range(3):
            memory.record_force_follow_stalled(389, special=103, tag=5)
        memory.record_attempt(389, status="remembered_progression_use")
        self.assertEqual(memory.state_for(389), "opening")
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(forward_open=True, route_waypoint=None)

        action = planner._remembered_progression_line_action(
            st,
            {"point": Point(0, 0), "angle": 0},
            FakeAgentPb2,
            memory,
            prefer_exit=False,
        )

        self.assertIsNone(action)
    def test_live_exit_line_centers_before_using_endpoint(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(80, -80, 180)
        st.navigation = SimpleNamespace(
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(-64)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(22),
                    distance_fp=fp(22),
                )
            ],
        )
        action = planner.objective_action(st, ("exit", "complete_level"), FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["target"], "center_exit_line")
        self.assertNotEqual(action.action.action, FakeRawAgentPb2.ACTION_USE)
    def test_live_exit_line_turns_before_use_when_off_angle(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 20)
        st.navigation = SimpleNamespace(
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(64),
                )
            ],
        )
        action = planner.objective_action(st, ("exit", "complete_level"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.detail["action"], "turn")
    def test_live_exit_line_faces_through_vertical_switch_from_front_side(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(80, 0, 268)
        st.navigation = SimpleNamespace(
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(16),
                    distance_fp=fp(16),
                )
            ],
        )
        action = planner.objective_action(st, ("exit", "complete_level"), FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "turn")
        self.assertLess(action.action.raw.angle_turn, 0)
    def test_static_exit_line_uses_nearest_point_not_midpoint(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(330, 0, 1, special=11, front_sector=2, exit=True)],
            sectors=[sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        player = {"point": Point(fp(80), fp(-60)), "angle": 180}
        action = planner._line_objective_action(player, FakeAgentPb2, DoorMemory(), exit_only=True)
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 330)
    def test_static_exit_line_turns_when_not_tightly_aligned(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(330, 0, 1, special=11, front_sector=2, exit=True)],
            sectors=[sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        player = {"point": Point(fp(80), fp(-60)), "angle": 213}
        action = planner._line_objective_action(player, FakeAgentPb2, DoorMemory(), exit_only=True)
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertGreaterEqual(action.action.amount, 48)
        self.assertGreaterEqual(action.action.duration_tics, 4)
    def test_static_exit_route_closeout_uses_line_instead_of_orbiting_route_node(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(330, 0, 1, special=11, front_sector=2, exit=True)],
            sectors=[sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        player = {"point": Point(fp(20), fp(0)), "angle": 0}
        route = Route([RouteStep(Point(fp(24), fp(0)))])
        action = planner._route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "planner_route_to_use_line", "line": 330, "route": 1},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 330)
        self.assertEqual(action.detail["skill"], "static_exit_route_closeout")
    def test_static_exit_route_closeout_turns_to_line_before_use(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(330, 0, 1, special=11, front_sector=2, exit=True)],
            sectors=[sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        player = {"point": Point(fp(20), fp(0)), "angle": 90}
        route = Route([RouteStep(Point(fp(24), fp(0)))])
        action = planner._route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="press_exit",
            detail={"skill": "planner_route_to_use_line", "line": 330, "route": 1},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 330)
        self.assertEqual(action.detail["skill"], "static_exit_route_closeout")
    def test_static_use_route_closeout_uses_line_instead_of_orbiting_route_node(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(389, 0, 1, special=1, front_sector=2, door=True, use_trigger=True)],
            sectors=[sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        player = {"point": Point(fp(20), fp(0)), "angle": 0}
        route = Route([RouteStep(Point(fp(24), fp(0)))])
        action = planner._route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="open_use_line",
            detail={"skill": "planner_route_to_use_line", "line": 389, "route": 1},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 389)
        self.assertEqual(action.detail["skill"], "static_use_route_closeout")
    def test_static_use_route_closeout_turns_to_line_before_use(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(389, 0, 1, special=1, front_sector=2, door=True, use_trigger=True)],
            sectors=[sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        player = {"point": Point(fp(20), fp(0)), "angle": 90}
        route = Route([RouteStep(Point(fp(24), fp(0)))])
        action = planner._route_action(
            player,
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="open_use_line",
            detail={"skill": "planner_route_to_use_line", "line": 389, "route": 1},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 389)
        self.assertEqual(action.detail["skill"], "static_use_route_closeout")
    def test_use_objective_prefers_live_opening_use_line_before_static_graph_route(self):
        snap = snapshot(
            [vertex(0, 512, -64), vertex(1, 512, 64), vertex(2, 1024, -64), vertex(3, 1024, 64)],
            [
                line(389, 0, 1, special=103, front_sector=2, back_sector=3, two_sided=True, door=True),
                line(777, 2, 3, special=1, front_sector=3, back_sector=4, two_sided=True, door=True, use_trigger=True),
            ],
            sectors=[sector(2), sector(3), sector(4)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.observe_line(389, special=103)
        memory.record_attempt(389, status="planner_live_use")
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            use_lines=[
                SimpleNamespace(
                    line_id=389,
                    special=103,
                    nearest_point=SimpleNamespace(x_fp=fp(320), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(512), y_fp=fp(0)),
                    nearest_distance_fp=fp(320),
                    distance_fp=fp(320),
                )
            ],
        )
        player = {"point": Point(fp(0), fp(0)), "angle": 0}
        action = planner._line_objective_action(player, FakeAgentPb2, memory, exit_only=False, state=st)

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.door_line_id, 389)
        self.assertEqual(action.detail["skill"], "planner_live_use_line")
        self.assertEqual(action.detail["action"], "follow_opening")
    def test_live_exit_line_does_not_bypass_route_from_other_sector(self):
        snap = snapshot(
            [vertex(0, 64, -64), vertex(1, 64, 64)],
            [line(330, 0, 1, special=11, front_sector=2, exit=True)],
            sectors=[sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            current_sector=SimpleNamespace(sector_id=1),
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(48), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(48),
                    distance_fp=fp(64),
                )
            ]
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertNotEqual(action.detail["skill"], "live_exit_line")
    def test_live_exit_line_near_side_bearing_strafes_instead_of_spinning(self):
        snap = snapshot([vertex(0, 0, 0)], [])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(0), y_fp=fp(180)),
                    midpoint=SimpleNamespace(x_fp=fp(0), y_fp=fp(192)),
                    nearest_distance_fp=fp(180),
                    distance_fp=fp(192),
                )
            ],
            direction_probes=[SimpleNamespace(angle_offset_degrees=90, open=True, block_distance_fp=fp(256))],
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_STRAFE_LEFT)
        self.assertEqual(action.detail["action"], "close_strafe")
    def test_live_exit_line_near_blocked_side_turns_instead_of_strafing(self):
        snap = snapshot([vertex(0, 0, 0)], [])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(0), y_fp=fp(180)),
                    midpoint=SimpleNamespace(x_fp=fp(0), y_fp=fp(192)),
                    nearest_distance_fp=fp(180),
                    distance_fp=fp(192),
                )
            ],
            direction_probes=[SimpleNamespace(angle_offset_degrees=90, open=False, block_distance_fp=fp(256))],
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertIn(action.action.action, {FakeAgentPb2.ACTION_TURN_LEFT, FakeAgentPb2.ACTION_TURN_RIGHT})
        self.assertEqual(action.detail["action"], "turn")
    def test_live_exit_line_near_behind_reverses_instead_of_spinning(self):
        snap = snapshot([vertex(0, 0, 0)], [])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            back_open=True,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(-180), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(-192), y_fp=fp(0)),
                    nearest_distance_fp=fp(180),
                    distance_fp=fp(192),
                )
            ]
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(action.detail["action"], "close_reverse")
    def test_live_exit_line_near_behind_with_blocked_back_turns(self):
        snap = snapshot([vertex(0, 0, 0)], [])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            back_open=False,
            use_lines=[
                SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(-180), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(-192), y_fp=fp(0)),
                    nearest_distance_fp=fp(180),
                    distance_fp=fp(192),
                )
            ],
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertIn(action.action.action, {FakeAgentPb2.ACTION_TURN_LEFT, FakeAgentPb2.ACTION_TURN_RIGHT})
        self.assertEqual(action.detail["action"], "turn")
    def test_exit_objective_does_not_retreat_on_low_health(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.player.health = 10
        st.navigation = SimpleNamespace(
            route_waypoint=SimpleNamespace(
                walk_trigger=False,
                exit=True,
                line=SimpleNamespace(
                    line_id=330,
                    special=11,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(64),
                    distance_fp=fp(64),
                ),
            )
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "press_exit")
        self.assertNotEqual(action.skill, "retreat")
    def test_exit_waypoint_opens_local_door_before_far_target(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 1200, -64),
                vertex(3, 1200, 64),
            ],
            [
                line(151, 0, 1, special=1, door=True, use_trigger=True),
                line(195, 2, 3, special=88, walk_trigger=True),
            ],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            route_waypoint=SimpleNamespace(
                walk_trigger=True,
                exit=False,
                line=SimpleNamespace(
                    line_id=195,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(1200), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(1200), y_fp=fp(0)),
                    nearest_distance_fp=fp(1200),
                    distance_fp=fp(1200),
                ),
            ),
            use_lines=[
                SimpleNamespace(
                    line_id=151,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(64),
                    distance_fp=fp(64),
                )
            ],
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "open_use_line")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 151)
        self.assertEqual(action.detail["skill"], "route_local_door")
    def test_exit_waypoint_crosses_open_local_door_before_far_target(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 1200, -64),
                vertex(3, 1200, 64),
            ],
            [line(151, 0, 1, special=1, door=True, use_trigger=True), line(195, 2, 3, special=88, walk_trigger=True)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.record_progress(151)
        st = state(0, 0, 0)
        st.navigation = SimpleNamespace(
            route_waypoint=SimpleNamespace(
                walk_trigger=True,
                exit=False,
                line=SimpleNamespace(
                    line_id=195,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(1200), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(1200), y_fp=fp(0)),
                    nearest_distance_fp=fp(1200),
                    distance_fp=fp(1200),
                ),
            ),
            use_lines=[
                SimpleNamespace(
                    line_id=151,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(64),
                    distance_fp=fp(64),
                )
            ],
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, memory)
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)
        self.assertEqual(action.door_line_id, 151)
        self.assertEqual(action.detail["action"], "cross")
    def test_exit_waypoint_ignores_local_door_behind_player(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
                vertex(2, 1200, -64),
                vertex(3, 1200, 64),
            ],
            [line(151, 0, 1, special=1, door=True, use_trigger=True), line(195, 2, 3, special=88, walk_trigger=True)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        memory = DoorMemory()
        memory.record_progress(151)
        st = state(128, 0, 0)
        st.navigation = SimpleNamespace(
            route_waypoint=SimpleNamespace(
                walk_trigger=True,
                exit=False,
                line=SimpleNamespace(
                    line_id=195,
                    special=88,
                    nearest_point=SimpleNamespace(x_fp=fp(1200), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(1200), y_fp=fp(0)),
                    nearest_distance_fp=fp(1072),
                    distance_fp=fp(1072),
                ),
            ),
            use_lines=[
                SimpleNamespace(
                    line_id=151,
                    special=1,
                    nearest_point=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    midpoint=SimpleNamespace(x_fp=fp(64), y_fp=fp(0)),
                    nearest_distance_fp=fp(64),
                    distance_fp=fp(64),
                )
            ],
        )
        action = planner.objective_action(st, ("exit", "use"), FakeAgentPb2, memory)
        self.assertIsNotNone(action)
        self.assertNotEqual(action.detail.get("skill"), "route_local_door")
        self.assertNotEqual(action.door_line_id, 151)
    def test_use_objective_presses_near_aligned_door(self):
        snap = snapshot(
            [
                vertex(0, 64, -64),
                vertex(1, 64, 64),
            ],
            [line(5, 0, 1, special=1, door=True, use_trigger=True)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        action = planner.objective_action(state(0, 0, 0), ("use",), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(action.door_line_id, 5)
        self.assertEqual(action.detail["skill"], "planner_use_line")


if __name__ == "__main__":
    unittest.main()
