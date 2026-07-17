"""Goal parsing, contract compilation, directive rules, and eval/finish semantics for Agent DOOM."""

from __future__ import annotations

import sys
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from doom_fakes import make_agent_pb2, make_controller, make_enemy  # noqa: E402

from goal_contract import compile_goal_contract, contract_rules, filter_allowed_skills  # noqa: E402
from brain_runtime import (  # noqa: E402
    BrainRuntime,
    _normalize_drive_goal_payload,
    parse_directive,
)
from door_memory import DoorMemory  # noqa: E402
from planner import Point  # noqa: E402
from threat_model import classify_enemy  # noqa: E402


class TestAgentDoomGoalSemantics(unittest.TestCase):
    def test_melee_no_ammo_goal_compiles_to_fist_contract(self):
        contract = compile_goal_contract("go find bad guy and punch him down - don't use any ammo")
        self.assertEqual(contract.objective, "kill_enemy")
        self.assertEqual(contract.style, "melee")
        self.assertEqual(contract.constraints["ammo_budget"], 0)
        self.assertEqual(contract.constraints["weapon_policy"], "fist_only")
        self.assertIn("spend_ammo", contract.forbidden)
        self.assertIn("ammo_unchanged", contract.success_evidence)
    def test_contract_filters_unsafe_skills(self):
        contract = compile_goal_contract("race to the exit without killing anything")
        skills = filter_allowed_skills(["fire", "engage", "press_exit", "route_progression", "retreat"], contract)
        self.assertNotIn("fire", skills)
        self.assertNotIn("engage", skills)
        self.assertEqual(skills[:2], ["press_exit", "route_progression"])
        self.assertIn("retreat", skills)
    def test_no_kill_exit_directive_does_not_inherit_attack_rules(self):
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        self.assertIn("exit", directive.rules)
        self.assertIn("speedrun", directive.rules)
        self.assertIn("no_kills", directive.rules)
        self.assertIn("avoid_combat", directive.rules)
        self.assertNotIn("attack", directive.rules)
        self.assertNotIn("find_enemy", directive.rules)
        self.assertNotIn("fire", directive.allowed_skills)
        self.assertIn("retreat", directive.allowed_skills)
        self.assertIn("recover_stuck", directive.allowed_skills)
    def test_runtime_line_crossing_distinguishes_slide_from_crossing(self):
        fp = 65536

        def fake_state(x: int, y: int) -> SimpleNamespace:
            return SimpleNamespace(
                player=SimpleNamespace(
                    object=SimpleNamespace(
                        position=SimpleNamespace(x_fp=x * fp, y_fp=y * fp),
                        angle_degrees=0,
                    ),
                    ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
                    health=100,
                    kills=0,
                ),
                level=SimpleNamespace(episode=1, map=2, total_kills=0),
                combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False),
                enemies=[],
            )

        line = SimpleNamespace(
            a=SimpleNamespace(x=64 * fp, y=-64 * fp),
            b=SimpleNamespace(x=64 * fp, y=64 * fp),
        )
        runtime = BrainRuntime()

        self.assertTrue(runtime._crossed_planner_line(fake_state(32, 0), fake_state(96, 0), line))
        self.assertFalse(runtime._crossed_planner_line(fake_state(32, -80), fake_state(96, -80), line))
    def test_parse_directive_carries_goal_contract(self):
        directive = parse_directive({"goal": "find an enemy and punch it, no ammo", "max_tics": 900})
        self.assertEqual(directive.contract.style, "melee")
        self.assertIn("attack", directive.rules)
        self.assertIn("find_enemy", directive.rules)
        self.assertNotIn("fire", directive.allowed_skills)
        self.assertLessEqual(directive.max_tics, 4200)
    def test_parse_directive_does_not_shorten_explicit_tic_budget(self):
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level", "max_tics": 4200})
        self.assertEqual(directive.max_tics, 4200)
        self.assertEqual(directive.max_steps, 4200)
    def test_parse_directive_honors_generalization_tic_budget(self):
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level", "max_tics": 8000})
        self.assertEqual(directive.max_tics, 8000)
        self.assertEqual(directive.max_steps, 8000)
    def test_parse_directive_respects_explicit_step_budget(self):
        directive = parse_directive(
            {"goal": "beat the level", "objective": "complete_level", "max_tics": 4200, "budget": 128}
        )
        self.assertEqual(directive.max_tics, 4200)
        self.assertEqual(directive.max_steps, 128)
    def test_parse_directive_carries_eval_human_interrupt_isolation(self):
        directive = parse_directive(
            {"goal": "beat the level", "objective": "complete_level", "ignore_human_interrupt": True}
        )

        self.assertTrue(directive.ignore_human_interrupt)
        self.assertTrue(directive.as_dict()["ignore_human_interrupt"])
    def test_commander_enums_compile_to_enforced_contract(self):
        payload = _normalize_drive_goal_payload(
            {
                "goal": "race to the exit without killing anyone",
                "objective": "exit_level",
                "constraints": ["no_kills", "no_ammo", "avoid_damage"],
                "max_tics": 3500,
            }
        )
        directive = parse_directive(payload)
        self.assertEqual(directive.contract.objective, "exit_level")
        self.assertEqual(directive.contract.constraints["kill_budget"], 0)
        self.assertEqual(directive.contract.constraints["ammo_budget"], 0)
        self.assertTrue(directive.contract.constraints["preserve_health"])
        self.assertIn("no_kills", directive.rules)
        self.assertNotIn("fire", directive.allowed_skills)
    def test_finish_returns_commander_contract_fields(self):
        runtime = BrainRuntime()
        directive = parse_directive(
            _normalize_drive_goal_payload(
                {
                    "goal": "race to the exit without killing anyone",
                    "objective": "exit_level",
                    "constraints": ["no_kills"],
                    "max_tics": 3500,
                }
            )
        )
        baseline = {
            "tick": 10,
            "episode": 1,
            "map": 1,
            "x": 0,
            "y": 0,
            "health": 100,
            "kills": 0,
            "bullets": 50,
            "shells": 0,
            "rockets": 0,
            "cells": 0,
            "ammo_total": 50,
            "enemy_count": 4,
            "weapon": 1,
            "visible_enemy": False,
            "shootable": False,
            "exit_line": False,
            "exit_dist": 0,
        }
        final = dict(baseline)
        final.update({"tick": 80, "x": 128 * 65536, "exit_line": True})
        result = runtime._finish(
            directive,
            {"status": "achieved", "summary": "exit affordance is in use range"},
            baseline,
            final,
            object(),
            12,
            80,
            "press_exit",
            [],
            False,
            "test-run",
            fired=False,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["driver_status"], "achieved")
        self.assertEqual(result["stop_reason"], "reached_exit")
        self.assertEqual(result["committed_contract"]["objective"], "exit_level")
        self.assertIn("no_kills", result["committed_contract"]["constraints"])
        self.assertIn("progress_metrics", result)
        self.assertIn("evidence", result)
        compact = runtime._compact_goal_result(result)
        self.assertLess(len(json.dumps(compact, sort_keys=True, separators=(",", ":")).encode("utf-8")), 500)
        self.assertEqual(compact["committed_contract"]["objective"], "exit_level")
        self.assertEqual(compact["committed_contract"]["style"], "speedrun")
        self.assertNotIn("delta", compact)
        self.assertNotIn("summary", compact)
    def test_tactical_status_returns_cached_busy_status_when_driver_lock_is_held(self):
        class BusyLock:
            def acquire(self, blocking=True):
                return False

            def release(self):
                raise AssertionError("busy path must not release an unacquired lock")

        runtime = BrainRuntime()
        runtime._lock = BusyLock()  # type: ignore[assignment]
        runtime._human_check_active = True
        runtime._last_status = {
            "status": "running",
            "objective": "beat the level",
            "skill": "route_progression",
            "steps": 42,
            "tics": 512,
            "summary": "routing toward exit",
        }
        runtime._last_plan = {"planner_skill": "route_progression", "kind": "planner_route_to_use_line", "line_id": 329}

        status = runtime.tactical_status()

        self.assertTrue(status["busy"])
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["objective"], "beat the level")
        self.assertEqual(status["phase"], "route_progression")
        self.assertEqual(status["steps"], 42)
        self.assertEqual(status["tics"], 512)
        self.assertEqual(status["state"], "busy")
        self.assertTrue(status["human_active"])
        self.assertEqual(status["plan"]["line_id"], 329)
    def test_drive_goal_default_budget_allows_melee_route(self):
        payload = {"goal": "find an enemy and punch it down without using ammo"}
        contract = compile_goal_contract(payload["goal"], payload)
        self.assertEqual(contract.constraints["ammo_budget"], 0)
        # Keep this aligned with BrainRuntime.drive_goal's no-ammo/melee default.
        self.assertEqual(1600 if contract.style == "melee" or contract.constraints.get("ammo_budget") == 0 else 900, 1600)
    def test_complete_level_rules_keep_exit_and_use(self):
        contract = compile_goal_contract("beat the level")
        rules = contract_rules(contract)
        self.assertIn("complete_level", rules)
        self.assertIn("exit", rules)
        self.assertIn("use", rules)
    def test_clear_room_sets_multi_kill_target(self):
        contract = compile_goal_contract("clear this room safely")
        self.assertEqual(contract.objective, "clear_area")
        self.assertEqual(contract.style, "cautious")
        self.assertEqual(contract.constraints["kill_target"], 2)
        self.assertTrue(contract.constraints["preserve_health"])
    def test_preserve_health_goal_avoids_damage(self):
        contract = compile_goal_contract("preserve health and avoid damage")
        self.assertEqual(contract.objective, "preserve_health")
        self.assertTrue(contract.constraints["preserve_health"])
        self.assertIn("health_drop", contract.failure_evidence)
    def test_exit_lines_remain_targetable(self):
        memory = DoorMemory()
        memory.observe_line(330, special=11, exit_line=True)
        self.assertEqual(memory.state_for(330), "exit")
        self.assertTrue(memory.can_retry(330))
    def test_no_kill_guard_blocks_fire_buttons(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_SHOOT=7, ACTION_STRAFE_LEFT=5)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat", "recover_stuck"]}
        state = SimpleNamespace(navigation=SimpleNamespace(back_open=True), player=SimpleNamespace(ready_weapon=1))
        cases = [
            (
                SimpleNamespace(action=0, raw=SimpleNamespace(buttons=1), mouse=SimpleNamespace(buttons=0), keys=[]),
                {"action": 0, "raw": {"buttons": 1}, "mouse": {"buttons": 0}, "keys": []},
            ),
            (
                SimpleNamespace(action=0, raw=SimpleNamespace(buttons=0), mouse=SimpleNamespace(buttons=1), keys=[]),
                {"action": 0, "raw": {"buttons": 0}, "mouse": {"buttons": 1}, "keys": []},
            ),
        ]
        for action, summary in cases:
            modules["summarize_action"] = lambda _action, s=summary: s
            _index, _skill, guarded, decision = runtime._guard_contract_action(
                state,
                directive,
                FakeController(),
                modules,
                0,
                "retreat",
                action,
                {"skill": "ppo_defensive_fire"},
            )
            self.assertEqual(guarded.action, FakeAgentPb2.ACTION_BACKWARD)
            self.assertEqual(decision["reason"], "blocked_shot_no_kills")
    def test_no_kill_selection_does_not_fall_back_to_fire_heuristic(self):
        FakeController = make_controller(4, mask_value=False, heuristic_action_index=0)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        modules = {"SKILL_ACTIONS": ["fire", "retreat", "recover_stuck", "route_progression"]}
        index, skill = runtime._select_skill(FakeController(), object(), directive, modules)
        self.assertNotEqual(skill, "fire")
        self.assertIn(skill, directive.allowed_skills)
        self.assertEqual(index, modules["SKILL_ACTIONS"].index(skill))
    def test_evaluate_fails_when_episode_state_resets_mid_objective(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        baseline = {
            "kills": 1,
            "health": 22,
            "ammo_total": 54,
            "x": 0,
            "y": 0,
            "episode": 1,
            "map": 1,
        }
        current = {
            **baseline,
            "kills": 0,
            "health": 100,
            "tick": 500,
            "enemy_count": 6,
            "visible_enemy": False,
            "shootable": False,
            "exit_line": False,
            "exit_dist": 0,
        }
        result = runtime._evaluate(directive, baseline, current, fired=False, shootable_seen=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("state reset", result["summary"])
    def test_evaluate_allows_unattributed_kill_delta_under_no_kill(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        baseline = {
            "kills": 0,
            "health": 100,
            "ammo_total": 50,
            "x": 0,
            "y": 0,
            "episode": 1,
            "map": 1,
            "tick": 100,
        }
        current = {
            **baseline,
            "kills": 1,
            "tick": 500,
            "enemy_count": 6,
            "visible_enemy": False,
            "shootable": False,
            "exit_line": False,
            "exit_dist": 0,
        }
        result = runtime._evaluate(directive, baseline, current, fired=False, shootable_seen=False)
        self.assertEqual(result["status"], "tracking")
        self.assertNotIn("kill budget violated", result["summary"])
    def test_evaluate_fails_agent_caused_kill_delta_under_no_kill(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        baseline = {
            "kills": 0,
            "health": 100,
            "ammo_total": 50,
            "x": 0,
            "y": 0,
            "episode": 1,
            "map": 1,
            "tick": 100,
        }
        current = {
            **baseline,
            "kills": 1,
            "tick": 500,
            "enemy_count": 6,
            "visible_enemy": False,
            "shootable": False,
            "exit_line": False,
            "exit_dist": 0,
        }
        result = runtime._evaluate(directive, baseline, current, fired=True, shootable_seen=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("kill budget violated", result["summary"])
    def test_evaluate_health_allowance_for_clear_area(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]})
        baseline = {
            "kills": 0,
            "health": 100,
            "ammo_total": 50,
            "x": 0,
            "y": 0,
            "episode": 1,
            "map": 1,
            "tick": 100,
            "enemy_count": 4,
            "visible_enemy": False,
            "shootable": False,
            "exit_line": False,
            "exit_dist": 0,
        }
        current = {
            **baseline,
            "kills": 2,
            "health": 99,
            "ammo_total": 45,
            "tick": 500,
            "enemy_count": 2,
        }
        # avoid_damage carries a 9hp allowance (one median zombieman bullet,
        # 3×d5): the run survives one graze in an otherwise perfect clear.
        result = runtime._evaluate(directive, baseline, current, fired=True, shootable_seen=True)
        self.assertEqual(result["status"], "achieved")

        grazed = dict(current, health=91)
        result = runtime._evaluate(directive, baseline, grazed, fired=True, shootable_seen=True)
        self.assertEqual(result["status"], "achieved")

        # Two bullets (or one heavy roll) past the allowance is a fail.
        wounded = dict(current, health=90)
        result = runtime._evaluate(directive, baseline, wounded, fired=True, shootable_seen=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("health budget violated", result["summary"])
        self.assertIn("allowance", result["summary"])
    def test_no_kill_finish_reports_agent_kills_and_infighting(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        baseline = {
            "kills": 0,
            "health": 100,
            "bullets": 50,
            "shells": 0,
            "ammo_total": 50,
        }
        final_metrics = {
            "kills": 1,
            "health": 80,
            "bullets": 50,
            "shells": 0,
            "ammo_total": 50,
            "tick": 300,
            "episode": 1,
            "map": 1,
            "enemy_count": 5,
            "weapon": 2,
            "visible_enemy": True,
            "shootable": False,
        }
        result = runtime._finish(
            directive,
            {"status": "achieved", "summary": "exit affordance is in use range"},
            baseline,
            final_metrics,
            object(),
            12,
            96,
            "press_exit",
            [],
            False,
            "test-run",
            fired=False,
        )
        self.assertEqual(result["delta"]["kills"], 1)
        self.assertEqual(result["delta"]["agent_kills"], 0)
        self.assertEqual(result["delta"]["infight"], 1)
    def test_terminal_sanitizer_strips_fire_under_no_kill(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, player_action=None, ACTION_SHOOT=7, ACTION_TURN_LEFT=3)

        runtime = BrainRuntime()
        action = SimpleNamespace(
            action=FakeAgentPb2.ACTION_SHOOT,
            amount=1,
            duration_tics=8,
            raw=SimpleNamespace(buttons=1),
            mouse=SimpleNamespace(buttons=1),
            keys=[SimpleNamespace(key=99, pressed=True)],
        )
        runtime._strip_fire(action, FakeAgentPb2)
        self.assertEqual(action.action, FakeAgentPb2.ACTION_TURN_LEFT)
        self.assertEqual(action.raw.buttons, 0)
        self.assertEqual(action.mouse.buttons, 0)
        self.assertEqual(action.keys, [])
    def test_no_ammo_fist_does_not_forbid_punch(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "find an enemy and punch it down without using ammo"})
        runtime._weapon_id = lambda _state: 0  # type: ignore[method-assign]
        self.assertFalse(runtime._fire_forbidden(directive, object()))
        runtime._weapon_id = lambda _state: 1  # type: ignore[method-assign]
        self.assertTrue(runtime._fire_forbidden(directive, object()))
    def test_pinky_classifies_as_melee_rush(self):
        enemy = SimpleNamespace(object=SimpleNamespace(type_id=3002))
        self.assertEqual(classify_enemy(enemy), "melee_rush")
    def test_fist_only_visible_melee_rush_backpedals_while_punching(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_FORWARD=1, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7, ACTION_USE=8, ACTION_SWITCH_WEAPON=9)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "find an enemy and punch it, no ammo"})
        state = SimpleNamespace(
            player=SimpleNamespace(
                ready_weapon=0,
                object=SimpleNamespace(
                    position=SimpleNamespace(x_fp=0, y_fp=0),
                    angle_degrees=0,
                ),
            ),
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
            enemies=[
                make_enemy(id=77, x_fp=80 * 65536, y_fp=0, type_id=3002, health=150, distance_fp=80 * 65536)
            ],
            combat=SimpleNamespace(has_shootable_target=False, target_is_enemy=False, target_id=0),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "seek_enemy"]}

        override = runtime._contract_override(state, directive, FakeController(), modules)

        self.assertIsNotNone(override)
        _index, skill, action, decision = override
        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(decision["skill"], "melee_rush_kite_punch")
        self.assertEqual(decision["action"], "backpedal_punch")
        self.assertLess(action.raw.forward_move, 0)
        self.assertEqual(action.raw.buttons, 1)
    def test_finish_omits_empty_optional_fields(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "x" * 100})
        metrics = {
            "kills": 0,
            "health": 100,
            "bullets": 50,
            "shells": 0,
            "ammo_total": 50,
            "tick": 1,
            "episode": 1,
            "map": 1,
            "enemy_count": 0,
            "weapon": 1,
            "visible_enemy": False,
            "shootable": False,
        }
        result = runtime._finish(
            directive,
            {"status": "budget_exhausted", "summary": "objective still in progress"},
            metrics,
            metrics,
            object(),
            1,
            1,
            None,
            [],
            False,
            "test",
        )
        self.assertLessEqual(len(result["objective"]), 60)
        self.assertNotIn("skill", result)
        self.assertNotIn("unsupported", result)
    def test_finish_reports_wall_clock_budget_separately_from_tic_budget(self):
        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level", "max_tics": 7000})
        metrics = {
            "kills": 0,
            "health": 100,
            "bullets": 50,
            "shells": 0,
            "ammo_total": 50,
            "tick": 1,
            "episode": 1,
            "map": 2,
            "enemy_count": 0,
            "weapon": 1,
            "visible_enemy": False,
            "shootable": False,
        }
        result = runtime._finish(
            directive,
            {"status": "budget_exhausted", "summary": "wall-clock budget exhausted"},
            metrics,
            metrics,
            object(),
            915,
            2931,
            "route_progression",
            [],
            False,
            "test",
        )
        self.assertEqual(result["stop_reason"], "wall_clock_exceeded")
    def test_world_model_key_inventory_repairs_key_door_memory(self):
        runtime = BrainRuntime()
        runtime._door_memory = DoorMemory()
        runtime._door_memory.observe_line(527, special=28)
        runtime._door_memory.record_failure(527, status="key_required")

        class FakePlanner:
            key_items = []

            def player_from_state(self, state):
                pos = state.player.object.position
                return {"point": Point(int(pos.x_fp), int(pos.y_fp)), "angle": 0}

            def sector_for_player(self, _state, _player=None):
                return 1

            def neighbor_sector_ids(self, _sector_id):
                return set()

            def sector_is_damaging(self, _sector_id):
                return False

            def sector_for_point_fp(self, _x, _y):
                return 1

        runtime._planner = FakePlanner()  # type: ignore[assignment]
        state = SimpleNamespace(
            tick=1,
            level=SimpleNamespace(episode=1, map=2),
            player=SimpleNamespace(
                key_cards=1 << 2,
                object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0)),
            ),
            navigation=SimpleNamespace(use_lines=[]),
            enemies=[],
        )

        runtime._update_world_model(state)

        self.assertIn("red", runtime._world_memory.acquired_keys)
        self.assertEqual(runtime._door_memory.state_for(527), "closed")
        self.assertTrue(runtime._door_memory.can_retry(527))


if __name__ == "__main__":
    unittest.main()
