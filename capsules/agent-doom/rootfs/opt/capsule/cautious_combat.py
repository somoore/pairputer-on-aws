#!/usr/bin/env python3.11
"""Cautious-combat FSM for the Agent DOOM brain (preserve-health combat).

Lure/ping/ambush/scoot machinery: cover-and-wait overrides, door-flash and
door-ambush holds, jiggle peeks/breaches, threshold ambushes, peek-fire and
prefire alignment, post-shot scoots, break-LOS retreats, and their cover/side
profiling helpers. Extracted verbatim from brain_runtime.BrainRuntime.

CautiousCombatMixin is a mixin over BrainRuntime state: every method runs on
the BrainRuntime instance and reads/writes attributes initialized in
BrainRuntime.__init__ (e.g. self._cautious_* counters) and calls shared
BrainRuntime helpers (self._metrics, self._nearest_enemy, ...). It holds no
state of its own. This module must not import brain_runtime at runtime.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from brain_runtime import ObjectiveDirective

# Constants used by the cautious-combat FSM. Shared ones are imported back
# into brain_runtime (which re-exports them for existing importers).
CAUTIOUS_COVER_AMBUSH_WINDOW = 18
CAUTIOUS_DOOR_AMBUSH_MAX_UNITS = 512.0
CAUTIOUS_RETREAT_COMMIT_STEPS = 12
FP_UNIT = 65536.0
HEALTHY_BREAK_LOS_PUSH_HEALTH = 60
HEALTHY_BREAK_LOS_PUSH_REPEATS = 6
ROUTE_CRITICAL_HEALTH_BREAKAWAY = 40
ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE = 300.0


class CautiousCombatMixin:
    """Preserve-health cautious-combat FSM, mixed into BrainRuntime."""

    def _cautious_combat_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        constraints = directive.contract.constraints
        rules = set(directive.rules)
        # RAMPAGE never goes cautious: it's the aggressive demo autopilot (dying is fine,
        # watchable > safe). The cautious FSM's survival-pressure break-LOS/hide behavior
        # was hijacking it — traced 17 straight break_los_right steps hiding from a distant
        # hitscanner instead of charging. Aggressive combat lives in the planner path.
        if directive.contract.objective == "rampage":
            return None
        metrics = self._metrics(state)
        preserve_health = bool(constraints.get("preserve_health"))
        health = int(metrics.get("health", 100) or 100)
        survival_pressure = health <= 55 and bool(metrics.get("visible_enemy") or metrics.get("shootable"))
        wounded_complete_level = directive.contract.objective == "complete_level" and not preserve_health and health <= 55
        retreat_commit_active = self._cautious_retreat_commit_steps > 0
        if not (preserve_health or survival_pressure or retreat_commit_active or self._cautious_retreat_steps > 0):
            return None
        if not ({"attack", "shoot", "find_enemy"} & rules or directive.contract.objective in {"clear_area", "kill_enemy", "complete_level"}):
            return None
        if self._cautious_lure_cooldown > 0:
            self._cautious_lure_cooldown -= 1
        if self._cautious_ambush_window > 0:
            self._cautious_ambush_window -= 1
        if self._cautious_threshold_cooldown > 0:
            self._cautious_threshold_cooldown -= 1
        if self._cautious_recent_hit_window > 0:
            self._cautious_recent_hit_window -= 1
        if self._cautious_probe_cooldown > 0:
            self._cautious_probe_cooldown -= 1
        if self._cautious_jiggle_peek_steps > 0:
            self._cautious_jiggle_peek_steps -= 1
        if self._cautious_cover_side_lock_steps > 0:
            self._cautious_cover_side_lock_steps -= 1
            if self._cautious_cover_side_lock_steps <= 0:
                self._cautious_cover_side_lock = 0
        if self._cautious_cover_hold_steps > 0:
            self._cautious_cover_hold_steps -= 1
        if self._cautious_retreat_commit_steps > 0:
            self._cautious_retreat_commit_steps -= 1
        if self._cautious_post_shot_scoot:
            # ponytail: shoot-and-scoot — the step after an ambush shot is a free
            # hitscan RNG roll for anyone still in LOS. Break LOS unconditionally
            # before the duel/align/threshold machinery gets to reassess.
            self._cautious_post_shot_scoot = False
            if preserve_health and bool(metrics.get("visible_enemy") or metrics.get("shootable")):
                enemy = self._nearest_enemy(state, prefer_visible=True) or {
                    "id": int(self._cautious_target_id or 0),
                    "threat": "unknown",
                }
                scoot = self._cautious_post_shot_scoot_action(
                    state,
                    directive,
                    controller,
                    modules,
                    enemy=enemy,
                )
                if scoot is not None:
                    if self._cautious_retreat_steps > 0:
                        self._cautious_retreat_steps -= 1
                    return scoot
        if preserve_health and self._cautious_door_ambush_hold_steps > 0 and self._cautious_retreat_steps <= 0:
            enemy = self._nearest_enemy(state, prefer_visible=True) or {
                "id": int(self._cautious_target_id or 0),
                "threat": "unknown",
                "visible": False,
                "turn": 0.0,
            }
            hold = self._cautious_door_ambush_hold_action(
                state,
                directive,
                controller,
                modules,
                enemy=enemy,
            )
            if hold is not None:
                return hold
        if bool(metrics.get("shootable")):
            self._cautious_probe_steps = 0
            self._cautious_lure_wait_steps = 0
        if self._cautious_retreat_steps > 0:
            self._cautious_retreat_steps -= 1
            enemy = self._nearest_enemy(state, prefer_visible=True) or {
                "id": int(self._cautious_target_id or 0),
                "threat": "unknown",
            }
            if (
                bool(metrics.get("shootable"))
                and (
                    (survival_pressure and not preserve_health)
                    or (
                        directive.contract.objective == "clear_area"
                        # ponytail: inside pain-lock range committing to the duel beats
                        # retreating with his queued shot inbound; past it the odds invert.
                        and float(enemy.get("distance", 9999.0) or 9999.0) <= 224.0
                    )
                )
                and not (wounded_complete_level and not self._complete_level_survival_fire_allowed(state, enemy))
            ):
                # ponytail: retreating with a shootable hitscan enemy in LOS trades free
                # damage; keep evade-firing until the kill converts, then break contact.
                # DOOM autoaim only forgives a few degrees, so square up first.
                if abs(float(enemy.get("turn", 0.0) or 0.0)) > 10.0:
                    align = self._cautious_align_visible_action(state, directive, controller, modules, enemy=enemy)
                    if align is not None:
                        self._cautious_target_id = int(enemy.get("id", 0) or 0)
                        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 2)
                        return align
                enemy_id = int(enemy.get("id", 0) or 0)
                if enemy_id != self._cautious_duel_target:
                    self._cautious_duel_target = enemy_id
                    self._cautious_duel_steps = 0
                self._cautious_duel_steps += 1
                # ponytail: 10 duel steps ≈ 2 pistol refires; a still-undamaged enemy
                # means the strafe is defeating our own autoaim — stop feeding him
                # free shots and break contact instead.
                duel_futile = (
                    self._cautious_duel_steps > 10
                    and int(enemy.get("health", 0) or 0) >= 20
                )
                if not duel_futile:
                    followup = self._cautious_peek_fire_action(state, directive, controller, modules, enemy=enemy, evade=preserve_health)
                    if followup is not None:
                        self._cautious_target_id = enemy_id
                        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 1)
                        index, skill, action, decision = followup
                        decision = dict(decision)
                        decision["reason"] = "survival_pressure_followup_shot"
                        return index, skill, action, decision
            ambush_armed = preserve_health and self._cautious_ambush_armed(enemy)
            finish_wounded = ambush_armed and self._cautious_finishable_threshold_target(enemy)
            followup_hit = False
            if ambush_armed and (self._cautious_threshold_cooldown <= 0 or finish_wounded or followup_hit):
                ambush = self._cautious_threshold_ambush_action(state, directive, controller, modules, enemy=enemy)
                if ambush is not None:
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    self._cautious_retreat_steps = max(self._cautious_retreat_steps, 4)
                    self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
                    self._cautious_threshold_cooldown = 1 if followup_hit else 3
                    return ambush
                align = self._cautious_threshold_align_action(state, directive, controller, modules, enemy=enemy)
                if align is not None:
                    self._cautious_retreat_steps = max(self._cautious_retreat_steps, 2)
                    return align
                if followup_hit:
                    followup = self._cautious_recent_hit_followup_action(state, directive, controller, modules, enemy=enemy)
                    if followup is not None:
                        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 3)
                        self._cautious_threshold_cooldown = 1
                        return followup
                    return self._cautious_cover_action(
                        state,
                        directive,
                        controller,
                        modules,
                        reason="recent_hit_break_los",
                        enemy=enemy,
                    )
            if (
                preserve_health
                and ambush_armed
                and self._cautious_threshold_cooldown > 0
                and self._cautious_threshold_hold_target(enemy)
            ):
                if bool(enemy.get("visible")) or bool(metrics.get("shootable")):
                    return self._cautious_cover_action(
                        state,
                        directive,
                        controller,
                        modules,
                        reason="threshold_cooldown_break_los",
                        enemy=enemy,
                    )
                return self._cautious_wait_action(
                    state,
                    directive,
                    controller,
                    modules,
                    reason="threshold_cooldown_hold",
                    enemy=enemy,
                )
            if preserve_health and not bool(enemy.get("visible")):
                if ambush_armed:
                    self._cautious_lure_wait_steps += 1
                    # ponytail: peeking at a 2v1 exposes us to the second shooter no
                    # matter how the duel goes; hold the funnel until they string out.
                    if self._cautious_lure_wait_steps >= 4 and self._close_enemy_count(state) < 2:
                        cover_profile = self._cautious_cover_profile(state) or {
                            "cover_evidence": "ambush_window",
                            "cover_dist": int(float(enemy.get("distance", 0.0) or 0.0)),
                        }
                        jiggle = self._cautious_jiggle_peek_action(
                            state,
                            directive,
                            controller,
                            modules,
                            enemy=enemy,
                            cover_profile=cover_profile,
                            reason="lure_retreat_jiggle_peek",
                        )
                        if jiggle is not None:
                            self._cautious_target_id = int(enemy.get("id", 0) or 0)
                            self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
                            return jiggle
                return self._cautious_wait_action(state, directive, controller, modules, reason="lure_and_wait_hidden", enemy=enemy)
            return self._cautious_cover_action(
                state,
                directive,
                controller,
                modules,
                reason="kite_and_funnel_retreat",
                enemy=enemy,
                arm_commit=not retreat_commit_active,
            )
        if retreat_commit_active:
            enemy = self._nearest_enemy(state, prefer_visible=True) or {
                "id": int(self._cautious_target_id or 0),
                "threat": "unknown",
                "visible": False,
                "turn": 0.0,
            }
            return self._cautious_cover_action(
                state,
                directive,
                controller,
                modules,
                reason="retreat_commit",
                enemy=enemy,
                arm_commit=False,
            )
        enemy = self._nearest_enemy(state, prefer_visible=True)
        if enemy is None:
            if preserve_health and self._cautious_ambush_window > 0:
                return self._cautious_wait_action(
                    state,
                    directive,
                    controller,
                    modules,
                    reason="ambush_window_hold_no_enemy_snapshot",
                    enemy={"id": int(self._cautious_target_id or 0), "threat": "unknown"},
                )
            return None
        if not bool(enemy.get("visible")):
            threat = str(enemy.get("threat") or "unknown")
            cover_profile = self._cautious_cover_profile(state)
            if preserve_health and threat in {"hitscan", "unknown"} and cover_profile is not None:
                # ponytail: after 3 jiggle cycles with no contact the lure has failed;
                # fall through so prefire/door-flash escalation (or the planner) takes over.
                if self._cautious_ambush_armed(enemy) and self._cautious_jiggle_probe_attempts < 3:
                    if self._cautious_lure_wait_steps < 6 or self._close_enemy_count(state) >= 2:
                        self._cautious_lure_wait_steps += 1
                        return self._cautious_wait_action(
                            state,
                            directive,
                            controller,
                            modules,
                            reason="ambush_window_hold_hidden",
                            enemy=enemy,
                        )
                    self._cautious_lure_wait_steps = 0
                    jiggle = self._cautious_jiggle_peek_action(
                        state,
                        directive,
                        controller,
                        modules,
                        enemy=enemy,
                        cover_profile=cover_profile,
                        reason="ambush_window_jiggle_peek",
                    )
                    if jiggle is not None:
                        self._cautious_target_id = int(enemy.get("id", 0) or 0)
                        self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
                        return jiggle
                enemy_distance = float(enemy.get("distance", 9999.0) or 9999.0)
                if enemy_distance > CAUTIOUS_DOOR_AMBUSH_MAX_UNITS:
                    self._cautious_lure_wait_steps = 0
                    return None
                contact_dist = self._nearest_use_line_distance_units(state)
                if contact_dist is not None and contact_dist > 160:
                    self._cautious_lure_wait_steps = 0
                    return None
                align = self._cautious_prefire_align_action(
                    state,
                    directive,
                    controller,
                    modules,
                    enemy=enemy,
                    cover_profile=cover_profile,
                )
                if align is not None:
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    return align
                if self._combat_state.shots >= 3 and not bool(metrics.get("shootable")):
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    self._cautious_ambush_window = max(self._cautious_ambush_window, 12)
                    if self._cautious_lure_wait_steps < 6:
                        self._cautious_lure_wait_steps += 1
                        return self._cautious_wait_action(
                            state,
                            directive,
                            controller,
                            modules,
                            reason="stale_prefire_lure_wait",
                            enemy=enemy,
                        )
                    self._cautious_lure_wait_steps = 0
                    self._combat_state.shots = 0
                prefire = self._cautious_prefire_peek_action(
                    state,
                    directive,
                    controller,
                    modules,
                    enemy=enemy,
                    cover_profile=cover_profile,
                )
                if prefire is not None:
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    self._cautious_lure_wait_steps = 0
                    self._cautious_retreat_steps = 6
                    self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
                    return prefire
                if self._cautious_lure_cooldown <= 0:
                    lure = self._cautious_peek_fire_action(state, directive, controller, modules, enemy=enemy, lure=True)
                    if lure is not None:
                        self._cautious_target_id = int(enemy.get("id", 0) or 0)
                        self._cautious_lure_wait_steps = 0
                        self._cautious_retreat_steps = 8
                        self._cautious_lure_cooldown = 30
                        self._cautious_ambush_window = 30
                        decision = dict(lure[3])
                        decision["skill"] = "blind_lure_shot"
                        decision["state"] = "lure_and_wait"
                        decision["cover"] = "door_jamb"
                        decision.update(self._cover_decision_fields(cover_profile))
                        return lure[0], lure[1], lure[2], decision
            if preserve_health and threat in {"hitscan", "unknown"} and cover_profile is not None:
                return self._cautious_wait_action(
                    state,
                    directive,
                    controller,
                    modules,
                    reason="hidden_hitscan_lure_wait",
                    enemy=enemy,
                )
            return None
        threat = str(enemy.get("threat") or "unknown")
        if threat == "projectile":
            if survival_pressure:
                if bool(metrics.get("shootable")) and not (
                    wounded_complete_level and not self._complete_level_survival_fire_allowed(state, enemy)
                ):
                    fired = self._cautious_peek_fire_action(state, directive, controller, modules, enemy=enemy, evade=True)
                    if fired is not None:
                        self._cautious_target_id = int(enemy.get("id", 0) or 0)
                        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 3)
                        index, skill, action, decision = fired
                        decision = dict(decision)
                        decision["reason"] = "survival_projectile_fire_evade"
                        return index, skill, action, decision
                self._cautious_target_id = int(enemy.get("id", 0) or 0)
                self._cautious_retreat_steps = max(self._cautious_retreat_steps, 4)
                return self._cautious_cover_action(
                    state,
                    directive,
                    controller,
                    modules,
                    reason="survival_projectile_evasion",
                    enemy=enemy,
                )
            if preserve_health and bool(metrics.get("shootable")):
                fired = self._cautious_peek_fire_action(state, directive, controller, modules, enemy=enemy, evade=True)
                if fired is not None:
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    self._cautious_retreat_steps = max(self._cautious_retreat_steps, 3)
                    self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
                    index, skill, action, decision = fired
                    decision = dict(decision)
                    decision["reason"] = "ambush_projectile_fire_evade"
                    return index, skill, action, decision
            if preserve_health and bool(metrics.get("visible_enemy")) and not bool(metrics.get("shootable")):
                return self._cautious_cover_action(
                    state,
                    directive,
                    controller,
                    modules,
                    reason="avoid_projectile_threshold",
                    enemy=enemy,
                )
            return None
        if threat in {"hitscan", "unknown"} and bool(metrics.get("shootable")):
            cover_profile = self._cautious_cover_profile(state) if preserve_health else None
            if preserve_health and cover_profile is None:
                enemy_distance = float(enemy.get("distance", 9999.0) or 9999.0)
                # ponytail: open-field trades bleed health, but with no cover profile
                # and a stuck cover loop (repeat>=3) the alternatives are worse; take
                # the long-range trade where his accuracy has decayed the most.
                if self._cautious_cover_repeat_count >= 3 and enemy_distance >= 384.0:
                    fired = self._cautious_peek_fire_action(state, directive, controller, modules, enemy=enemy, evade=True)
                    if fired is not None:
                        self._cautious_target_id = int(enemy.get("id", 0) or 0)
                        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 4)
                        index, skill, action, decision = fired
                        decision = dict(decision)
                        decision["reason"] = "open_field_visible_return_fire"
                        decision["repeat"] = int(self._cautious_cover_repeat_count)
                        return index, skill, action, decision
                navigation = getattr(state, "navigation", None)
                if (
                    enemy_distance <= 128.0
                    and not self._cautious_escape_ready(navigation)
                ):
                    fired = self._cautious_peek_fire_action(state, directive, controller, modules, enemy=enemy, evade=False)
                    if fired is not None:
                        self._cautious_target_id = int(enemy.get("id", 0) or 0)
                        self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
                        index, skill, action, decision = fired
                        decision = dict(decision)
                        decision["reason"] = "last_resort_close_no_escape"
                        return index, skill, action, decision
                return self._cautious_cover_action(
                    state,
                    directive,
                    controller,
                    modules,
                    reason="hold_fire_visible_hitscan_without_cover",
                    enemy=enemy,
                )
            if preserve_health and cover_profile is not None and self._cautious_jiggle_peek_steps > 0:
                jiggle_shot = self._cautious_jiggle_prefire_action(
                    state,
                    directive,
                    controller,
                    modules,
                    enemy=enemy,
                    cover_profile=cover_profile,
                )
                if jiggle_shot is not None:
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    return jiggle_shot
            if preserve_health and self._cautious_recent_hit_window <= 0 and float(enemy.get("distance", 9999.0) or 9999.0) > 224.0:
                return self._cautious_cover_action(
                    state,
                    directive,
                    controller,
                    modules,
                    reason="defer_visible_hitscan_to_lure",
                    enemy=enemy,
                )
            fired = self._cautious_peek_fire_action(state, directive, controller, modules, enemy=enemy, evade=preserve_health)
            if fired is not None:
                self._cautious_target_id = int(enemy.get("id", 0) or 0)
                self._cautious_retreat_steps = 10 if preserve_health else 3
                return fired
        if survival_pressure and threat in {"hitscan", "unknown"}:
            if not bool(metrics.get("shootable")):
                self._cautious_target_id = int(enemy.get("id", 0) or 0)
                self._cautious_retreat_steps = max(self._cautious_retreat_steps, 4)
                return self._cautious_cover_action(
                    state,
                    directive,
                    controller,
                    modules,
                    reason="survival_pressure_break_los_without_shot",
                    enemy=enemy,
                )
            if (
                self._cautious_lure_cooldown <= 0
                and not (wounded_complete_level and not self._complete_level_survival_fire_allowed(state, enemy))
            ):
                lure = self._cautious_peek_fire_action(state, directive, controller, modules, enemy=enemy, lure=True)
                if lure is not None:
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    self._cautious_retreat_steps = 5
                    self._cautious_lure_cooldown = 12
                    return lure
        if threat in {"hitscan", "melee", "melee_rush", "unknown"}:
            if preserve_health and bool(metrics.get("visible_enemy")) and not bool(metrics.get("shootable")):
                if threat in {"hitscan", "unknown"}:
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    self._cautious_ambush_window = max(self._cautious_ambush_window, 12)
                    if (
                        directive.contract.objective == "clear_area"
                        and float(enemy.get("distance", 9999.0) or 9999.0) <= 224.0
                    ):
                        # ponytail: he's already inside pain-lock range; square up and
                        # let the duel machinery convert instead of resetting the lure.
                        align = self._cautious_align_visible_action(state, directive, controller, modules, enemy=enemy)
                        if align is not None:
                            return align
                    if (
                        float(enemy.get("distance", 9999.0) or 9999.0) > 400.0
                        and self._cautious_cover_profile(state) is None
                    ):
                        # ponytail: an open-field side-dance vs a far sniper breaks no
                        # LOS — alternating break_los steps gave him 40+ tics of free
                        # windup (traced -3/-12/-15 at 600-680u). Yield to the planner:
                        # purposeful routed movement is the only cover out here.
                        return None
                    return self._cautious_cover_action(
                        state,
                        directive,
                        controller,
                        modules,
                        reason="visible_hitscan_break_los_before_align",
                        enemy=enemy,
                    )
                if threat in {"hitscan", "unknown"} and self._cautious_cover_profile(state) is None:
                    if self._cautious_cover_repeat_count >= 3:
                        align_open = self._cautious_open_field_align_action(
                            state,
                            directive,
                            controller,
                            modules,
                            enemy=enemy,
                        )
                        if align_open is not None:
                            return align_open
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    return self._cautious_cover_action(
                        state,
                        directive,
                        controller,
                        modules,
                        reason="hold_align_visible_hitscan_without_cover",
                        enemy=enemy,
                    )
                align = self._cautious_align_visible_action(state, directive, controller, modules, enemy=enemy)
                if align is not None:
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    self._cautious_retreat_steps = max(self._cautious_retreat_steps, 2)
                    self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
                    return align
                self._cautious_target_id = int(enemy.get("id", 0) or 0)
                self._cautious_ambush_window = max(self._cautious_ambush_window, 12)
            return self._cautious_cover_action(state, directive, controller, modules, reason=f"avoid_{threat}_los", enemy=enemy)
        return None

    def _cautious_align_visible_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        cover_profile: dict[str, Any] | None = None,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        turn = float(enemy.get("turn", 0.0) or 0.0)
        if abs(turn) <= 10.0:
            return None
        selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        action_type = agent_pb2.ACTION_TURN_LEFT if turn > 0 else agent_pb2.ACTION_TURN_RIGHT
        action = agent_pb2.PlayerAction(action=action_type, amount=max(6, min(34, int(abs(turn)))), duration_tics=4)
        index, skill = selected
        return index, skill, action, {
            "source": "cautious_combat",
            "skill": "slice_align",
            "state": "slice_the_pie",
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "turn": round(turn, 1),
        }

    def _cautious_peek_fire_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        lure: bool = False,
        evade: bool = False,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        selected = self._planner_skill_index("fire", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        index, skill = selected
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if evade and raw_cls is not None:
            navigation = getattr(state, "navigation", None)
            distance = float(enemy.get("distance", 9999.0) or 9999.0)
            # ponytail: backpedaling drops zero angular displacement on his aim while
            # bleeding our refire hit rate with range — the losing duel shape. Strafe
            # to dodge, steer to keep autoaim on him, and only give ground point-blank.
            # The fire tic must never be a stationary tic: fall back to any open side,
            # then to backpedaling, so the shot and the scoot share the same ticcmd.
            side = self._best_cover_side(navigation) or self._best_open_cover_side(navigation)
            back_off = bool(getattr(navigation, "back_open", False)) and (distance < 48.0 or not side)
            turn = float(enemy.get("turn", 0.0) or 0.0)
            action = agent_pb2.PlayerAction(
                duration_tics=2,
                raw=raw_cls(
                    forward_move=-42 if back_off else 0,
                    side_move=self._raw_side_move_for_cover_side(side, 52) if side else 0,
                    angle_turn=self._raw_steer_turn_units(turn) if abs(turn) > 2.0 else 0,
                    buttons=1,
                ),
            )
            action_name = "fire_evade"
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=2 if lure else 4)
            action_name = "fire"
        self._cautious_probe_steps = 0
        self._cautious_lure_wait_steps = 0
        self._cautious_jiggle_probe_attempts = 0
        if directive.contract.constraints.get("preserve_health"):
            self._cautious_post_shot_scoot = True
        return index, skill, action, {
            "source": "cautious_combat",
            "skill": "funnel_lure_shot" if lure else "peek_fire",
            "state": "kite_and_funnel" if lure else "slice_the_pie",
            "action": action_name,
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(enemy.get("distance", 0) or 0),
            "ehp": int(enemy.get("health", 0) or 0),
            "turn": round(float(enemy.get("turn", 0.0) or 0.0), 1),
        }

    def _cautious_prefire_peek_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        cover_profile: dict[str, Any] | None = None,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if float(enemy.get("distance", 9999.0) or 9999.0) > CAUTIOUS_DOOR_AMBUSH_MAX_UNITS:
            return None
        if abs(float(enemy.get("turn", 0.0) or 0.0)) > 24.0:
            return None
        selected = self._planner_skill_index("fire", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is None:
            return None
        side = self._best_cover_side(getattr(state, "navigation", None))
        side_move = self._raw_side_move_for_cover_side(side, 56)
        action = agent_pb2.PlayerAction(duration_tics=2, raw=raw_cls(side_move=side_move, buttons=1))
        self._cautious_probe_steps = 0
        self._cautious_lure_wait_steps = 0
        index, skill = selected
        decision = {
            "source": "cautious_combat",
            "skill": "prefire_peek",
            "state": "pre_fire_peek",
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(enemy.get("distance", 0) or 0),
            "cover": "door_jamb",
        }
        decision.update(self._cover_decision_fields(cover_profile))
        return index, skill, action, decision

    def _cautious_prefire_align_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        cover_profile: dict[str, Any] | None = None,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        if distance > CAUTIOUS_DOOR_AMBUSH_MAX_UNITS:
            return None
        turn = float(enemy.get("turn", 0.0) or 0.0)
        # Far pistol pre-fire needs tighter aim than close threshold work. Turn while
        # still behind cover instead of spending ammo on a trace that will miss.
        tolerance = 4.0 if distance > 224.0 else 12.0
        if abs(turn) <= tolerance or abs(turn) > 24.0:
            return None
        selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        left = getattr(agent_pb2, "ACTION_TURN_LEFT", None)
        right = getattr(agent_pb2, "ACTION_TURN_RIGHT", None)
        if left is None or right is None:
            return None
        action_type = left if turn > 0 else right
        action = agent_pb2.PlayerAction(action=action_type, amount=max(3, min(12, int(abs(turn)))), duration_tics=2)
        index, skill = selected
        decision = {
            "source": "cautious_combat",
            "skill": "prefire_align",
            "state": "pre_fire_peek",
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(distance),
            "turn": round(turn, 1),
            "cover": "door_jamb",
        }
        decision.update(self._cover_decision_fields(cover_profile))
        return index, skill, action, decision

    def _cautious_probe_opening_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        cover_profile: dict[str, Any] | None = None,
        reason: str,
        pre_fire: bool = False,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if cover_profile is None:
            return None
        if self._cautious_probe_steps >= 8:
            self._cautious_probe_steps = 0
            self._cautious_probe_cooldown = 20
            return None
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        if distance > 896.0:
            return None
        turn = float(enemy.get("turn", 0.0) or 0.0)
        if abs(turn) > 18.0:
            return None
        selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is None:
            return None
        navigation = getattr(state, "navigation", None)
        side = self._best_open_cover_side(navigation)
        if side:
            raw = raw_cls(side_move=self._raw_side_move_for_cover_side(side, 42))
            action_name = "side_probe"
        elif bool(getattr(navigation, "forward_open", False)):
            raw = raw_cls(forward_move=24)
            action_name = "forward_probe"
        else:
            return None
        action = agent_pb2.PlayerAction(duration_tics=1, raw=raw)
        self._cautious_probe_steps += 1
        self._cautious_probe_cooldown = 6
        index, skill = selected
        decision = {
            "source": "cautious_combat",
            "skill": "probe_to_shootable",
            "state": "pre_fire_peek",
            "action": action_name,
            "reason": reason,
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(distance),
            "turn": round(turn, 1),
            "cover": "door_jamb",
            "probe_steps": self._cautious_probe_steps,
        }
        decision.update(self._cover_decision_fields(cover_profile))
        return index, skill, action, decision

    def _cautious_jiggle_peek_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        cover_profile: dict[str, Any] | None = None,
        reason: str,
        pre_fire: bool = False,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if cover_profile is None:
            return None
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        if distance > CAUTIOUS_DOOR_AMBUSH_MAX_UNITS:
            return None
        turn = float(enemy.get("turn", 0.0) or 0.0)
        if abs(turn) > 24.0:
            return None
        if self._cautious_probe_steps >= 10:
            self._cautious_probe_steps = 0
            self._cautious_probe_cooldown = 12
            return None
        metrics = self._metrics(state)
        fire_ready = bool(pre_fire and metrics.get("shootable"))
        selected = self._planner_skill_index("fire", controller, state, modules, directive) if fire_ready else None
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is None:
            return None
        navigation = getattr(state, "navigation", None)
        side = self._cautious_jiggle_side(navigation)
        if fire_ready:
            # ponytail: shoot-and-scoot — he's already shootable, so the fire tic
            # spends its movement bailing back behind the jamb instead of advancing
            # deeper into his hitscan RNG window.
            bail_side = self._best_open_cover_side(navigation) or self._best_cover_side(navigation) or side
            raw = raw_cls(
                forward_move=-24 if bool(getattr(navigation, "back_open", False)) else 0,
                side_move=self._raw_side_move_for_cover_side(bail_side, 44) if bail_side else 0,
                buttons=1,
            )
            action_name = "jiggle_prefire_probe"
        elif side:
            raw = raw_cls(
                forward_move=16,
                side_move=self._raw_side_move_for_cover_side(side, 50),
            )
            action_name = "jiggle_side_probe"
        elif bool(getattr(navigation, "forward_open", False)):
            raw = raw_cls(forward_move=18)
            action_name = "jiggle_forward_probe"
        else:
            return None
        # ponytail: deep peek — a 2-tic/38-speed strafe moved ~9 units, enough for the
        # bounding-box edge to see the enemy but not for the center point that
        # P_AimLineAttack raycasts from to clear the door jamb, so shootable never
        # flipped and the FSM ghost-peeked forever. 5 tics at 50 clears the jamb.
        action = agent_pb2.PlayerAction(duration_tics=2 if pre_fire else 5, raw=raw)
        self._cautious_probe_steps += 1
        self._cautious_jiggle_probe_attempts += 1
        self._cautious_jiggle_peek_steps = max(self._cautious_jiggle_peek_steps, 6)
        if fire_ready:
            self._cautious_lure_wait_steps = 0
            if directive.contract.constraints.get("preserve_health"):
                self._cautious_post_shot_scoot = True
        if pre_fire:
            self._cautious_retreat_steps = max(self._cautious_retreat_steps, 2)
            self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
            self._cautious_threshold_cooldown = max(self._cautious_threshold_cooldown, 2)
        index, skill = selected
        decision = {
            "source": "cautious_combat",
            "skill": "jiggle_prefire_probe" if fire_ready else "jiggle_peek_probe",
            "state": "pre_fire_peek",
            "action": action_name,
            "reason": reason,
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(distance),
            "turn": round(turn, 1),
            "cover": "door_jamb",
            "probe_steps": self._cautious_probe_steps,
            "jiggle_attempts": self._cautious_jiggle_probe_attempts,
            "jiggle_side": int(side or 0),
            "fire_ready": fire_ready,
        }
        decision.update(self._cover_decision_fields(cover_profile))
        return index, skill, action, decision

    def _cautious_jiggle_breach_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        route_distance: int,
        cover_profile: dict[str, Any] | None = None,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        long_breach = self._cautious_jiggle_probe_attempts >= 8 and route_distance <= CAUTIOUS_DOOR_AMBUSH_MAX_UNITS
        if route_distance > 144 and not long_breach:
            return None
        metrics = self._metrics(state)
        if int(metrics.get("health", 100) or 100) < 95:
            return None
        fire_ready = bool(metrics.get("shootable"))
        selected = self._planner_skill_index("fire", controller, state, modules, directive) if fire_ready else None
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is None:
            return None
        navigation = getattr(state, "navigation", None)
        side = self._cautious_jiggle_side(navigation)
        action = agent_pb2.PlayerAction(
            duration_tics=8 if long_breach else 6,
            raw=raw_cls(
                forward_move=72 if long_breach else 58,
                side_move=self._raw_side_move_for_cover_side(side, 12) if side else 0,
                buttons=1 if fire_ready else 0,
            ),
        )
        self._cautious_probe_steps = 0
        self._cautious_lure_wait_steps = 0
        self._cautious_jiggle_peek_steps = 0
        self._cautious_jiggle_probe_attempts = 0
        self._cautious_jiggle_peek_steps = max(self._cautious_jiggle_peek_steps, 12)
        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 4 if long_breach else 6)
        self._cautious_ambush_window = max(self._cautious_ambush_window, 8 if long_breach else 10)
        self._cautious_threshold_cooldown = max(self._cautious_threshold_cooldown, 3)
        index, skill = selected
        decision = {
            "source": "cautious_combat",
            "skill": "jiggle_commit_los_breach",
            "state": "pre_fire_peek",
            "action": ("wide_prefire_breach" if long_breach else "forward_prefire_breach") if fire_ready else ("wide_los_breach" if long_breach else "forward_los_breach"),
            "reason": "repeated_prefire_no_los",
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(route_distance),
            "turn": 0.0,
            "cover": "door_jamb",
            "jiggle_side": int(side or 0),
            "fire_ready": fire_ready,
        }
        decision.update(self._cover_decision_fields(cover_profile))
        return index, skill, action, decision

    def _cautious_door_flash_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        route_distance: int,
        cover_profile: dict[str, Any] | None = None,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        use_info = self._nearest_use_line_info(state)
        use_dist = int(use_info["distance_units"]) if use_info is not None else None
        if route_distance > 128 and (use_dist is None or use_dist > 96):
            return None
        turn = float(enemy.get("turn", 0.0) or 0.0)
        if abs(turn) > 8.0:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
            if selected is None:
                selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
            if selected is None:
                return None
            agent_pb2 = modules["agent_pb2"]
            raw_cls = getattr(agent_pb2, "RawTiccmd", None)
            if raw_cls is not None:
                action = agent_pb2.PlayerAction(duration_tics=2, raw=raw_cls(angle_turn=self._raw_steer_turn_units(turn)))
            else:
                left = getattr(agent_pb2, "ACTION_TURN_LEFT", None)
                right = getattr(agent_pb2, "ACTION_TURN_RIGHT", None)
                if left is None or right is None:
                    return None
                action_type = left if turn > 0 else right
                action = agent_pb2.PlayerAction(action=action_type, amount=max(3, min(14, int(abs(turn)))), duration_tics=2)
            index, skill = selected
            decision = {
                "source": "cautious_combat",
                "skill": "jiggle_door_flash_align",
                "state": "pre_fire_peek",
                "action": "align_use_line",
                "reason": "align_before_door_flash",
                "enemy": int(enemy.get("id", 0) or 0),
                "threat": str(enemy.get("threat") or "unknown"),
                "dist": int(route_distance),
                "use_dist": int(use_dist) if use_dist is not None else None,
                "turn": round(turn, 1),
                "cover": "door_jamb",
            }
            decision.update(self._cover_decision_fields(cover_profile))
            return index, skill, action, decision
        selected = self._planner_skill_index("open_use_line", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        use_action = getattr(agent_pb2, "ACTION_USE", None)
        if use_action is None:
            return None
        action = agent_pb2.PlayerAction(action=use_action, amount=1, duration_tics=2)
        self._cautious_jiggle_probe_attempts = 0
        self._cautious_probe_steps = 0
        self._cautious_lure_wait_steps = 0
        self._cautious_retreat_steps = 0
        self._cautious_door_ambush_line_id = self._door_ambush_line_id(state, cover_profile=cover_profile, use_info=use_info)
        self._cautious_door_ambush_hold_steps = max(self._cautious_door_ambush_hold_steps, 80)
        self._cautious_jiggle_peek_steps = max(self._cautious_jiggle_peek_steps, 80)
        self._cautious_ambush_window = max(self._cautious_ambush_window, 80)
        self._cautious_threshold_cooldown = 0
        index, skill = selected
        decision = {
            "source": "cautious_combat",
            "skill": "jiggle_door_flash",
            "state": "pre_fire_peek",
            "action": "use_and_bail",
            "reason": "repeated_hidden_peek_near_use_line",
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(route_distance),
            "use_dist": int(use_dist) if use_dist is not None else None,
            "ambush_line": int(self._cautious_door_ambush_line_id) if self._cautious_door_ambush_line_id is not None else None,
            "turn": round(turn, 1),
            "cover": "door_jamb",
        }
        decision.update(self._cover_decision_fields(cover_profile))
        return index, skill, action, decision

    def _cautious_door_ambush_hold_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if self._cautious_door_ambush_hold_steps <= 0:
            self._cautious_door_ambush_hold_spent = 0
            self._cautious_door_tuck_steps = 0
            return None
        self._cautious_door_ambush_hold_steps -= 1
        cover_profile = self._cautious_cover_profile(state) or {
            "cover_evidence": "door_flash_ambush",
            "cover_dist": int(float(enemy.get("distance", 0.0) or 0.0)),
        }
        metrics = self._metrics(state)
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        visible = bool(enemy.get("visible"))
        anchor = self._door_ambush_anchor(state)
        if anchor is None and not bool(metrics.get("shootable")) and distance > 256.0 and self._cautious_door_ambush_hold_steps <= 64:
            self._cautious_door_ambush_hold_steps = 0
            return None
        last_distance = self._cautious_door_ambush_last_dist
        self._cautious_door_ambush_last_dist = distance
        if (
            not visible
            and not bool(metrics.get("shootable"))
            and distance > 256.0
            and last_distance is not None
            and distance >= last_distance - 1.0
        ):
            # ponytail: a hidden enemy parked past 256 units and not closing isn't taking
            # the lure; drain the hold 4x faster instead of camping the door for 160 tics.
            self._cautious_door_ambush_hold_steps = max(0, self._cautious_door_ambush_hold_steps - 3)
        if bool(metrics.get("shootable")):
            shot = self._cautious_jiggle_prefire_action(
                state,
                directive,
                controller,
                modules,
                enemy=enemy,
                cover_profile=cover_profile,
            )
            if shot is not None:
                self._cautious_retreat_steps = min(max(self._cautious_retreat_steps, 1), 3)
                self._cautious_door_ambush_hold_steps = max(self._cautious_door_ambush_hold_steps, 48)
                self._cautious_door_ambush_hold_spent = 0
                return shot
            # ponytail: shootable but no safe shot (out of pain-lock range) means we
            # are standing in his line of fire; abandon the hold and break LOS.
            self._cautious_door_ambush_hold_steps = 0
            self._cautious_door_ambush_hold_spent = 0
            return None
        # ponytail: anchor-creep re-arms the hold budget every step, so an enemy
        # hovering just out of LOS pins us here forever; hard-cap idle holding at
        # 60 consecutive stepless waits, then bail so escalation forces contact.
        self._cautious_door_ambush_hold_spent += 1
        if self._cautious_door_ambush_hold_spent > 60:
            self._cautious_door_ambush_hold_steps = 0
            self._cautious_door_ambush_hold_spent = 0
            return None
        selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        turn_source = "enemy"
        turn = float(enemy.get("turn", 0.0) or 0.0)
        # ponytail: once the prey is visible and close, the door anchor is stale
        # aim; track the enemy or he strolls through the cone un-shootable.
        if anchor is not None and not (visible and distance <= 256.0):
            turn = float(anchor.get("turn", turn) or 0.0)
            turn_source = "door_anchor"
        action_name = "ambush_hold_wait"
        # ponytail: a visible enemy 60° off-axis used to fail the <=45° gate and we
        # sat facing the wrong wall while he closed; always track a close visible.
        should_turn = abs(turn) > 4.0 and (anchor is not None or visible)
        navigation = getattr(state, "navigation", None)
        quiet_creep_side = self._best_open_cover_side(navigation) if navigation is not None else 0
        quiet_forward_open = bool(getattr(navigation, "forward_open", False))
        # ponytail: trap geometry — standing centered on the door axis means the
        # arriving hitscanner sees us the instant he has LOS and starts his attack
        # roll while we align. Tuck flush against the wall BESIDE the opening: his
        # LOS stays blocked until he's fully through, and he emerges point-blank
        # into a pre-aimed threshold shot (our pain-lock beats his acquisition).
        tuck_side, tuck_wall_dist = self._door_tuck_side(navigation)
        wall_tuck = (
            anchor is not None
            and raw_cls is not None
            and not bool(metrics.get("shootable"))
            and not bool(metrics.get("visible_enemy"))
            # <=160u: near the jamb, where "beside the door plane" means something.
            # The anchor creep's tests pin >=192u approaches, so no overlap in
            # practice; when both could apply the tuck wins (safer).
            and int(anchor.get("distance", 0) or 0) <= 160
            and self._cautious_door_tuck_steps < 6
            and bool(tuck_side)
            and tuck_wall_dist > 24.0
        )
        quiet_anchor_creep = (
            anchor is not None
            and not bool(metrics.get("shootable"))
            and not bool(metrics.get("visible_enemy"))
            and self._cautious_door_ambush_hold_steps <= 48
            and int(anchor.get("distance", 0) or 0) > 96
            and (quiet_forward_open or bool(quiet_creep_side))
        )
        if raw_cls is not None:
            raw_kwargs: dict[str, int] = {}
            if wall_tuck:
                raw_kwargs["side_move"] = self._raw_side_move_for_cover_side(tuck_side, 36)
                if abs(turn) > 3.0:
                    raw_kwargs["angle_turn"] = self._raw_steer_turn_units(turn)
                action_name = "ambush_hold_wall_tuck"
                self._cautious_door_tuck_steps += 1
                self._cautious_door_ambush_hold_steps = max(self._cautious_door_ambush_hold_steps, 16)
            elif quiet_anchor_creep:
                raw_kwargs["forward_move"] = 18 if quiet_forward_open else 0
                if not quiet_forward_open and quiet_creep_side:
                    raw_kwargs["side_move"] = self._raw_side_move_for_cover_side(quiet_creep_side, 24)
                if abs(turn) > 3.0:
                    raw_kwargs["angle_turn"] = self._raw_steer_turn_units(turn)
                action_name = "ambush_hold_anchor_creep"
                self._cautious_door_ambush_hold_steps = max(self._cautious_door_ambush_hold_steps, 16)
            elif should_turn:
                raw_kwargs["angle_turn"] = self._raw_steer_turn_units(turn)
                action_name = "ambush_hold_anchor_turn" if anchor is not None else "ambush_hold_turn"
            action = agent_pb2.PlayerAction(duration_tics=2, raw=raw_cls(**raw_kwargs))
        else:
            left = getattr(agent_pb2, "ACTION_TURN_LEFT", None)
            right = getattr(agent_pb2, "ACTION_TURN_RIGHT", None)
            forward = getattr(agent_pb2, "ACTION_FORWARD", None)
            if quiet_anchor_creep and forward is not None:
                action = agent_pb2.PlayerAction(action=forward, amount=18, duration_tics=2)
                action_name = "ambush_hold_anchor_creep"
                self._cautious_door_ambush_hold_steps = max(self._cautious_door_ambush_hold_steps, 16)
            elif should_turn and left is not None and right is not None:
                action_type = left if turn > 0 else right
                action = agent_pb2.PlayerAction(action=action_type, amount=max(3, min(16, int(abs(turn)))), duration_tics=2)
                action_name = "ambush_hold_anchor_turn" if anchor is not None else "ambush_hold_turn"
            else:
                action = agent_pb2.PlayerAction(duration_tics=2)
        self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
        self._cautious_jiggle_peek_steps = max(self._cautious_jiggle_peek_steps, 4)
        index, skill = selected
        decision = {
            "source": "cautious_combat",
            "skill": "door_flash_ambush_hold",
            "state": "ambush_chokepoint",
            "action": action_name,
            "reason": "post_door_flash_hold",
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(distance),
            "turn": round(turn, 1),
            "turn_source": turn_source,
            "hold_steps": int(self._cautious_door_ambush_hold_steps),
        }
        if anchor is not None:
            decision["ambush_line"] = int(anchor.get("line_id", -1))
            decision["anchor_dist"] = int(anchor.get("distance", 0))
        decision.update(self._cover_decision_fields(cover_profile))
        return index, skill, action, decision

    def _cautious_jiggle_prefire_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        cover_profile: dict[str, Any] | None = None,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if self._cautious_jiggle_peek_steps <= 0:
            return None
        if cover_profile is None:
            return None
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        if distance > CAUTIOUS_DOOR_AMBUSH_MAX_UNITS:
            return None
        if bool(directive.contract.constraints.get("preserve_health")) and distance > 224.0:
            # ponytail: past pain-lock range the peek shot just grants the enemy a
            # free aimed reply; duck back and let him keep walking into the jamb.
            return None
        turn = float(enemy.get("turn", 0.0) or 0.0)
        if abs(turn) > 14.0:
            return None
        selected = self._planner_skill_index("fire", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        navigation = getattr(state, "navigation", None)
        if raw_cls is not None:
            side = self._best_open_cover_side(navigation) or self._best_cover_side(navigation)
            action = agent_pb2.PlayerAction(
                duration_tics=2,
                raw=raw_cls(
                    forward_move=-24 if bool(getattr(navigation, "back_open", False)) else 0,
                    side_move=self._raw_side_move_for_cover_side(side, 26) if side else 0,
                    buttons=1,
                ),
            )
            action_name = "raw_fire_bail"
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=2)
            action_name = "fire_bail"
        self._cautious_jiggle_peek_steps = 0
        self._cautious_jiggle_probe_attempts = 0
        self._cautious_probe_steps = 0
        self._cautious_lure_wait_steps = 0
        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 10)
        self._cautious_ambush_window = max(self._cautious_ambush_window, 12)
        self._cautious_threshold_cooldown = max(self._cautious_threshold_cooldown, 3)
        if directive.contract.constraints.get("preserve_health"):
            self._cautious_post_shot_scoot = True
        index, skill = selected
        decision = {
            "source": "cautious_combat",
            "skill": "jiggle_prefire_shot",
            "state": "pre_fire_peek",
            "action": action_name,
            "reason": "shootable_after_jiggle_peek",
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(distance),
            "turn": round(turn, 1),
            "cover": "door_jamb",
        }
        decision.update(self._cover_decision_fields(cover_profile))
        return index, skill, action, decision

    def _cautious_ambush_armed(self, enemy: dict[str, Any]) -> bool:
        # ponytail: a lure wakes whoever hears it, not just the aimed-at target,
        # so any enemy arriving inside the armed window is valid ambush prey.
        del enemy
        return self._cautious_ambush_window > 0

    def _cautious_finishable_threshold_target(self, enemy: dict[str, Any]) -> bool:
        if float(enemy.get("distance", 9999.0) or 9999.0) > 96.0:
            return False
        if abs(float(enemy.get("turn", 0.0) or 0.0)) > 18.0:
            return False
        return int(enemy.get("health", 999) or 999) <= 10

    def _cautious_followup_hit_target(self, enemy: dict[str, Any]) -> bool:
        if float(enemy.get("distance", 9999.0) or 9999.0) > 320.0:
            return False
        if abs(float(enemy.get("turn", 0.0) or 0.0)) > 70.0:
            return False
        return int(enemy.get("health", 999) or 999) <= 100

    def _cautious_threshold_hold_target(self, enemy: dict[str, Any]) -> bool:
        if float(enemy.get("distance", 9999.0) or 9999.0) > 160.0:
            return False
        if abs(float(enemy.get("turn", 0.0) or 0.0)) > 42.0:
            return False
        return self._cautious_target_id is None or int(enemy.get("id", 0) or 0) in {0, int(self._cautious_target_id or 0)}

    def _cautious_threshold_ambush_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        threat = str(enemy.get("threat") or "unknown")
        if threat not in {"hitscan", "unknown"}:
            return None
        preserve_health = bool(directive.contract.constraints.get("preserve_health"))
        recent_hit = self._cautious_recent_hit_window > 0 and not preserve_health
        if bool(enemy.get("visible")) and not bool(self._metrics(state).get("shootable")) and not recent_hit:
            return None
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        distance_limit = 320.0 if recent_hit else 128.0
        if distance > distance_limit:
            return None
        turn = float(enemy.get("turn", 0.0) or 0.0)
        turn_limit = 24.0 if recent_hit else 18.0
        if abs(turn) > turn_limit:
            return None
        cover_profile = self._cautious_cover_profile(state)
        if cover_profile is None:
            return None
        selected = self._planner_skill_index("fire", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        shot_tics = 2 if bool(enemy.get("visible")) or recent_hit else 4
        if raw_cls is not None:
            navigation = getattr(state, "navigation", None)
            side = self._best_cover_side(navigation) or self._best_open_cover_side(navigation)
            action = agent_pb2.PlayerAction(
                duration_tics=shot_tics,
                raw=raw_cls(side_move=self._raw_side_move_for_cover_side(side, 56), buttons=1),
            )
            action_name = "prefire_threshold"
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=shot_tics)
            action_name = "fire_threshold"
        self._cautious_probe_steps = 0
        self._cautious_lure_wait_steps = 0
        if preserve_health:
            self._cautious_post_shot_scoot = True
        index, skill = selected
        decision = {
            "source": "cautious_combat",
            "skill": "threshold_ambush_shot",
            "state": "lure_and_wait",
            "action": action_name,
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": threat,
            "dist": int(distance),
            "turn": round(turn, 1),
            "cover": "door_jamb",
        }
        decision.update(self._cover_decision_fields(cover_profile))
        return index, skill, action, decision

    def _cautious_recent_hit_followup_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if self._cautious_recent_hit_window <= 0:
            return None
        if directive.contract.constraints.get("preserve_health"):
            return None
        if not self._cautious_followup_hit_target(enemy):
            return None
        if abs(float(enemy.get("turn", 0.0) or 0.0)) > 24.0:
            return None
        selected = self._planner_skill_index("fire", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        navigation = getattr(state, "navigation", None)
        if raw_cls is not None:
            side = self._best_open_cover_side(navigation) or self._best_cover_side(navigation)
            action = agent_pb2.PlayerAction(
                duration_tics=2,
                raw=raw_cls(
                    forward_move=-24 if bool(getattr(navigation, "back_open", False)) else 0,
                    side_move=self._raw_side_move_for_cover_side(side, 44) if side else 0,
                    buttons=1,
                ),
            )
            action_name = "recent_hit_prefire"
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=2)
            action_name = "recent_hit_fire"
        index, skill = selected
        return index, skill, action, {
            "source": "cautious_combat",
            "skill": "recent_hit_followup_shot",
            "state": "lure_and_wait",
            "action": action_name,
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(enemy.get("distance", 0) or 0),
            "turn": round(float(enemy.get("turn", 0.0) or 0.0), 1),
        }

    def _cautious_open_field_align_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        turn = float(enemy.get("turn", 0.0) or 0.0)
        if abs(turn) <= 8.0:
            return None
        selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        navigation = getattr(state, "navigation", None)
        if raw_cls is not None:
            side = self._best_open_cover_side(navigation)
            action = agent_pb2.PlayerAction(
                duration_tics=4,
                raw=raw_cls(
                    forward_move=-36 if bool(getattr(navigation, "back_open", False)) else 0,
                    side_move=self._raw_side_move_for_cover_side(side, 24) if side else 0,
                    angle_turn=self._raw_steer_turn_units(turn),
                ),
            )
            action_name = "raw_reverse_align"
        else:
            left = getattr(agent_pb2, "ACTION_TURN_LEFT", None)
            right = getattr(agent_pb2, "ACTION_TURN_RIGHT", None)
            if left is None or right is None:
                return None
            action_type = left if turn > 0 else right
            action = agent_pb2.PlayerAction(action=action_type, amount=max(6, min(34, int(abs(turn)))), duration_tics=4)
            action_name = "turn_align"
        index, skill = selected
        return index, skill, action, {
            "source": "cautious_combat",
            "skill": "open_field_align_evade",
            "state": "kite_and_funnel",
            "reason": "visible_hitscan_no_cover_align_evade",
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "dist": int(enemy.get("distance", 0) or 0),
            "turn": round(turn, 1),
            "action": action_name,
            "repeat": int(self._cautious_cover_repeat_count),
        }

    def _cautious_threshold_align_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        threat = str(enemy.get("threat") or "unknown")
        if threat not in {"hitscan", "unknown"}:
            return None
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        if distance > 160.0:
            return None
        turn = float(enemy.get("turn", 0.0) or 0.0)
        if abs(turn) <= 18.0 or abs(turn) > 70.0:
            return None
        cover_profile = self._cautious_cover_profile(state)
        recent_hit_reacquire = self._cautious_recent_hit_window > 0
        if cover_profile is None and not recent_hit_reacquire:
            return None
        selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        left = getattr(agent_pb2, "ACTION_TURN_LEFT", None)
        right = getattr(agent_pb2, "ACTION_TURN_RIGHT", None)
        if left is None or right is None:
            return None
        action_type = left if turn > 0 else right
        action = agent_pb2.PlayerAction(action=action_type, amount=max(4, min(22, int(abs(turn)))), duration_tics=3)
        index, skill = selected
        decision = {
            "source": "cautious_combat",
            "skill": "threshold_align",
            "state": "lure_and_wait",
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": threat,
            "dist": int(distance),
            "turn": round(turn, 1),
            "cover": "door_jamb" if cover_profile is not None else "recent_hit_reacquire",
        }
        decision.update(self._cover_decision_fields(cover_profile))
        return index, skill, action, decision

    def _cautious_post_shot_scoot_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        reason: str = "post_shot_scoot",
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        navigation = getattr(state, "navigation", None)
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        # ponytail: lateral-first — funnel_back retreats ALONG his sightline (zero
        # angular displacement for his aim, LOS never breaks), which is exactly where
        # the traced RNG hits landed. A hard side strafe ducks behind the jamb in a
        # few tics; backpedal only when no side exists at all.
        side = self._best_open_cover_side(navigation)
        if not side and not bool(getattr(navigation, "back_open", False)):
            side = self._best_cover_side(navigation)
        if side:
            if raw_cls is not None:
                action = agent_pb2.PlayerAction(
                    duration_tics=6,
                    raw=raw_cls(
                        forward_move=-18 if bool(getattr(navigation, "back_open", False)) else 0,
                        side_move=self._raw_side_move_for_cover_side(side, 52),
                    ),
                )
            else:
                action_type = agent_pb2.ACTION_STRAFE_RIGHT if side < 0 else agent_pb2.ACTION_STRAFE_LEFT
                action = agent_pb2.PlayerAction(action=action_type, amount=34, duration_tics=6)
            kind = "break_los_right" if side < 0 else "break_los_left"
        elif raw_cls is not None:
            action = self._cautious_funnel_back_raw_action(agent_pb2, navigation, enemy, preserve_health=True)
            kind = "funnel_back_raw"
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=38, duration_tics=10)
            kind = "funnel_back"
        index, skill = selected
        return index, skill, action, {
            "source": "cautious_combat",
            "skill": kind,
            "state": "kite_and_funnel",
            "reason": reason,
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
        }

    def _cautious_cover_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        reason: str,
        enemy: dict[str, Any],
        arm_commit: bool = True,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        navigation = getattr(state, "navigation", None)
        preserve_health = bool(directive.contract.constraints.get("preserve_health"))
        recorded_repeat = False
        turn_and_burn_commit = self._critical_turn_and_burn_commit_action(
            state,
            directive,
            controller,
            modules,
            enemy=enemy,
        )
        if turn_and_burn_commit is not None:
            return turn_and_burn_commit
        turn_and_burn_recommit = self._critical_turn_and_burn_recommit_action(
            state,
            directive,
            controller,
            modules,
            enemy=enemy,
            reason=reason,
        )
        if turn_and_burn_recommit is not None:
            return turn_and_burn_recommit
        if preserve_health:
            self._cautious_target_id = int(enemy.get("id", 0) or 0)
            self._cautious_ambush_window = max(self._cautious_ambush_window, CAUTIOUS_COVER_AMBUSH_WINDOW)
        if preserve_health and self._cautious_recent_hit_window > 0 and not bool(enemy.get("visible")):
            action = agent_pb2.PlayerAction(duration_tics=4)
            index, skill = selected
            return index, skill, action, {
                "source": "cautious_combat",
                "skill": "hold_recent_hit_cover",
                "state": "lure_and_wait",
                "reason": reason,
                "enemy": int(enemy.get("id", 0) or 0),
                "threat": str(enemy.get("threat") or "unknown"),
                "hit_window": int(self._cautious_recent_hit_window),
            }
        if bool(getattr(navigation, "back_open", False)):
            raw_cls = getattr(agent_pb2, "RawTiccmd", None)
            if raw_cls is not None:
                action = self._cautious_funnel_back_raw_action(agent_pb2, navigation, enemy, preserve_health=preserve_health)
                kind = "funnel_back_raw"
            else:
                action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=38, duration_tics=10)
                kind = "funnel_back"
            repeat_count = self._record_cautious_cover_repeat(kind, reason, enemy)
            recorded_repeat = True
            return_fire = self._cautious_repeated_break_los_return_fire(
                state,
                directive,
                controller,
                modules,
                enemy=enemy,
                repeat_count=repeat_count,
            )
            if return_fire is not None:
                return return_fire
            escape = self._cautious_repeated_funnel_escape(agent_pb2, navigation, repeat_count)
            if escape is not None:
                action, kind = escape
            self._cautious_cover_side_lock = 0
            self._cautious_cover_side_lock_steps = 0
        else:
            # ponytail: standing still while VISIBLE is a free windup for any
            # hitscanner (traced: -15 after four motionless hold_cover_window
            # steps in a sniper's LOS). Holds are for hidden enemies only; when
            # seen, keep moving even without a good side.
            hold_safe = not bool(enemy.get("visible"))
            if preserve_health and hold_safe and self._cautious_cover_hold_steps > 0:
                action = agent_pb2.PlayerAction(duration_tics=4)
                kind = "hold_cover_window"
                index, skill = selected
                return index, skill, action, {
                    "source": "cautious_combat",
                    "skill": kind,
                    "state": "lure_and_wait",
                    "reason": reason,
                    "enemy": int(enemy.get("id", 0) or 0),
                    "threat": str(enemy.get("threat") or "unknown"),
                    "hold_steps": int(self._cautious_cover_hold_steps),
                }
            side = self._best_open_cover_side(navigation)
            if preserve_health and hold_safe and not side:
                action = agent_pb2.PlayerAction(duration_tics=4)
                kind = "hold_cover_no_probe"
                self._cautious_cover_hold_steps = max(self._cautious_cover_hold_steps, 8)
                index, skill = selected
                return index, skill, action, {
                    "source": "cautious_combat",
                    "skill": kind,
                    "state": "lure_and_wait",
                    "reason": reason,
                    "enemy": int(enemy.get("id", 0) or 0),
                    "threat": str(enemy.get("threat") or "unknown"),
                }
            if not side:
                side = self._best_cover_side(navigation)
            locked_side = int(self._cautious_cover_side_lock or 0)
            if preserve_health and locked_side and locked_side != side:
                if self._cover_side_available(navigation, locked_side):
                    side = locked_side
                    locked = True
                elif bool(enemy.get("visible")):
                    self._cautious_cover_side_lock = 0
                    self._cautious_cover_side_lock_steps = 0
                    locked = False
                else:
                    action = agent_pb2.PlayerAction(duration_tics=4)
                    kind = "hold_cover_lock"
                    self._cautious_cover_side_lock_steps = max(self._cautious_cover_side_lock_steps, 2)
                    index, skill = selected
                    return index, skill, action, {
                        "source": "cautious_combat",
                        "skill": kind,
                        "state": "lure_and_wait",
                        "reason": reason,
                        "enemy": int(enemy.get("id", 0) or 0),
                        "threat": str(enemy.get("threat") or "unknown"),
                        "locked_side": locked_side,
                    }
            else:
                locked = False
            raw_cls = getattr(agent_pb2, "RawTiccmd", None)
            if side < 0:
                side_name = "right"
            else:
                side_name = "left"
            if raw_cls is not None:
                action = agent_pb2.PlayerAction(
                    duration_tics=8,
                    raw=raw_cls(side_move=self._raw_side_move_for_cover_side(side, 44)),
                )
            else:
                action_type = agent_pb2.ACTION_STRAFE_RIGHT if side < 0 else agent_pb2.ACTION_STRAFE_LEFT
                action = agent_pb2.PlayerAction(action=action_type, amount=34, duration_tics=8)
            kind = f"break_los_{side_name}{'_locked' if locked else ''}"
            if preserve_health:
                self._cautious_cover_side_lock = int(side)
                self._cautious_cover_side_lock_steps = max(self._cautious_cover_side_lock_steps, 4)
            repeat_count = self._record_cautious_cover_repeat(kind, reason, enemy)
            recorded_repeat = True
            critical_breakaway = self._cautious_critical_lateral_turn_and_burn(
                state,
                directive,
                controller,
                modules,
                enemy=enemy,
                kind=str(kind),
                reason=reason,
            )
            if critical_breakaway is not None:
                return critical_breakaway
            return_fire = self._cautious_repeated_break_los_return_fire(
                state,
                directive,
                controller,
                modules,
                enemy=enemy,
                repeat_count=repeat_count,
            )
            if return_fire is not None:
                return return_fire
            healthy_push = self._cautious_healthy_break_los_push(
                state,
                directive,
                controller,
                modules,
                enemy=enemy,
                repeat_count=repeat_count,
                replaced_kind=str(kind),
            )
            if healthy_push is not None:
                return healthy_push
            escape = self._cautious_repeated_cover_escape(agent_pb2, navigation, side, repeat_count)
            if escape is not None:
                action, kind = escape
        critical_breakaway = self._cautious_critical_lateral_turn_and_burn(
            state,
            directive,
            controller,
            modules,
            enemy=enemy,
            kind=str(kind),
            reason=reason,
        )
        if critical_breakaway is not None:
            return critical_breakaway
        if not recorded_repeat and not str(kind).startswith("break_los_"):
            self._record_cautious_cover_repeat(kind, reason, enemy)
        if arm_commit and not preserve_health and self._retreat_kind_commits(str(kind)):
            self._cautious_target_id = int(enemy.get("id", 0) or self._cautious_target_id or 0)
            self._cautious_retreat_commit_steps = max(
                self._cautious_retreat_commit_steps,
                CAUTIOUS_RETREAT_COMMIT_STEPS,
            )
        index, skill = selected
        return index, skill, action, {
            "source": "cautious_combat",
            "skill": kind,
            "state": "kite_and_funnel",
            "reason": reason,
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
        }

    def _cautious_critical_lateral_turn_and_burn(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        kind: str,
        reason: str,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if not (
            kind.startswith("break_los")
            or kind.startswith("funnel_escape_side")
            or kind.startswith("funnel_escape_left")
            or kind.startswith("funnel_escape_right")
        ):
            return None
        metrics = self._metrics(state)
        try:
            health = int(metrics.get("health", 100) or 100)
        except Exception:
            health = 100
        if health <= 0 or health > ROUTE_CRITICAL_HEALTH_BREAKAWAY:
            return None
        if not bool(metrics.get("visible_enemy") or metrics.get("shootable") or enemy.get("visible")):
            return None
        try:
            distance = float(enemy.get("distance", 0.0) or 0.0)
        except Exception:
            distance = 0.0
        if distance > ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE:
            return None
        escape = self._route_threshold_turn_and_burn_action(
            state,
            directive,
            controller,
            modules,
            reason="critical_lateral_evasion_breakaway",
            enemy=enemy,
        )
        if escape is None:
            return None
        index, skill, action, decision = escape
        decision = dict(decision)
        decision.update(
            {
                "reason": "critical_lateral_evasion_breakaway",
                "previous_reason": str(reason),
                "replaced_lateral_skill": str(kind),
                "health": int(health),
                "critical_health": ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                "contact_distance": int(distance),
                "recommit_distance": int(ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE),
            }
        )
        return index, skill, action, decision

    def _cautious_funnel_back_raw_action(
        self,
        agent_pb2: Any,
        navigation: Any,
        enemy: dict[str, Any],
        *,
        preserve_health: bool,
    ) -> Any:
        raw_cls = getattr(agent_pb2, "RawTiccmd")
        side = self._best_open_cover_side(navigation)
        turn_delta = float(enemy.get("turn", 0.0) or 0.0)
        abs_turn = abs(turn_delta)
        reverse = -62 if abs_turn <= 70.0 else -46
        side_amount = 28 if preserve_health else 38
        return agent_pb2.PlayerAction(
            duration_tics=8,
            raw=raw_cls(
                forward_move=reverse,
                side_move=self._raw_side_move_for_cover_side(side, side_amount) if side else 0,
                angle_turn=self._raw_steer_turn_units(turn_delta),
            ),
        )

    def _record_cautious_cover_repeat(self, kind: str, reason: str, enemy: dict[str, Any]) -> int:
        kind_name = str(kind)
        if kind_name.startswith("break_los_"):
            group = "break_los"
        elif kind_name.startswith("funnel_back"):
            group = "funnel_back"
        else:
            self._cautious_cover_repeat_key = None
            self._cautious_cover_repeat_count = 0
            return 0
        key = (str(reason), int(enemy.get("id", 0) or 0), group)
        if key == self._cautious_cover_repeat_key:
            self._cautious_cover_repeat_count += 1
        else:
            self._cautious_cover_repeat_key = key
            self._cautious_cover_repeat_count = 1
        return self._cautious_cover_repeat_count

    def _retreat_kind_commits(self, kind: str) -> bool:
        return kind.startswith(
            (
                "funnel_back",
                "funnel_escape",
                "break_los_",
                "break_los_escape",
                "break_los_reverse",
            )
        )

    def _cautious_repeated_funnel_escape(
        self,
        agent_pb2: Any,
        navigation: Any,
        repeat_count: int,
    ) -> tuple[Any, str] | None:
        if repeat_count < 4:
            return None
        side = self._best_open_cover_side(navigation)
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if side:
            if raw_cls is not None:
                turn = -1024 if int(side) < 0 else 1024
                return (
                    agent_pb2.PlayerAction(
                        duration_tics=10,
                        raw=raw_cls(
                            forward_move=18,
                            side_move=self._raw_side_move_for_cover_side(side, 62),
                            angle_turn=turn,
                        ),
                    ),
                    "funnel_escape_side",
                )
            action_type = agent_pb2.ACTION_STRAFE_RIGHT if int(side) < 0 else agent_pb2.ACTION_STRAFE_LEFT
            side_name = "right" if int(side) < 0 else "left"
            return agent_pb2.PlayerAction(action=action_type, amount=48, duration_tics=10), f"funnel_escape_{side_name}"
        if repeat_count >= 5 and bool(getattr(navigation, "forward_open", False)):
            if raw_cls is not None:
                return (
                    agent_pb2.PlayerAction(duration_tics=10, raw=raw_cls(forward_move=46)),
                    "funnel_escape_forward",
                )
            return agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=42, duration_tics=10), "funnel_escape_forward"
        return None

    def _cautious_repeated_cover_escape(
        self,
        agent_pb2: Any,
        navigation: Any,
        side: int,
        repeat_count: int,
    ) -> tuple[Any, str] | None:
        if repeat_count < 4:
            return None
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is not None:
            if bool(getattr(navigation, "back_open", False)):
                return (
                    agent_pb2.PlayerAction(duration_tics=10, raw=raw_cls(forward_move=-62)),
                    "break_los_escape_back",
                )
            turn = -1536 if int(side) < 0 else 1536
            return (
                agent_pb2.PlayerAction(
                    duration_tics=10,
                    raw=raw_cls(
                        forward_move=42,
                        side_move=self._raw_side_move_for_cover_side(side, 62),
                        angle_turn=turn,
                    ),
                ),
                "break_los_escape_turn",
            )
        reverse = -int(side or 90)
        if self._cover_side_available(navigation, reverse):
            action_type = agent_pb2.ACTION_STRAFE_RIGHT if reverse < 0 else agent_pb2.ACTION_STRAFE_LEFT
            side_name = "right" if reverse < 0 else "left"
            return agent_pb2.PlayerAction(action=action_type, amount=48, duration_tics=10), f"break_los_reverse_{side_name}"
        return None

    def _cautious_repeated_break_los_return_fire(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        repeat_count: int,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if repeat_count < 4 or self._fire_forbidden(directive, state):
            return None
        metrics = self._metrics(state)
        if not bool(metrics.get("shootable")):
            return None
        if int(metrics.get("ammo_total", 0) or 0) <= 0 and int(metrics.get("weapon", 0) or 0) != 0:
            return None
        fired = self._cautious_peek_fire_action(
            state,
            directive,
            controller,
            modules,
            enemy=enemy,
            evade=True,
        )
        if fired is None:
            return None
        self._cautious_target_id = int(enemy.get("id", 0) or self._cautious_target_id or 0)
        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 2)
        index, skill, action, decision = fired
        decision = dict(decision)
        decision.update(
            {
                "skill": "break_los_return_fire",
                "state": "pinned_return_fire",
                "reason": "break_los_loop_return_fire",
                "repeat": int(repeat_count),
            }
        )
        return index, skill, action, decision

    def _cautious_healthy_break_los_push(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        repeat_count: int,
        replaced_kind: str,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if repeat_count < HEALTHY_BREAK_LOS_PUSH_REPEATS:
            return None
        constraints = directive.contract.constraints
        if directive.contract.objective != "complete_level":
            return None
        if (
            constraints.get("kill_budget") == 0
            or constraints.get("avoid_combat")
            or constraints.get("preserve_health")
        ):
            return None
        try:
            health = int(self._metrics(state).get("health", 100) or 100)
        except Exception:
            health = 100
        if health <= HEALTHY_BREAK_LOS_PUSH_HEALTH:
            return None
        selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            return None
        try:
            turn = float(enemy.get("turn", 0.0) or 0.0)
        except Exception:
            turn = 0.0
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is not None:
            action = agent_pb2.PlayerAction(
                duration_tics=10,
                raw=raw_cls(
                    forward_move=58,
                    angle_turn=self._raw_steer_turn_units(turn),
                ),
            )
            action_name = "healthy_push_forward_raw"
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=50, duration_tics=10)
            action_name = "healthy_push_forward"
        self._cautious_cover_repeat_key = None
        self._cautious_cover_repeat_count = 0
        index, skill = selected
        return index, skill, action, {
            "source": "cautious_combat",
            "skill": "healthy_break_los_push",
            "state": "healthy_push",
            "reason": "break_los_loop_healthy_push",
            "action": action_name,
            "replaced_lateral_skill": str(replaced_kind),
            "repeat": int(repeat_count),
            "threshold": int(HEALTHY_BREAK_LOS_PUSH_REPEATS),
            "health": int(health),
            "health_threshold": int(HEALTHY_BREAK_LOS_PUSH_HEALTH),
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "turn": round(turn, 1),
        }

    def _complete_level_survival_fire_allowed(self, state: Any, enemy: dict[str, Any]) -> bool:
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        if distance > 128.0:
            return False
        return not self._cautious_escape_ready(getattr(state, "navigation", None))

    def _cautious_cover_ready(self, state: Any) -> bool:
        return self._cautious_cover_profile(state) is not None

    def _cautious_cover_profile(self, state: Any) -> dict[str, Any] | None:
        navigation = getattr(state, "navigation", None)
        if navigation is None:
            return None
        front_open = bool(getattr(navigation, "forward_open", False))
        front_distance = int(getattr(navigation, "front_block_distance_fp", 0) or 0)
        front_cover = (not front_open) or (front_distance > 0 and front_distance <= 48 * FP_UNIT)
        if not front_cover:
            return None
        if not self._cautious_escape_ready(navigation):
            return None
        threshold = self._cautious_threshold_profile(state, navigation)
        if threshold is not None:
            return threshold
        return {"cover_evidence": "local_probe", "cover_dist": int(front_distance / FP_UNIT) if front_distance else 0}

    def _cautious_escape_ready(self, navigation: Any) -> bool:
        if bool(getattr(navigation, "back_open", False)):
            return True
        for probe in getattr(navigation, "direction_probes", []) or []:
            if not bool(getattr(probe, "open", False)):
                continue
            offset = abs(int(getattr(probe, "angle_offset_degrees", 0) or 0))
            if 45 <= offset <= 135:
                return True
        return False

    def _cautious_threshold_profile(self, state: Any, navigation: Any) -> dict[str, Any] | None:
        waypoint = getattr(navigation, "route_waypoint", None)
        waypoint_line = getattr(waypoint, "line", None)
        if waypoint_line is not None:
            profile = self._cautious_line_cover_profile(
                waypoint_line,
                source="route_waypoint",
                extra={
                    "route_priority": int(getattr(waypoint, "priority", 0) or 0),
                    "route_exit": bool(getattr(waypoint, "exit", False)),
                    "route_walk": bool(getattr(waypoint, "walk_trigger", False)),
                },
            )
            if profile is not None:
                return profile
        best: dict[str, Any] | None = None
        for raw_line in getattr(navigation, "use_lines", []) or []:
            profile = self._cautious_line_cover_profile(raw_line, source="near_use_line")
            if profile is None:
                continue
            if best is None or int(profile.get("cover_dist", 9999)) < int(best.get("cover_dist", 9999)):
                best = profile
        if best is not None:
            return best
        if bool(getattr(navigation, "use_line_ahead", False)) or int(getattr(navigation, "front_blocking_line_special", 0) or 0) != 0:
            return {
                "cover_evidence": "front_special",
                "cover_special": int(getattr(navigation, "front_blocking_line_special", 0) or 0),
            }
        return self._cautious_portal_cover_profile(state, navigation)

    def _cautious_line_cover_profile(self, raw_line: Any, *, source: str, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
        try:
            line_id = int(getattr(raw_line, "line_id", -1))
        except Exception:
            line_id = -1
        special = int(getattr(raw_line, "special", 0) or 0)
        distance_fp = int(getattr(raw_line, "nearest_distance_fp", 0) or getattr(raw_line, "distance_fp", 0) or 0)
        if distance_fp and distance_fp > 192 * FP_UNIT:
            return None
        static_portal = self._planner_line_is_portal(line_id)
        if special == 0 and not static_portal:
            return None
        profile: dict[str, Any] = {
            "cover_evidence": source,
            "cover_line": line_id,
            "cover_special": special,
            "cover_dist": int(distance_fp / FP_UNIT) if distance_fp else 0,
        }
        if static_portal:
            profile["cover_portal"] = True
        if extra:
            profile.update(extra)
        return profile

    def _cautious_portal_cover_profile(self, state: Any, navigation: Any) -> dict[str, Any] | None:
        planner = self._planner
        if planner is None:
            return None
        player_from_state = getattr(planner, "player_from_state", None)
        line_by_id = getattr(planner, "_line_by_id", None)
        nearest_point = getattr(planner, "_nearest_point_on_line", None)
        if not callable(player_from_state) or not callable(line_by_id) or not callable(nearest_point):
            return None
        player = player_from_state(state)
        if player is None:
            return None
        current = getattr(navigation, "current_sector", None)
        current_id = getattr(current, "sector_id", None)
        if current_id is None:
            sector_for_player = getattr(planner, "sector_for_player", None)
            if not callable(sector_for_player):
                return None
            current_id = sector_for_player(state, player)
        if current_id is None:
            return None
        graph = getattr(planner, "_portal_graph", {})
        best: dict[str, Any] | None = None
        for edge in graph.get(int(current_id), []) or []:
            try:
                line_id = int(getattr(edge, "line_id", -1))
            except Exception:
                line_id = -1
            line = line_by_id(line_id)
            if line is None:
                continue
            point = nearest_point(player["point"], line)
            distance_fp = int(math.hypot(point.x - player["point"].x, point.y - player["point"].y))
            if distance_fp > 192 * FP_UNIT:
                continue
            if not (bool(getattr(edge, "use_line", False)) or bool(getattr(edge, "door", False)) or bool(getattr(edge, "passable", False))):
                continue
            profile = {
                "cover_evidence": "portal_graph",
                "cover_line": line_id,
                "cover_special": int(getattr(edge, "special", 0) or 0),
                "cover_dist": int(distance_fp / FP_UNIT),
                "cover_portal": True,
            }
            if best is None or profile["cover_dist"] < int(best.get("cover_dist", 9999)):
                best = profile
        return best

    def _planner_line_is_portal(self, line_id: int) -> bool:
        if line_id < 0 or self._planner is None:
            return False
        line_by_id = getattr(self._planner, "_line_by_id", None)
        if not callable(line_by_id):
            return False
        line = line_by_id(int(line_id))
        if line is None:
            return False
        front_sector = int(getattr(line, "front_sector", -1) or -1)
        back_sector = int(getattr(line, "back_sector", -1) or -1)
        return front_sector >= 0 and back_sector >= 0

    def _cover_decision_fields(self, cover_profile: dict[str, Any] | None) -> dict[str, Any]:
        if not cover_profile:
            return {}
        keys = ("cover_evidence", "cover_line", "cover_special", "cover_dist", "cover_portal")
        return {key: cover_profile[key] for key in keys if key in cover_profile}

    def _cautious_wait_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        reason: str,
        enemy: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        index, skill = selected
        # ponytail: 4-tic waits react to an arriving enemy twice as fast as the old
        # 8-tic waits; the lure dance is the pacing bottleneck for timed evals.
        action = agent_pb2.PlayerAction(duration_tics=4)
        # Pre-aim at the tracked hidden enemy while waiting: when he rounds the
        # corner our first bullet beats his 8-tic reaction window instead of
        # spotting him, aligning, and eating his already-queued shot.
        turn = float(enemy.get("turn", 0.0) or 0.0)
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        # ponytail: sonar ping — a parked hidden hitscanner never takes a silent
        # lure (traced: 6/7 episodes stalled at max_tics with enemies 280-380u out).
        # Every ~100 waited tics, fire one round into the wall we hide behind: the
        # noise re-targets monster pathing to our recessed ambush spot and walks
        # them through the jamb into the crosshair. No movement — stay hidden.
        self._cautious_lure_ping_wait_tics += 4
        if distance <= 150.0:
            # The lure is working — he's closing. Fresh escalation budget.
            self._cautious_lure_ping_count = 0
        if self._cautious_lure_ping_wait_tics > 100 and distance > 150.0:
            if self._cautious_lure_ping_count >= 3:
                # ponytail: flush 'em out — 3 unanswered pings means he's not
                # coming (geometry-stuck). Drop the ambush state and yield so the
                # spatial planner routes to him; contact re-arms normal combat.
                self._cautious_lure_ping_count = 0
                self._cautious_lure_ping_wait_tics = 0
                self._cautious_ambush_window = 0
                self._cautious_lure_wait_steps = 0
                self._cautious_door_ambush_hold_steps = 0
                self._cautious_door_ambush_hold_spent = 0
                return None
            shoot = getattr(agent_pb2, "ACTION_SHOOT", None)
            if raw_cls is not None or shoot is not None:
                self._cautious_lure_ping_wait_tics = 0
                self._cautious_lure_ping_count += 1
                # Fresh lure round: the ping may pull him in, so allow re-tucking
                # flush beside the door plane while we wait.
                self._cautious_door_tuck_steps = 0
                fire_selected = self._planner_skill_index("fire", controller, state, modules, directive) or selected
                if raw_cls is not None:
                    ping_action = agent_pb2.PlayerAction(duration_tics=2, raw=raw_cls(buttons=1))
                    ping_name = "raw_fire"
                else:
                    ping_action = agent_pb2.PlayerAction(action=shoot, amount=1, duration_tics=2)
                    ping_name = "fire"
                ping_index, ping_skill = fire_selected
                return ping_index, ping_skill, ping_action, {
                    "source": "cautious_combat",
                    "skill": "sustained_lure_ping",
                    "state": "lure_and_wait",
                    "action": ping_name,
                    "reason": f"sonar_ping:{reason}",
                    "enemy": int(enemy.get("id", 0) or 0),
                    "threat": str(enemy.get("threat") or "unknown"),
                    "dist": int(distance) if distance < 9999.0 else -1,
                }
        # Trap geometry from the lure wait too: while he's hidden and we're parked
        # near the door plane, spend wait steps tucking flush beside the opening so
        # he emerges point-blank into the pre-aimed shot instead of seeing us
        # framed in the doorway. (The door-hold path has the same move; most long
        # waits happen HERE, which is why the hold-only tuck never engaged.)
        if raw_cls is not None and not bool(enemy.get("visible")) and self._cautious_door_tuck_steps < 6:
            navigation = getattr(state, "navigation", None)
            anchor = self._door_ambush_anchor(state)
            if anchor is not None and int(anchor.get("distance", 0) or 0) <= 160:
                tuck_side, tuck_wall_dist = self._door_tuck_side(navigation)
                if tuck_side and tuck_wall_dist > 24.0:
                    self._cautious_door_tuck_steps += 1
                    anchor_turn = float(anchor.get("turn", 0.0) or 0.0)
                    action = agent_pb2.PlayerAction(
                        duration_tics=2,
                        raw=raw_cls(
                            side_move=self._raw_side_move_for_cover_side(tuck_side, 36),
                            angle_turn=self._raw_steer_turn_units(anchor_turn) if abs(anchor_turn) > 3.0 else 0,
                        ),
                    )
                    return index, skill, action, {
                        "source": "cautious_combat",
                        "skill": "lure_wall_tuck",
                        "state": "lure_and_wait",
                        "action": "ambush_hold_wall_tuck",
                        "reason": f"wall_tuck:{reason}",
                        "enemy": int(enemy.get("id", 0) or 0),
                        "threat": str(enemy.get("threat") or "unknown"),
                        "anchor_dist": int(anchor.get("distance", 0) or 0),
                        "tuck_steps": int(self._cautious_door_tuck_steps),
                    }
        if raw_cls is not None and distance <= 320.0 and abs(turn) > 5.0:
            # Same 4-tic cadence as the plain wait: the wait-step counters gate the
            # jiggle escalation, so shorter pre-aim steps would double the peek rate.
            action = agent_pb2.PlayerAction(duration_tics=4, raw=raw_cls(angle_turn=self._raw_steer_turn_units(turn) // 2))
        return index, skill, action, {
            "source": "cautious_combat",
            "skill": "lure_and_wait",
            "state": "lure_and_wait",
            "reason": reason,
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
        }

    def _door_tuck_side(self, navigation: Any) -> tuple[int, float]:
        """Nearest OPEN lateral probe: the closest wall we can flush up against.

        Opposite selection from _best_open_cover_side (deepest side) — tucking
        wants the shortest sidestep to a wall beside the door plane.
        """
        nearest_offset = 0
        nearest_units = 0.0
        for probe in getattr(navigation, "direction_probes", []) or []:
            if not bool(getattr(probe, "open", False)):
                continue
            offset = int(getattr(probe, "angle_offset_degrees", 0) or 0)
            if abs(offset) < 45 or abs(offset) > 135:
                continue
            units = float(getattr(probe, "block_distance_fp", 0) or 0) / float(FP_UNIT)
            if not nearest_offset or units < nearest_units:
                nearest_offset = offset
                nearest_units = units
        return nearest_offset, nearest_units

    def _best_cover_side(self, navigation: Any) -> int:
        best_offset = 90
        best_distance = -1
        for probe in getattr(navigation, "direction_probes", []) or []:
            if not bool(getattr(probe, "open", False)):
                continue
            offset = int(getattr(probe, "angle_offset_degrees", 0) or 0)
            if abs(offset) < 45 or abs(offset) > 135:
                continue
            distance = int(getattr(probe, "block_distance_fp", 0) or 0)
            if distance > best_distance:
                best_distance = distance
                best_offset = offset
        return best_offset

    def _cover_side_available(self, navigation: Any, side: int) -> bool:
        side = int(side or 0)
        if side == 0:
            return False
        for probe in getattr(navigation, "direction_probes", []) or []:
            if not bool(getattr(probe, "open", False)):
                continue
            offset = int(getattr(probe, "angle_offset_degrees", 0) or 0)
            if abs(offset) < 45 or abs(offset) > 135:
                continue
            if (offset > 0) == (side > 0):
                return True
        return False

    def _cautious_jiggle_side(self, navigation: Any) -> int:
        side = self._best_open_cover_side(navigation) or self._best_cover_side(navigation)
        if not side:
            return side
        attempts = int(self._cautious_jiggle_probe_attempts or 0)
        if attempts < 2:
            return side
        alternate = -int(side)
        if self._cover_side_available(navigation, alternate) or attempts >= 4:
            if (attempts // 2) % 2 == 1:
                return alternate
        return side

    def _close_enemy_count(self, state: Any, radius: float = 256.0) -> int:
        player = getattr(state, "player", None)
        pos = getattr(getattr(player, "object", None), "position", None)
        px = int(getattr(pos, "x_fp", 0))
        py = int(getattr(pos, "y_fp", 0))
        count = 0
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            epos = getattr(obj, "position", None)
            if obj is None or epos is None or int(getattr(obj, "health", 0) or 0) <= 0:
                continue
            dx = (int(getattr(epos, "x_fp", 0)) - px) / FP_UNIT
            dy = (int(getattr(epos, "y_fp", 0)) - py) / FP_UNIT
            if math.hypot(dx, dy) <= radius:
                count += 1
        return count

    def _cautious_post_kill_los_peek_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        action: Any,
        decision: dict[str, Any],
        action_summary: dict[str, Any],
        agent_pb2: Any,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if directive.contract.objective != "clear_area":
            return None
        if int(self._metrics(state).get("kills", 0) or 0) <= int(self._objective_baseline_kills):
            return None
        if str(decision.get("source", "")) != "spatial_planner":
            return None
        planner_skill = str(decision.get("planner_skill") or decision.get("skill", ""))
        if planner_skill not in {"planner_route_to_los", "sector_route_to_los"}:
            return None
        if str(decision.get("action", "")) not in {"forward", "follow_opening", "cross_passable_portal"}:
            return None
        try:
            distance = int(decision.get("dist", 9999) or 9999)
        except Exception:
            distance = 9999
        if distance > 176:
            return None
        action_type = int(getattr(action, "action", 0) or 0)
        raw = action_summary.get("raw") if isinstance(action_summary, dict) else {}
        try:
            raw_forward = int(raw.get("forward_move", 0) or 0) > 0
        except Exception:
            raw_forward = False
        if action_type != int(agent_pb2.ACTION_FORWARD) and not raw_forward:
            return None
        selected = self._planner_skill_index("fire", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        navigation = getattr(state, "navigation", None)
        if raw_cls is not None:
            side = self._best_open_cover_side(navigation)
            peek_action = agent_pb2.PlayerAction(
                duration_tics=2,
                raw=raw_cls(
                    forward_move=22,
                    side_move=self._raw_side_move_for_cover_side(side, 28) if side else 0,
                    buttons=1,
                ),
            )
            action_name = "raw_peek_fire"
        else:
            peek_action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=2)
            action_name = "peek_fire"
        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 8)
        self._cautious_ambush_window = max(self._cautious_ambush_window, 12)
        self._cautious_threshold_cooldown = 1
        index, skill = selected
        return index, skill, peek_action, {
            "source": "cautious_combat",
            "skill": "post_kill_los_peek_shot",
            "state": "slice_the_pie",
            "action": action_name,
            "route_skill": planner_skill,
            "dist": distance,
        }

    def _nearest_use_line_distance_units(self, state: Any) -> int | None:
        info = self._nearest_use_line_info(state)
        return int(info["distance_units"]) if info is not None else None

    def _nearest_use_line_info(self, state: Any) -> dict[str, Any] | None:
        navigation = getattr(state, "navigation", None)
        best: dict[str, Any] | None = None
        for line in getattr(navigation, "use_lines", []) or []:
            distance = int(getattr(line, "nearest_distance_fp", 0) or getattr(line, "distance_fp", 0) or 0)
            if distance <= 0:
                continue
            try:
                line_id = int(getattr(line, "line_id", -1))
            except Exception:
                line_id = -1
            info = {
                "line_id": line_id if line_id >= 0 else None,
                "distance_fp": distance,
                "distance_units": int(distance / FP_UNIT),
                "special": int(getattr(line, "special", 0) or 0),
            }
            if best is None or int(info["distance_fp"]) < int(best["distance_fp"]):
                best = info
        return best

    def _door_ambush_line_id(
        self,
        state: Any,
        *,
        cover_profile: dict[str, Any] | None = None,
        use_info: dict[str, Any] | None = None,
    ) -> int | None:
        candidates = [
            getattr(use_info, "get", lambda _key, _default=None: _default)("line_id", None) if use_info is not None else None,
            cover_profile.get("cover_line") if cover_profile else None,
        ]
        if use_info is None:
            nearest = self._nearest_use_line_info(state)
            candidates.append(nearest.get("line_id") if nearest is not None else None)
        for raw in candidates:
            try:
                line_id = int(raw)
            except Exception:
                continue
            if line_id >= 0:
                return line_id
        return None

    def _door_ambush_anchor(self, state: Any) -> dict[str, Any] | None:
        line_id = self._cautious_door_ambush_line_id
        if line_id is None:
            line_id = self._door_ambush_line_id(state)
        if line_id is None or self._planner is None:
            return None
        line_by_id = getattr(self._planner, "_line_by_id", None)
        if not callable(line_by_id):
            return None
        line = line_by_id(int(line_id))
        midpoint = getattr(line, "midpoint", None)
        if midpoint is None:
            return None
        player = getattr(state, "player", None)
        obj = getattr(player, "object", None)
        pos = getattr(obj, "position", None)
        if pos is None:
            return None
        px = int(getattr(pos, "x_fp", 0) or 0)
        py = int(getattr(pos, "y_fp", 0) or 0)
        mx = int(getattr(midpoint, "x", getattr(midpoint, "x_fp", 0)) or 0)
        my = int(getattr(midpoint, "y", getattr(midpoint, "y_fp", 0)) or 0)
        dx = (mx - px) / FP_UNIT
        dy = (my - py) / FP_UNIT
        distance = math.hypot(dx, dy)
        angle = float(getattr(obj, "angle_degrees", 0) or 0)
        bearing = math.degrees(math.atan2(dy, dx)) % 360.0 if distance else angle
        turn = ((bearing - angle + 540.0) % 360.0) - 180.0
        return {
            "line_id": int(line_id),
            "turn": turn,
            "distance": int(distance),
        }
