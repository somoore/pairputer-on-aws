"""Cautious escalation: break-LOS loops, critical turn-and-burn, and funnel-escape escalation for Agent DOOM."""

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
    CAUTIOUS_RETREAT_COMMIT_STEPS,
    HEALTHY_BREAK_LOS_PUSH_HEALTH,
    HEALTHY_BREAK_LOS_PUSH_REPEATS,
    ROUTE_CRITICAL_HEALTH_BREAKAWAY,
    ROUTE_CRITICAL_TURN_AND_BURN_COMMIT_STEPS,
    ROUTE_CRITICAL_TURN_AND_BURN_DEFLECT_DEGREES,
    ROUTE_CRITICAL_TURN_AND_BURN_MAX_CHAIN,
    ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE,
    ROUTE_CRITICAL_TURN_AND_BURN_STALL_THRESHOLD,
    parse_directive,
)


class TestAgentDoomCautiousEscalation(unittest.TestCase):
    def test_repeated_cautious_cover_break_escalates_to_escape_turn(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                back_open=False,
                direction_probes=[
                    SimpleNamespace(open=False, angle_offset_degrees=90, block_distance_fp=96 * 65536),
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            )
        )
        action = None
        decision = None

        for _ in range(4):
            _index, _skill, action, decision = runtime._cautious_cover_action(
                state,
                directive,
                FakeController(),
                modules,
                reason="survival_pressure_break_los_without_shot",
                enemy={"id": 9, "threat": "hitscan", "visible": True},
            )

        self.assertEqual(decision["skill"], "break_los_escape_turn")
        self.assertEqual(runtime._cautious_cover_repeat_count, 4)
        self.assertGreater(action.raw.forward_move, 0)
        self.assertNotEqual(action.raw.side_move, 0)
        self.assertNotEqual(action.raw.angle_turn, 0)
    def test_repeated_break_los_loop_returns_fire_when_shootable(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=9),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=False,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=False, angle_offset_degrees=90, block_distance_fp=96 * 65536),
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            ),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY + 5,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=9, x_fp=96 * 65536, y_fp=0)
            ],
        )
        action = None
        decision = None
        skill = None

        for _ in range(4):
            _index, skill, action, decision = runtime._cautious_cover_action(
                state,
                directive,
                FakeController(),
                modules,
                reason="survival_pressure_break_los_without_shot",
                enemy={"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96},
            )

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "break_los_return_fire")
        self.assertEqual(decision["reason"], "break_los_loop_return_fire")
        self.assertEqual(decision["repeat"], 4)
        self.assertTrue(action.raw.buttons & 1)
        self.assertNotEqual(action.raw.side_move, 0)
    def test_repeated_funnel_back_loop_returns_fire_when_shootable(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=9),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=True,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=256 * 65536),
                ],
            ),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY + 5,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=9, x_fp=96 * 65536, y_fp=0)
            ],
        )
        action = None
        decision = None
        skill = None

        for _ in range(4):
            _index, skill, action, decision = runtime._cautious_cover_action(
                state,
                directive,
                FakeController(),
                modules,
                reason="health_route_threat_refusal",
                enemy={"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96},
            )

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "break_los_return_fire")
        self.assertEqual(decision["reason"], "break_los_loop_return_fire")
        self.assertEqual(decision["repeat"], 4)
        self.assertTrue(action.raw.buttons & 1)
    def test_repeated_break_los_loop_respects_no_kill_fire_forbidden(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "race to the exit without killing anyone", "objective": "complete_level", "constraints": ["no_kills"]}
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=9),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=False,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            ),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY + 5,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=9, x_fp=96 * 65536, y_fp=0)
            ],
        )
        action = None
        decision = None
        skill = None

        for _ in range(4):
            _index, skill, action, decision = runtime._cautious_cover_action(
                state,
                directive,
                FakeController(),
                modules,
                reason="survival_pressure_break_los_without_shot",
                enemy={"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96},
            )

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["skill"], "break_los_escape_turn")
        self.assertFalse(getattr(action.raw, "buttons", 0) & 1)
    def test_critical_break_los_side_step_turns_and_burns(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=3, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=False,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            ),
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

        _index, skill, action, decision = runtime._cautious_cover_action(
            state,
            directive,
            FakeController(),
            modules,
            reason="survival_pressure_break_los_without_shot",
            enemy={"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96},
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["skill"], "critical_turn_and_burn_raw")
        self.assertEqual(decision["reason"], "critical_lateral_evasion_breakaway")
        self.assertEqual(decision["replaced_lateral_skill"], "break_los_right")
        self.assertGreater(action.raw.forward_move, 0)
        self.assertNotEqual(action.raw.angle_turn, 0)
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
    def test_critical_lateral_turn_and_burn_releases_when_threat_is_distant(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=3, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=False,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            ),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                SimpleNamespace(
                    line_of_sight=True,
                    object=SimpleNamespace(
                        id=9,
                        type_id=3004,
                        health=20,
                        position=SimpleNamespace(
                            x_fp=int(ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE + 96) * 65536,
                            y_fp=0,
                        ),
                    ),
                )
            ],
        )

        result = runtime._cautious_critical_lateral_turn_and_burn(
            state,
            directive,
            FakeController(),
            modules,
            enemy={
                "id": 9,
                "threat": "hitscan",
                "visible": True,
                "turn": 0.0,
                "distance": ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE + 96,
            },
            kind="break_los_right",
            reason="survival_pressure_break_los_without_shot",
        )

        self.assertIsNone(result)
    def test_critical_funnel_escape_side_turns_and_burns(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=3, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=True,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                ],
            ),
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
        action = None
        decision = None
        skill = None

        for _ in range(4):
            _index, skill, action, decision = runtime._cautious_cover_action(
                state,
                directive,
                FakeController(),
                modules,
                reason="kite_and_funnel_retreat",
                enemy={"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96},
            )

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["skill"], "critical_turn_and_burn_raw")
        self.assertEqual(decision["reason"], "critical_lateral_evasion_breakaway")
        self.assertEqual(decision["replaced_lateral_skill"], "funnel_escape_side")
        self.assertGreater(action.raw.forward_move, 0)
        self.assertNotEqual(action.raw.angle_turn, 0)
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
    def test_critical_turn_and_burn_commits_before_funnel_back(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=3, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=True,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                ],
            ),
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
        enemy = {"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96}

        for _ in range(4):
            runtime._cautious_cover_action(
                state,
                directive,
                FakeController(),
                modules,
                reason="kite_and_funnel_retreat",
                enemy=enemy,
            )

        self.assertEqual(runtime._critical_turn_and_burn_steps, ROUTE_CRITICAL_TURN_AND_BURN_COMMIT_STEPS)
        _index, skill, action, decision = runtime._cautious_cover_action(
            state,
            directive,
            FakeController(),
            modules,
            reason="kite_and_funnel_retreat",
            enemy=enemy,
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["skill"], "critical_turn_and_burn_raw")
        self.assertEqual(decision["reason"], "critical_turn_and_burn_commit")
        self.assertEqual(decision["commit_steps_remaining"], ROUTE_CRITICAL_TURN_AND_BURN_COMMIT_STEPS - 1)
        self.assertGreater(action.raw.forward_move, 0)
        self.assertNotEqual(action.raw.angle_turn, 0)
        self.assertNotEqual(decision["skill"], "funnel_back_raw")
    def test_critical_turn_and_burn_recommits_before_funnel_back_when_contact_remains(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=3, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._critical_turn_and_burn_steps = 0
        runtime._critical_turn_and_burn_handoff_steps = 1
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=True,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                ],
            ),
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

        _index, skill, action, decision = runtime._cautious_cover_action(
            state,
            directive,
            FakeController(),
            modules,
            reason="kite_and_funnel_retreat",
            enemy={"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96},
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["skill"], "critical_turn_and_burn_raw")
        self.assertEqual(decision["reason"], "critical_turn_and_burn_recommit")
        self.assertEqual(decision["previous_reason"], "kite_and_funnel_retreat")
        self.assertEqual(decision["health"], ROUTE_CRITICAL_HEALTH_BREAKAWAY)
        self.assertGreater(action.raw.forward_move, 0)
        self.assertNotEqual(action.raw.angle_turn, 0)
        self.assertNotEqual(decision["skill"], "funnel_back_raw")
        self.assertEqual(runtime._critical_turn_and_burn_steps, ROUTE_CRITICAL_TURN_AND_BURN_COMMIT_STEPS)
    def test_critical_turn_and_burn_recommit_releases_when_contact_is_distant(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=3, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._critical_turn_and_burn_handoff_steps = 1
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        far_units = int(ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE + 96)
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=9),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(back_open=True, use_lines=[], direction_probes=[]),
            player=SimpleNamespace(
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=9, x_fp=far_units * 65536, y_fp=0)
            ],
        )

        result = runtime._critical_turn_and_burn_recommit_action(
            state,
            directive,
            FakeController(),
            modules,
            enemy={
                "id": 9,
                "threat": "hitscan",
                "visible": True,
                "turn": 0.0,
                "distance": float(far_units),
            },
            reason="kite_and_funnel_retreat",
        )

        self.assertIsNone(result)
        self.assertEqual(runtime._critical_turn_and_burn_handoff_steps, 0)
    def test_critical_turn_and_burn_chain_cap_blocks_fresh_arming(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=3, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._critical_turn_and_burn_chain_count = ROUTE_CRITICAL_TURN_AND_BURN_MAX_CHAIN
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(navigation=SimpleNamespace(back_open=True, direction_probes=[]))

        result = runtime._route_threshold_turn_and_burn_action(
            state,
            directive,
            FakeController(),
            modules,
            reason="critical_lateral_evasion_breakaway",
            enemy={"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96},
        )

        self.assertIsNone(result)
    def test_critical_turn_and_burn_chain_cap_allows_existing_commit(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=3, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._critical_turn_and_burn_chain_count = ROUTE_CRITICAL_TURN_AND_BURN_MAX_CHAIN
        runtime._critical_turn_and_burn_steps = 1
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
            player=SimpleNamespace(health=ROUTE_CRITICAL_HEALTH_BREAKAWAY),
        )

        result = runtime._critical_turn_and_burn_commit_action(
            state,
            directive,
            FakeController(),
            modules,
            enemy={"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96},
        )

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["skill"], "critical_turn_and_burn_raw")
        self.assertGreater(action.raw.forward_move, 0)
    def test_stalled_critical_turn_and_burn_deflects_next_sprint(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=3, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        action = FakeAgentPb2.PlayerAction(
            duration_tics=10,
            raw=FakeAgentPb2.RawTiccmd(forward_move=64, side_move=0, angle_turn=1536),
        )
        decision = {
            "source": "cautious_combat",
            "skill": "critical_turn_and_burn_raw",
            "state": "turn_and_burn",
            "turn_sign": 1,
        }
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=True,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=192 * 65536),
                ],
            ),
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

        for _ in range(ROUTE_CRITICAL_TURN_AND_BURN_STALL_THRESHOLD):
            runtime._record_critical_turn_and_burn_outcome(state, action, decision, 0.0, modules)

        self.assertEqual(runtime._critical_turn_and_burn_deflect_steps, 2)
        self.assertEqual(runtime._critical_turn_and_burn_deflect_sign, 1)
        runtime._critical_turn_and_burn_steps = 1

        _index, skill, deflected_action, deflected_decision = runtime._cautious_cover_action(
            state,
            directive,
            FakeController(),
            modules,
            reason="kite_and_funnel_retreat",
            enemy={"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96},
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(deflected_decision["skill"], "critical_turn_and_burn_raw")
        self.assertEqual(deflected_decision["deflect"], 1)
        self.assertEqual(deflected_decision["deflect_sign"], 1)
        self.assertEqual(deflected_action.raw.forward_move, 64)
        self.assertEqual(deflected_action.raw.side_move, -48)
        self.assertEqual(
            deflected_action.raw.angle_turn,
            runtime._raw_steer_turn_units(ROUTE_CRITICAL_TURN_AND_BURN_DEFLECT_DEGREES),
        )
        self.assertEqual(getattr(deflected_action.raw, "buttons", 0), 0)
    def test_healthy_break_los_loop_pushes_forward_for_complete_level(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "recover_stuck"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=False,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                ],
            ),
            player=SimpleNamespace(
                health=HEALTHY_BREAK_LOS_PUSH_HEALTH + 15,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=9, x_fp=96 * 65536, y_fp=0)
            ],
        )
        enemy = {"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96}
        result = None

        for _ in range(HEALTHY_BREAK_LOS_PUSH_REPEATS):
            result = runtime._cautious_cover_action(
                state,
                directive,
                FakeController(),
                modules,
                reason="kite_and_funnel_retreat",
                enemy=enemy,
            )

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "route_progression")
        self.assertEqual(decision["skill"], "healthy_break_los_push")
        self.assertEqual(decision["reason"], "break_los_loop_healthy_push")
        self.assertEqual(decision["repeat"], HEALTHY_BREAK_LOS_PUSH_REPEATS)
        self.assertEqual(decision["health"], HEALTHY_BREAK_LOS_PUSH_HEALTH + 15)
        self.assertGreater(action.raw.forward_move, 0)
        self.assertEqual(runtime._cautious_cover_repeat_count, 0)
    def test_healthy_break_los_loop_does_not_push_for_avoid_damage(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive(
            {
                "goal": "beat the level safely",
                "objective": "complete_level",
                "constraints": ["avoid_damage"],
            }
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "recover_stuck"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            level=SimpleNamespace(episode=1, map=1, total_kills=0),
            navigation=SimpleNamespace(
                back_open=False,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                ],
            ),
            player=SimpleNamespace(
                health=HEALTHY_BREAK_LOS_PUSH_HEALTH + 15,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=20, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=9, x_fp=96 * 65536, y_fp=0)
            ],
        )
        enemy = {"id": 9, "threat": "hitscan", "visible": True, "turn": 0.0, "distance": 96}
        result = None

        for _ in range(HEALTHY_BREAK_LOS_PUSH_REPEATS):
            result = runtime._cautious_cover_action(
                state,
                directive,
                FakeController(),
                modules,
                reason="kite_and_funnel_retreat",
                enemy=enemy,
            )

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "retreat")
        self.assertNotEqual(decision["skill"], "healthy_break_los_push")
        self.assertIn(decision["skill"], {"break_los_escape_turn", "break_los_reverse_left", "break_los_reverse_right"})
        self.assertGreater(getattr(action.raw, "forward_move", 0), 0)
    def test_repeated_funnel_back_escalates_to_side_escape(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_FORWARD=3, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                back_open=True,
                forward_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                ],
            )
        )
        action = None
        decision = None

        for _ in range(4):
            _index, _skill, action, decision = runtime._cautious_cover_action(
                state,
                directive,
                FakeController(),
                modules,
                reason="kite_and_funnel_retreat",
                enemy={"id": 9, "threat": "hitscan", "visible": True},
            )

        self.assertEqual(decision["skill"], "funnel_escape_side")
        self.assertEqual(runtime._cautious_cover_repeat_count, 4)
        self.assertGreater(action.raw.forward_move, 0)
        self.assertNotEqual(action.raw.side_move, 0)
        self.assertNotEqual(action.raw.angle_turn, 0)
    def test_alternating_break_los_sides_still_count_as_repeated_cover(self):
        runtime = BrainRuntime()
        enemy = {"id": 9, "threat": "hitscan", "visible": True}

        self.assertEqual(
            runtime._record_cautious_cover_repeat("break_los_left", "survival_pressure_break_los_without_shot", enemy),
            1,
        )
        self.assertEqual(
            runtime._record_cautious_cover_repeat("break_los_right", "survival_pressure_break_los_without_shot", enemy),
            2,
        )
        self.assertEqual(
            runtime._record_cautious_cover_repeat("break_los_left", "survival_pressure_break_los_without_shot", enemy),
            3,
        )
        self.assertEqual(runtime._record_cautious_cover_repeat("funnel_back", "kite_and_funnel_retreat", enemy), 1)
        self.assertEqual(runtime._record_cautious_cover_repeat("funnel_back_raw", "kite_and_funnel_retreat", enemy), 2)
    def test_complete_level_funnel_back_uses_raw_reverse_steer_when_available(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=256 * 65536),
                ],
            )
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}

        _index, skill, action, decision = runtime._cautious_cover_action(
            state,
            directive,
            FakeController(),
            modules,
            reason="kite_and_funnel_retreat",
            enemy={"id": 9, "threat": "hitscan", "visible": True, "turn": 36.0},
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["skill"], "funnel_back_raw")
        self.assertLess(action.raw.forward_move, 0)
        self.assertLess(action.raw.side_move, 0)
        self.assertGreater(action.raw.angle_turn, 0)
        self.assertEqual(runtime._cautious_retreat_commit_steps, CAUTIOUS_RETREAT_COMMIT_STEPS)


if __name__ == "__main__":
    unittest.main()
