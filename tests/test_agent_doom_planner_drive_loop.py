"""Planner-integration drive loop: route/sector/frontier/navcell crossing, anti-grind, and use-door progress for Agent DOOM."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from doom_fakes import make_agent_pb2, make_controller  # noqa: E402

from goal_contract import compile_goal_contract  # noqa: E402
from brain_runtime import (  # noqa: E402
    ANTI_GRIND_STUCK_THRESHOLD,
    BrainRuntime,
    ROTATIONAL_STALL_TURN_THRESHOLD,
    USE_STUCK_THRESHOLD,
    parse_directive,
)
from door_memory import DoorMemory  # noqa: E402


class TestAgentDoomPlannerDriveLoop(unittest.TestCase):
    def test_speedrun_no_kill_goal_compiles_to_exit_contract(self):
        contract = compile_goal_contract("race to the exit and get to next level as fast as you can without killing a bad guy")
        self.assertEqual(contract.objective, "complete_level")
        self.assertEqual(contract.style, "speedrun")
        self.assertEqual(contract.constraints["kill_budget"], 0)
        self.assertTrue(contract.constraints["avoid_combat"])
        self.assertIn("kill_delta", contract.failure_evidence)
        self.assertIn("level_transition", contract.success_evidence)
    def test_cautious_cover_holds_when_no_lateral_probe_is_open(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                back_open=False,
                direction_probes=[
                    SimpleNamespace(open=False, angle_offset_degrees=90, block_distance_fp=96 * 65536),
                    SimpleNamespace(open=False, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            )
        )

        _index, skill, action, decision = runtime._cautious_cover_action(
            state, directive, FakeController(), modules, reason="avoid_hitscan_los", enemy={"id": 32, "threat": "hitscan"}
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(getattr(action, "action", 0), 0)
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(decision["skill"], "hold_cover_no_probe")

        open_side_state = SimpleNamespace(
            navigation=SimpleNamespace(
                back_open=False,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            )
        )
        _index, skill, action, decision = runtime._cautious_cover_action(
            open_side_state, directive, FakeController(), modules, reason="avoid_hitscan_los", enemy={"id": 32, "threat": "hitscan"}
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(getattr(action, "action", 0), 0)
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(decision["skill"], "hold_cover_window")
    def test_cautious_cover_profile_prefers_route_waypoint_threshold_evidence(self):
        runtime = BrainRuntime()
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
                route_waypoint=SimpleNamespace(
                    priority=9,
                    exit=False,
                    walk_trigger=False,
                    line=SimpleNamespace(line_id=0, special=1, nearest_distance_fp=64 * 65536),
                ),
                use_lines=[],
            )
        )

        profile = runtime._cautious_cover_profile(state)

        self.assertIsNotNone(profile)
        self.assertEqual(profile["cover_evidence"], "route_waypoint")
        self.assertEqual(profile["cover_line"], 0)
        self.assertEqual(profile["cover_special"], 1)
        self.assertEqual(profile["cover_dist"], 64)
        self.assertEqual(profile["route_priority"], 9)
    def test_cautious_cover_profile_uses_nearest_use_line_threshold(self):
        runtime = BrainRuntime()
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
                route_waypoint=SimpleNamespace(line=None),
                use_lines=[
                    SimpleNamespace(line_id=21, special=1, nearest_distance_fp=144 * 65536),
                    SimpleNamespace(line_id=22, special=1, nearest_distance_fp=48 * 65536),
                ],
            )
        )

        profile = runtime._cautious_cover_profile(state)

        self.assertIsNotNone(profile)
        self.assertEqual(profile["cover_evidence"], "near_use_line")
        self.assertEqual(profile["cover_line"], 22)
        self.assertEqual(profile["cover_dist"], 48)
    def test_cautious_cover_profile_can_use_static_portal_graph(self):
        class FakePlanner:
            def __init__(self):
                self._portal_graph = {
                    3: [
                        SimpleNamespace(
                            line_id=0,
                            special=0,
                            use_line=False,
                            door=False,
                            passable=True,
                        )
                    ]
                }

            def player_from_state(self, _state):
                return {"point": SimpleNamespace(x=0, y=0)}

            def _line_by_id(self, line_id):
                if line_id != 0:
                    return None
                return SimpleNamespace(front_sector=3, back_sector=4)

            def _nearest_point_on_line(self, _point, _line):
                return SimpleNamespace(x=32 * 65536, y=0)

        runtime = BrainRuntime()
        runtime._planner = FakePlanner()  # type: ignore[assignment]
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
                route_waypoint=SimpleNamespace(line=None),
                use_lines=[],
                current_sector=SimpleNamespace(sector_id=3),
            )
        )

        profile = runtime._cautious_cover_profile(state)

        self.assertIsNotNone(profile)
        self.assertEqual(profile["cover_evidence"], "portal_graph")
        self.assertEqual(profile["cover_line"], 0)
        self.assertTrue(profile["cover_portal"])
    def test_repeated_failed_use_returns_fire_when_blocker_is_shootable(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_USE=8, ACTION_SHOOT=7)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._failed_use_count = USE_STUCK_THRESHOLD
        runtime._failed_use_line_matches_state = lambda _state, _line_id: True  # type: ignore[method-assign]
        runtime._metrics = lambda _state: {"health": 75, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "fire", "retreat"]}
        planned = FakeAgentPb2.PlayerAction(action=FakeAgentPb2.ACTION_USE, amount=1, duration_tics=3)

        result = runtime._repeated_failed_use_escape(
            SimpleNamespace(),
            directive,
            FakeController(),
            modules,
            planned,
            {"line_id": 325},
        )

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["skill"], "repeated_failed_use_return_fire")
        self.assertEqual(decision["line_id"], 325)
    def test_planner_route_skill_can_bypass_controller_mask(self):
        FakeController = make_controller(3, mask_value=False)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        modules = {"SKILL_ACTIONS": ["press_exit", "route_progression", "recover_stuck"]}
        index, skill = runtime._planner_skill_index("press_exit", FakeController(), object(), modules, directive)
        self.assertEqual(skill, "press_exit")
        self.assertEqual(index, 0)
    def test_failed_movement_counts_as_stalled_even_when_space_claims_open(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        runtime = BrainRuntime()
        modules = {"agent_pb2": FakeAgentPb2}
        self.assertTrue(runtime._movement_stalled(SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD), 2.5, modules))
        self.assertFalse(runtime._movement_stalled(SimpleNamespace(action=FakeAgentPb2.ACTION_TURN_LEFT), 0.0, modules))
        self.assertFalse(runtime._movement_stalled(SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD), 12.0, modules))
        self.assertTrue(
            runtime._movement_stalled(
                SimpleNamespace(action=0, raw=SimpleNamespace(forward_move=42, side_move=0)),
                2.5,
                modules,
            )
        )
        self.assertFalse(
            runtime._movement_stalled(
                SimpleNamespace(action=0, raw=SimpleNamespace(forward_move=0, side_move=0)),
                0.0,
                modules,
            )
        )
    def test_repeated_frontier_probe_stall_triggers_anti_grind_backoff(self):
        FakeAgentPb2 = make_agent_pb2(player_action="explicit", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        FakeController = make_controller(3)

        def summarize(action):
            raw = getattr(action, "raw", None)
            return {
                "action": int(getattr(action, "action", 0) or 0),
                "raw": {} if raw is None else {
                    "forward_move": int(getattr(raw, "forward_move", 0) or 0),
                    "side_move": int(getattr(raw, "side_move", 0) or 0),
                    "buttons": int(getattr(raw, "buttons", 0) or 0),
                },
                "mouse": {},
            }

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory(max_failures=2)
        runtime._world_memory.frontier_sectors.add(99)
        runtime._metrics = lambda state: state if isinstance(state, dict) else state.metrics  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["recover_stuck", "route_progression", "retreat"],
            "summarize_action": summarize,
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "frontier_probe_escape",
            "action": "strafe",
            "line": 378,
            "line_id": 378,
            "sector": 99,
            "probe": 90.0,
        }
        planned_action = FakeAgentPb2.PlayerAction(action=FakeAgentPb2.ACTION_STRAFE_LEFT, amount=22, duration_tics=8)
        previous = {"episode": 1, "map": 2, "x": 400 * 65536, "y": 96 * 65536, "health": 58}
        current = {"episode": 1, "map": 2, "x": 400 * 65536, "y": 96 * 65536, "health": 58}

        for _ in range(ANTI_GRIND_STUCK_THRESHOLD):
            runtime._record_planner_outcome(previous, current, planned_action, decision, {}, modules)

        self.assertGreaterEqual(runtime._anti_grind_count, ANTI_GRIND_STUCK_THRESHOLD)
        self.assertEqual(runtime._anti_grind_escape_steps, 2)
        self.assertIn(99, runtime._world_memory.blocked_frontier_sectors)

        state = SimpleNamespace(
            metrics=current,
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        result = runtime._anti_grind_escape(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            planned_action,
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, escape_action, escape_decision = result
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(escape_decision["skill"], "anti_grind_escape")
        self.assertEqual(escape_decision["reason"], "planner_movement_no_displacement")
        self.assertEqual(escape_decision["blocked_skill"], "frontier_probe_escape")
        self.assertEqual(escape_decision["blocked_action"], "strafe")
        self.assertEqual(escape_decision["line"], 378)
        self.assertEqual(escape_action.raw.forward_move, -52)
        self.assertEqual(getattr(escape_action.raw, "buttons", 0), 0)
    def test_route_to_health_repeated_turn_triggers_rotational_unstick(self):
        FakeAgentPb2 = make_agent_pb2(player_action="explicit", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        FakeController = make_controller(3)

        def summarize(action):
            raw = getattr(action, "raw", None)
            return {
                "action": int(getattr(action, "action", 0) or 0),
                "raw": {} if raw is None else {
                    "forward_move": int(getattr(raw, "forward_move", 0) or 0),
                    "side_move": int(getattr(raw, "side_move", 0) or 0),
                    "buttons": int(getattr(raw, "buttons", 0) or 0),
                },
                "mouse": {},
            }

        runtime = BrainRuntime()
        runtime._metrics = lambda state: state if isinstance(state, dict) else state.metrics  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["recover_stuck", "route_progression", "retreat"],
            "summarize_action": summarize,
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "route_to_health",
            "action": "turn",
        }
        planned_action = FakeAgentPb2.PlayerAction(action=FakeAgentPb2.ACTION_TURN_RIGHT, amount=24, duration_tics=6)
        previous = {"episode": 1, "map": 1, "x": 340 * 65536, "y": -128 * 65536, "health": 36}
        current = {"episode": 1, "map": 1, "x": 340 * 65536, "y": -128 * 65536, "health": 36}

        for _ in range(ROTATIONAL_STALL_TURN_THRESHOLD):
            runtime._record_planner_outcome(previous, current, planned_action, decision, {}, modules)

        self.assertGreaterEqual(runtime._rotational_stall_count, ROTATIONAL_STALL_TURN_THRESHOLD)
        self.assertEqual(runtime._rotational_stall_escape_steps, 2)

        state = SimpleNamespace(
            metrics=current,
            navigation=SimpleNamespace(forward_open=True, back_open=False, direction_probes=[]),
        )
        result = runtime._rotational_stall_escape(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            planned_action,
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, escape_action, escape_decision = result
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(escape_decision["skill"], "rotational_stall_escape")
        self.assertEqual(escape_decision["reason"], "route_repeated_turn")
        self.assertEqual(escape_decision["blocked_skill"], "route_to_health")
        self.assertEqual(escape_decision["blocked_action"], "turn")
        self.assertGreater(escape_action.raw.forward_move, 0)
        self.assertEqual(getattr(escape_action.raw, "buttons", 0), 0)
    def test_center_passable_portal_repeated_turn_triggers_rotational_unstick(self):
        FakeAgentPb2 = make_agent_pb2(player_action="explicit", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        FakeController = make_controller(3)

        def summarize(action):
            raw = getattr(action, "raw", None)
            return {
                "action": int(getattr(action, "action", 0) or 0),
                "raw": {} if raw is None else {
                    "forward_move": int(getattr(raw, "forward_move", 0) or 0),
                    "side_move": int(getattr(raw, "side_move", 0) or 0),
                    "buttons": int(getattr(raw, "buttons", 0) or 0),
                },
                "mouse": {},
            }

        runtime = BrainRuntime()
        runtime._metrics = lambda state: state if isinstance(state, dict) else state.metrics  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["recover_stuck", "route_progression", "retreat"],
            "summarize_action": summarize,
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "center_passable_portal",
            "action": "turn",
            "line": 315,
        }
        planned_action = FakeAgentPb2.PlayerAction(action=FakeAgentPb2.ACTION_TURN_RIGHT, amount=24, duration_tics=6)
        previous = {"episode": 1, "map": 1, "x": 3223 * 65536, "y": -4463 * 65536, "health": 24}
        current = {"episode": 1, "map": 1, "x": 3223 * 65536, "y": -4463 * 65536, "health": 24}

        for _ in range(ROTATIONAL_STALL_TURN_THRESHOLD):
            runtime._record_planner_outcome(previous, current, planned_action, decision, {}, modules)

        self.assertGreaterEqual(runtime._rotational_stall_count, ROTATIONAL_STALL_TURN_THRESHOLD)
        self.assertEqual(runtime._rotational_stall_escape_steps, 2)

        state = SimpleNamespace(
            metrics=current,
            navigation=SimpleNamespace(forward_open=True, back_open=False, direction_probes=[]),
        )
        result = runtime._rotational_stall_escape(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            planned_action,
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, escape_action, escape_decision = result
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(escape_decision["skill"], "rotational_stall_escape")
        self.assertEqual(escape_decision["reason"], "route_repeated_turn")
        self.assertEqual(escape_decision["blocked_skill"], "center_passable_portal")
        self.assertEqual(escape_decision["blocked_action"], "turn")
        self.assertGreater(escape_action.raw.forward_move, 0)
    def test_frontier_probe_displacement_resets_anti_grind_tracker(self):
        FakeAgentPb2 = make_agent_pb2(player_action="explicit", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda action: {"action": int(getattr(action, "action", 0) or 0), "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "frontier_probe_escape",
            "action": "strafe",
            "line": 378,
            "line_id": 378,
            "sector": 99,
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_STRAFE_LEFT)
        previous = {"episode": 1, "map": 2, "x": 400 * 65536, "y": 96 * 65536, "health": 58}
        stalled = {"episode": 1, "map": 2, "x": 400 * 65536, "y": 96 * 65536, "health": 58}
        moved = {"episode": 1, "map": 2, "x": 432 * 65536, "y": 96 * 65536, "health": 58}

        runtime._record_planner_outcome(previous, stalled, action, decision, {}, modules)
        self.assertEqual(runtime._anti_grind_count, 1)

        runtime._record_planner_outcome(previous, moved, action, decision, {}, modules)
        self.assertIsNone(runtime._anti_grind_key)
        self.assertEqual(runtime._anti_grind_count, 0)
        self.assertEqual(runtime._anti_grind_escape_steps, 0)
    def test_repeated_sector_route_squeeze_triggers_anti_grind_backoff(self):
        FakeAgentPb2 = make_agent_pb2(player_action="explicit", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._metrics = lambda state: state if isinstance(state, dict) else state.metrics  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["recover_stuck", "route_progression", "retreat"],
            "summarize_action": lambda action: {
                "action": int(getattr(action, "action", 0) or 0),
                "raw": {
                    "forward_move": int(getattr(getattr(action, "raw", None), "forward_move", 0) or 0),
                    "side_move": int(getattr(getattr(action, "raw", None), "side_move", 0) or 0),
                    "buttons": int(getattr(getattr(action, "raw", None), "buttons", 0) or 0),
                },
                "mouse": {},
            },
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "sector_route_to_exit_line",
            "action": "squeeze_passable_portal",
            "line": 310,
            "line_id": 310,
            "route_step_sector": 75,
            "probe": 0.0,
        }
        planned_action = FakeAgentPb2.PlayerAction(
            duration_tics=10,
            raw=FakeAgentPb2.RawTiccmd(forward_move=34, side_move=20),
        )
        previous = {"episode": 1, "map": 1, "x": 2960 * 65536, "y": -4000 * 65536, "health": 54}
        current = {"episode": 1, "map": 1, "x": 2960 * 65536, "y": -4000 * 65536, "health": 54}

        for _ in range(ANTI_GRIND_STUCK_THRESHOLD):
            runtime._record_planner_outcome(previous, current, planned_action, decision, {}, modules)

        self.assertGreaterEqual(runtime._anti_grind_count, ANTI_GRIND_STUCK_THRESHOLD)
        state = SimpleNamespace(metrics=current, navigation=SimpleNamespace(back_open=True, direction_probes=[]))
        result = runtime._anti_grind_escape(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            planned_action,
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, escape_action, escape_decision = result
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(escape_decision["blocked_skill"], "sector_route_to_exit_line")
        self.assertEqual(escape_decision["blocked_action"], "squeeze_passable_portal")
        self.assertEqual(escape_decision["line"], 310)
        self.assertLess(escape_action.raw.forward_move, 0)
        self.assertEqual(getattr(escape_action.raw, "buttons", 0), 0)
    def test_repeated_navcell_portal_steer_triggers_anti_grind_unhook_strafe(self):
        FakeAgentPb2 = make_agent_pb2(player_action="explicit", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._metrics = lambda state: state if isinstance(state, dict) else state.metrics  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["recover_stuck", "route_progression", "retreat"],
            "summarize_action": lambda action: {
                "action": int(getattr(action, "action", 0) or 0),
                "raw": {
                    "forward_move": int(getattr(getattr(action, "raw", None), "forward_move", 0) or 0),
                    "side_move": int(getattr(getattr(action, "raw", None), "side_move", 0) or 0),
                    "buttons": int(getattr(getattr(action, "raw", None), "buttons", 0) or 0),
                },
                "mouse": {},
            },
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "navcell_to_portal",
            "action": "steer_forward",
            "line": 385,
            "line_id": 385,
            "route_step_sector": 54,
        }
        planned_action = FakeAgentPb2.PlayerAction(
            duration_tics=8,
            raw=FakeAgentPb2.RawTiccmd(forward_move=50, angle_turn=256),
        )
        previous = {"episode": 1, "map": 1, "x": 2374 * 65536, "y": -2293 * 65536, "health": 92}
        current = {"episode": 1, "map": 1, "x": 2374 * 65536, "y": -2293 * 65536, "health": 92}

        for _ in range(ANTI_GRIND_STUCK_THRESHOLD):
            runtime._record_planner_outcome(previous, current, planned_action, decision, {}, modules)

        self.assertGreaterEqual(runtime._anti_grind_count, ANTI_GRIND_STUCK_THRESHOLD)
        self.assertTrue(runtime._anti_grind_candidate(decision))
        state = SimpleNamespace(
            metrics=current,
            navigation=SimpleNamespace(
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=128 * 65536),
                ],
            ),
        )
        result = runtime._anti_grind_escape(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            planned_action,
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, escape_action, escape_decision = result
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(escape_decision["blocked_skill"], "navcell_to_portal")
        self.assertEqual(escape_decision["blocked_action"], "steer_forward")
        self.assertEqual(escape_decision["line"], 385)
        self.assertEqual(escape_decision["action"], "anti_grind_unhook_strafe_raw")
        self.assertEqual(getattr(escape_action.raw, "forward_move", 0), 0)
        self.assertNotEqual(escape_action.raw.side_move, 0)
        self.assertEqual(getattr(escape_action.raw, "buttons", 0), 0)
    def test_repeated_forward_recovery_stalls_bias_to_lateral_probe(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action="class", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["recover_stuck"]}
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=0, block_distance_fp=220 * 65536),
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=96 * 65536),
                ]
            )
        )

        _index, skill, action, decision = runtime._stuck_recovery_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_FORWARD)
        self.assertEqual(decision["skill"], "live_probe_forward")

        runtime._record_recovery_outcome(action, decision, 0.0, modules)
        runtime._record_recovery_outcome(action, decision, 0.0, modules)

        _index, skill, action, decision = runtime._stuck_recovery_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_STRAFE_LEFT)
        self.assertEqual(decision["skill"], "live_probe_escape_strafe")
        self.assertEqual(decision["prior_forward_stalls"], 2)
    def test_repeated_lateral_recovery_escalates_to_backoff(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action="class", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["recover_stuck"]}
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=96 * 65536),
                ],
            )
        )

        for _ in range(4):
            _index, _skill, action, decision = runtime._stuck_recovery_override(state, directive, FakeController(), modules)
            self.assertEqual(decision["skill"], "live_probe_strafe")
            self.assertEqual(decision["action"], "strafe")
            self.assertTrue(runtime._anti_grind_candidate(decision))
            runtime._record_recovery_outcome(action, decision, 12.0, modules)

        _index, skill, action, decision = runtime._stuck_recovery_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(decision["skill"], "live_probe_escape_backoff")
        self.assertEqual(decision["action"], "backoff")
        self.assertTrue(runtime._anti_grind_candidate(decision))
        self.assertEqual(decision["repeat"], 4)
    def test_repeated_recovery_turn_charges_last_spatial_door_line(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._last_spatial_route_line_id = 378

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=False,
                    walk_trigger=False,
                    door=True,
                    use_trigger=True,
                    exit=False,
                    special=1,
                    tag=0,
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {"agent_pb2": FakeAgentPb2}
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_TURN_LEFT)
        decision = {"source": "stuck_recovery", "skill": "live_probe_turn", "probe": 30.0}

        for _ in range(3):
            runtime._record_recovery_outcome(action, decision, 0.0, modules)

        self.assertEqual(runtime._door_memory.state_for(378), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(378), 0)
        self.assertIsNone(runtime._last_spatial_route_line_id)
    def test_repeated_probe_explore_turn_escalates_to_recovery(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action="class", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        class FakePlanner:
            @staticmethod
            def sector_for_point_fp(_x, _y):
                return 32

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["recover_stuck"]}
        state = SimpleNamespace(
            level=SimpleNamespace(episode=1, map=2, total_kills=0),
            player=SimpleNamespace(
                health=9,
                kills=0,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(
                    angle_degrees=0,
                    position=SimpleNamespace(x_fp=256 * 65536, y_fp=128 * 65536),
                ),
            ),
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            enemies=[],
            navigation=SimpleNamespace(
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=64 * 65536),
                ],
            ),
        )
        planned_action = FakeAgentPb2.PlayerAction(action=FakeAgentPb2.ACTION_TURN_LEFT, amount=26, duration_tics=3)
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "planner_probe_explore",
            "action": "turn",
        }

        for _ in range(5):
            self.assertIsNone(
                runtime._repeated_probe_explore_escape(
                    state,
                    directive,
                    FakeController(),
                    modules,
                    FakePlanner(),
                    planned_action,
                    decision,
                )
            )

        result = runtime._repeated_probe_explore_escape(
            state,
            directive,
            FakeController(),
            modules,
            FakePlanner(),
            planned_action,
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, decision_out = result
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_STRAFE_LEFT)
        self.assertEqual(decision_out["skill"], "planner_probe_explore_escape")
        self.assertEqual(decision_out["reason"], "repeated_probe_explore")
        self.assertIn(32, runtime._world_memory.blocked_frontier_sectors)

        chained = runtime._repeated_probe_explore_escape(
            state,
            directive,
            FakeController(),
            modules,
            FakePlanner(),
            planned_action,
            decision,
        )
        self.assertIsNotNone(chained)
        _index, skill, action, decision_out = chained
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_STRAFE_LEFT)
        self.assertEqual(decision_out["reason"], "repeated_probe_explore_chain")
        self.assertEqual(decision_out["action"], "strafe_escape_chain")
    def test_speedrun_tune_skips_close_exit_actions(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        modules = {"agent_pb2": FakeAgentPb2}
        close = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=34, duration_tics=4)
        runtime._tune_action_for_directive(close, directive, modules, {"skill": "sector_route_to_exit_line", "dist": 48})
        self.assertEqual(close.amount, 34)
        self.assertEqual(close.duration_tics, 4)
        far = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=34, duration_tics=4)
        runtime._tune_action_for_directive(far, directive, modules, {"skill": "sector_route_to_exit_line", "dist": 240})
        self.assertEqual(far.amount, 50)
        self.assertEqual(far.duration_tics, 16)
    def test_run_action_sends_use_press_then_release(self):
        class FakeAction:
            def __init__(self, **kwargs):
                self.action = kwargs.get("action", 0)
                self.amount = kwargs.get("amount", 0)
                self.duration_tics = kwargs.get("duration_tics", 1)
                self.raw = kwargs.get("raw", None)

            def CopyFrom(self, other):
                self.action = getattr(other, "action", 0)
                self.amount = getattr(other, "amount", 0)
                self.duration_tics = getattr(other, "duration_tics", 1)
                self.raw = getattr(other, "raw", None)

        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=FakeAction, ACTION_USE=8)

        class FakeStub:
            def __init__(self):
                self.actions = []

            def GameSession(self, actions, timeout=None):
                for item in actions:
                    self.actions.append(item)
                    yield SimpleNamespace(tick=len(self.actions))

        runtime = BrainRuntime()
        stub = FakeStub()
        action = FakeAction(action=FakeAgentPb2.ACTION_USE, amount=1, duration_tics=4)
        state = runtime._run_action(stub, action, {"agent_pb2": FakeAgentPb2})
        self.assertEqual(state.tick, 2)
        self.assertEqual(action.duration_tics, 2)
        self.assertEqual([item.action for item in stub.actions], [FakeAgentPb2.ACTION_USE, 0])
        self.assertEqual([item.duration_tics for item in stub.actions], [1, 1])
    def test_sector_route_contact_stall_marks_line_blocked(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory(max_failures=2)
        runtime._metrics = lambda state: state  # type: ignore[method-assign]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {"source": "spatial_planner", "line_id": 385, "skill": "sector_route_to_exit_line"}
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": 0, "health": 20}
        current = {"x": 0, "y": 0, "health": 15}
        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)
        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)
        self.assertTrue(runtime._door_memory.is_blocked(385))
    def test_passable_sector_route_contact_adds_penalty_without_blocking(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory(max_failures=2)
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(passable=True)

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {"source": "spatial_planner", "line_id": 385, "skill": "sector_route_to_exit_line"}
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": 0, "health": 20}
        current = {"x": 0, "y": 0, "health": 15}
        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)
        self.assertFalse(runtime._door_memory.is_blocked(385))
        self.assertEqual(runtime._door_memory.state_for(385), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(385), 0)
    def test_repeated_force_follow_live_use_suppresses_line(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory(max_failures=2)
        runtime._door_memory.observe_line(389, special=103)
        runtime._metrics = lambda state: state  # type: ignore[method-assign]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": 0, "raw": {"forward_move": 56}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "line_id": 389,
            "skill": "last_chance_live_use_line",
            "action": "force_follow_live_use_line",
            "special": 103,
        }
        action = SimpleNamespace(action=0, raw=SimpleNamespace(forward_move=56, side_move=0))
        previous = {"x": 0, "y": 0, "health": 73}
        current = {"x": 0, "y": 0, "health": 73}

        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)
        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)
        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertEqual(runtime._door_memory.force_follow_failures_for(389), 1)
        self.assertEqual(runtime._door_memory.state_for(389), "congested")
        self.assertTrue(runtime._door_memory.live_line_suppressed(389))
    def test_frontier_route_contact_retires_blocked_frontier_sector(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory(max_failures=2)
        runtime._world_memory.frontier_sectors.add(32)
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(passable=True)

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "line_id": 151,
            "sector": 32,
            "planner_skill": "route_progression",
            "skill": "frontier_sector_route",
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": 0, "health": 70}
        current = {"x": 0, "y": 0, "health": 70}

        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertEqual(runtime._door_memory.state_for(151), "congested")
        self.assertNotIn(32, runtime._world_memory.frontier_sectors)
        self.assertIn(32, runtime._world_memory.blocked_frontier_sectors)
    def test_repeated_frontier_route_retires_sector_even_when_route_reports_reached(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory(max_failures=2)
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=False,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    a=SimpleNamespace(x=313 * 65536, y=0),
                    b=SimpleNamespace(x=313 * 65536, y=64 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "line_id": 313,
            "line": 313,
            "sector": 89,
            "planner_skill": "route_progression",
            "skill": "frontier_sector_route",
            "action": "forward",
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": 128 * 65536, "health": 76}
        current = {"x": 32 * 65536, "y": 128 * 65536, "health": 76}

        for _ in range(8):
            runtime._record_planner_outcome(previous, current, action, decision, {"reached": True}, modules)

        self.assertIn(89, runtime._world_memory.blocked_frontier_sectors)
        self.assertEqual(runtime._door_memory.state_for(313), "congested")
    def test_alternating_frontier_lines_in_same_sector_retire_frontier(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory(max_failures=2)
        runtime._world_memory.frontier_sectors.add(77)
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=False,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    a=SimpleNamespace(x=0, y=0),
                    b=SimpleNamespace(x=0, y=64 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": 0, "health": 88}
        current = {"x": 0, "y": 0, "health": 88}

        for line_id in (216, 219, 221, 222):
            decision = {
                "source": "spatial_planner",
                "line_id": line_id,
                "line": line_id,
                "sector": 77,
                "planner_skill": "route_progression",
                "skill": "frontier_sector_route",
                "action": "squeeze_passable_portal",
            }
            runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertIn(77, runtime._world_memory.blocked_frontier_sectors)
        self.assertEqual(runtime._door_memory.state_for(222), "congested")
    def test_passable_portal_squeeze_damage_adds_congestion_penalty(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory(max_failures=2)
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(passable=True)

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": 0, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "line_id": 310,
            "planner_skill": "route_progression",
            "skill": "squeeze_passable_portal",
        }
        action = SimpleNamespace(action=0, raw=SimpleNamespace(forward_move=54, side_move=42))
        previous = {"x": 0, "y": 0, "health": 20}
        current = {"x": 10 * 65536, "y": 0, "health": 14}

        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertFalse(runtime._door_memory.is_blocked(310))
        self.assertEqual(runtime._door_memory.state_for(310), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(310), 0)
    def test_route_waypoint_walk_progress_uses_detail_line_when_no_door_line_id(self):
        FakeAgentPb2 = make_agent_pb2(player_action="explicit", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "route_waypoint_walk",
            "line": 289,
            "special": 88,
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": 0, "health": 100}
        current = {"x": 40 * 65536, "y": 0, "health": 100}

        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertEqual(runtime._door_memory.state_for(289), "opened")
        self.assertEqual(runtime._door_memory.last_status_for(289), "progress_after_line")
    def test_non_lift_walk_trigger_progress_does_not_require_crossing_proof(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=True,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    special=97,
                    tag=0,
                    a=SimpleNamespace(x=128 * 65536, y=0),
                    b=SimpleNamespace(x=128 * 65536, y=64 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "planner_route_walk_trigger_for_contact",
            "line_id": 289,
            "special": 97,
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": -80 * 65536, "health": 100}
        current = {"x": 40 * 65536, "y": -80 * 65536, "health": 100}

        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertEqual(runtime._door_memory.state_for(289), "opened")
        self.assertEqual(runtime._door_memory.last_status_for(289), "progress_after_line")
    def test_walk_lift_progress_requires_actual_crossing(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=True,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    special=88,
                    tag=4,
                    a=SimpleNamespace(x=128 * 65536, y=0),
                    b=SimpleNamespace(x=128 * 65536, y=64 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "planner_route_walk_trigger_for_contact",
            "line_id": 289,
            "special": 88,
            "tag": 4,
            "action": "cross_walk_trigger",
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": -80 * 65536, "health": 100}
        current = {"x": 40 * 65536, "y": -80 * 65536, "health": 100}

        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertFalse(runtime._door_memory.is_open(289))
        self.assertEqual(runtime._door_memory.state_for(289), "closed")
    def test_walk_lift_crossing_marks_trigger_as_settling(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=True,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    special=88,
                    tag=4,
                    a=SimpleNamespace(x=128 * 65536, y=0),
                    b=SimpleNamespace(x=128 * 65536, y=64 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "planner_route_walk_trigger_for_contact",
            "line_id": 289,
            "special": 88,
            "tag": 4,
            "action": "cross_walk_trigger",
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 96 * 65536, "y": 32 * 65536, "health": 100}
        current = {"x": 160 * 65536, "y": 32 * 65536, "health": 100}

        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertTrue(runtime._door_memory.is_opening(289))
        self.assertEqual(runtime._door_memory.state_for(289), "opening")
        self.assertEqual(runtime._door_memory.last_status_for(289), "walk_lift_triggered")
    def test_repeated_route_waypoint_without_cross_abandons_line(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=True,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    special=88,
                    tag=0,
                    a=SimpleNamespace(x=192 * 65536, y=128 * 65536),
                    b=SimpleNamespace(x=192 * 65536, y=192 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "route_waypoint_walk",
            "line": 288,
            "special": 88,
            "action": "cross",
            "dist": 24,
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": 0, "health": 100}
        current = {"x": 40 * 65536, "y": 0, "health": 100}

        for _ in range(3):
            runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        # Special 88 is a WR walk-over lift on a PASSABLE line (E1M2 lines 288/289): repeated
        # no-cross is congestion (monster in the way, lift timing), never a hard block — the
        # line may be the only bridge into the map's east half. Non-passable lines still block.
        self.assertFalse(runtime._door_memory.is_blocked(288))
        self.assertEqual(runtime._door_memory.state_for(288), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(288), 0)
        self.assertEqual(runtime._door_memory.last_status_for(288), "repeated_route_no_cross")
    def test_repeated_sector_route_without_cross_abandons_line(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=False,
                    walk_trigger=False,
                    door=True,
                    use_trigger=True,
                    exit=False,
                    special=1,
                    tag=0,
                    a=SimpleNamespace(x=192 * 65536, y=128 * 65536),
                    b=SimpleNamespace(x=192 * 65536, y=192 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "sector_route_to_use_line",
            "line_id": 378,
            "special": 1,
            "action": "steer_forward",
            "dist": 96,
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": 0, "health": 100}
        current = {"x": 40 * 65536, "y": 0, "health": 100}

        for _ in range(4):
            runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertFalse(runtime._door_memory.is_blocked(378))
        self.assertEqual(runtime._door_memory.state_for(378), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(378), 0)
        self.assertEqual(runtime._door_memory.last_status_for(378), "repeated_route_no_cross")
    def test_repeated_navcell_portal_without_cross_abandons_line(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=False,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    special=0,
                    tag=0,
                    a=SimpleNamespace(x=512 * 65536, y=128 * 65536),
                    b=SimpleNamespace(x=512 * 65536, y=192 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": 0, "raw": {"forward_move": 50}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "navcell_to_portal",
            "line_id": 324,
            "action": "steer_forward",
            "dist": 420,
        }
        action = SimpleNamespace(action=0, raw=SimpleNamespace(forward_move=50, angle_turn=128))
        previous = {"x": 0, "y": 0, "health": 100}
        current = {"x": 40 * 65536, "y": 0, "health": 100}

        for _ in range(4):
            runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        # Passable plain boundary: congested with a route penalty, never hard-blocked — it may be
        # the map's only bridge (E1M2 line 400 regression).
        self.assertFalse(runtime._door_memory.is_blocked(324))
        self.assertEqual(runtime._door_memory.state_for(324), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(324), 0)
        self.assertEqual(runtime._door_memory.last_status_for(324), "repeated_route_no_cross")
    def test_repeated_navcell_portal_reached_without_cross_abandons_line(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=False,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    special=0,
                    tag=0,
                    a=SimpleNamespace(x=512 * 65536, y=128 * 65536),
                    b=SimpleNamespace(x=512 * 65536, y=192 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": 0, "raw": {"forward_move": 50}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "navcell_to_portal",
            "line_id": 385,
            "line": 385,
            "action": "steer_forward",
            "dist": 96,
            "route_step_sector": 54,
        }
        action = SimpleNamespace(action=0, raw=SimpleNamespace(forward_move=50, angle_turn=128))
        previous = {"x": 480 * 65536, "y": 96 * 65536, "health": 100}
        current = {"x": 496 * 65536, "y": 96 * 65536, "health": 100}

        for _ in range(3):
            runtime._record_planner_outcome(previous, current, action, decision, {"reached": True}, modules)

        self.assertFalse(runtime._door_memory.is_blocked(385))
        self.assertEqual(runtime._door_memory.state_for(385), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(385), 0)
        self.assertEqual(runtime._door_memory.last_status_for(385), "repeated_route_no_cross")
    def test_repeated_center_passable_portal_reached_without_cross_abandons_line(self):
        FakeAgentPb2 = make_agent_pb2(player_action="explicit", ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=False,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    special=0,
                    tag=0,
                    a=SimpleNamespace(x=3200 * 65536, y=-4200 * 65536),
                    b=SimpleNamespace(x=3264 * 65536, y=-4200 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["recover_stuck", "route_progression", "retreat"],
            "summarize_action": lambda _action: {"action": 0, "raw": {"forward_move": 46, "side_move": 34}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "center_passable_portal",
            "line_id": 308,
            "line": 308,
            "action": "center_passable_portal_raw",
            "dist": 180,
            "side": "left",
        }
        action = SimpleNamespace(action=0, raw=SimpleNamespace(forward_move=46, side_move=34, angle_turn=128))
        previous = {"x": 3140 * 65536, "y": -4300 * 65536, "health": 100}
        current = {"x": 3160 * 65536, "y": -4280 * 65536, "health": 100}

        for _ in range(3):
            runtime._record_planner_outcome(previous, current, action, decision, {"reached": True}, modules)

        self.assertFalse(runtime._door_memory.is_blocked(308))
        self.assertEqual(runtime._door_memory.state_for(308), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(308), 0)
        self.assertEqual(runtime._door_memory.last_status_for(308), "repeated_route_no_cross")
        self.assertGreaterEqual(runtime._anti_grind_count, ANTI_GRIND_STUCK_THRESHOLD)
        self.assertEqual(runtime._anti_grind_escape_steps, 2)

        state = SimpleNamespace(
            metrics=current,
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        result = runtime._anti_grind_escape(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            action,
            decision,
        )
        self.assertIsNotNone(result)
        _index, skill, escape_action, escape_decision = result
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(escape_decision["skill"], "anti_grind_escape")
        self.assertEqual(escape_decision["blocked_action"], "center_passable_portal_raw")
        self.assertEqual(escape_action.raw.forward_move, -24)
        self.assertEqual(escape_action.raw.side_move, -52)
        self.assertEqual(getattr(escape_action.raw, "buttons", 0), 0)
    def test_repeated_route_turn_without_cross_abandons_line(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=False,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    special=0,
                    tag=0,
                    a=SimpleNamespace(x=512 * 65536, y=128 * 65536),
                    b=SimpleNamespace(x=512 * 65536, y=192 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_TURN_LEFT, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "frontier_sector_route",
            "line_id": 142,
            "action": "turn",
            "dist": 96,
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_TURN_LEFT)
        previous = {"x": 0, "y": 0, "health": 100}
        current = {"x": 0, "y": 0, "health": 100}

        for _ in range(3):
            runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertFalse(runtime._door_memory.is_blocked(142))
        self.assertEqual(runtime._door_memory.state_for(142), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(142), 0)
        self.assertEqual(runtime._door_memory.last_status_for(142), "repeated_route_no_cross")
    def test_frontier_route_to_door_without_advance_marks_congested(self):
        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        line = SimpleNamespace(
            passable=False,
            walk_trigger=False,
            door=True,
            use_trigger=True,
            exit=False,
            special=1,
            tag=0,
        )
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "frontier_sector_route",
            "line_id": 325,
            "action": "forward",
            "sector": 78,
        }

        for _ in range(8):
            runtime._record_frontier_route_repeat(
                line_id=325,
                line=line,
                decision=decision,
                crossed_line=False,
                moved_units=40,
            )

        self.assertEqual(runtime._door_memory.state_for(325), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(325), 0)
    def test_far_repeated_navcell_turn_eventually_abandons_line(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=True,
                    walk_trigger=False,
                    door=False,
                    use_trigger=False,
                    exit=False,
                    special=0,
                    tag=0,
                    a=SimpleNamespace(x=900 * 65536, y=128 * 65536),
                    b=SimpleNamespace(x=900 * 65536, y=192 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_TURN_LEFT, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "navcell_to_portal",
            "line_id": 324,
            "action": "turn",
            "dist": 900,
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_TURN_LEFT)
        previous = {"x": 0, "y": 0, "health": 100}
        current = {"x": 0, "y": 0, "health": 100}

        for _ in range(6):
            runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertFalse(runtime._door_memory.is_blocked(324))
        self.assertEqual(runtime._door_memory.state_for(324), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(324), 0)
        self.assertEqual(runtime._door_memory.last_status_for(324), "repeated_route_no_cross")
    def test_closed_use_door_progress_requires_crossing_or_reached_evidence(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._metrics = lambda state: state  # type: ignore[method-assign]

        class FakePlanner:
            @staticmethod
            def _line_by_id(_line_id):
                return SimpleNamespace(
                    passable=False,
                    walk_trigger=False,
                    door=True,
                    use_trigger=True,
                    exit=False,
                    a=SimpleNamespace(x=128 * 65536, y=0),
                    b=SimpleNamespace(x=128 * 65536, y=64 * 65536),
                )

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "route_progression",
            "skill": "planner_route_use_line_for_contact",
            "line_id": 151,
            "special": 1,
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": -80 * 65536, "health": 100}
        current = {"x": 40 * 65536, "y": -80 * 65536, "health": 100}

        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertNotEqual(runtime._door_memory.state_for(151), "opened")
    def test_use_action_incidental_movement_does_not_mark_blocked_door_opened(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()

        def fake_metrics(state):
            return {
                "x": state.x,
                "y": state.y,
                "health": 100,
            }

        runtime._metrics = fake_metrics  # type: ignore[method-assign]
        runtime._door_memory.observe_line(389, special=103)
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_USE, "raw": {}, "mouse": {}},
        }
        decision = {
            "source": "spatial_planner",
            "planner_skill": "open_use_line",
            "skill": "planner_live_use_line",
            "line": 389,
            "special": 103,
        }
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_USE)
        previous = SimpleNamespace(x=0, y=0)
        current = SimpleNamespace(
            x=14 * 65536,
            y=0,
            navigation=SimpleNamespace(forward_open=False, front_block_distance_fp=32 * 65536),
        )

        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertNotEqual(runtime._door_memory.state_for(389), "opened")
        self.assertEqual(runtime._door_memory.state_for(389), "opening")
        self.assertEqual(runtime._door_memory.last_status_for(389), "assumed_opening")
    def test_planner_contact_use_line_stall_adds_congestion_penalty(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8)

        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory(max_failures=2)
        runtime._metrics = lambda state: state  # type: ignore[method-assign]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}},
        }
        decision = {"source": "spatial_planner", "line_id": 248, "skill": "planner_route_use_line_for_contact"}
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD)
        previous = {"x": 0, "y": 0, "health": 100}
        current = {"x": 0, "y": 0, "health": 100}

        runtime._record_planner_outcome(previous, current, action, decision, {}, modules)

        self.assertEqual(runtime._door_memory.state_for(248), "congested")
        self.assertGreater(runtime._door_memory.route_penalty_for(248), 0)
    def test_normal_use_door_route_contact_stays_retryable(self):
        memory = DoorMemory(max_failures=2)
        memory.observe_line(325, special=1)
        memory.record_failure(325, status="route_contact_blocked")
        memory.record_failure(325, status="route_contact_blocked")
        self.assertEqual(memory.state_for(325), "closed")
        self.assertTrue(memory.can_retry(325))
    def test_speedrun_no_kill_allows_route_recovery_under_combat_contact(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        self.assertTrue(runtime._route_recovery_allowed_under_contact(directive))
        combat = parse_directive({"goal": "kill first enemy"})
        self.assertFalse(runtime._route_recovery_allowed_under_contact(combat))
    def test_speedrun_exit_prefers_planner_before_generic_recovery(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        self.assertTrue(runtime._prefer_planner_before_recovery(directive))
        combat = parse_directive({"goal": "kill first enemy"})
        self.assertFalse(runtime._prefer_planner_before_recovery(combat))
    def test_speedrun_exit_switches_to_recovery_after_repeated_stalls(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        self.assertTrue(runtime._planner_first_for_stall(directive, 3))
        self.assertFalse(runtime._planner_first_for_stall(directive, 4))
    def test_speedrun_no_kill_keeps_routing_past_far_visible_contact(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        runtime._metrics = lambda _state: {"shootable": False, "visible_enemy": True, "health": 100}  # type: ignore[method-assign]
        runtime._nearest_enemy = lambda _state, prefer_visible=False: {"distance": 900.0}  # type: ignore[method-assign]
        self.assertIsNone(runtime._contract_override(object(), directive, object(), {"agent_pb2": object()}))
    def test_speedrun_no_kill_keeps_routing_past_point_blank_contact(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        runtime._metrics = lambda _state: {"shootable": True, "visible_enemy": True, "health": 100}  # type: ignore[method-assign]
        runtime._nearest_enemy = lambda _state, prefer_visible=False: {"distance": 96.0}  # type: ignore[method-assign]
        runtime._safe_contract_action = lambda *_args, **_kwargs: (1, "retreat", object(), {"reason": _kwargs["reason"]})  # type: ignore[method-assign]
        self.assertIsNone(runtime._contract_override(object(), directive, object(), {"agent_pb2": object()}))
    def test_speedrun_no_kill_keeps_routing_when_low_health(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        runtime._metrics = lambda _state: {"shootable": True, "visible_enemy": True, "health": 20}  # type: ignore[method-assign]
        runtime._nearest_enemy = lambda _state, prefer_visible=False: {"distance": 96.0}  # type: ignore[method-assign]
        runtime._safe_contract_action = lambda *_args, **_kwargs: (1, "retreat", object(), {"reason": _kwargs["reason"]})  # type: ignore[method-assign]
        self.assertIsNone(runtime._contract_override(object(), directive, object(), {"agent_pb2": object()}))


if __name__ == "__main__":
    unittest.main()
