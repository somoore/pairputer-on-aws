"""Survival retreat, point-blank retaliation, exposed-idle, wounded-route, hazard-floor, and defensive-override guards for Agent DOOM."""

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
    CAUTIOUS_COVER_AMBUSH_WINDOW,
    CAUTIOUS_RETREAT_COMMIT_STEPS,
    FP_UNIT,
    ROUTE_CRITICAL_HEALTH_BREAKAWAY,
    ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE,
    parse_directive,
)
from planner import PlanAction  # noqa: E402


class TestAgentDoomRetreatGuards(unittest.TestCase):
    def test_survival_retreat_window_evades_when_still_shootable_with_escape(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_retreat_steps = 3
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {"health": 35, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=9, x_fp=96 * 65536, y_fp=0, type_id=9, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(back_open=True),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(decision["reason"], "kite_and_funnel_retreat")
        self.assertEqual(decision["skill"], "funnel_back")
        self.assertEqual(runtime._cautious_retreat_commit_steps, CAUTIOUS_RETREAT_COMMIT_STEPS)
    def test_retreat_commit_continues_without_current_enemy_snapshot(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 9
        runtime._cautious_retreat_commit_steps = 3
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[],
            navigation=SimpleNamespace(
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=256 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["reason"], "retreat_commit")
        self.assertEqual(decision["enemy"], 9)
        self.assertEqual(decision["skill"], "funnel_back_raw")
        self.assertLess(action.raw.forward_move, 0)
        self.assertLess(action.raw.side_move, 0)
        self.assertEqual(runtime._cautious_retreat_commit_steps, 2)
    def test_survival_retreat_window_allows_point_blank_followup_without_escape(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_retreat_steps = 3
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {"health": 35, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=9, x_fp=96 * 65536, y_fp=0, type_id=9, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(back_open=False, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["reason"], "survival_pressure_followup_shot")
    def test_urgent_point_blank_retaliation_preempts_wounded_route_priority(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {"health": 35, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
                ammo=SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0),
                ready_weapon=1,
                health=35,
            ),
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=20),
            enemies=[
                make_enemy(id=20, x_fp=96 * 65536, y_fp=0)
            ],
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "route_progression"]}

        _index, skill, action, decision = runtime._urgent_hitscan_retaliation_override(
            state,
            directive,
            FakeController(),
            modules,
        )

        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["source"], "urgent_retaliation")
        self.assertEqual(decision["skill"], "point_blank_hitscan_retaliation")
    def test_active_critical_escape_suppresses_point_blank_retaliation(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._critical_turn_and_burn_handoff_steps = 1
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {
            "health": ROUTE_CRITICAL_HEALTH_BREAKAWAY,
            "visible_enemy": True,
            "shootable": True,
            "nearest_enemy_dist": 96,
        }  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
                ammo=SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0),
                ready_weapon=1,
                health=ROUTE_CRITICAL_HEALTH_BREAKAWAY,
            ),
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=20),
            enemies=[
                make_enemy(id=20, x_fp=96 * 65536, y_fp=0)
            ],
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "route_progression"]}

        result = runtime._urgent_hitscan_retaliation_override(
            state,
            directive,
            FakeController(),
            modules,
        )

        self.assertIsNone(result)
    def test_urgent_point_blank_retaliation_moves_after_recent_hit_when_not_shootable(self):
        class FakeRaw:
            def __init__(self, forward_move=0, side_move=0, angle_turn=0, buttons=0):
                self.forward_move = forward_move
                self.side_move = side_move
                self.angle_turn = angle_turn
                self.buttons = buttons

        FakeAgentPb2 = make_agent_pb2(raw=FakeRaw, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._cautious_recent_hit_window = 4
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {"health": 35, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
                ammo=SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0),
                ready_weapon=1,
                health=35,
            ),
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
            enemies=[
                make_enemy(id=20, x_fp=96 * 65536, y_fp=0)
            ],
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}

        _index, skill, action, decision = runtime._urgent_hitscan_retaliation_override(
            state,
            directive,
            FakeController(),
            modules,
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["source"], "urgent_retaliation")
        self.assertEqual(decision["reason"], "recent_hit_point_blank_break_los")
        self.assertLess(action.raw.forward_move, 0)
    def test_urgent_point_blank_retaliation_respects_no_kill_contract(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "race to the exit without killing anyone", "objective": "complete_level", "constraints": ["no_kills"]}
        )
        runtime._metrics = lambda _state: {"health": 35, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
                ammo=SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0),
                ready_weapon=1,
                health=35,
            ),
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=20),
            enemies=[
                make_enemy(id=20, x_fp=96 * 65536, y_fp=0)
            ],
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire"]}

        self.assertIsNone(runtime._urgent_hitscan_retaliation_override(state, directive, FakeController(), modules))
    def test_exposed_idle_under_shootable_enemy_returns_fire(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7, ACTION_USE=8, ACTION_SWITCH_WEAPON=9)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {  # type: ignore[method-assign]
            "health": 80,
            "visible_enemy": True,
            "shootable": True,
            "ammo_total": 12,
            "weapon": 1,
        }
        runtime._nearest_enemy = lambda _state, prefer_visible=False: {  # type: ignore[method-assign]
            "id": 9,
            "visible": True,
            "distance": 96.0,
        }
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["fire", "retreat"],
            "summarize_action": lambda _action: {"action": 0, "raw": {}, "mouse": {}},
        }
        inert_action = FakeAgentPb2.PlayerAction(duration_tics=4)

        result = runtime._exposed_idle_under_fire_guard(
            SimpleNamespace(),
            directive,
            FakeController(),
            modules,
            1,
            "retreat",
            inert_action,
            {"skill": "hold_cover_no_probe"},
        )

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["source"], "exposed_idle_guard")
        self.assertEqual(decision["skill"], "idle_under_fire_return_fire")
        self.assertEqual(decision["replaced_skill"], "retreat")
    def test_exposed_idle_under_visible_enemy_breaks_los_when_not_shootable(self):
        class FakeRaw:
            def __init__(self, forward_move=0, side_move=0, angle_turn=0, buttons=0):
                self.forward_move = forward_move
                self.side_move = side_move
                self.angle_turn = angle_turn
                self.buttons = buttons

        FakeAgentPb2 = make_agent_pb2(raw=FakeRaw, ACTION_BACKWARD=2, ACTION_SHOOT=7, ACTION_USE=8, ACTION_SWITCH_WEAPON=9)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {  # type: ignore[method-assign]
            "health": 80,
            "visible_enemy": True,
            "shootable": False,
            "ammo_total": 12,
            "weapon": 1,
        }
        runtime._nearest_enemy = lambda _state, prefer_visible=False: {  # type: ignore[method-assign]
            "id": 9,
            "visible": True,
            "distance": 160.0,
            "turn": 0.0,
            "threat": "hitscan",
        }
        state = SimpleNamespace(
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["retreat"],
            "summarize_action": lambda _action: {"action": 0, "raw": {}, "mouse": {}},
        }
        inert_action = FakeAgentPb2.PlayerAction(duration_tics=4)

        result = runtime._exposed_idle_under_fire_guard(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "retreat",
            inert_action,
            {"skill": "hold_cover_no_probe"},
        )

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["source"], "exposed_idle_guard")
        self.assertEqual(decision["reason"], "idle_under_fire_break_los")
        self.assertEqual(decision["skill"], "funnel_back_raw")
        self.assertLess(action.raw.forward_move, 0)
    def test_exposed_idle_guard_respects_no_kill_contract(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7, ACTION_USE=8, ACTION_SWITCH_WEAPON=9)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "race to the exit without killing anyone", "objective": "complete_level", "constraints": ["no_kills"]}
        )
        runtime._metrics = lambda _state: {"health": 80, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["fire"],
            "summarize_action": lambda _action: {"action": 0, "raw": {}, "mouse": {}},
        }
        inert_action = FakeAgentPb2.PlayerAction(duration_tics=4)

        result = runtime._exposed_idle_under_fire_guard(
            SimpleNamespace(),
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            inert_action,
            {"skill": "hold_cover_no_probe"},
        )

        self.assertIsNone(result)
    def test_wounded_route_under_fire_guard_returns_fire(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1, ACTION_SHOOT=7, ACTION_USE=8, ACTION_SWITCH_WEAPON=9)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._metrics = lambda _state: {"health": 35, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "fire"],
            "summarize_action": lambda _action: {"action": 0, "raw": {}, "mouse": {}},
        }
        movement = FakeAgentPb2.PlayerAction(action=FakeAgentPb2.ACTION_FORWARD, amount=48, duration_tics=8)

        result = runtime._wounded_route_under_fire_guard(
            SimpleNamespace(),
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            movement,
            {"skill": "center_passable_portal"},
        )

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["skill"], "wounded_route_return_fire")
        self.assertEqual(decision["health"], 35)
        self.assertEqual(decision["commit_steps_remaining"], 2)

        runtime._metrics = lambda _state: {"health": 80, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        continued = runtime._wounded_route_under_fire_guard(
            SimpleNamespace(),
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            movement,
            {"skill": "center_passable_portal"},
        )

        self.assertIsNotNone(continued)
        _index2, _skill2, _action2, decision2 = continued
        self.assertEqual(decision2["commit_steps_remaining"], 1)
    def test_wounded_route_under_fire_guard_skips_final_exit_and_hazard(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1, ACTION_SHOOT=7, ACTION_USE=8, ACTION_SWITCH_WEAPON=9)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._metrics = lambda _state: {"health": 25, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "fire"],
            "summarize_action": lambda _action: {"action": 0, "raw": {}, "mouse": {}},
        }
        movement = FakeAgentPb2.PlayerAction(action=FakeAgentPb2.ACTION_FORWARD, amount=48, duration_tics=8)

        self.assertIsNone(
            runtime._wounded_route_under_fire_guard(
                SimpleNamespace(),
                directive,
                FakeController(),
                modules,
                0,
                "route_progression",
                movement,
                {"skill": "sector_route_to_exit_line", "final_exit_commit": 1},
            )
        )
        self.assertIsNone(
            runtime._wounded_route_under_fire_guard(
                SimpleNamespace(),
                directive,
                FakeController(),
                modules,
                0,
                "route_progression",
                movement,
                {"skill": "sector_route_hazard_escape", "state": "hazard_floor_escape"},
            )
        )
    def test_hazard_floor_escape_override_preempts_combat_contact(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1, ACTION_SHOOT=7, ACTION_USE=8, ACTION_SWITCH_WEAPON=9)

        FakeController = make_controller(3)

        class FakePlanner:
            def hazard_escape_action(self, _state, agent_pb2, _door_memory):
                return PlanAction(
                    skill="route_progression",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=48, duration_tics=8),
                    detail={
                        "skill": "sector_route_hazard_escape",
                        "hazard_sector": 7,
                        "route_step_sector": 8,
                        "line": 99,
                    },
                    door_line_id=99,
                )

        runtime = BrainRuntime()
        runtime._refresh_planner = lambda *_args: FakePlanner()  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "fire"]}
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True),
            enemies=[SimpleNamespace(line_of_sight=True)],
        )

        result = runtime._hazard_floor_escape_override(state, directive, FakeController(), modules, stub=None)

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "route_progression")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_FORWARD)
        self.assertEqual(decision["source"], "hazard_floor_guard")
        self.assertEqual(decision["state"], "hazard_floor_escape")
        self.assertEqual(decision["hazard_sector"], 7)
        self.assertEqual(decision["line_id"], 99)
    def test_hazard_floor_escape_commits_after_leaving_damaging_sector(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1, ACTION_SHOOT=7, ACTION_USE=8, ACTION_SWITCH_WEAPON=9)

        FakeController = make_controller(3)

        class FakePlanner:
            def player_from_state(self, _state):
                return {"point": SimpleNamespace(x=0, y=0), "angle": 0}

            def sector_for_player(self, state, _player=None):
                return state.current_sector

            def sector_is_damaging(self, sector_id):
                return int(sector_id) == 7

            def hazard_escape_action(self, _state, agent_pb2, _door_memory):
                return PlanAction(
                    skill="route_progression",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=48, duration_tics=8),
                    detail={"skill": "sector_route_hazard_escape", "hazard_sector": 7},
                )

        runtime = BrainRuntime()
        runtime._refresh_planner = lambda *_args: FakePlanner()  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "fire"]}

        first = runtime._hazard_floor_escape_override(
            SimpleNamespace(current_sector=7),
            directive,
            FakeController(),
            modules,
            stub=None,
        )
        second = runtime._hazard_floor_escape_override(
            SimpleNamespace(current_sector=8),
            directive,
            FakeController(),
            modules,
            stub=None,
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        _index, skill, action, decision = second
        self.assertEqual(skill, "route_progression")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_FORWARD)
        self.assertEqual(decision["reason"], "hazard_floor_escape_commit")
        self.assertGreaterEqual(decision["commit_steps_remaining"], 0)
    def test_hazard_floor_critical_contact_uses_emergency_sprint(self):
        class FakeRawTiccmd:
            def __init__(self, **kwargs):
                self.forward_move = kwargs.get("forward_move", 0)
                self.side_move = kwargs.get("side_move", 0)
                self.angle_turn = kwargs.get("angle_turn", 0)

        FakeAgentPb2 = make_agent_pb2(raw=FakeRawTiccmd, ACTION_FORWARD=1, ACTION_SHOOT=7, ACTION_USE=8, ACTION_SWITCH_WEAPON=9)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._refresh_planner = lambda *_args: object()  # type: ignore[method-assign]
        runtime._metrics = lambda _state: {  # type: ignore[method-assign]
            "health": ROUTE_CRITICAL_HEALTH_BREAKAWAY,
            "visible_enemy": True,
            "shootable": False,
        }
        runtime._nearest_enemy = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "id": 42,
            "threat": "hitscan",
            "distance": 96.0,
            "turn": 20.0,
            "visible": True,
        }
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "recover_stuck"]}

        result = runtime._hazard_floor_escape_override(SimpleNamespace(), directive, FakeController(), modules, stub=None)

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["source"], "hazard_floor_guard")
        self.assertEqual(decision["skill"], "critical_turn_and_burn_raw")
        self.assertEqual(decision["reason"], "hazard_floor_critical_contact_escape")
        self.assertEqual(action.raw.forward_move, 64)
    def test_hazard_floor_turn_only_route_uses_raw_escape_step(self):
        class FakeRawTiccmd:
            def __init__(self, **kwargs):
                self.forward_move = kwargs.get("forward_move", 0)
                self.side_move = kwargs.get("side_move", 0)
                self.angle_turn = kwargs.get("angle_turn", 0)

        FakeAgentPb2 = make_agent_pb2(raw=FakeRawTiccmd, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=3, ACTION_STRAFE_RIGHT=4, ACTION_TURN_LEFT=5, ACTION_SHOOT=7, ACTION_USE=8, ACTION_SWITCH_WEAPON=9)

        FakeController = make_controller(3)

        class FakePlanner:
            def hazard_escape_action(self, _state, agent_pb2, _door_memory):
                return PlanAction(
                    skill="route_progression",
                    action=agent_pb2.PlayerAction(action=agent_pb2.ACTION_TURN_LEFT, amount=20, duration_tics=4),
                    detail={"skill": "sector_route_hazard_escape", "hazard_sector": 7},
                    door_line_id=184,
                )

        runtime = BrainRuntime()
        runtime._refresh_planner = lambda *_args: FakePlanner()  # type: ignore[method-assign]
        runtime._metrics = lambda _state: {"health": 85, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["route_progression", "retreat", "recover_stuck"]}
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90.0, block_distance_fp=int(256 * FP_UNIT))
                ],
                forward_open=False,
                back_open=False,
            )
        )

        result = runtime._hazard_floor_escape_override(state, directive, FakeController(), modules, stub=None)

        self.assertIsNotNone(result)
        _index, skill, action, decision = result
        self.assertEqual(skill, "route_progression")
        self.assertEqual(decision["skill"], "hazard_floor_raw_escape")
        self.assertEqual(decision["reason"], "hazard_floor_rotation_escape")
        self.assertEqual(decision["line_id"], 184)
        self.assertEqual(decision["turn_suppressed"], 1)
        self.assertEqual(action.raw.forward_move, 64)
        self.assertEqual(action.raw.side_move, 54)
        self.assertEqual(action.raw.angle_turn, 0)
    def test_wounded_complete_level_prefers_progress_planner_override(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        metrics = {"health": 35, "visible_enemy": True, "shootable": True}

        self.assertTrue(runtime._wounded_complete_level_route_priority(directive, metrics))
        self.assertTrue(
            runtime._planner_override_is_progress(
                (0, "open_use_line", object(), {"source": "spatial_planner", "skill": "sector_route_to_use_line"})
            )
        )
        self.assertFalse(
            runtime._planner_override_is_progress(
                (0, "fire", object(), {"source": "spatial_planner", "skill": "complete_level_fire_burst"})
            )
        )
    def test_active_critical_escape_suppresses_wounded_route_priority(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        metrics = {"health": ROUTE_CRITICAL_HEALTH_BREAKAWAY, "visible_enemy": True, "shootable": True}

        runtime._critical_turn_and_burn_steps = 1
        self.assertFalse(runtime._wounded_complete_level_route_priority(directive, metrics))

        runtime._critical_turn_and_burn_steps = 0
        runtime._critical_turn_and_burn_handoff_steps = 1
        self.assertFalse(runtime._wounded_complete_level_route_priority(directive, metrics))

        distant_metrics = {
            **metrics,
            "nearest_enemy_dist": int(ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE + 96),
        }
        self.assertTrue(runtime._wounded_complete_level_route_priority(directive, distant_metrics))

        runtime._critical_turn_and_burn_handoff_steps = 0
        runtime._critical_turn_and_burn_deflect_steps = 1
        self.assertFalse(runtime._wounded_complete_level_route_priority(directive, metrics))

        runtime._critical_turn_and_burn_deflect_steps = 0
        self.assertTrue(runtime._wounded_complete_level_route_priority(directive, metrics))
    def test_complete_level_damage_arms_survival_retreat_window(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})

        runtime._record_damage_reaction(directive, 12, {"health": 88})

        self.assertEqual(runtime._cautious_recent_hit_window, 30)
        self.assertEqual(runtime._cautious_threshold_cooldown, 0)
        self.assertEqual(runtime._cautious_ambush_window, CAUTIOUS_COVER_AMBUSH_WINDOW)
        self.assertEqual(runtime._cautious_retreat_steps, 6)
    def test_non_survival_damage_waits_until_low_health(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "find an enemy", "objective": "find_enemy"})

        runtime._record_damage_reaction(directive, 12, {"health": 88})
        self.assertEqual(runtime._cautious_recent_hit_window, 0)

        runtime._record_damage_reaction(directive, 12, {"health": 55})
        self.assertEqual(runtime._cautious_recent_hit_window, 30)
        self.assertEqual(runtime._cautious_retreat_steps, 6)
    def test_unconstrained_complete_level_allows_defensive_combat(self):
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        self.assertIn("fire", directive.allowed_skills)
        self.assertIn("close_visible_contact", directive.allowed_skills)

        no_kill = parse_directive(
            {"goal": "race to the exit without killing anyone", "objective": "complete_level", "constraints": ["no_kills"]}
        )
        self.assertNotIn("fire", no_kill.allowed_skills)
        self.assertNotIn("close_visible_contact", no_kill.allowed_skills)
    def test_complete_level_defensive_override_fires_on_shootable_threat(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action="class", ACTION_SHOOT=7, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact"]}
        state = SimpleNamespace(
            tick=100,
            level=SimpleNamespace(episode=1, map=1, total_kills=6),
            player=SimpleNamespace(
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                ready_weapon=1,
                health=50,
                kills=0,
            ),
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True),
            navigation=SimpleNamespace(use_lines=[], route_waypoint=None),
            enemies=[
                SimpleNamespace(
                    line_of_sight=True,
                    object=SimpleNamespace(id=12, type_id=3004, health=20, position=SimpleNamespace(x_fp=256 * 65536, y_fp=0)),
                )
            ],
        )

        _index, skill, action, decision = runtime._defensive_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["source"], "defensive_combat")
    def test_complete_level_full_health_defensive_override_still_fires_on_shootable_threat(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action="class", ACTION_SHOOT=7, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact"]}
        state = SimpleNamespace(
            tick=100,
            level=SimpleNamespace(episode=1, map=2, total_kills=20),
            player=SimpleNamespace(
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                ready_weapon=1,
                health=100,
                kills=0,
            ),
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True),
            navigation=SimpleNamespace(use_lines=[], route_waypoint=None),
            enemies=[
                make_enemy(id=12, x_fp=320 * 65536, y_fp=0, distance_fp=320 * 65536)
            ],
        )

        _index, skill, action, decision = runtime._defensive_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["skill"], "shootable_threat")
    def test_complete_level_defensive_override_fires_on_close_visible_blocker(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action="class", ACTION_SHOOT=7, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact"]}
        state = SimpleNamespace(
            tick=100,
            level=SimpleNamespace(episode=1, map=2, total_kills=20),
            player=SimpleNamespace(
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                ready_weapon=1,
                health=58,
                kills=0,
            ),
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            navigation=SimpleNamespace(use_lines=[], route_waypoint=None),
            enemies=[
                make_enemy(id=20, x_fp=64 * 65536, y_fp=8 * 65536)
            ],
        )

        _index, skill, action, decision = runtime._defensive_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["skill"], "close_visible_threat_shot")
        self.assertEqual(decision["evidence"], "visible_close")
    def test_no_kill_complete_level_blocks_close_visible_pressure_shot(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action="class", ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "race to the exit without killing anyone", "objective": "complete_level", "constraints": ["no_kills"]}
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact"]}
        state = SimpleNamespace(
            tick=100,
            level=SimpleNamespace(episode=1, map=2, total_kills=20),
            player=SimpleNamespace(
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                ready_weapon=1,
                health=82,
                kills=0,
            ),
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            navigation=SimpleNamespace(use_lines=[], route_waypoint=None),
            enemies=[
                make_enemy(id=20, x_fp=64 * 65536, y_fp=0)
            ],
        )

        self.assertIsNone(runtime._defensive_combat_override(state, directive, FakeController(), modules))


if __name__ == "__main__":
    unittest.main()
