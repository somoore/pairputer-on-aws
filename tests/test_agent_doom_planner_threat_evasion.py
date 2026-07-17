"""Threat-aware route pricing, no-kill/speedrun evasion, and barrel targeting for Agent DOOM's spatial planner."""

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
    Point,
    PortalEdge,
    ROUTE_THREAT_REFUSE_MULT,
    Route,
    RouteStep,
    SpatialPlanner,
    THREAT_ROUTE_CONFIDENT_LOW_TIER_CAP,
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
    thing,
    enemy_state,
    probe,
)


class TestAgentDoomPlannerThreatEvasion(unittest.TestCase):
    def test_hidden_enemy_does_not_fire_without_los(self):
        snap = snapshot(
            [
                vertex(0, 128, -128),
                vertex(1, 128, 128),
            ],
            [line(10, 0, 1)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        action = planner.objective_action(
            state(0, 0, 0, enemy=(256, 0), shootable=False),
            ("shoot", "find_enemy"),
            FakeAgentPb2,
            DoorMemory(),
        )
        self.assertIsNotNone(action)
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_SHOOT)
    def test_no_kill_sector_route_penalizes_enemy_sectors(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._portal_graph = {
            1: [
                PortalEdge(1, 2, 10, Point(fp(64), fp(0)), 0, True, False, False, False, False, 1.0),
                PortalEdge(1, 4, 12, Point(fp(0), fp(64)), 0, True, False, False, False, False, 4.0),
            ],
            2: [PortalEdge(2, 3, 11, Point(fp(128), fp(0)), 0, True, False, False, False, False, 1.0)],
            4: [PortalEdge(4, 3, 13, Point(fp(128), fp(64)), 0, True, False, False, False, False, 4.0)],
        }
        direct = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(direct)
        self.assertEqual([edge.dst for edge in direct], [2, 3])
        planner._avoid_sector_ids = {2}
        safe = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(safe)
        self.assertEqual([edge.dst for edge in safe], [4, 3])
    def test_threat_aware_sector_route_penalizes_hitscan_los(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner.sectors = {
            1: SimpleNamespace(center=Point(fp(0), fp(0)), damaging=False),
            2: SimpleNamespace(center=Point(fp(128), fp(0)), damaging=False),
            3: SimpleNamespace(center=Point(fp(256), fp(0)), damaging=False),
            4: SimpleNamespace(center=Point(fp(128), fp(192)), damaging=False),
        }
        planner._portal_graph = {
            1: [
                PortalEdge(1, 2, 10, Point(fp(64), fp(0)), 0, True, False, False, False, False, 1.0),
                PortalEdge(1, 4, 12, Point(fp(64), fp(96)), 0, True, False, False, False, False, 2.0),
            ],
            2: [PortalEdge(2, 3, 11, Point(fp(192), fp(0)), 0, True, False, False, False, False, 1.0)],
            4: [PortalEdge(4, 3, 13, Point(fp(192), fp(96)), 0, True, False, False, False, False, 2.0)],
        }
        direct = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(direct)
        self.assertEqual([edge.dst for edge in direct], [2, 3])
        planner._threats = [{"id": 7, "point": Point(fp(128), fp(0)), "threat": "hitscan"}]
        planner.has_line_of_sight = lambda point, _enemy: point.y == 0  # type: ignore[method-assign]
        safer = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(safer)
        self.assertEqual([edge.dst for edge in safer], [4, 3])
    def test_complete_level_routes_populate_threat_metadata(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(128, 0))

        planner.objective_action(st, ("complete_level", "exit", "use", "explore"), FakeAgentPb2, DoorMemory())

        self.assertEqual(planner._threat_cost_mode, "route")
        self.assertEqual(len(planner._threats), 1)

        planner.objective_action(st, ("exit", "use", "explore", "no_kills"), FakeAgentPb2, DoorMemory())

        self.assertEqual(planner._threat_cost_mode, "no_kill")
        self.assertEqual(len(planner._threats), 1)
    def test_static_barrels_are_available_from_wad_things(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(21, 2035, 256, 0)])
        planner = SpatialPlanner(snap, cell_units=96)

        self.assertEqual(len(planner.barrel_items), 1)
        self.assertEqual(planner.barrel_items[0].type_id, 2035)
        self.assertEqual(planner.barrel_items[0].point, Point(fp(256), 0))
    def test_complete_level_shoots_safe_barrel_when_enemy_is_in_blast_radius(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(21, 2035, 256, 0)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 0, [{"id": 77, "pos": (320, 0), "los": True}])
        st.player.health = 65
        st.player.ready_weapon = 1
        st.player.ammo = SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0)

        action = planner.objective_action(st, ("complete_level", "exit", "use", "explore"), FakeAgentPb2, DoorMemory())

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "fire")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(action.detail["skill"], "barrel_shot")
        self.assertEqual(action.detail["barrel"], 21)
        self.assertEqual(action.detail["enemies_near"], 1)
    def test_complete_level_does_not_open_with_speculative_full_health_barrel_shot(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(21, 2035, 256, 0)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 0, [{"id": 77, "pos": (320, 0), "los": True}])
        st.player.health = 100
        st.player.ready_weapon = 1
        st.player.ammo = SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0)

        action = planner._barrel_shot_action(st, planner._player(st), FakeAgentPb2)

        self.assertIsNone(action)
    def test_clear_area_attack_can_use_safe_barrel_at_full_health(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(21, 2035, 256, 0)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 0, [{"id": 77, "pos": (320, 0), "los": True}])
        st.player.health = 100
        st.player.ready_weapon = 1
        st.player.ammo = SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0)

        action = planner.objective_action(st, ("attack", "find_enemy"), FakeAgentPb2, DoorMemory())

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "fire")
        self.assertEqual(action.detail["skill"], "barrel_shot")
        self.assertEqual(action.detail["barrel"], 21)
    def test_preserve_health_still_allows_safe_barrel_shots(self):
        # Deliberately pinned BOTH ways in history: banning barrel play under
        # preserve_health was tried and adjudicated at 2/20 vs the 30% baseline
        # (detonations convert kills; slower kills bleed more hitscan damage
        # than the blast risk costs). Safety comes from the chain guard, not a
        # goal-level ban — a SAFE barrel shot stays available.
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(21, 2035, 256, 0)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 0, [{"id": 77, "pos": (320, 0), "los": True}])
        st.player.health = 100
        st.player.ready_weapon = 1
        st.player.ammo = SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0)

        action = planner.objective_action(st, ("attack", "find_enemy", "preserve_health"), FakeAgentPb2, DoorMemory())

        self.assertIsNotNone(action)
        self.assertEqual(action.detail.get("skill"), "barrel_shot")

    def test_barrel_shot_refused_when_chain_reaches_player(self):
        # Barrels chain-detonate: the target (256u, safely outside blast) chains
        # into an intermediate barrel at 128u (within 144u blast of the target),
        # which can blast the player (128u < 160u guard) — refuse the shot.
        snap = snapshot(
            [vertex(0, 0, 0)],
            [],
            things=[thing(21, 2035, 256, 0), thing(22, 2035, 128, 0)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 0, [{"id": 77, "pos": (320, 0), "los": True}])
        st.player.health = 65
        st.player.ready_weapon = 1
        st.player.ammo = SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0)

        action = planner._barrel_shot_action(st, planner._player(st), FakeAgentPb2)

        self.assertIsNone(action)

    def test_barrel_shot_allowed_when_nearby_barrel_is_not_in_the_chain(self):
        # A bystander barrel near the player (120u) but OUTSIDE the target's
        # chain (283u from it) must not veto the shot — the blanket
        # any-barrel-near-me guard was adjudicated at 1/10 vs the 30% baseline.
        snap = snapshot(
            [vertex(0, 0, 0)],
            [],
            things=[thing(21, 2035, 256, 0), thing(22, 2035, 0, 120)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 0, [{"id": 77, "pos": (320, 0), "los": True}])
        st.player.health = 65
        st.player.ready_weapon = 1
        st.player.ammo = SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0)

        action = planner._barrel_shot_action(st, planner._player(st), FakeAgentPb2)

        self.assertIsNotNone(action)
        self.assertEqual(action.detail["barrel"], 21)

    def test_barrel_shot_does_not_steer_away_from_route_to_align(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(21, 2035, 256, 0)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 90, [{"id": 77, "pos": (320, 0), "los": True}])
        st.player.health = 65
        st.player.ready_weapon = 1
        st.player.ammo = SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0)

        action = planner._barrel_shot_action(st, planner._player(st), FakeAgentPb2)

        self.assertIsNone(action)
    def test_complete_level_does_not_shoot_barrel_inside_player_blast_radius(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(21, 2035, 128, 0)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 0, [{"id": 77, "pos": (160, 0), "los": True}])
        st.player.health = 65
        st.player.ready_weapon = 1
        st.player.ammo = SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0)

        action = planner.objective_action(st, ("complete_level",), FakeAgentPb2, DoorMemory())

        self.assertIsNone(action)
    def test_complete_level_no_kill_does_not_shoot_barrel(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(21, 2035, 256, 0)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 0, [{"id": 77, "pos": (320, 0), "los": True}])
        st.player.health = 65
        st.player.ready_weapon = 1
        st.player.ammo = SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0)

        action = planner.objective_action(st, ("complete_level", "no_kills", "avoid_combat"), FakeAgentPb2, DoorMemory())

        self.assertIsNone(action)
    def test_barrel_shot_records_one_fired_attempt(self):
        snap = snapshot([vertex(0, 0, 0)], [], things=[thing(21, 2035, 256, 0)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 0, [{"id": 77, "pos": (320, 0), "los": True}])
        st.player.health = 65
        st.player.ready_weapon = 1
        st.player.ammo = SimpleNamespace(bullets=12, shells=0, rockets=0, cells=0)

        first = planner.objective_action(st, ("complete_level",), FakeAgentPb2, DoorMemory())
        second = planner.objective_action(st, ("complete_level",), FakeAgentPb2, DoorMemory())

        self.assertIsNotNone(first)
        self.assertEqual(first.detail["skill"], "barrel_shot")
        self.assertIsNone(second)
    def test_sector_route_penalizes_threat_cluster_on_portal_midpoint(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner.sectors = {
            1: SimpleNamespace(center=Point(fp(0), fp(800)), damaging=False),
            2: SimpleNamespace(center=Point(fp(128), fp(800)), damaging=False),
            3: SimpleNamespace(center=Point(fp(256), fp(800)), damaging=False),
            4: SimpleNamespace(center=Point(fp(128), fp(1600)), damaging=False),
        }
        planner._portal_graph = {
            1: [
                PortalEdge(1, 2, 10, Point(fp(64), fp(0)), 0, True, False, False, False, False, 1.0),
                PortalEdge(1, 4, 12, Point(fp(64), fp(1600)), 0, True, False, False, False, False, 2.0),
            ],
            2: [PortalEdge(2, 3, 11, Point(fp(128), fp(0)), 0, True, False, False, False, False, 1.0)],
            4: [PortalEdge(4, 3, 13, Point(fp(192), fp(1600)), 0, True, False, False, False, False, 2.0)],
        }
        direct = planner._sector_route(1, [3], DoorMemory())
        self.assertIsNotNone(direct)
        self.assertEqual([edge.dst for edge in direct], [2, 3])

        planner._threats = [
            {"id": 7, "point": Point(fp(64), fp(0)), "threat": "projectile"},
            {"id": 8, "point": Point(fp(128), fp(0)), "threat": "projectile"},
        ]
        planner._threat_mult_cache.clear()
        safer = planner._sector_route(1, [3], DoorMemory())

        self.assertIsNotNone(safer)
        self.assertEqual([edge.dst for edge in safer], [4, 3])
    def test_portal_route_annotates_lethal_threshold_step(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._threats = [{"id": 7, "point": Point(fp(64), fp(0)), "threat": "hitscan"}]
        planner.has_line_of_sight = lambda _point, _enemy: True  # type: ignore[method-assign]
        route = [PortalEdge(1, 2, 265, Point(fp(64), fp(0)), 0, True, False, False, False, False, 1.0)]

        action = planner._portal_route_action(
            {"point": Point(fp(0), fp(0)), "angle": 0},
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="route_progression",
            detail={"skill": "sector_route_to_use_line", "route": 5},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertEqual(action.door_line_id, 265)
        self.assertEqual(action.detail["route_step_kind"], "portal")
        self.assertEqual(action.detail["route_step_line"], 265)
        self.assertEqual(action.detail["route_step_sector"], 2)
        self.assertGreaterEqual(action.detail["route_step_threat_mult"], 30.0)
    def test_no_kill_route_annotates_without_firing(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._threat_cost_mode = "no_kill"
        planner._threats = [{"id": 7, "point": Point(fp(64), fp(0)), "threat": "hitscan"}]
        planner.has_line_of_sight = lambda _point, _enemy: True  # type: ignore[method-assign]
        route = [PortalEdge(1, 2, 265, Point(fp(64), fp(0)), 0, True, False, False, False, False, 1.0)]

        action = planner._portal_route_action(
            {"point": Point(fp(0), fp(0)), "angle": 0},
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="route_progression",
            detail={"skill": "sector_route_to_use_line", "route": 5},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(action.detail["route_step_kind"], "portal")
        self.assertGreaterEqual(action.detail["route_step_threat_mult"], 30.0)
    def test_navcell_route_annotates_lethal_next_step(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._threats = [{"id": 7, "point": Point(fp(128), fp(0)), "threat": "hitscan"}]
        planner.has_line_of_sight = lambda _point, _enemy: True  # type: ignore[method-assign]
        route = Route([RouteStep(Point(fp(128), fp(0)))], cost=1.0)

        action = planner._route_action(
            {"point": Point(fp(0), fp(0)), "angle": 0},
            route,
            FakeAgentPb2,
            DoorMemory(),
            skill="route_progression",
            detail={"skill": "navcell_to_portal"},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(action.detail["route_step_kind"], "nav")
        self.assertEqual(action.detail["route_step_x"], fp(128))
        self.assertEqual(action.detail["route_step_y"], fp(0))
        self.assertGreaterEqual(action.detail["route_step_threat_mult"], 30.0)
    def test_threat_aware_navcell_route_multiplies_enemy_proximity_cost(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        near = Point(fp(128), fp(0))
        far = Point(fp(128), fp(800))
        planner._threats = [{"id": 7, "point": Point(fp(128), fp(0)), "threat": "projectile"}]
        self.assertGreaterEqual(planner._point_threat_multiplier(near), 10.0)
        self.assertEqual(planner._point_threat_multiplier(far), 1.0)
    def test_threat_density_multiplier_accumulates_nearby_enemies(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        target = Point(fp(128), fp(0))
        planner._threats = [{"id": 7, "point": Point(fp(128), fp(0)), "threat": "projectile"}]
        single = planner._point_threat_multiplier(target)

        planner._threats = [
            {"id": 7, "point": Point(fp(128), fp(0)), "threat": "projectile"},
            {"id": 8, "point": Point(fp(160), fp(0)), "threat": "projectile"},
            {"id": 9, "point": Point(fp(96), fp(0)), "threat": "projectile"},
        ]
        planner._threat_mult_cache.clear()
        cluster = planner._point_threat_multiplier(target)

        planner._threat_cost_mode = "no_kill"
        planner._threat_mult_cache.clear()
        no_kill_cluster = planner._point_threat_multiplier(target)

        self.assertGreater(cluster, single)
        self.assertGreater(no_kill_cluster, cluster)
        self.assertEqual(planner._point_threat_multiplier(Point(fp(128), fp(2000))), 1.0)
    def test_threat_aware_navcell_route_multiplies_hitscan_los_cost(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._threats = [{"id": 7, "point": Point(fp(512), fp(0)), "threat": "hitscan"}]
        self.assertGreaterEqual(planner._point_threat_multiplier(Point(fp(0), fp(0))), 50.0)
    def test_confident_armed_route_caps_low_tier_hitscan_threat(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._route_confident = True
        planner._threats = [
            {"id": 7, "point": Point(fp(512), fp(0)), "threat": "hitscan", "type_id": 3004},
            {"id": 8, "point": Point(fp(544), fp(0)), "threat": "hitscan", "type_id": 3004},
        ]
        planner.has_line_of_sight = lambda _point, _enemy: True  # type: ignore[method-assign]

        multiplier = planner._point_threat_multiplier(Point(fp(0), fp(0)))

        self.assertEqual(multiplier, THREAT_ROUTE_CONFIDENT_LOW_TIER_CAP)
        self.assertLess(multiplier, ROUTE_THREAT_REFUSE_MULT)
    def test_confident_armed_route_does_not_cap_shotgunner_threat(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._route_confident = True
        planner._threats = [{"id": 7, "point": Point(fp(512), fp(0)), "threat": "hitscan", "type_id": 9}]
        planner.has_line_of_sight = lambda _point, _enemy: True  # type: ignore[method-assign]

        self.assertGreaterEqual(planner._point_threat_multiplier(Point(fp(0), fp(0))), ROUTE_THREAT_REFUSE_MULT)
    def test_sector_route_penalizes_congested_passable_portal(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._portal_graph = {
            1: [
                PortalEdge(1, 2, 10, Point(fp(64), fp(0)), 0, True, False, False, False, False, 1.0),
                PortalEdge(1, 4, 12, Point(fp(0), fp(64)), 0, True, False, False, False, False, 4.0),
            ],
            2: [PortalEdge(2, 3, 11, Point(fp(128), fp(0)), 0, True, False, False, False, False, 1.0)],
            4: [PortalEdge(4, 3, 13, Point(fp(128), fp(64)), 0, True, False, False, False, False, 4.0)],
        }
        memory = DoorMemory()
        memory.record_route_contact(10)
        route = planner._sector_route(1, [3], memory)
        self.assertIsNotNone(route)
        self.assertEqual([edge.dst for edge in route], [4, 3])
        self.assertFalse(memory.is_blocked(10))
    def test_no_kill_exit_evades_close_visible_enemy_with_raw_strafe(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(64, 0))
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=160),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertEqual(action.detail["skill"], "no_kill_route_evasion")
        self.assertEqual(action.detail["action"], "strafe_past")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertGreater(action.action.raw.side_move, 0)
        self.assertEqual(action.action.buttons if hasattr(action.action, "buttons") else 0, 0)
    def test_no_kill_exit_sprints_through_distant_hitscan_los_without_firing(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(512, 0))
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=256),
                probe(90, open=True, distance=160),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "route_progression")
        self.assertEqual(action.detail["skill"], "no_kill_route_evasion")
        self.assertEqual(action.detail["action"], "exposure_sprint")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertNotEqual(action.action.raw.side_move, 0)
        self.assertEqual(action.action.raw.buttons, 0)
    def test_no_kill_exit_breaks_hitscan_los_when_health_is_low(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(512, 0))
        st.player.health = 52
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=256),
                probe(90, open=True, distance=160),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "no_kill_route_evasion")
        self.assertEqual(action.detail["action"], "break_los_low_health")
        self.assertEqual(action.detail["hp"], 52)
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertNotEqual(action.action.raw.side_move, 0)
        self.assertNotEqual(action.detail["action"], "exposure_sprint")
    def test_no_kill_exit_breaks_los_when_hurt_before_exit_commit_range(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, 900, -64), vertex(1, 900, 64)],
                [line(330, 0, 1, special=11, exit=True, use_trigger=True)],
            ),
            cell_units=96,
        )
        st = state(0, 0, 0, enemy=(512, 0))
        st.player.health = 28
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=256),
                probe(90, open=True, distance=160),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "no_kill_route_evasion")
        self.assertEqual(action.detail["action"], "break_los_low_health")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertNotEqual(action.action.raw.side_move, 0)
    def test_no_kill_exit_keeps_sprinting_inside_exit_commit_range(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, 320, -64), vertex(1, 320, 64)],
                [line(330, 0, 1, special=11, exit=True, use_trigger=True)],
            ),
            cell_units=96,
        )
        st = state(0, 0, 0, enemy=(512, 0))
        st.player.health = 28
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=256),
                probe(90, open=True, distance=160),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "no_kill_route_evasion")
        self.assertEqual(action.detail["action"], "exposure_sprint")
    def test_speedrun_no_kill_ignores_nonblocking_close_enemy(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(64, 0))
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=160),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        player = planner._player(st)
        self.assertIsNotNone(player)
        action = planner._no_kill_route_evasion(st, player, FakeRawAgentPb2, blocking_only=True)
        self.assertIsNone(action)
    def test_speedrun_no_kill_keeps_racing_when_health_is_only_moderately_low(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(512, 0))
        st.player.health = 64
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=160),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        player = planner._player(st)
        self.assertIsNotNone(player)
        action = planner._no_kill_route_evasion(st, player, FakeRawAgentPb2, blocking_only=True)
        self.assertIsNone(action)
    def test_speedrun_no_kill_keeps_committing_when_low_health_inside_exit_range(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, 320, -64), vertex(1, 320, 64)],
                [line(330, 0, 1, special=11, exit=True, use_trigger=True)],
            ),
            cell_units=96,
        )
        st = state(0, 0, 0, enemy=(512, 0))
        st.player.health = 42
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=160),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        player = planner._player(st)
        self.assertIsNotNone(player)
        action = planner._no_kill_route_evasion(st, player, FakeRawAgentPb2, blocking_only=True)
        self.assertIsNone(action)
    def test_speedrun_no_kill_breaks_los_inside_exit_range_when_shootable_and_hurt(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, 320, -64), vertex(1, 320, 64)],
                [line(330, 0, 1, special=11, exit=True, use_trigger=True)],
            ),
            cell_units=96,
        )
        st = state(0, 0, 0, enemy=(512, 0), shootable=True)
        st.player.health = 64
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=160),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        player = planner._player(st)
        self.assertIsNotNone(player)
        action = planner._no_kill_route_evasion(st, player, FakeRawAgentPb2, blocking_only=True)
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "break_los_low_health")
        self.assertEqual(action.detail["hp"], 64)
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertNotEqual(action.action.raw.side_move, 0)
    def test_speedrun_no_kill_breaks_los_inside_exit_range_when_close_visible_and_hurt(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, 320, -64), vertex(1, 320, 64)],
                [line(330, 0, 1, special=11, exit=True, use_trigger=True)],
            ),
            cell_units=96,
        )
        st = state(0, 0, 0, enemy=(256, 0), shootable=False)
        st.player.health = 42
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=160),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        player = planner._player(st)
        self.assertIsNotNone(player)
        action = planner._no_kill_route_evasion(st, player, FakeRawAgentPb2, blocking_only=True)
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "break_los_low_health")
        self.assertEqual(action.detail["hp"], 42)
    def test_speedrun_no_kill_avoids_forward_panic_when_hurt_far_from_exit(self):
        planner = SpatialPlanner(
            snapshot(
                [vertex(0, 1200, -64), vertex(1, 1200, 64)],
                [line(330, 0, 1, special=11, exit=True, use_trigger=True)],
            ),
            cell_units=96,
        )
        st = state(0, 0, 0, enemy=(256, 0), shootable=False)
        st.player.health = 42
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=160),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        player = planner._player(st)
        self.assertIsNotNone(player)
        action = planner._no_kill_route_evasion(st, player, FakeRawAgentPb2, blocking_only=True)
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "break_los_low_health")
        self.assertEqual(action.detail["mode"], "low_health_lateral")
        self.assertLessEqual(action.action.raw.forward_move, 0)
        self.assertNotEqual(action.action.raw.side_move, 0)
    def test_no_kill_exit_evades_blocking_enemy_with_pure_side_step(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(64, 0))
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=320),
                probe(90, open=False, distance=0),
            ],
            forward_open=False,
            front_block_distance_fp=fp(64),
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["mode"], "side_step")
        self.assertIsNotNone(action.action.raw)
        self.assertEqual(action.action.raw.forward_move, 0)
        self.assertLess(action.action.raw.side_move, 0)
    def test_no_kill_exit_baits_very_close_frontal_blocker(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(48, 0))
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=320),
                probe(90, open=True, distance=320),
            ],
            forward_open=False,
            front_block_distance_fp=fp(48),
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "bait_back")
        self.assertEqual(action.action.action, FakeRawAgentPb2.ACTION_BACKWARD)
        self.assertGreaterEqual(action.action.amount, 42)
    def test_no_kill_exit_runs_past_close_enemy_when_forward_route_is_open(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(48, 0))
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=False, distance=0),
                probe(90, open=False, distance=0),
            ],
            forward_open=True,
            front_block_distance_fp=fp(192),
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "no_kill_route_evasion")
        self.assertEqual(action.detail["action"], "run_past")
        self.assertEqual(action.detail["mode"], "forward_rush")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertEqual(action.action.raw.buttons, 0)
    def test_no_kill_exit_sidestep_close_blocker_when_critically_hurt(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(48, 0))
        st.player.health = 24
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=False, distance=0),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(96),
            back_open=False,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "no_kill_route_evasion")
        self.assertEqual(action.detail["action"], "panic_sidestep_close_blocker")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, 0)
        self.assertGreater(abs(action.action.raw.side_move), action.action.raw.forward_move)
        self.assertEqual(action.action.raw.buttons, 0)
    def test_no_kill_close_blocker_sidestep_cools_down_after_short_burst(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(48, 0))
        st.player.health = 24
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=False, distance=0),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(96),
            back_open=False,
        )

        actions = [
            planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
            for _ in range(4)
        ]

        for action in actions[:3]:
            self.assertIsNotNone(action)
            self.assertEqual(action.detail["action"], "panic_sidestep_close_blocker")
        if actions[3] is not None:
            self.assertNotEqual(actions[3].detail.get("action"), "panic_sidestep_close_blocker")
    def test_no_kill_exit_uses_forward_biased_panic_run_when_critically_hurt(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(128, 0))
        st.player.health = 24
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=False, distance=0),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(96),
            back_open=False,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "no_kill_route_evasion")
        self.assertEqual(action.detail["action"], "panic_run_past")
        self.assertIsNotNone(action.action.raw)
        self.assertGreater(action.action.raw.forward_move, abs(action.action.raw.side_move))
        self.assertEqual(action.action.raw.buttons, 0)
    def test_no_kill_exit_forward_panic_bypasses_evasion_cooldown(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._no_kill_evasion_cooldown = 5
        st = state(0, 0, 0, enemy=(128, 0))
        st.player.health = 24
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=False, distance=0),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(96),
            back_open=False,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "panic_run_past")
        self.assertGreater(action.action.raw.forward_move, abs(action.action.raw.side_move))
        self.assertEqual(planner._no_kill_evasion_cooldown, 0)
    def test_no_kill_exit_close_blocker_respects_evasion_cooldown(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._no_kill_evasion_cooldown = 5
        st = state(0, 0, 0, enemy=(48, 24))
        st.player.health = 24
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=False, distance=0),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(96),
            back_open=False,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        if action is not None:
            self.assertNotEqual(action.detail.get("action"), "panic_sidestep_close_blocker")
        self.assertEqual(planner._no_kill_evasion_cooldown, 4)
    def test_no_kill_exit_critical_side_escape_when_forward_blocked(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(112, 24))
        st.player.health = 17
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=False, distance=16),
                probe(90, open=True, distance=96),
            ],
            forward_open=False,
            front_block_distance_fp=fp(32),
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["skill"], "no_kill_route_evasion")
        self.assertEqual(action.detail["action"], "panic_escape_side")
        self.assertIsNotNone(action.action.raw)
        self.assertLess(action.action.raw.forward_move, 0)
        self.assertGreaterEqual(abs(action.action.raw.side_move), 62)
        self.assertGreater(action.action.raw.angle_turn, 0)
        self.assertEqual(action.action.raw.buttons, 0)
    def test_speedrun_no_kill_does_not_bait_when_exact_forward_clearance_is_open(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(55, 0))
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=320),
                probe(90, open=True, distance=320),
            ],
            forward_open=True,
            front_block_distance_fp=fp(96),
            back_open=True,
        )
        player = planner._player(st)
        self.assertIsNotNone(player)
        action = planner._no_kill_route_evasion(st, player, FakeRawAgentPb2, blocking_only=True)
        self.assertIsNone(action)
    def test_no_kill_exit_baits_point_blank_blocker_during_cooldown(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        planner._no_kill_evasion_cooldown = 5
        st = state(0, 0, 0, enemy=(48, 0))
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=320),
                probe(90, open=True, distance=320),
            ],
            forward_open=False,
            front_block_distance_fp=fp(48),
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["action"], "bait_back")
        self.assertEqual(planner._no_kill_evasion_cooldown, 4)
    def test_no_kill_exit_evades_close_visible_enemy_without_raw_fire(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(64, 0))
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=360),
                probe(90, open=False, distance=0),
            ],
            back_open=True,
        )
        action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_STRAFE_RIGHT)
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(action.detail["side"], "right")
    def test_no_kill_exit_evasion_has_cooldown_so_route_can_resume(self):
        planner = SpatialPlanner(snapshot([vertex(0, 0, 0)], []), cell_units=96)
        st = state(0, 0, 0, enemy=(64, 0))
        st.enemies[0].line_of_sight = True
        st.navigation = SimpleNamespace(
            direction_probes=[
                probe(-90, open=True, distance=160),
                probe(90, open=True, distance=320),
            ],
            back_open=True,
        )
        for _ in range(3):
            action = planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory())
            self.assertIsNotNone(action)
            self.assertEqual(action.detail["skill"], "no_kill_route_evasion")
        self.assertIsNone(planner.objective_action(st, ["exit", "no_kills", "avoid_combat"], FakeRawAgentPb2, DoorMemory()))
    def test_complete_level_fires_on_shootable_combat_probe(self):
        planner = SpatialPlanner(snapshot([], []), cell_units=96)
        st = state(0, 0, 0, shootable=True)

        action = planner.objective_action(st, ("complete_level", "exit", "use", "explore"), FakeAgentPb2, DoorMemory())

        self.assertIsNotNone(action)
        self.assertEqual(action.skill, "fire")
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_SHOOT)
        self.assertEqual(action.detail["skill"], "complete_level_fire_burst")
        self.assertEqual(action.detail["evidence"], "combat_probe")
    def test_complete_level_no_kill_does_not_fire_on_shootable_probe(self):
        planner = SpatialPlanner(snapshot([], []), cell_units=96)
        st = state(0, 0, 0, shootable=True)

        action = planner.objective_action(
            st,
            ("complete_level", "exit", "use", "explore", "no_kills", "avoid_combat"),
            FakeAgentPb2,
            DoorMemory(),
        )

        self.assertNotEqual(getattr(getattr(action, "action", None), "action", None), FakeAgentPb2.ACTION_SHOOT)
    def test_no_kill_portal_target_offsets_away_from_visible_blocker(self):
        snap = snapshot(
            [vertex(0, 0, -64), vertex(1, 0, 64)],
            [line(385, 0, 1, front_sector=1, back_sector=2, two_sided=True, passable=True, blocking=False, sight_blocking=False)],
            [sector(1), sector(2)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        planner._avoid_sector_ids = {2}
        line_obj = planner._line_by_id(385)
        edge = PortalEdge(
            src=1,
            dst=2,
            line_id=385,
            point=Point(fp(0), fp(0)),
            special=0,
            passable=True,
            use_line=False,
            door=False,
            exit=False,
            walk_trigger=False,
            cost=1.0,
        )
        st = state(-80, 0, 0, enemy=(0, 48))
        st.enemies[0].line_of_sight = True
        player = {"point": Point(fp(-80), fp(0)), "angle": 0}
        target = planner._portal_target_avoiding_visible_enemy(
            edge,
            Point(fp(96), fp(0)),
            line_obj,
            state=st,
            player=player,
        )
        self.assertLess(target.y, 0)
    def test_no_kill_exit_keeps_routing_on_low_health(self):
        snap = snapshot([vertex(0, 64, -64), vertex(1, 64, 64)], [line(330, 0, 1, special=11, exit=True)])
        planner = SpatialPlanner(snap, cell_units=96)
        st = state(0, 0, 0)
        st.player.health = 10
        st.navigation = SimpleNamespace(
            back_open=True,
            direction_probes=[],
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
            ),
        )
        action = planner.objective_action(st, ("exit", "use", "no_kills"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertNotEqual(action.skill, "retreat")
    def test_visible_enemy_is_preferred_over_closer_hidden_enemy(self):
        snap = snapshot(
            [
                vertex(0, 128, -128),
                vertex(1, 128, 128),
            ],
            [line(10, 0, 1)],
        )
        planner = SpatialPlanner(snap, cell_units=96)
        action = planner.objective_action(
            enemy_state(
                0,
                0,
                90,
                [
                    {"id": 1, "pos": (256, 0), "los": False},
                    {"id": 2, "pos": (0, 512), "los": True},
                ],
            ),
            ("attack", "find_enemy"),
            FakeAgentPb2,
            DoorMemory(),
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.detail["enemy"], 2)
        self.assertEqual(action.detail["skill"], "combat_reposition_visible")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_SHOOT)
    def test_visible_combat_strafes_when_aligned_and_close(self):
        snap = snapshot([], [])
        planner = SpatialPlanner(snap, cell_units=96)
        st = enemy_state(0, 0, 0, [{"id": 2, "pos": (128, 0), "los": True}])
        st.navigation = SimpleNamespace(
            direction_probes=[
                SimpleNamespace(angle_offset_degrees=90, open=True, block_distance_fp=fp(384), use_line_ahead=False),
                SimpleNamespace(angle_offset_degrees=-90, open=False, block_distance_fp=fp(64), use_line_ahead=False),
            ],
            forward_open=True,
        )
        action = planner.objective_action(st, ("attack", "find_enemy"), FakeAgentPb2, DoorMemory())
        self.assertIsNotNone(action)
        self.assertEqual(action.action.action, FakeAgentPb2.ACTION_STRAFE_LEFT)
        self.assertEqual(action.detail["action"], "strafe")
        self.assertNotEqual(action.action.action, FakeAgentPb2.ACTION_SHOOT)


if __name__ == "__main__":
    unittest.main()
