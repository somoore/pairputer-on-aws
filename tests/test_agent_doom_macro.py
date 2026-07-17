"""Hermetic tests for the Agent DOOM macro route-segment executor.

The macro is the throughput fix: when the planner tags a movement with a macro
target ("mt", a point on a known-passable route), the brain drives toward it in
repeated bursts inside the capsule instead of re-running the full plan stack
every 6-14 tics. These tests prove eligibility, multi-burst execution, and the
hand-back guards (damage, threat, target reached).
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))

from brain_runtime import BrainRuntime, FP_UNIT, parse_directive  # noqa: E402
from door_memory import DoorMemory  # noqa: E402
from planner import Point, SpatialPlanner  # noqa: E402


@dataclass
class FakeRawTiccmd:
    forward_move: int = 0
    side_move: int = 0
    angle_turn: int = 0
    buttons: int = 0


@dataclass
class FakeAction:
    action: int = 0
    amount: int = 0
    duration_tics: int = 1
    raw: object | None = None
    keys: list = field(default_factory=list)


class FakeAgentPb2:
    ACTION_FORWARD = 1
    ACTION_BACKWARD = 2
    ACTION_TURN_LEFT = 3
    ACTION_TURN_RIGHT = 4
    ACTION_STRAFE_LEFT = 5
    ACTION_STRAFE_RIGHT = 6
    ACTION_SHOOT = 7
    ACTION_USE = 8
    PlayerAction = FakeAction
    RawTiccmd = FakeRawTiccmd


def fp(units: float) -> int:
    return int(units * FP_UNIT)


def game_state(x_units: float, *, health: int = 100, visible: bool = False) -> SimpleNamespace:
    enemies = []
    if visible:
        enemies.append(
            SimpleNamespace(
                object=SimpleNamespace(
                    id=9,
                    health=20,
                    distance_fp=fp(100),
                    position=SimpleNamespace(x_fp=fp(x_units + 100), y_fp=0, z_fp=0),
                ),
                line_of_sight=True,
            )
        )
    return SimpleNamespace(
        tick=int(x_units),
        level=SimpleNamespace(episode=1, map=1, total_kills=0),
        player=SimpleNamespace(
            object=SimpleNamespace(
                position=SimpleNamespace(x_fp=fp(x_units), y_fp=0, z_fp=0),
                angle_degrees=0.0,
            ),
            health=health,
            kills=0,
            ammo=SimpleNamespace(bullets=50, shells=0, rockets=0, cells=0),
            ready_weapon="WEAPON_PISTOL",
        ),
        enemies=enemies,
        combat=SimpleNamespace(has_shootable_target=visible, target_is_enemy=visible),
        navigation=SimpleNamespace(
            forward_open=True,
            back_open=True,
            left_open=True,
            right_open=True,
            front_block_distance_fp=fp(512),
            direction_probes=[],
            use_lines=[],
        ),
    )


def macro_brain(states: list[SimpleNamespace]) -> tuple[BrainRuntime, list]:
    """BrainRuntime whose _run_action pops from `states`; also disables human polling."""
    brain = BrainRuntime()
    ran: list = []

    def fake_run(stub, action, modules):
        ran.append(action)
        return states.pop(0)

    brain._run_action = fake_run  # type: ignore[method-assign]
    brain._human_active = lambda: False  # type: ignore[method-assign]
    return brain, ran


MODULES = {"agent_pb2": FakeAgentPb2}


def planner_modules():
    return {
        "agent_pb2": FakeAgentPb2,
        "SKILL_ACTIONS": ["recover_stuck", "retreat", "route_progression", "open_use_line", "press_exit"],
        "summarize_action": lambda action: {"action": int(getattr(action, "action", 0) or 0), "raw": {}, "mouse": {}},
    }


class FakeController:
    def action_mask(self, _state):
        return [True, True, True, True, True]


def forward_action(tics: int = 8) -> FakeAction:
    return FakeAction(action=FakeAgentPb2.ACTION_FORWARD, amount=42, duration_tics=tics)


class TestMacroRouteSegment(unittest.TestCase):
    def test_macro_drives_multiple_bursts_to_target(self):
        # 60 units per burst toward a target 300 units out; stops within 44 units.
        states = [game_state(60.0 * i) for i in range(1, 6)]
        brain, ran = macro_brain(states)
        decision = {"source": "spatial_planner", "mt": [fp(300), 0]}
        result = brain._macro_route_segment(
            None, MODULES, game_state(0), forward_action(), decision, max_tics=70
        )
        self.assertIsNotNone(result)
        final_state, tics = result
        self.assertEqual(int(final_state.player.object.position.x_fp), fp(300))
        self.assertEqual(tics, 40)
        self.assertEqual(decision["macro"], {"bursts": 5, "tics": 40})
        self.assertEqual(len(ran), 5)

    def test_macro_stops_on_damage(self):
        states = [game_state(60), game_state(120, health=90), game_state(180)]
        brain, ran = macro_brain(states)
        decision = {"source": "spatial_planner", "mt": [fp(600), 0]}
        result = brain._macro_route_segment(
            None, MODULES, game_state(0), forward_action(), decision, max_tics=70
        )
        self.assertIsNotNone(result)
        _final, tics = result
        self.assertEqual(tics, 16)
        self.assertEqual(len(ran), 2)

    def test_macro_stops_when_threat_appears(self):
        states = [game_state(60), game_state(120, visible=True), game_state(180)]
        brain, ran = macro_brain(states)
        decision = {"source": "spatial_planner", "mt": [fp(600), 0]}
        result = brain._macro_route_segment(
            None, MODULES, game_state(0), forward_action(), decision, max_tics=70
        )
        self.assertIsNotNone(result)
        self.assertEqual(len(ran), 2)

    def test_macro_stops_on_stall(self):
        # Second burst moves under MACRO_STALL_UNITS -> blocked, hand back.
        states = [game_state(60), game_state(62), game_state(120)]
        brain, ran = macro_brain(states)
        decision = {"source": "spatial_planner", "mt": [fp(600), 0]}
        result = brain._macro_route_segment(
            None, MODULES, game_state(0), forward_action(), decision, max_tics=70
        )
        self.assertIsNotNone(result)
        self.assertEqual(len(ran), 2)

    def test_not_eligible_without_macro_target(self):
        brain, ran = macro_brain([])
        result = brain._macro_route_segment(
            None, MODULES, game_state(0), forward_action(), {"source": "spatial_planner"}, max_tics=70
        )
        self.assertIsNone(result)
        self.assertEqual(ran, [])

    def test_not_eligible_with_visible_threat_at_start(self):
        brain, ran = macro_brain([])
        decision = {"source": "spatial_planner", "mt": [fp(600), 0]}
        result = brain._macro_route_segment(
            None, MODULES, game_state(0, visible=True), forward_action(), decision, max_tics=70
        )
        self.assertIsNone(result)
        self.assertEqual(ran, [])

    def test_not_eligible_for_fire_or_use_actions(self):
        brain, _ran = macro_brain([])
        decision = {"source": "spatial_planner", "mt": [fp(600), 0]}
        for act in (FakeAgentPb2.ACTION_SHOOT, FakeAgentPb2.ACTION_USE, FakeAgentPb2.ACTION_BACKWARD):
            action = FakeAction(action=act, duration_tics=8)
            self.assertIsNone(
                brain._macro_route_segment(None, MODULES, game_state(0), action, decision, max_tics=70)
            )

    def test_raw_steer_forward_is_eligible(self):
        states = [game_state(60.0 * i) for i in range(1, 6)]
        brain, ran = macro_brain(states)
        action = FakeAction(duration_tics=8, raw=FakeRawTiccmd(forward_move=50, angle_turn=0))
        decision = {"source": "spatial_planner", "mt": [fp(300), 0]}
        result = brain._macro_route_segment(None, MODULES, game_state(0), action, decision, max_tics=70)
        self.assertIsNotNone(result)
        self.assertEqual(len(ran), 5)
        # Re-steered follow-up bursts carry a raw forward ticcmd aimed at the target.
        self.assertGreater(int(ran[1].raw.forward_move), 0)


class TestPlannerMacroTagging(unittest.TestCase):
    def _planner(self, lines):
        snap = SimpleNamespace(
            episode=1,
            map=1,
            digest=1,
            vertices=[
                SimpleNamespace(id=0, x_fp=fp(128), y_fp=fp(-128)),
                SimpleNamespace(id=1, x_fp=fp(128), y_fp=fp(128)),
            ],
            lines=lines,
            sectors=[],
            things=[],
            truncated=False,
            bbox_left_fp=fp(-64),
            bbox_right_fp=fp(384),
            bbox_bottom_fp=fp(-256),
            bbox_top_fp=fp(256),
        )
        return SpatialPlanner(snap, cell_units=96)

    def _line(self, idx, **kw):
        defaults = dict(
            v1=0, v2=1, special=0, tag=0, front_sector=-1, back_sector=-1,
            two_sided=False, passable=False, blocking=True, sight_blocking=True,
            door=False, use_trigger=False, walk_trigger=False, exit=False,
        )
        defaults.update(kw)
        return SimpleNamespace(id=idx, **defaults)

    def test_turn_or_forward_tags_macro_target_on_non_door_route(self):
        planner = self._planner([self._line(10)])
        player = {"point": Point(0, 0), "angle": 0.0}
        action = planner._turn_or_forward(
            player, Point(fp(300), 0), FakeAgentPb2,
            skill="route_progression", detail={"skill": "frontier_sector_route"},
        )
        self.assertEqual(action.detail.get("mt"), [fp(300), 0])

    def test_turn_or_forward_skips_macro_target_on_door_route(self):
        planner = self._planner([self._line(10, door=True, special=1, two_sided=True)])
        player = {"point": Point(0, 0), "angle": 0.0}
        action = planner._turn_or_forward(
            player, Point(fp(300), 0), FakeAgentPb2,
            skill="route_progression", detail={"skill": "frontier_sector_route"},
            door_line_id=10,
        )
        self.assertNotIn("mt", action.detail)

    def test_door_memory_unaffected_smoke(self):
        # Tagging must not disturb the action itself.
        planner = self._planner([self._line(10)])
        player = {"point": Point(0, 0), "angle": 0.0}
        action = planner._turn_or_forward(
            player, Point(fp(300), 0), FakeAgentPb2,
            skill="route_progression", detail={"skill": "planner_explore"},
        )
        DoorMemory()  # import smoke
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_FORWARD)


class TestRepeatedUseEscape(unittest.TestCase):
    def test_repeated_failed_use_forces_recovery_before_next_use(self):
        runtime = BrainRuntime()
        modules = planner_modules()
        directive = parse_directive({"goal": "race to the exit without killing anyone"})
        controller = FakeController()
        state = game_state(120)
        action = FakeAction(action=FakeAgentPb2.ACTION_USE, amount=1, duration_tics=4)
        decision = {
            "source": "spatial_planner",
            "planner_skill": "press_exit",
            "skill": "remembered_exit_line",
            "line_id": 330,
            "line": 330,
        }

        for _ in range(3):
            runtime._record_planner_outcome(state, state, action, decision, {}, modules)

        escape = runtime._repeated_failed_use_escape(state, directive, controller, modules, action, decision)

        self.assertIsNotNone(escape)
        _index, skill, escape_action, escape_decision = escape
        self.assertEqual(skill, "recover_stuck")
        self.assertEqual(escape_decision["skill"], "repeated_failed_use_escape")
        self.assertEqual(escape_decision["line_id"], 330)
        self.assertNotEqual(int(getattr(escape_action, "action", 0) or 0), FakeAgentPb2.ACTION_USE)
        self.assertEqual(runtime._last_plan["line"], 330)

    def test_failed_use_tracker_resets_after_movement_progress(self):
        runtime = BrainRuntime()
        modules = planner_modules()
        stuck_state = game_state(120)
        moved_state = game_state(180)
        action = FakeAction(action=FakeAgentPb2.ACTION_USE, amount=1, duration_tics=4)
        decision = {
            "source": "spatial_planner",
            "planner_skill": "press_exit",
            "skill": "remembered_exit_line",
            "line_id": 330,
        }

        for _ in range(2):
            runtime._record_planner_outcome(stuck_state, stuck_state, action, decision, {}, modules)
        self.assertEqual(runtime._failed_use_count, 2)

        runtime._record_planner_outcome(stuck_state, moved_state, action, decision, {}, modules)

        self.assertEqual(runtime._failed_use_count, 0)
        self.assertIsNone(runtime._failed_use_key)


if __name__ == "__main__":
    unittest.main()
