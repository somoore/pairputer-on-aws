"""Avoid-damage route guards and door-flash stationary ambush holds for Agent DOOM."""

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
    parse_directive,
)
from planner import Point  # noqa: E402


class TestAgentDoomAvoidDamageDoorAmbush(unittest.TestCase):
    def test_avoid_damage_guard_blocks_near_threshold_planner_crossing(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {
                "goal": "clear this room safely",
                "objective": "clear this room safely",
                "objective_type": "clear_area",
                "constraints": ["avoid_damage"],
            }
        )
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "retreat"],
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}, "keys": []},
        }
        state = SimpleNamespace(navigation=SimpleNamespace(back_open=True), player=SimpleNamespace(ready_weapon=1))
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=52, duration_tics=8)
        decision = {
            "source": "spatial_planner",
            "skill": "planner_route_use_line_for_contact",
            "action": "follow_opening",
            "dist": 16,
        }

        _index, skill, guarded, guarded_decision = runtime._guard_contract_action(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            action,
            decision,
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(guarded.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(guarded_decision["reason"], "blocked_threshold_avoid_damage")
    def test_avoid_damage_guard_allows_hidden_far_threshold_crossing(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "retreat"],
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}, "keys": []},
        }
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            level=SimpleNamespace(episode=1, map=1, total_kills=4),
            navigation=SimpleNamespace(back_open=True, use_lines=[]),
            player=SimpleNamespace(
                health=100,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=44, x_fp=960 * 65536, y_fp=0, line_of_sight=False)
            ],
        )
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=52, duration_tics=8)
        decision = {
            "source": "spatial_planner",
            "skill": "planner_route_use_line_for_contact",
            "action": "follow_opening",
            "dist": 16,
        }

        _index, skill, guarded, guarded_decision = runtime._guard_contract_action(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            action,
            decision,
        )

        self.assertEqual(skill, "route_progression")
        self.assertIs(guarded, action)
        self.assertEqual(guarded_decision, decision)
    def test_avoid_damage_guard_blocks_visible_route_to_los_forward(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "retreat"],
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}, "keys": []},
        }
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            level=SimpleNamespace(episode=1, map=1, total_kills=4),
            navigation=SimpleNamespace(back_open=True, use_lines=[]),
            player=SimpleNamespace(
                health=100,
                kills=0,  # no prior kill -> tests the pure threshold block, not post-kill peek
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=44, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=120 * 65536, y_fp=0)
            ],
        )
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=52, duration_tics=8)
        decision = {
            "source": "spatial_planner",
            "skill": "planner_route_to_los",
            "action": "forward",
            "dist": 120,
        }

        _index, skill, guarded, guarded_decision = runtime._guard_contract_action(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            action,
            decision,
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(guarded.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(guarded_decision["reason"], "blocked_threshold_avoid_damage")
    def test_avoid_damage_guard_blocks_close_hidden_route_to_los_forward(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "retreat"],
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}, "keys": []},
        }
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            level=SimpleNamespace(episode=1, map=1, total_kills=4),
            navigation=SimpleNamespace(back_open=True, use_lines=[]),
            player=SimpleNamespace(
                health=100,
                kills=0,  # no prior kill -> tests the pure threshold block, not post-kill peek
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=44, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=768 * 65536, y_fp=0, line_of_sight=False)
            ],
        )
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=52, duration_tics=8)
        decision = {
            "source": "spatial_planner",
            "skill": "planner_route_to_los",
            "action": "forward",
            "dist": 110,
        }

        _index, skill, guarded, guarded_decision = runtime._guard_contract_action(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            action,
            decision,
        )

        # A HIDDEN close enemy under avoid_damage: the guard blocks the forward route —
        # either a retreat OR a blind lure-shot from cover (drawing the hidden enemy into
        # the ambush is a valid preserve-health tactic). It must NOT let the raw forward
        # route-to-los proceed.
        self.assertNotEqual(guarded_decision.get("skill"), "planner_route_to_los")
        self.assertIn(guarded_decision.get("reason"), {"blocked_threshold_avoid_damage", "blocked_threshold_hidden_lure"})
        self.assertNotEqual(getattr(guarded, "action", None), FakeAgentPb2.ACTION_FORWARD)
    def test_avoid_damage_guard_blocks_midrange_clear_room_route_to_los_forward(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {
                "goal": "clear this room safely",
                "objective": "clear this room safely",
                "objective_type": "clear_area",
                "constraints": ["avoid_damage"],
            }
        )
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "retreat"],
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}, "keys": []},
        }
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            level=SimpleNamespace(episode=1, map=1, total_kills=4),
            navigation=SimpleNamespace(back_open=True, use_lines=[]),
            player=SimpleNamespace(
                health=100,
                kills=1,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=44, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=256 * 65536, y_fp=0, line_of_sight=False)
            ],
        )
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=52, duration_tics=8)
        decision = {
            "source": "spatial_planner",
            "skill": "planner_route_to_los",
            "action": "forward",
            "dist": 220,
        }

        _index, skill, guarded, guarded_decision = runtime._guard_contract_action(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            action,
            decision,
        )

        self.assertEqual(skill, "route_progression")
        self.assertEqual(guarded.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(guarded_decision["skill"], "blocked_los_blind_lure_shot")
        self.assertEqual(guarded_decision["reason"], "blocked_threshold_hidden_lure")
    def test_avoid_damage_guard_blind_lures_hidden_midrange_route_to_los(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "fire", "retreat"],
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}, "keys": []},
        }
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            level=SimpleNamespace(episode=1, map=1, total_kills=4),
            navigation=SimpleNamespace(back_open=True, use_lines=[]),
            player=SimpleNamespace(
                health=100,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=44, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=220 * 65536, y_fp=0, line_of_sight=False)
            ],
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=52, duration_tics=8)
        decision = {
            "source": "spatial_planner",
            "skill": "planner_route_to_los",
            "action": "forward",
            "dist": 220,
        }

        _index, skill, guarded, guarded_decision = runtime._guard_contract_action(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            action,
            decision,
        )

        self.assertEqual(skill, "fire")
        self.assertEqual(guarded.raw.buttons, 1)
        self.assertEqual(guarded_decision["skill"], "blocked_los_blind_lure_shot")
        self.assertEqual(guarded_decision["reason"], "blocked_threshold_hidden_lure")
    def test_avoid_damage_guard_jiggle_peeks_after_repeated_hidden_lure(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(4)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 45
        runtime._cautious_ambush_window = 8
        runtime._cautious_lure_wait_steps = 3
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "fire", "close_visible_contact", "retreat"],
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}, "keys": []},
        }
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            level=SimpleNamespace(episode=1, map=1, total_kills=4),
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=0,
                back_open=True,
                use_lines=[SimpleNamespace(line_id=151, special=1, nearest_distance_fp=32 * int(FP_UNIT))],
                direction_probes=[],
            ),
            player=SimpleNamespace(
                health=100,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=44, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=220 * 65536, y_fp=0, line_of_sight=False)
            ],
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=52, duration_tics=8)
        decision = {
            "source": "spatial_planner",
            "skill": "planner_route_to_los",
            "action": "forward",
            "dist": 220,
        }

        _index, skill, guarded, guarded_decision = runtime._guard_contract_action(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            action,
            decision,
        )

        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(guarded_decision["skill"], "jiggle_peek_probe")
        self.assertEqual(guarded_decision["reason"], "blocked_threshold_jiggle_peek")
        self.assertEqual(guarded_decision["state"], "pre_fire_peek")
        self.assertEqual(guarded_decision["action"], "jiggle_side_probe")
        self.assertEqual(guarded_decision["cover_evidence"], "blocked_route_to_los")
        self.assertEqual(guarded_decision["dist"], 220)
        self.assertEqual(guarded_decision["turn"], 0.0)
        self.assertFalse(guarded_decision["fire_ready"])
        self.assertEqual(getattr(guarded.raw, "buttons", 0), 0)
        self.assertGreater(guarded.raw.forward_move, 0)
        self.assertNotEqual(guarded.raw.side_move, 0)
        self.assertGreater(runtime._cautious_jiggle_peek_steps, 0)
        self.assertGreater(runtime._cautious_retreat_steps, 0)
    def test_avoid_damage_guard_commits_short_breach_after_repeated_safe_prefires(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(4)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 45
        runtime._cautious_ambush_window = 8
        runtime._cautious_jiggle_probe_attempts = 4
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "fire", "close_visible_contact", "retreat"],
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}, "keys": []},
        }
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            level=SimpleNamespace(episode=1, map=1, total_kills=4),
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=0,
                back_open=True,
                use_lines=[SimpleNamespace(line_id=151, special=1, nearest_distance_fp=32 * int(FP_UNIT))],
                direction_probes=[],
            ),
            player=SimpleNamespace(
                health=100,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=44, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=512 * 65536, y_fp=256 * 65536, line_of_sight=False)
            ],
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=52, duration_tics=8)
        decision = {
            "source": "spatial_planner",
            "skill": "planner_route_to_los",
            "action": "forward",
            "dist": 120,
        }

        _index, skill, guarded, guarded_decision = runtime._guard_contract_action(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            action,
            decision,
        )

        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(guarded_decision["skill"], "jiggle_commit_los_breach")
        self.assertEqual(guarded_decision["reason"], "repeated_prefire_no_los")
        self.assertEqual(guarded_decision["dist"], 120)
        self.assertFalse(guarded_decision["fire_ready"])
        self.assertEqual(guarded.raw.buttons, 0)
        self.assertGreater(guarded.raw.forward_move, 0)
        self.assertGreater(runtime._cautious_jiggle_peek_steps, 0)
        self.assertGreater(runtime._cautious_retreat_steps, 0)
        self.assertEqual(runtime._cautious_probe_steps, 0)
        self.assertEqual(runtime._cautious_jiggle_probe_attempts, 0)
    def test_avoid_damage_guard_door_flash_arms_stationary_ambush_hold(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_SHOOT=7, ACTION_USE=8)

        FakeController = make_controller(5)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 45
        runtime._cautious_ambush_window = 8
        runtime._cautious_jiggle_probe_attempts = 4
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "fire", "close_visible_contact", "retreat", "open_use_line"],
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}, "keys": []},
        }
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            level=SimpleNamespace(episode=1, map=1, total_kills=4),
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=0,
                back_open=True,
                use_lines=[SimpleNamespace(line_id=151, special=1, nearest_distance_fp=32 * int(FP_UNIT))],
                direction_probes=[],
            ),
            player=SimpleNamespace(
                health=100,
                kills=0,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=44, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=120 * 65536, y_fp=0, line_of_sight=False)
            ],
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=52, duration_tics=8)
        decision = {
            "source": "spatial_planner",
            "skill": "planner_route_to_los",
            "action": "forward",
            "dist": 120,
        }

        _index, skill, guarded, guarded_decision = runtime._guard_contract_action(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            action,
            decision,
        )

        self.assertIn(skill, {"open_use_line", "route_progression"})
        self.assertEqual(guarded.action, FakeAgentPb2.ACTION_USE)
        self.assertEqual(guarded_decision["skill"], "jiggle_door_flash")
        self.assertEqual(guarded_decision["action"], "use_and_bail")
        self.assertEqual(guarded_decision["ambush_line"], 151)
        self.assertEqual(runtime._cautious_door_ambush_line_id, 151)
        self.assertGreater(runtime._cautious_door_ambush_hold_steps, 0)
        self.assertGreater(runtime._cautious_jiggle_peek_steps, 0)
        self.assertEqual(runtime._cautious_retreat_steps, 0)
    def test_door_flash_ambush_hold_waits_without_translation(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 45
        runtime._cautious_door_ambush_hold_steps = 3
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            navigation=SimpleNamespace(back_open=True, use_lines=[], direction_probes=[]),
            player=SimpleNamespace(
                health=100,
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=96 * 65536, y_fp=0, line_of_sight=False)
            ],
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(decision["skill"], "door_flash_ambush_hold")
        self.assertEqual(decision["state"], "ambush_chokepoint")
        self.assertEqual(getattr(action.raw, "forward_move", 0), 0)
        self.assertEqual(getattr(action.raw, "side_move", 0), 0)
        self.assertEqual(runtime._cautious_door_ambush_hold_steps, 2)
    def test_door_flash_ambush_hold_aims_at_stored_door_line_not_hidden_enemy(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2)

        FakeController = make_controller(2)

        class FakePlanner:
            def _line_by_id(self, line_id):
                if int(line_id) == 151:
                    return SimpleNamespace(midpoint=Point(0, 96 * int(FP_UNIT)))
                return None

        runtime = BrainRuntime()
        runtime._planner = FakePlanner()  # type: ignore[assignment]
        runtime._cautious_target_id = 45
        runtime._cautious_door_ambush_hold_steps = 3
        runtime._cautious_door_ambush_line_id = 151
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            navigation=SimpleNamespace(back_open=True, use_lines=[], direction_probes=[]),
            player=SimpleNamespace(
                health=100,
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=-96 * int(FP_UNIT), y_fp=0, line_of_sight=False)
            ],
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(decision["skill"], "door_flash_ambush_hold")
        self.assertEqual(decision["action"], "ambush_hold_anchor_turn")
        self.assertEqual(decision["turn_source"], "door_anchor")
        self.assertEqual(decision["ambush_line"], 151)
        self.assertAlmostEqual(decision["turn"], 90.0)
        self.assertGreater(getattr(action.raw, "angle_turn", 0), 0)
        self.assertEqual(getattr(action.raw, "forward_move", 0), 0)
        self.assertEqual(getattr(action.raw, "side_move", 0), 0)
    def test_door_flash_ambush_hold_creeps_toward_quiet_anchor_before_expiring(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2)

        FakeController = make_controller(2)

        class FakePlanner:
            def _line_by_id(self, line_id):
                if int(line_id) == 151:
                    return SimpleNamespace(midpoint=Point(192 * int(FP_UNIT), 0))
                return None

        runtime = BrainRuntime()
        runtime._planner = FakePlanner()  # type: ignore[assignment]
        runtime._cautious_target_id = 45
        runtime._cautious_door_ambush_hold_steps = 32
        runtime._cautious_door_ambush_line_id = 151
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            navigation=SimpleNamespace(forward_open=True, back_open=True, use_lines=[], direction_probes=[]),
            player=SimpleNamespace(
                health=100,
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=900 * int(FP_UNIT), y_fp=0, line_of_sight=False)
            ],
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(decision["skill"], "door_flash_ambush_hold")
        self.assertEqual(decision["action"], "ambush_hold_anchor_creep")
        self.assertEqual(decision["turn_source"], "door_anchor")
        self.assertEqual(decision["ambush_line"], 151)
        self.assertGreater(getattr(action.raw, "forward_move", 0), 0)
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
        self.assertEqual(getattr(action.raw, "side_move", 0), 0)
    def test_door_flash_ambush_hold_tucks_flush_beside_near_door_plane(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2)

        FakeController = make_controller(2)

        class FakePlanner:
            def _line_by_id(self, line_id):
                if int(line_id) == 151:
                    return SimpleNamespace(midpoint=Point(64 * int(FP_UNIT), 0))
                return None

        runtime = BrainRuntime()
        runtime._planner = FakePlanner()  # type: ignore[assignment]
        runtime._cautious_target_id = 45
        runtime._cautious_door_ambush_hold_steps = 32
        runtime._cautious_door_ambush_line_id = 151
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            navigation=SimpleNamespace(
                forward_open=True,
                back_open=True,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=40 * int(FP_UNIT)),
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=96 * int(FP_UNIT)),
                ],
            ),
            player=SimpleNamespace(
                health=100,
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=900 * int(FP_UNIT), y_fp=0, line_of_sight=False)
            ],
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        # Anchor at 64u (inside the 96u jamb window) with the nearest open wall
        # 40u to the right (-90): tuck toward it instead of holding on the door
        # axis, keeping aim tracked on the anchor.
        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(decision["skill"], "door_flash_ambush_hold")
        self.assertEqual(decision["action"], "ambush_hold_wall_tuck")
        self.assertGreater(getattr(action.raw, "side_move", 0), 0)
        self.assertEqual(getattr(action.raw, "forward_move", 0), 0)
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
        self.assertEqual(runtime._cautious_door_tuck_steps, 1)

        # Budget: after 6 tuck steps the hold stops repositioning (no creep
        # either — anchor is inside its >96u range) and just holds aim.
        runtime._cautious_door_tuck_steps = 6
        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(decision["skill"], "door_flash_ambush_hold")
        self.assertNotEqual(decision["action"], "ambush_hold_wall_tuck")
        self.assertEqual(getattr(action.raw, "side_move", 0), 0)
    def test_door_flash_ambush_hold_sidestep_creeps_when_anchor_forward_is_blocked(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2)

        FakeController = make_controller(2)

        class FakePlanner:
            def _line_by_id(self, line_id):
                if int(line_id) == 151:
                    return SimpleNamespace(midpoint=Point(192 * int(FP_UNIT), 0))
                return None

        runtime = BrainRuntime()
        runtime._planner = FakePlanner()  # type: ignore[assignment]
        runtime._cautious_target_id = 45
        runtime._cautious_door_ambush_hold_steps = 32
        runtime._cautious_door_ambush_line_id = 151
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            navigation=SimpleNamespace(
                forward_open=False,
                back_open=True,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=False, angle_offset_degrees=-90, block_distance_fp=96 * int(FP_UNIT)),
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=96 * int(FP_UNIT)),
                ],
            ),
            player=SimpleNamespace(
                health=100,
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=900 * int(FP_UNIT), y_fp=0, line_of_sight=False)
            ],
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(decision["action"], "ambush_hold_anchor_creep")
        self.assertEqual(getattr(action.raw, "forward_move", 0), 0)
        self.assertNotEqual(getattr(action.raw, "side_move", 0), 0)
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
    def test_door_flash_ambush_hold_releases_stale_far_hidden_target(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 45
        runtime._cautious_door_ambush_hold_steps = 56
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            navigation=SimpleNamespace(back_open=True, use_lines=[], direction_probes=[]),
            player=SimpleNamespace(
                health=100,
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=900 * 65536, y_fp=0, line_of_sight=False)
            ],
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        result = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertIsNone(result)
        self.assertEqual(runtime._cautious_door_ambush_hold_steps, 0)
    def test_door_flash_ambush_hold_prefires_when_target_becomes_shootable(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 45
        runtime._cautious_door_ambush_hold_steps = 3
        runtime._cautious_jiggle_peek_steps = 4
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=True, target_is_enemy=True, target_id=45),
            navigation=SimpleNamespace(back_open=True, use_lines=[], direction_probes=[]),
            player=SimpleNamespace(
                health=100,
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[
                make_enemy(id=45, x_fp=96 * 65536, y_fp=0)
            ],
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat", "fire"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "jiggle_prefire_shot")
        self.assertEqual(getattr(action.raw, "buttons", 0), 1)
        self.assertLess(getattr(action.raw, "forward_move", 0), 0)
        self.assertGreater(runtime._cautious_door_ambush_hold_steps, 0)
        self.assertGreater(runtime._cautious_retreat_steps, 0)
    def test_avoid_damage_guard_replaces_post_kill_los_route_with_peek_shot(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._objective_baseline_kills = 0
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear this room safely", "objective_type": "clear_area", "constraints": ["avoid_damage"]}
        )
        modules = {
            "agent_pb2": FakeAgentPb2,
            "SKILL_ACTIONS": ["route_progression", "retreat", "fire"],
            "summarize_action": lambda _action: {"action": FakeAgentPb2.ACTION_FORWARD, "raw": {}, "mouse": {}, "keys": []},
        }
        state = SimpleNamespace(
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
            level=SimpleNamespace(episode=1, map=1, total_kills=4),
            navigation=SimpleNamespace(
                back_open=True,
                use_lines=[],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            ),
            player=SimpleNamespace(
                health=100,
                kills=1,
                ready_weapon=1,
                ammo=SimpleNamespace(bullets=44, shells=0, rockets=0, cells=0),
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0),
            ),
            enemies=[],
        )
        action = SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD, amount=42, duration_tics=14)
        decision = {
            "source": "spatial_planner",
            "skill": "planner_route_to_los",
            "action": "forward",
            "dist": 135,
        }

        _index, skill, guarded, guarded_decision = runtime._guard_contract_action(
            state,
            directive,
            FakeController(),
            modules,
            0,
            "route_progression",
            action,
            decision,
        )

        self.assertEqual(skill, "fire")
        self.assertEqual(guarded.duration_tics, 2)
        self.assertEqual(guarded.raw.buttons, 1)
        self.assertGreater(guarded.raw.forward_move, 0)
        self.assertEqual(guarded_decision["skill"], "post_kill_los_peek_shot")


if __name__ == "__main__":
    unittest.main()
