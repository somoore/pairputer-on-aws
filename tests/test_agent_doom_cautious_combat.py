"""Cautious combat core: peek/scoot/cover/hitscan/lure/prefire/recent-hit/projectile FSM for Agent DOOM."""

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
    FP_UNIT,
    parse_directive,
)
from planner import Point  # noqa: E402


class TestAgentDoomCautiousCombat(unittest.TestCase):
    def test_cautious_hitscan_combat_peeks_then_funnels(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7)

        FakeController = make_controller(4)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "clear this room safely"})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=7, x_fp=64 * 65536, y_fp=0, distance_fp=64 * 65536)
            ],
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat", "recover_stuck", "route_progression"]}
        index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual((index, skill), (0, "fire"))
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(decision["skill"], "peek_fire")
        self.assertEqual(decision["threat"], "hitscan")
        self.assertEqual(runtime._cautious_retreat_steps, 10)

        state.enemies[0].line_of_sight = False
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "retreat")
        self.assertEqual(getattr(action, "action", 0), 0)
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(decision["state"], "lure_and_wait")
        self.assertEqual(decision["skill"], "lure_and_wait")

        state.enemies[0].line_of_sight = True
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        # At point-blank (64u) with a shootable enemy the agent commits to the duel:
        # kiting back from this range only hands the enemy free shots.
        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["reason"], "survival_pressure_followup_shot")
    def test_cautious_post_shot_scoot_breaks_los_before_reassessing(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7)

        FakeController = make_controller(4)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "clear this room safely"})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=7, x_fp=64 * 65536, y_fp=0, distance_fp=64 * 65536)
            ],
            navigation=SimpleNamespace(
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=96 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat", "recover_stuck", "route_progression"]}
        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertTrue(runtime._cautious_post_shot_scoot)

        # The enemy is STILL shootable on the very next step: the FSM must scoot
        # laterally (break LOS behind the jamb) instead of rolling the hitscan RNG
        # with another duel shot — and never backpedal along his sightline when an
        # open side exists (that is exactly where the traced RNG hits landed).
        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["reason"], "post_shot_scoot")
        self.assertEqual(decision["skill"], "break_los_left")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_STRAFE_LEFT)
        self.assertFalse(runtime._cautious_post_shot_scoot)

        # Scoot consumed: the step after the scoot may resume the duel.
        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "fire")
        self.assertEqual(decision["reason"], "survival_pressure_followup_shot")
        self.assertTrue(runtime._cautious_post_shot_scoot)
    def test_cautious_sustained_lure_pings_after_long_hidden_wait(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_SHOOT=7)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        state = SimpleNamespace(navigation=SimpleNamespace(direction_probes=[]))
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat", "recover_stuck"]}
        enemy = {"id": 82, "threat": "hitscan", "distance": 320.0, "turn": 0.0}

        # 25 four-tic waits = 100 tics: still silent. The 26th crosses the alarm
        # threshold and fires the sonar ping (no movement, stay behind cover).
        for _ in range(25):
            _index, skill, action, decision = runtime._cautious_wait_action(
                state, directive, FakeController(), modules, reason="lure_and_wait_hidden", enemy=enemy
            )
            self.assertEqual(decision["skill"], "lure_and_wait")
        _index, skill, action, decision = runtime._cautious_wait_action(
            state, directive, FakeController(), modules, reason="lure_and_wait_hidden", enemy=enemy
        )
        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "sustained_lure_ping")
        self.assertEqual(decision["reason"], "sonar_ping:lure_and_wait_hidden")
        self.assertEqual(getattr(action.raw, "buttons", 0), 1)
        self.assertEqual(getattr(action.raw, "forward_move", 0), 0)
        self.assertEqual(getattr(action.raw, "side_move", 0), 0)
        self.assertEqual(runtime._cautious_lure_ping_wait_tics, 0)
        # The avoid_damage fire guard must not strip the ping (it silently
        # converted every ping into a cover action before it was whitelisted).
        self.assertTrue(runtime._preserve_health_fire_allowed(decision))

        # Counter reset: the very next wait is silent again.
        _index, skill, action, decision = runtime._cautious_wait_action(
            state, directive, FakeController(), modules, reason="lure_and_wait_hidden", enemy=enemy
        )
        self.assertEqual(decision["skill"], "lure_and_wait")

        # A close hidden threat (<=150u) never gets pinged — he's already coming.
        runtime._cautious_lure_ping_wait_tics = 200
        _index, skill, action, decision = runtime._cautious_wait_action(
            state, directive, FakeController(), modules, reason="lure_and_wait_hidden", enemy={"id": 82, "threat": "hitscan", "distance": 120.0, "turn": 0.0}
        )
        self.assertEqual(decision["skill"], "lure_and_wait")

        # Flush-'em-out escalation: after 3 unanswered pings the 4th threshold
        # crossing abandons the ambush (window/hold zeroed) and yields None so
        # the spatial planner routes to the geometry-stuck enemy.
        runtime._cautious_lure_ping_count = 3
        runtime._cautious_lure_ping_wait_tics = 200
        runtime._cautious_ambush_window = 12
        runtime._cautious_door_ambush_hold_steps = 20
        result = runtime._cautious_wait_action(
            state, directive, FakeController(), modules, reason="lure_and_wait_hidden", enemy=enemy
        )
        self.assertIsNone(result)
        self.assertEqual(runtime._cautious_lure_ping_count, 0)
        self.assertEqual(runtime._cautious_ambush_window, 0)
        self.assertEqual(runtime._cautious_door_ambush_hold_steps, 0)

        # A close threat resets the escalation budget: the lure is working.
        runtime._cautious_lure_ping_count = 2
        runtime._cautious_wait_action(
            state, directive, FakeController(), modules, reason="lure_and_wait_hidden", enemy={"id": 82, "threat": "hitscan", "distance": 120.0, "turn": 0.0}
        )
        self.assertEqual(runtime._cautious_lure_ping_count, 0)
    def test_cautious_cover_never_stands_still_while_visible(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        state = SimpleNamespace(navigation=SimpleNamespace(back_open=False, direction_probes=[]))
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat", "recover_stuck"]}
        enemy = {"id": 9, "threat": "hitscan", "visible": True, "distance": 600.0, "turn": 0.0}

        # No open side, back closed, cover-hold armed — but the enemy SEES us:
        # standing still is a free windup, so the cover action must keep moving.
        runtime._cautious_cover_hold_steps = 6
        _index, _skill, action, decision = runtime._cautious_cover_action(
            state, directive, FakeController(), modules, reason="hold_fire_visible_hitscan_without_cover", enemy=enemy
        )
        self.assertNotIn(decision["skill"], ("hold_cover_window", "hold_cover_no_probe"))
        self.assertNotEqual(getattr(action.raw, "side_move", 0), 0)

        # Hidden enemy: the hold is fine again.
        hidden = dict(enemy, visible=False)
        _index, _skill, action, decision = runtime._cautious_cover_action(
            state, directive, FakeController(), modules, reason="hold_fire_visible_hitscan_without_cover", enemy=hidden
        )
        self.assertEqual(decision["skill"], "hold_cover_window")
    def test_cautious_far_open_field_sniper_yields_to_planner(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_cover_profile = lambda _state: None  # type: ignore[method-assign]
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=16, x_fp=600 * 65536, y_fp=0, distance_fp=600 * 65536)
            ],
            navigation=SimpleNamespace(forward_open=True, back_open=True, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        # Visible non-shootable hitscanner at 600u with NO cover profile: the
        # open-field side-dance breaks no LOS — yield so the planner routes.
        result = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertIsNone(result)
    def test_cautious_lure_wait_tucks_beside_near_door_plane(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        class FakePlanner:
            def _line_by_id(self, line_id):
                if int(line_id) == 151:
                    return SimpleNamespace(midpoint=Point(100 * int(FP_UNIT), 0))
                return None

        runtime = BrainRuntime()
        runtime._planner = FakePlanner()  # type: ignore[assignment]
        runtime._cautious_door_ambush_line_id = 151
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        state = SimpleNamespace(
            navigation=SimpleNamespace(
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=48 * int(FP_UNIT)),
                ],
            ),
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat", "recover_stuck"]}
        enemy = {"id": 82, "threat": "hitscan", "visible": False, "distance": 300.0, "turn": 0.0}

        # Hidden enemy, door anchor 100u away, open wall 48u to the left: the
        # lure wait spends its steps tucking flush beside the opening.
        _index, _skill, action, decision = runtime._cautious_wait_action(
            state, directive, FakeController(), modules, reason="lure_and_wait_hidden", enemy=enemy
        )
        self.assertEqual(decision["skill"], "lure_wall_tuck")
        self.assertEqual(decision["reason"], "wall_tuck:lure_and_wait_hidden")
        self.assertNotEqual(getattr(action.raw, "side_move", 0), 0)
        self.assertEqual(runtime._cautious_door_tuck_steps, 1)

        # Budget spent: back to the plain wait.
        runtime._cautious_door_tuck_steps = 6
        _index, _skill, action, decision = runtime._cautious_wait_action(
            state, directive, FakeController(), modules, reason="lure_and_wait_hidden", enemy=enemy
        )
        self.assertEqual(decision["skill"], "lure_and_wait")
    def test_cautious_projectile_combat_evade_fires_when_shootable(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "clear this room safely"})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=8, x_fp=192 * 65536, y_fp=0, type_id=3001, distance_fp=192 * 65536)
            ],
            navigation=SimpleNamespace(back_open=True),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}
        # A shootable projectile enemy under preserve_health now gets an evade-fire
        # (strafe-and-shoot dodges the dodgeable fireball) rather than deferring to the
        # plain strafe path.
        index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual((index, skill), (0, "fire"))
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["skill"], "peek_fire")
        self.assertEqual(decision["reason"], "ambush_projectile_fire_evade")
    def test_cautious_visible_projectile_not_shootable_retreats_under_avoid_damage(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=8, x_fp=192 * 65536, y_fp=0, type_id=3001, distance_fp=192 * 65536)
            ],
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(decision["reason"], "avoid_projectile_threshold")
        self.assertGreaterEqual(runtime._cautious_ambush_window, CAUTIOUS_COVER_AMBUSH_WINDOW)
    def test_complete_level_low_health_projectile_shootable_evades_when_escape_exists(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {"health": 35, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=18, x_fp=192 * 65536, y_fp=0, type_id=3001, distance_fp=192 * 65536)
            ],
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat", "close_visible_contact"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertLess(action.raw.forward_move, 0)
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
        self.assertEqual(decision["reason"], "survival_projectile_evasion")
    def test_complete_level_low_health_projectile_point_blank_no_escape_can_fire(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {"health": 35, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=18, x_fp=96 * 65536, y_fp=0, type_id=3001, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(back_open=False, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat", "close_visible_contact"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["reason"], "survival_projectile_fire_evade")
        self.assertEqual(decision["action"], "fire_evade")
        self.assertEqual(action.duration_tics, 2)
        self.assertEqual(action.raw.buttons, 1)
    def test_complete_level_low_health_projectile_not_shootable_evades(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "beat the level", "objective": "complete_level"})
        runtime._metrics = lambda _state: {"health": 35, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=19, x_fp=192 * 65536, y_fp=0, type_id=3001, distance_fp=192 * 65536)
            ],
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(decision["reason"], "survival_projectile_evasion")
    def test_cautious_visible_far_shootable_hitscan_breaks_los_instead_of_dueling(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=13, x_fp=803 * 65536, y_fp=0, distance_fp=803 * 65536)
            ],
            navigation=SimpleNamespace(back_open=True, direction_probes=[]),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["skill"], "funnel_back_raw")
        self.assertEqual(decision["reason"], "defer_visible_hitscan_to_lure")
        self.assertLess(action.raw.forward_move, 0)
        self.assertNotEqual(getattr(action.raw, "buttons", 0) if getattr(action, "raw", None) else 0, 1)
    def test_cautious_visible_close_hitscan_fires_and_evades_with_raw_ticcmd(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=14, x_fp=96 * 65536, y_fp=0, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(
                back_open=False,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=96 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "peek_fire")
        self.assertEqual(decision["action"], "fire_evade")
        self.assertEqual(action.duration_tics, 2)
        self.assertEqual(action.raw.buttons, 1)
        self.assertNotEqual(action.raw.side_move, 0)
    def test_cautious_visible_close_hitscan_holds_fire_without_cover_profile(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=15, x_fp=96 * 65536, y_fp=0, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=96 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertNotEqual(getattr(action, "action", None), FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["reason"], "hold_fire_visible_hitscan_without_cover")
    def test_cautious_visible_hitscan_holds_fire_at_midrange_without_cover(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_cover_repeat_count = 3
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=15, x_fp=192 * 65536, y_fp=0, distance_fp=192 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=96 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        # No cover profile at midrange (192u): an open-field trade bleeds health, so the
        # agent holds fire and breaks LOS. Open-field return fire is reserved for >=384u
        # where the enemy's hitscan accuracy has decayed the most.
        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["skill"], "funnel_back_raw")
        self.assertEqual(decision["reason"], "hold_fire_visible_hitscan_without_cover")
        self.assertLess(action.raw.forward_move, 0)
    def test_cautious_visible_close_hitscan_fires_when_trapped_without_cover_profile(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=115, x_fp=96 * 65536, y_fp=0, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=96 * 65536,
                back_open=False,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(decision["skill"], "peek_fire")
        self.assertEqual(decision["reason"], "last_resort_close_no_escape")
    def test_cautious_visible_hitscan_does_not_slice_align_without_cover_profile(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6, ACTION_TURN_LEFT=3)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=16, x_fp=96 * 65536, y_fp=96 * 65536, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=96 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        # Pain-lock-close (96u) visible hitscanner under clear_area+avoid_damage: the
        # cautious FSM engages it — square up to convert the duel or break LOS — rather
        # than idling. It must produce a real tactical response (a skill + action), not None.
        self.assertIn(skill, {"retreat", "close_visible_contact"})
        self.assertIsNotNone(getattr(action, "action", None))
    def test_cautious_visible_hitscan_aligns_when_inside_pain_lock_range(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_cover_repeat_count = 3
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=16, x_fp=80 * 65536, y_fp=80 * 65536, distance_fp=112 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=96 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        # Visible hitscan inside threshold range (112u < 128u, clear_area): square up
        # to the target so the duel machinery can convert, rather than resetting the
        # lure. Past 128u the FSM retreats to cover instead — midrange aligning in
        # LOS was a traced damage source.
        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(decision["skill"], "slice_align")
        self.assertEqual(decision["state"], "slice_the_pie")
        self.assertIn(action.action, (FakeAgentPb2.ACTION_TURN_LEFT, FakeAgentPb2.ACTION_TURN_RIGHT))
    def test_cautious_hidden_hitscan_prefires_from_door_jamb(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=10, x_fp=96 * 65536, y_fp=0, line_of_sight=False, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "prefire_peek")
        self.assertEqual(decision["state"], "pre_fire_peek")
        self.assertEqual(decision["cover"], "door_jamb")
        self.assertEqual(action.duration_tics, 2)
        self.assertEqual(action.raw.buttons, 1)
        self.assertIn(action.raw.side_move, {-56, 56})
        self.assertGreater(runtime._cautious_retreat_steps, 0)
    def test_cautious_hidden_hitscan_prefires_at_pistol_distance_from_cover(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=24, x_fp=320 * 65536, y_fp=0, line_of_sight=False, distance_fp=320 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "prefire_peek")
        self.assertEqual(decision["state"], "pre_fire_peek")
        self.assertEqual(decision["dist"], 320)
        self.assertEqual(action.raw.buttons, 1)
    def test_cautious_hidden_far_hitscan_aligns_before_prefire(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=26, x_fp=318 * 65536, y_fp=34 * 65536, line_of_sight=False, distance_fp=320 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "route_progression"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_TURN_LEFT)
        self.assertEqual(decision["skill"], "prefire_align")
        self.assertEqual(decision["state"], "pre_fire_peek")
        self.assertGreater(decision["turn"], 4.0)
    def test_cautious_hidden_hitscan_waits_after_stale_prefire_shots(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._combat_state.shots = 3
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=27, x_fp=320 * 65536, y_fp=0, line_of_sight=False, distance_fp=320 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                    SimpleNamespace(open=False, angle_offset_degrees=90, block_distance_fp=192 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertEqual(decision["skill"], "lure_and_wait")
        self.assertEqual(decision["reason"], "stale_prefire_lure_wait")
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(runtime._cautious_ambush_window, 12)
    def test_cautious_hidden_hitscan_rearms_prefire_after_lure_wait(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._combat_state.shots = 3
        runtime._cautious_lure_wait_steps = 6
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=28, x_fp=320 * 65536, y_fp=0, line_of_sight=False, distance_fp=320 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                    SimpleNamespace(open=False, angle_offset_degrees=90, block_distance_fp=192 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "prefire_peek")
        self.assertEqual(decision["state"], "pre_fire_peek")
        self.assertEqual(runtime._combat_state.shots, 0)
        self.assertEqual(runtime._cautious_lure_wait_steps, 0)
        self.assertEqual(action.duration_tics, 2)
        self.assertEqual(action.raw.buttons, 1)
    def test_cautious_hidden_hitscan_defers_to_planner_until_threshold(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=33, x_fp=960 * 65536, y_fp=0, line_of_sight=False, distance_fp=960 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                use_lines=[SimpleNamespace(nearest_distance_fp=384 * 65536, distance_fp=384 * 65536)],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=192 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}

        self.assertIsNone(runtime._cautious_combat_override(state, directive, FakeController(), modules))
    def test_cautious_hidden_hitscan_holds_ambush_window_instead_of_reentering_threshold(self):
        FakeAgentPb2 = make_agent_pb2(raw=False)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 33
        runtime._cautious_ambush_window = 8
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=33, x_fp=960 * 65536, y_fp=0, line_of_sight=False, distance_fp=960 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                use_lines=[SimpleNamespace(nearest_distance_fp=384 * 65536, distance_fp=384 * 65536)],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=192 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(decision["skill"], "lure_and_wait")
        self.assertEqual(decision["reason"], "ambush_window_hold_hidden")
    def test_cautious_ambush_window_holds_when_enemy_snapshot_temporarily_missing(self):
        FakeAgentPb2 = make_agent_pb2(raw=False)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 33
        runtime._cautious_ambush_window = 8
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(decision["skill"], "lure_and_wait")
        self.assertEqual(decision["reason"], "ambush_window_hold_no_enemy_snapshot")
    def test_cautious_hidden_hitscan_defers_to_planner_for_far_target_at_threshold(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=34, x_fp=960 * 65536, y_fp=0, line_of_sight=False, distance_fp=960 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                use_lines=[SimpleNamespace(nearest_distance_fp=74 * 65536, distance_fp=74 * 65536)],
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=192 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}

        self.assertIsNone(runtime._cautious_combat_override(state, directive, FakeController(), modules))
    def test_cautious_probe_cap_keeps_avoid_damage_in_lure_wait(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._combat_state.shots = 3
        runtime._cautious_probe_steps = 8
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=29, x_fp=320 * 65536, y_fp=0, line_of_sight=False, distance_fp=320 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=96 * 65536,
                back_open=True,
                direction_probes=[SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536)],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "retreat")
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(decision["skill"], "lure_and_wait")
        self.assertEqual(decision["reason"], "stale_prefire_lure_wait")
    def test_cautious_visible_hitscan_breaks_los_when_aligned_but_not_shootable(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=28, x_fp=384 * 65536, y_fp=0, distance_fp=384 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=160 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertLess(action.raw.forward_move, 0)
        self.assertEqual(decision["reason"], "visible_hitscan_break_los_before_align")
        self.assertEqual(decision["skill"], "funnel_back_raw")
    def test_cautious_cover_lock_holds_instead_of_reversing_lateral_strafe(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        enemy = {"id": 31, "threat": "hitscan"}
        first_state = SimpleNamespace(
            navigation=SimpleNamespace(
                back_open=False,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=90, block_distance_fp=96 * 65536),
                    SimpleNamespace(open=False, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            )
        )

        _index, skill, action, decision = runtime._cautious_cover_action(
            first_state, directive, FakeController(), modules, reason="avoid_hitscan_los", enemy=enemy
        )
        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_STRAFE_LEFT)
        self.assertEqual(decision["skill"], "break_los_left")
        self.assertEqual(runtime._cautious_cover_side_lock, 90)

        flipped_state = SimpleNamespace(
            navigation=SimpleNamespace(
                back_open=False,
                direction_probes=[
                    SimpleNamespace(open=False, angle_offset_degrees=90, block_distance_fp=96 * 65536),
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            )
        )
        _index, skill, action, decision = runtime._cautious_cover_action(
            flipped_state, directive, FakeController(), modules, reason="avoid_hitscan_los", enemy=enemy
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(getattr(action, "action", 0), 0)
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(decision["skill"], "hold_cover_lock")
        self.assertEqual(decision["locked_side"], 90)
    def test_cautious_cover_action_arms_ambush_window_for_transient_enemy_loss(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        state = SimpleNamespace(navigation=SimpleNamespace(back_open=True, direction_probes=[]))
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}

        _index, skill, action, decision = runtime._cautious_cover_action(
            state,
            directive,
            FakeController(),
            modules,
            reason="avoid_hitscan_los",
            enemy={"id": 44, "visible": True, "threat": "hitscan"},
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(decision["skill"], "funnel_back")
        self.assertEqual(runtime._cautious_target_id, 44)
        self.assertGreaterEqual(runtime._cautious_ambush_window, CAUTIOUS_COVER_AMBUSH_WINDOW)
    def test_cautious_cover_lock_releases_for_visible_threat(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_STRAFE_LEFT=5, ACTION_STRAFE_RIGHT=6)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._cautious_cover_side_lock = 90
        runtime._cautious_cover_side_lock_steps = 4
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
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

        _index, skill, action, decision = runtime._cautious_cover_action(
            state,
            directive,
            FakeController(),
            modules,
            reason="avoid_hitscan_los",
            enemy={"id": 31, "threat": "hitscan", "visible": True},
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_STRAFE_RIGHT)
        self.assertEqual(decision["skill"], "break_los_right")
        self.assertEqual(runtime._cautious_cover_side_lock, -90)
    def test_cautious_prefire_raw_side_matches_cover_strafe_direction(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=25, x_fp=256 * 65536, y_fp=0, line_of_sight=False, distance_fp=256 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=192 * 65536),
                    SimpleNamespace(open=False, angle_offset_degrees=90, block_distance_fp=192 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}

        _index, _skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(decision["skill"], "prefire_peek")
        self.assertGreater(action.raw.side_move, 0)
    def test_cautious_hidden_hitscan_blind_lures_from_cover_when_prefire_is_not_safe(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=11, x_fp=256 * 65536, y_fp=0, type_id=9, line_of_sight=False, distance_fp=256 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(action.duration_tics, 2)
        self.assertEqual(decision["skill"], "blind_lure_shot")
        self.assertEqual(decision["state"], "lure_and_wait")
        self.assertEqual(decision["cover"], "door_jamb")
        self.assertGreater(runtime._cautious_lure_cooldown, 0)
        self.assertGreater(runtime._cautious_retreat_steps, 0)
    def test_cautious_lure_wait_threshold_ambush_fires_when_target_reaches_jamb(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 21
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=21, x_fp=96 * 65536, y_fp=0, line_of_sight=False, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "threshold_ambush_shot")
        self.assertEqual(decision["state"], "lure_and_wait")
        self.assertEqual(decision["action"], "prefire_threshold")
        self.assertEqual(decision["cover"], "door_jamb")
        self.assertEqual(action.raw.buttons, 1)
        self.assertEqual(action.duration_tics, 4)
        self.assertNotEqual(action.raw.side_move, 0)
        self.assertGreater(runtime._cautious_ambush_window, 0)
    def test_cautious_lure_wait_jiggle_peeks_after_hidden_hold_cap(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 21
        runtime._cautious_retreat_steps = 0
        runtime._cautious_ambush_window = 10
        runtime._cautious_lure_wait_steps = 6
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=21, x_fp=96 * 65536, y_fp=0, line_of_sight=False, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(decision["skill"], "jiggle_peek_probe")
        self.assertEqual(decision["state"], "pre_fire_peek")
        self.assertEqual(decision["action"], "jiggle_side_probe")
        self.assertEqual(decision["reason"], "ambush_window_jiggle_peek")
        self.assertEqual(getattr(action.raw, "buttons", 0), 0)
        self.assertNotEqual(action.raw.side_move, 0)
        self.assertEqual(runtime._cautious_lure_wait_steps, 0)
        self.assertGreater(runtime._cautious_jiggle_peek_steps, 0)
    def test_cautious_jiggle_prefire_bails_when_target_becomes_shootable(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 31
        runtime._cautious_ambush_window = 8
        runtime._cautious_jiggle_peek_steps = 4
        directive = parse_directive({"goal": "clear this room safely", "constraints": ["avoid_damage"]})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": True}  # type: ignore[method-assign]
        # 96u: inside the 128u threshold window — past it the bail shot is refused
        # and the FSM defers to cover instead (midrange exchanges bleed health).
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=31, x_fp=96 * 65536, y_fp=0, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=128 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "jiggle_prefire_shot")
        self.assertEqual(decision["state"], "pre_fire_peek")
        self.assertEqual(decision["action"], "raw_fire_bail")
        self.assertEqual(decision["reason"], "shootable_after_jiggle_peek")
        self.assertEqual(action.raw.buttons, 1)
        self.assertLess(action.raw.forward_move, 0)
        self.assertEqual(runtime._cautious_jiggle_peek_steps, 0)
        self.assertGreater(runtime._cautious_retreat_steps, 0)
        self.assertGreater(runtime._cautious_threshold_cooldown, 0)
    def test_cautious_threshold_cooldown_breaks_los_after_visible_ambush_shot(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_BACKWARD=2)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 21
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        runtime._cautious_threshold_cooldown = 2
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=21, x_fp=96 * 65536, y_fp=0, distance_fp=96 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertLess(action.raw.forward_move, 0)
        self.assertEqual(decision["skill"], "funnel_back_raw")
        self.assertEqual(decision["reason"], "threshold_cooldown_break_los")
    def test_cautious_threshold_cooldown_allows_wounded_finish(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 21
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        runtime._cautious_threshold_cooldown = 2
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=21, x_fp=64 * 65536, y_fp=0, health=5, line_of_sight=False, distance_fp=64 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "fire")
        self.assertEqual(decision["skill"], "threshold_ambush_shot")
        self.assertEqual(action.raw.buttons, 1)
    def test_cautious_recent_hit_window_holds_cover_through_cooldown_under_avoid_damage(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 21
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        runtime._cautious_threshold_cooldown = 2
        runtime._cautious_recent_hit_window = 6
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=21, x_fp=291 * 65536, y_fp=0, health=100, line_of_sight=False, distance_fp=291 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertNotIn(decision["skill"], {"threshold_ambush_shot", "recent_hit_followup_shot"})
        self.assertNotEqual(getattr(getattr(action, "raw", None), "buttons", 0), 1)
    def test_cautious_recent_hit_window_retreats_before_wide_followup_under_avoid_damage(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 21
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        runtime._cautious_threshold_cooldown = 2
        runtime._cautious_recent_hit_window = 6
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=21, x_fp=91 * 65536, y_fp=91 * 65536, health=100, line_of_sight=False, distance_fp=128 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat", "close_visible_contact"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertNotEqual(decision["skill"], "threshold_align")
    def test_cautious_recent_hit_window_holds_hidden_target_without_cover_under_avoid_damage(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 21
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        runtime._cautious_threshold_cooldown = 2
        runtime._cautious_recent_hit_window = 6
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=21, x_fp=44 * 65536, y_fp=46 * 65536, health=100, line_of_sight=False, distance_fp=64 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=96 * 65536,
                back_open=False,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat", "close_visible_contact"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertNotEqual(decision["skill"], "threshold_align")
    def test_cautious_recent_hit_window_retreats_visible_target_without_cover_under_avoid_damage(self):
        FakeAgentPb2 = make_agent_pb2(ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4)

        FakeController = make_controller(3)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 21
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        runtime._cautious_threshold_cooldown = 2
        runtime._cautious_recent_hit_window = 6
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=21, x_fp=44 * 65536, y_fp=46 * 65536, health=100, distance_fp=64 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=96 * 65536,
                back_open=False,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat", "close_visible_contact"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertNotEqual(decision["skill"], "threshold_align")
    def test_cautious_recent_hit_window_breaks_los_without_visible_followup_under_avoid_damage(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 21
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        runtime._cautious_threshold_cooldown = 2
        runtime._cautious_recent_hit_window = 6
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=21, x_fp=128 * 65536, y_fp=0, health=100, distance_fp=128 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=False,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertNotIn(decision["skill"], {"threshold_ambush_shot", "recent_hit_followup_shot"})
        self.assertNotEqual(getattr(getattr(action, "raw", None), "buttons", 0), 1)
    def test_cautious_recent_hit_followup_requires_non_avoid_damage_contract(self):
        FakeAgentPb2 = make_agent_pb2()

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 21
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        runtime._cautious_threshold_cooldown = 2
        runtime._cautious_recent_hit_window = 6
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=21, x_fp=128 * 65536, y_fp=0, health=100, distance_fp=128 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=True,
                front_block_distance_fp=96 * 65536,
                back_open=True,
                direction_probes=[
                    SimpleNamespace(open=True, angle_offset_degrees=-90, block_distance_fp=96 * 65536),
                ],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertNotEqual(decision["skill"], "recent_hit_followup_shot")
        self.assertNotEqual(getattr(getattr(action, "raw", None), "buttons", 0), 1)
    def test_cautious_recent_hit_window_holds_cover_when_no_followup_shot(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._cautious_recent_hit_window = 5
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(navigation=SimpleNamespace(back_open=True, direction_probes=[]))

        _index, skill, action, decision = runtime._cautious_cover_action(
            state,
            directive,
            FakeController(),
            modules,
            reason="kite_and_funnel_retreat",
            enemy={"id": 21, "threat": "hitscan"},
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(getattr(action, "action", 0), 0)
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(decision["skill"], "hold_recent_hit_cover")
    def test_cautious_recent_hit_visible_target_breaks_los_instead_of_holding(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2)

        FakeController = make_controller(1)

        runtime = BrainRuntime()
        runtime._cautious_recent_hit_window = 5
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["retreat"]}
        state = SimpleNamespace(navigation=SimpleNamespace(back_open=True, direction_probes=[]))

        _index, skill, action, decision = runtime._cautious_cover_action(
            state,
            directive,
            FakeController(),
            modules,
            reason="kite_and_funnel_retreat",
            enemy={"id": 21, "threat": "hitscan", "visible": True},
        )

        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(decision["skill"], "funnel_back")
    def test_cautious_lure_wait_aligns_threshold_target_from_cover(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 22
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=22, x_fp=96 * 65536, y_fp=96 * 65536, line_of_sight=False, distance_fp=136 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["close_visible_contact", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "close_visible_contact")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_TURN_LEFT)
        self.assertEqual(decision["skill"], "threshold_align")
        self.assertEqual(decision["state"], "lure_and_wait")
        self.assertEqual(decision["cover"], "door_jamb")
    def test_cautious_lure_wait_keeps_waiting_for_far_threshold_target(self):
        FakeAgentPb2 = make_agent_pb2(raw=False)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._cautious_target_id = 23
        runtime._cautious_retreat_steps = 4
        runtime._cautious_ambush_window = 10
        directive = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=23, x_fp=256 * 65536, y_fp=0, line_of_sight=False, distance_fp=256 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}

        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)

        self.assertEqual(skill, "retreat")
        self.assertEqual(action.duration_tics, 4)
        self.assertEqual(decision["skill"], "lure_and_wait")
        self.assertEqual(decision["reason"], "lure_and_wait_hidden")
    def test_cautious_hidden_cover_actions_require_preserve_health_and_cover(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": False, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=12, x_fp=256 * 65536, y_fp=0, type_id=9, line_of_sight=False, distance_fp=256 * 65536)
            ],
            navigation=SimpleNamespace(
                forward_open=False,
                front_block_distance_fp=16 * 65536,
                back_open=True,
                direction_probes=[],
            ),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}
        combat = parse_directive({"goal": "kill first enemy", "objective": "kill_enemy"})
        self.assertIsNone(runtime._cautious_combat_override(state, combat, FakeController(), modules))

        cautious = parse_directive(
            {"goal": "clear this room safely", "objective": "clear_area", "constraints": ["avoid_damage"]}
        )
        state.navigation.forward_open = True
        state.navigation.front_block_distance_fp = 0
        self.assertFalse(runtime._cautious_cover_ready(state))
        self.assertIsNone(runtime._cautious_combat_override(state, cautious, FakeController(), modules))
    def test_cautious_hitscan_does_not_lure_shot_under_avoid_damage(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_TURN_LEFT=3, ACTION_TURN_RIGHT=4, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "clear this room safely"})
        runtime._metrics = lambda _state: {"health": 100, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=9, x_fp=256 * 65536, y_fp=0, type_id=9, distance_fp=256 * 65536)
            ],
            navigation=SimpleNamespace(back_open=True),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "close_visible_contact", "retreat"]}
        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(decision["skill"], "funnel_back")

        state.enemies[0].object.position.y_fp = 256 * 65536
        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(decision["reason"], "visible_hitscan_break_los_before_align")
    def test_cautious_hitscan_survival_pressure_breaks_los_without_shootable(self):
        FakeAgentPb2 = make_agent_pb2(raw=False, ACTION_BACKWARD=2, ACTION_SHOOT=7)

        FakeController = make_controller(2)

        runtime = BrainRuntime()
        directive = parse_directive({"goal": "kill first enemy", "objective": "kill_enemy"})
        runtime._metrics = lambda _state: {"health": 45, "visible_enemy": True, "shootable": False}  # type: ignore[method-assign]
        state = SimpleNamespace(
            player=SimpleNamespace(object=SimpleNamespace(position=SimpleNamespace(x_fp=0, y_fp=0), angle_degrees=0)),
            enemies=[
                make_enemy(id=9, x_fp=256 * 65536, y_fp=0, type_id=9, distance_fp=256 * 65536)
            ],
            navigation=SimpleNamespace(back_open=True),
        )
        modules = {"agent_pb2": FakeAgentPb2, "SKILL_ACTIONS": ["fire", "retreat"]}
        _index, skill, action, decision = runtime._cautious_combat_override(state, directive, FakeController(), modules)
        self.assertEqual(skill, "retreat")
        self.assertEqual(action.action, FakeAgentPb2.ACTION_BACKWARD)
        self.assertEqual(decision["skill"], "funnel_back")
        self.assertEqual(decision["reason"], "survival_pressure_break_los_without_shot")
        self.assertGreater(runtime._cautious_retreat_steps, 0)


if __name__ == "__main__":
    unittest.main()
