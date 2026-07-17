"""Route-threshold refusal, final-exit commit, critical breakaway, and no-kill desperation for Agent DOOM."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from doom_fakes import make_agent_pb2, make_controller, make_enemy  # noqa: E402

from brain_runtime import (  # noqa: E402
    BrainRuntime,
    FP_UNIT,
    NO_KILL_DESPERATION_PANIC_REPEATS,
    NO_KILL_DESPERATION_SPRINT_BURST_TICS,
    NO_KILL_DESPERATION_SPRINT_TICS,
    NO_KILL_ROUTE_REFUSAL_MULT,
    ROUTE_CLEAN_SHOT_TURN_DEGREES,
    ROUTE_CRITICAL_HEALTH_BREAKAWAY,
    ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE,
    ROUTE_HITSCAN_FLINCH_STEPS,
    ROUTE_THREAT_HOLD_MIN_TICS,
    ROUTE_THREAT_RELEASE_MULT,
    parse_directive,
)
from planner import ROUTE_THREAT_REFUSE_MULT, PlanAction, Point  # noqa: E402


class TestAgentDoomRouteThresholdRefusal(unittest.TestCase):
    def test_armed_route_threshold_refusal_fires_instead_of_progression(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_SHOOT=7)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "fire", "close_visible_contact"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=0),
            player=SimpleNamespace(
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
            )
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "action": "steer_forward",
            "line": 265,
            "route_step_kind": "portal",
            "route_step_line": 265,
            "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
            "turn": 0.0,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, refusal_decision = result
        self.assertEqual(skill, "fire")
        self.assertEqual(action.raw.buttons, 1)
        self.assertEqual(refusal_decision["skill"], "threshold_route_refusal_fire")
        self.assertEqual(refusal_decision["refused_skill"], "route_progression")
        self.assertEqual(refusal_decision["refused_route_skill"], "sector_route_to_use_line")
        self.assertEqual(refusal_decision["route_step_line"], 265)
        self.assertEqual(refusal_decision["threshold"], ROUTE_THREAT_REFUSE_MULT)
        self.assertEqual(refusal_decision["release_threshold"], ROUTE_THREAT_RELEASE_MULT)
        self.assertEqual(refusal_decision["hold_tics"], ROUTE_THREAT_HOLD_MIN_TICS)
        self.assertEqual(refusal_decision["sustained"], 0)
        self.assertEqual(runtime._route_refusal_hold_tics, ROUTE_THREAT_HOLD_MIN_TICS - action.duration_tics)
    def test_route_threshold_refusal_breaks_away_at_critical_health(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=3, ACTION_TURN_LEFT=4, ACTION_TURN_RIGHT=5)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=0),
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
            ),
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "action": "steer_forward",
            "line": 271,
            "route_step_kind": "portal",
            "route_step_line": 271,
            "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
            "turn": 0.0,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, breakaway_decision = result
        self.assertEqual(skill, "retreat")
        self.assertGreater(action.raw.forward_move, 0)
        self.assertNotEqual(action.raw.angle_turn, 0)
        self.assertEqual(breakaway_decision["skill"], "critical_turn_and_burn_raw")
        self.assertEqual(breakaway_decision["state"], "critical_health_breakaway")
        self.assertEqual(breakaway_decision["reason"], "route_threshold_critical_health_breakaway")
        self.assertEqual(breakaway_decision["health"], ROUTE_CRITICAL_HEALTH_BREAKAWAY)
        self.assertEqual(breakaway_decision["critical_health"], ROUTE_CRITICAL_HEALTH_BREAKAWAY)
        self.assertEqual(runtime._route_refusal_hold_tics, 0)
        self.assertGreater(runtime._cautious_retreat_commit_steps, 0)
    def test_route_threshold_critical_breakaway_releases_when_threat_is_distant(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=3, ACTION_SHOOT=7)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "fire"]}
        far_units = int(ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE + 128)
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=9),
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=9, x_fp=far_units * 65536, y_fp=0)
            ],
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "action": "steer_forward",
            "line": 271,
            "route_step_kind": "portal",
            "route_step_line": 271,
            "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
            "turn": 0.0,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, refusal_decision = result
        self.assertEqual(skill, "fire")
        self.assertEqual(action.raw.buttons, 1)
        self.assertEqual(refusal_decision["skill"], "threshold_route_refusal_fire")
        self.assertNotEqual(refusal_decision.get("state"), "critical_health_breakaway")
    def test_route_threshold_refusal_breakaway_floor_is_shotgun_burst_safe(self):
        self.assertEqual(ROUTE_CRITICAL_HEALTH_BREAKAWAY, 40)
    def test_final_corridor_sprint_refuses_at_critical_health_under_contact(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_BACKWARD=3, ACTION_TURN_LEFT=4, ACTION_TURN_RIGHT=5)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=9),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=9, x_fp=96 * 65536, y_fp=0)
            ],
        )
        decision = {
            "source": "spatial_planner",
            "planner_skill": "open_use_line",
            "skill": "final_corridor_use_line",
            "action": "final_corridor_sprint_opening",
            "line": 341,
            "line_id": 341,
            "special": 1,
            "dist": 40,
            "turn": 0.0,
        }

        result = runtime._final_corridor_sprint_health_refusal(
            state,
            directive,
            FakeController(),
            modules,
            "open_use_line",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, refusal_decision = result
        self.assertEqual(skill, "retreat")
        self.assertEqual(refusal_decision["state"], "critical_health_breakaway")
        self.assertEqual(refusal_decision["final_corridor_sprint_refused"], 1)
        self.assertEqual(refusal_decision["refused_skill"], "open_use_line")
        self.assertEqual(refusal_decision["refused_route_skill"], "final_corridor_use_line")
        self.assertEqual(refusal_decision["refused_action"], "final_corridor_sprint_opening")
        self.assertEqual(refusal_decision["line"], 341)
        self.assertGreater(action.raw.forward_move, 0)
        self.assertNotEqual(action.raw.angle_turn, 0)
    def test_final_exit_commit_allows_critical_final_corridor_sprint(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=9),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(
                    position=SimpleNamespace(x_fp=int(3178 * FP_UNIT), y_fp=int(-4528 * FP_UNIT)),
                    angle_degrees=0,
                ),
            ),
            enemies=[
                make_enemy(id=9, x_fp=int(3096 * FP_UNIT), y_fp=int(-4560 * FP_UNIT))
            ],
        )
        decision = {
            "source": "spatial_planner",
            "planner_skill": "open_use_line",
            "skill": "final_corridor_use_line",
            "action": "final_corridor_sprint_opening",
            "line": 325,
            "line_id": 325,
            "special": 1,
            "dist": 40,
            "turn": 0.0,
        }

        result = runtime._final_corridor_sprint_health_refusal(
            state,
            directive,
            FakeController(),
            modules,
            "open_use_line",
            decision,
        )

        self.assertIsNone(result)
    def test_hot_health_route_breaks_away_instead_of_steering_forward(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_BACKWARD=3, ACTION_TURN_LEFT=4, ACTION_TURN_RIGHT=5)

        FakeController = make_controller(3)

        class FakePlanner:
            def objective_action(self, *_args):
                return PlanAction(
                    skill="route_progression",
                    action=FakeAgentPb2.PlayerAction(
                        duration_tics=8,
                        raw=FakeAgentPb2.RawTiccmd(forward_move=48),
                    ),
                    door_line_id=340,
                    detail={
                        "skill": "sector_route_to_health",
                        "action": "steer_forward",
                        "line": 340,
                        "route_step_line": 340,
                        "route_step_sector": 76,
                        "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
                        "turn": 0.0,
                    },
                )

        runtime = BrainRuntime()
        runtime._refresh_planner = lambda *_args: FakePlanner()  # type: ignore[method-assign]
        runtime._update_world_model = lambda _state: None  # type: ignore[method-assign]
        runtime._combat_state = SimpleNamespace(update=lambda _state: None)
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "recover_stuck"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(back_open=True, direction_probes=[], use_lines=[]),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY + 5,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=77, x_fp=96 * 65536, y_fp=0)
            ],
        )

        result = runtime._planner_override(state, directive, FakeController(), modules, stub=None)

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "retreat")
        self.assertLess(action.raw.forward_move, 0)
        self.assertEqual(decision["source"], "health_route_refusal")
        self.assertEqual(decision["reason"], "health_route_threat_refusal")
        self.assertEqual(decision["refused_route_skill"], "sector_route_to_health")
        self.assertEqual(decision["lethal_step"], 1)
        self.assertEqual(decision["route_step_line"], 340)
        self.assertEqual(decision["route_step_sector"], 76)
    def test_health_route_refusal_turns_and_burns_at_critical_contact(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=3, ACTION_FORWARD=1, ACTION_TURN_LEFT=4, ACTION_TURN_RIGHT=5)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "recover_stuck"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(back_open=True, direction_probes=[], use_lines=[]),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=77, x_fp=96 * 65536, y_fp=0)
            ],
        )
        decision = {
            "skill": "sector_route_to_health",
            "action": "steer_forward",
            "route_step_threat_mult": 1.0,
            "route_step_line": 340,
            "route_step_sector": 76,
        }

        result = runtime._health_route_threat_refusal(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "retreat")
        self.assertGreater(action.raw.forward_move, 0)
        self.assertEqual(decision["source"], "health_route_refusal")
        self.assertEqual(decision["reason"], "health_route_critical_breakaway")
        self.assertEqual(decision["critical_contact"], 1)
        self.assertEqual(decision["lethal_step"], 0)
    def test_safe_health_route_is_not_refused(self):
        runtime = BrainRuntime()
        runtime._metrics = lambda _state: {"health": ROUTE_CRITICAL_HEALTH_BREAKAWAY + 5, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        decision = {
            "skill": "sector_route_to_health",
            "action": "steer_forward",
            "route_step_threat_mult": 1.0,
        }

        result = runtime._health_route_threat_refusal(
            SimpleNamespace(),
            directive,
            SimpleNamespace(),
            {},
            "route_progression",
            decision,
        )

        self.assertIsNone(result)
    def test_route_threshold_refusal_flinches_after_damaged_clean_shot_alignment(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=3, ACTION_TURN_LEFT=4, ACTION_TURN_RIGHT=5)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        previous = SimpleNamespace(player=SimpleNamespace(health=58))
        current = SimpleNamespace(player=SimpleNamespace(health=43))
        damaged_alignment = {
            "source": "route_threshold_refusal",
            "skill": "threshold_route_clean_shot",
            "action": "align_clean_shot",
        }

        runtime._record_route_threshold_flinch(previous, current, damaged_alignment)

        self.assertEqual(runtime._route_refusal_flinch_steps, ROUTE_HITSCAN_FLINCH_STEPS)

        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "fire"]}
        runtime._route_refusal_hold_tics = 0
        runtime._route_refusal_hold_key = ("portal", 996, 64)
        runtime._route_refusal_hold_peak = ROUTE_THREAT_REFUSE_MULT
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=44),
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
            player=SimpleNamespace(
                health=43,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=44, x_fp=96 * 65536, y_fp=0, type_id=9, health=9)
            ],
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "action": "steer_forward",
            "line": 996,
            "route_step_kind": "portal",
            "route_step_line": 996,
            "route_step_sector": 64,
            "route_step_threat_mult": 1.0,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, flinch_decision = result
        self.assertEqual(skill, "retreat")
        self.assertLess(action.raw.forward_move, 0)
        self.assertEqual(flinch_decision["skill"], "funnel_back_raw")
        self.assertEqual(flinch_decision["state"], "hitscan_flinch_breakaway")
        self.assertEqual(flinch_decision["reason"], "route_threshold_hitscan_flinch")
        self.assertEqual(flinch_decision["enemy_threat"], "hitscan")
        self.assertEqual(flinch_decision["flinch_steps"], ROUTE_HITSCAN_FLINCH_STEPS)
    def test_route_threshold_refusal_aligns_instead_of_firing_when_off_angle(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=44),
            player=SimpleNamespace(
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=44, x_fp=0, y_fp=384 * 65536)
            ],
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "route_step_kind": "portal",
            "route_step_line": 265,
            "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
            "turn": 0.0,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, refusal_decision = result
        self.assertEqual(skill, "route_progression")
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
        self.assertGreater(action.raw.angle_turn, 0)
        self.assertEqual(refusal_decision["skill"], "threshold_route_clean_shot")
        self.assertEqual(refusal_decision["reason"], "clean_shot_off_angle")
        self.assertEqual(refusal_decision["clean_turn_threshold"], ROUTE_CLEAN_SHOT_TURN_DEGREES)
    def test_final_exit_commit_releases_route_threshold_clean_shot_hold(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=44),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(use_lines=[]),
            player=SimpleNamespace(
                health=35,
                kills=3,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=42, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(
                    position=SimpleNamespace(x_fp=int(3095 * FP_UNIT), y_fp=int(-3625 * FP_UNIT)),
                    angle_degrees=0,
                ),
            ),
            enemies=[
                make_enemy(id=44, x_fp=int(3095 * FP_UNIT), y_fp=int(-3800 * FP_UNIT))
            ],
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "route_step_kind": "portal",
            "route_step_line": 340,
            "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
            "turn": 90.0,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNone(result)
    def test_final_exit_commit_override_preempts_active_critical_retreat(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._critical_turn_and_burn_steps = 1
        runtime._critical_turn_and_burn_handoff_steps = 1
        runtime._critical_turn_and_burn_deflect_steps = 1
        runtime._metrics = lambda _state: {  # type: ignore[method-assign]
            "episode": 1,
            "map": 1,
            "x": int(2995 * FP_UNIT),
            "y": int(-3952 * FP_UNIT),
            "health": 20,
            "visible_enemy": True,
            "shootable": True,
        }
        runtime._planner_override = lambda *_args: (  # type: ignore[method-assign]
            0,
            "open_use_line",
            FakeAgentPb2.PlayerAction(action=FakeAgentPb2.ACTION_FORWARD, amount=48, duration_tics=8),
            {"source": "spatial_planner", "skill": "pressure_reopen_upcoming_use_line", "line": 340},
        )
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["open_use_line"]}

        result = runtime._final_exit_commit_override(SimpleNamespace(), directive, FakeController(), modules, stub=None)

        self.assertIsNotNone(result)
        _index, skill, _action, decision = result
        self.assertEqual(skill, "open_use_line")
        self.assertEqual(decision["final_exit_commit"], 1)
        self.assertEqual(runtime._critical_turn_and_burn_steps, 0)
        self.assertEqual(runtime._critical_turn_and_burn_handoff_steps, 0)
        self.assertEqual(runtime._critical_turn_and_burn_deflect_steps, 0)
    def test_final_exit_commit_window_includes_west_exit_room_edge(self):
        runtime = BrainRuntime()
        metrics = {
            "episode": 1,
            "map": 1,
            "x": int(2719 * FP_UNIT),
            "y": int(-4457 * FP_UNIT),
        }

        self.assertTrue(runtime._e1m1_near_final_exit_commit(metrics))
    def test_final_exit_commit_uses_direct_exit_route_before_health_fallbacks(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_USE=8)

        FakeController = make_controller(1)

        class FakePlanner:
            def _player(self, _state):
                return {"point": Point(0, 0), "angle": 0}

            def _live_exit_line_action(self, _state, _player, agent_pb2, _door_memory):
                return PlanAction(
                    skill="press_exit",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_USE, amount=1, duration_tics=4),
                    detail={"skill": "live_exit_line", "line": 341},
                    door_line_id=341,
                )

            def _line_objective_action(self, *_args, **_kwargs):
                raise AssertionError("direct live exit should win")

            def _remembered_progression_line_action(self, *_args, **_kwargs):
                raise AssertionError("direct live exit should win")

        runtime = BrainRuntime()
        runtime._refresh_planner = lambda *_args: FakePlanner()  # type: ignore[method-assign]
        runtime._metrics = lambda _state: {  # type: ignore[method-assign]
            "episode": 1,
            "map": 1,
            "x": int(3287 * FP_UNIT),
            "y": int(-4383 * FP_UNIT),
            "health": 15,
            "visible_enemy": True,
            "shootable": True,
        }
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["press_exit"]}

        result = runtime._final_exit_commit_override(SimpleNamespace(), directive, FakeController(), modules, stub=None)

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "press_exit")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(decision["final_exit_direct"], 1)
        self.assertEqual(decision["final_exit_commit"], 1)
        self.assertEqual(decision["line_id"], 341)
    def test_final_exit_box_suppresses_active_critical_turn_and_burn(self):
        runtime = BrainRuntime()
        runtime._critical_turn_and_burn_steps = 2
        runtime._critical_turn_and_burn_handoff_steps = 1
        runtime._critical_turn_and_burn_deflect_steps = 1
        runtime._metrics = lambda _state: {  # type: ignore[method-assign]
            "episode": 1,
            "map": 1,
            "x": int(3048 * FP_UNIT),
            "y": int(-4066 * FP_UNIT),
            "health": 20,
            "visible_enemy": True,
            "shootable": False,
        }
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})

        result = runtime._critical_turn_and_burn_commit_action(
            SimpleNamespace(),
            directive,
            SimpleNamespace(),
            {},
            enemy={"id": 7, "threat": "hitscan", "distance": 96.0},
        )

        self.assertIsNone(result)
        self.assertEqual(runtime._critical_turn_and_burn_steps, 0)
        self.assertEqual(runtime._critical_turn_and_burn_handoff_steps, 0)
        self.assertEqual(runtime._critical_turn_and_burn_deflect_steps, 0)
    def test_route_threshold_refusal_microstrafes_when_visible_but_not_shootable(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            navigation=SimpleNamespace(
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=256 * 65536),
                ]
            ),
            player=SimpleNamespace(
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=384 * 65536, y_fp=0)
            ],
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "route_step_kind": "portal",
            "route_step_line": 265,
            "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
            "turn": 0.0,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, refusal_decision = result
        self.assertEqual(skill, "route_progression")
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
        self.assertNotEqual(action.raw.side_move, 0)
        self.assertEqual(refusal_decision["skill"], "threshold_route_clean_shot")
        self.assertEqual(refusal_decision["reason"], "clean_shot_blocked")
    def test_route_threshold_refusal_sustains_until_low_watermark_releases(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=0),
            player=SimpleNamespace(
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
            )
        )
        first_decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "route_step_kind": "portal",
            "route_step_line": 265,
            "route_step_sector": 4,
            "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
        }

        first = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            first_decision,
        )
        self.assertIsNotNone(first)

        sustain_decision = {
            **first_decision,
            "route_step_threat_mult": ROUTE_THREAT_RELEASE_MULT - 1,
        }
        sustained = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            sustain_decision,
        )

        self.assertIsNotNone(sustained)
        _index, skill, action, refusal_decision = sustained
        self.assertEqual(skill, "fire")
        self.assertEqual(action.raw.buttons, 1)
        self.assertEqual(refusal_decision["sustained"], 1)
        self.assertLess(refusal_decision["route_step_threat_mult"], ROUTE_THREAT_REFUSE_MULT)
        self.assertGreater(refusal_decision["hold_tics"], 0)

        runtime._route_refusal_hold_tics = 0
        state.combat = SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0)
        released = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            sustain_decision,
        )

        self.assertIsNone(released)
        self.assertIsNone(runtime._route_refusal_hold_key)
    def test_route_threshold_refusal_sustains_on_shootable_threat_after_lockout(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "fire"]}
        runtime._route_refusal_hold_tics = 0
        runtime._route_refusal_hold_key = ("portal", 996, 7)
        runtime._route_refusal_hold_peak = ROUTE_THREAT_REFUSE_MULT
        state = SimpleNamespace(
            player=SimpleNamespace(
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0),
            ),
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True),
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "route_step_kind": "portal",
            "route_step_line": 996,
            "route_step_sector": 7,
            "route_step_threat_mult": 1.0,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, refusal_decision = result
        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(refusal_decision["sustained"], 1)
        self.assertEqual(refusal_decision["shootable"], 1)
    def test_route_threshold_refusal_sustains_on_live_melee_rush_after_lockout(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_SHOOT=7)

        FakeController = make_controller(4)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "fire", "close_visible_contact"]}
        runtime._route_refusal_hold_tics = 0
        runtime._route_refusal_hold_key = ("portal", 996, 7)
        runtime._route_refusal_hold_peak = ROUTE_THREAT_REFUSE_MULT
        runtime._route_refusal_melee_target_id = 88
        state = SimpleNamespace(
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
            player=SimpleNamespace(
                health=100,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            enemies=[
                make_enemy(id=88, x_fp=128 * 65536, y_fp=0, type_id=3002, health=150, line_of_sight=False)
            ],
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "route_step_kind": "portal",
            "route_step_line": 996,
            "route_step_sector": 7,
            "route_step_threat_mult": 1.0,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, refusal_decision = result
        self.assertEqual(skill, "retreat")
        self.assertLess(action.raw.forward_move, 0)
        self.assertEqual(refusal_decision["skill"], "melee_rush_kite_punch")
        self.assertEqual(refusal_decision["state"], "melee_rush_hold")
        self.assertEqual(refusal_decision["reason"], "route_threshold_melee_rush_hold")
        self.assertEqual(refusal_decision["enemy"], 88)
        self.assertEqual(refusal_decision["enemy_threat"], "melee_rush")
        self.assertEqual(refusal_decision["enemy_health"], 150)
        self.assertEqual(refusal_decision["sustained"], 1)
        self.assertIsNotNone(runtime._route_refusal_hold_key)
    def test_route_threshold_refusal_releases_when_tracked_melee_rush_is_dead(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "fire"]}
        runtime._route_refusal_hold_tics = 0
        runtime._route_refusal_hold_key = ("portal", 996, 7)
        runtime._route_refusal_hold_peak = ROUTE_THREAT_REFUSE_MULT
        runtime._route_refusal_melee_target_id = 88
        state = SimpleNamespace(
            player=SimpleNamespace(
                health=100,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            enemies=[
                make_enemy(id=88, x_fp=96 * 65536, y_fp=0, type_id=3002, health=0, line_of_sight=False)
            ],
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "route_step_kind": "portal",
            "route_step_line": 996,
            "route_step_sector": 7,
            "route_step_threat_mult": ROUTE_THREAT_RELEASE_MULT - 1,
        }

        released = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNone(released)
        self.assertIsNone(runtime._route_refusal_hold_key)
        self.assertEqual(runtime._route_refusal_melee_target_id, 0)
    def test_route_threshold_refusal_retreats_when_weapon_is_dry(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "fire", "retreat"],
            "summarize_action": lambda _action: {},
        }
        state = SimpleNamespace(
            navigation=SimpleNamespace(back_open=True),
            player=SimpleNamespace(
                ready_weapon=0,
                ammo=SimpleNamespace(bullets=0, shells=0, rockets=0, cells=0),
            ),
        )
        runtime._route_refusal_hold_key = ("portal", 313, 85)
        runtime._route_refusal_hold_tics = ROUTE_THREAT_HOLD_MIN_TICS
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "route_step_kind": "portal",
            "route_step_line": 313,
            "route_step_sector": 85,
            "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, refusal_decision = result
        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(refusal_decision["reason"], "route_threshold_no_ammo")
        self.assertIsNone(runtime._route_refusal_hold_key)
    def test_no_kill_route_threshold_refusal_does_not_fire(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level", "constraints": ["no_kills"]})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "fire"]}
        state = SimpleNamespace(player=SimpleNamespace(ready_weapon=1))
        runtime._route_refusal_hold_key = ("portal", 265, 2)
        runtime._route_refusal_hold_tics = ROUTE_THREAT_HOLD_MIN_TICS
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
        }

        result = runtime._route_threshold_refusal_fire(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNone(result)
        self.assertIsNone(runtime._route_refusal_hold_key)
    def test_ammo_forbidden_route_threshold_refusal_does_not_fire(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "fire"]}
        state = SimpleNamespace(player=SimpleNamespace(ready_weapon=1))
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "route_step_threat_mult": ROUTE_THREAT_REFUSE_MULT,
        }

        for directive in (
            parse_directive({"goal": "beat the level", "objective": "complete_level", "constraints": ["no_ammo"]}),
            parse_directive({"goal": "beat the level", "objective": "complete_level", "constraints": ["fist_only"]}),
        ):
            result = runtime._route_threshold_refusal_fire(
                state,
                directive,
                FakeController(),
                modules,
                "route_progression",
                decision,
            )
            self.assertIsNone(result)
    def test_no_kill_panic_loop_triggers_desperation_sprint_without_fire(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level without killing anything", "objective": "complete_level", "constraints": ["no_kills"]})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "recover_stuck"]}
        state = SimpleNamespace(
            tick=100,
            level=SimpleNamespace(episode=1, map=2, total_kills=0),
            player=SimpleNamespace(
                health=34,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=1024 * 65536, y_fp=512 * 65536), angle_degrees=0),
            ),
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True),
            enemies=[],
            navigation=SimpleNamespace(direction_probes=[]),
        )
        decision = {
            "source": "spatial_planner",
            "skill": "no_kill_route_evasion",
            "action": "panic_sidestep_close_blocker",
            "enemy": 12,
            "dist": 64,
            "turn": 8.0,
            "side": "left",
        }

        for _ in range(NO_KILL_DESPERATION_PANIC_REPEATS - 1):
            result = runtime._no_kill_desperation_sprint(
                state,
                directive,
                FakeController(),
                modules,
                "route_progression",
                decision,
            )
            self.assertIsNone(result)

        result = runtime._no_kill_desperation_sprint(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, sprint_decision = result
        self.assertEqual(skill, "route_progression")
        self.assertEqual(action.duration_tics, NO_KILL_DESPERATION_SPRINT_BURST_TICS)
        self.assertEqual(action.raw.forward_move, 42)
        self.assertEqual(action.raw.side_move, -52)
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
        self.assertEqual(sprint_decision["skill"], "no_kill_desperation_sprint")
        self.assertEqual(sprint_decision["reason"], "panic_loop_breakout")
        self.assertEqual(sprint_decision["refused_action"], "panic_sidestep_close_blocker")
        self.assertEqual(sprint_decision["panic_repeat"], NO_KILL_DESPERATION_PANIC_REPEATS)
        self.assertEqual(runtime._no_kill_desperation_sprint_tics, NO_KILL_DESPERATION_SPRINT_TICS - NO_KILL_DESPERATION_SPRINT_BURST_TICS)

        committed = runtime._no_kill_desperation_sprint(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )
        self.assertIsNotNone(committed)
        _index, _skill, _action, committed_decision = committed
        self.assertEqual(committed_decision["reason"], "committed_sprint")
        self.assertEqual(runtime._no_kill_desperation_sprint_tics, NO_KILL_DESPERATION_SPRINT_TICS - (NO_KILL_DESPERATION_SPRINT_BURST_TICS * 2))
    def test_no_kill_desperation_sprint_commits_after_panic_trigger(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_SHOOT=7)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._no_kill_desperation_sprint_tics = 12
        directive = parse_directive({"goal": "beat the level without killing anything", "objective": "complete_level", "constraints": ["no_kills"]})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression"]}
        state = SimpleNamespace(
            tick=112,
            level=SimpleNamespace(episode=1, map=2, total_kills=0),
            player=SimpleNamespace(
                health=33,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=1024 * 65536, y_fp=512 * 65536), angle_degrees=0),
            ),
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            enemies=[],
            navigation=SimpleNamespace(direction_probes=[]),
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_use_line",
            "action": "steer_forward",
            "turn": -4.0,
            "side": "right",
        }

        result = runtime._no_kill_desperation_sprint(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, sprint_decision = result
        self.assertEqual(skill, "route_progression")
        self.assertEqual(action.raw.forward_move, 42)
        self.assertEqual(action.raw.side_move, 52)
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
        self.assertEqual(sprint_decision["reason"], "committed_sprint")
        self.assertEqual(sprint_decision["refused_skill"], "sector_route_to_use_line")
    def test_armed_panic_decision_does_not_trigger_no_kill_desperation(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1, ACTION_SHOOT=7)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._no_kill_desperation_sprint_tics = 8
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression"]}
        state = SimpleNamespace(
            tick=100,
            level=SimpleNamespace(episode=1, map=2, total_kills=0),
            player=SimpleNamespace(
                health=34,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True),
            enemies=[],
        )
        decision = {
            "source": "spatial_planner",
            "skill": "no_kill_route_evasion",
            "action": "panic_escape_side",
            "enemy": 12,
        }

        result = runtime._no_kill_desperation_sprint(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNone(result)
        self.assertEqual(runtime._no_kill_desperation_sprint_tics, 0)
    def test_no_kill_high_threat_route_refuses_forward_without_fire(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=1, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level without killing anything", "objective": "complete_level", "constraints": ["no_kills"]})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "recover_stuck"]}
        state = SimpleNamespace(
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
            level=SimpleNamespace(episode=1, map=2, total_kills=0),
            player=SimpleNamespace(
                health=67,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            enemies=[],
        )
        decision = {
            "source": "spatial_planner",
            "skill": "sector_route_to_key",
            "action": "steer_forward",
            "route_step_kind": "portal",
            "route_step_line": 341,
            "route_step_sector": 25,
            "route_step_threat_mult": NO_KILL_ROUTE_REFUSAL_MULT,
        }

        result = runtime._no_kill_route_threat_refusal(
            state,
            directive,
            FakeController(),
            modules,
            "route_progression",
            decision,
        )

        self.assertIsNotNone(result)
        _index, skill, action, refusal_decision = result
        self.assertEqual(skill, "route_progression")
        self.assertGreater(action.raw.forward_move, 0)
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
        self.assertEqual(refusal_decision["skill"], "no_kill_desperation_sprint")
        self.assertEqual(refusal_decision["reason"], "preemptive_high_threat_route")
        self.assertEqual(refusal_decision["trigger_source"], "no_kill_route_refusal")
        self.assertEqual(refusal_decision["force_forward"], 1)
        self.assertEqual(refusal_decision["threshold"], NO_KILL_ROUTE_REFUSAL_MULT)
    def test_retreat_commit_overrides_high_health_route_resume(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._cautious_retreat_commit_steps = 3
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=9, x_fp=256 * 65536, y_fp=0, type_id=9, distance_fp=256 * 65536)
            ],
            navigation=SimpleNamespace(
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=256 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["reason"], "retreat_commit")
        self.assertEqual(decision["skill"], "funnel_back_raw")
        self.assertLess(action.raw.forward_move, 0)
        self.assertGreater(action.raw.side_move, 0)
        self.assertEqual(runtime._cautious_retreat_commit_steps, 2)
    def test_final_exit_commit_overrides_active_critical_escape_suppression(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        metrics = {
            "episode": 1,
            "map": 1,
            "x": int(3178 * FP_UNIT),
            "y": int(-4528 * FP_UNIT),
            "health": ROUTE_CRITICAL_HEALTH_BREAKAWAY,
            "visible_enemy": True,
            "shootable": True,
            "nearest_enemy_dist": 96,
        }

        runtime._critical_turn_and_burn_steps = 1
        self.assertTrue(runtime._wounded_complete_level_route_priority(directive, metrics))

        runtime._critical_turn_and_burn_steps = 0
        runtime._critical_turn_and_burn_handoff_steps = 1
        self.assertTrue(runtime._wounded_complete_level_route_priority(directive, metrics))

        runtime._critical_turn_and_burn_handoff_steps = 0
        runtime._critical_turn_and_burn_deflect_steps = 1
        self.assertTrue(runtime._wounded_complete_level_route_priority(directive, metrics))


if __name__ == "__main__":
    unittest.main()
