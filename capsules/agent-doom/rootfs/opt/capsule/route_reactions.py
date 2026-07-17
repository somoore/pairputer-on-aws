#!/usr/bin/env python3.11
"""Reactive route overrides for the Agent DOOM brain.

Owns the reactive machinery wrapped around planner routing: route-threshold
threat refusals (hold/refuse fire, clean-shot adjustment, hitscan flinch,
melee-rush handling, critical-health breakaway and turn-and-burn), no-kill
route refusal and desperation sprints, rotational-stall and anti-grind escape
trackers, and the repeated failed-use / probe-explore escapes with their
failed-use signature tracking. Extracted verbatim from
brain_runtime.BrainRuntime.

RouteReactionsMixin is a mixin over BrainRuntime state: every method runs on
the BrainRuntime instance, reads/writes attributes initialized in
BrainRuntime.__init__, and calls shared BrainRuntime helpers via self. It
holds no state of its own. This module must not import brain_runtime at
runtime.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from cautious_combat import (
    CAUTIOUS_RETREAT_COMMIT_STEPS,
    FP_UNIT,
    ROUTE_CRITICAL_HEALTH_BREAKAWAY,
    ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE,
)
from planner import ROUTE_THREAT_REFUSE_MULT, SpatialPlanner
from threat_model import classify_enemy

if TYPE_CHECKING:
    from brain_runtime import ObjectiveDirective

# Constants used by the reactive route machinery. Shared ones are imported
# back into brain_runtime (which re-exports them for existing importers).
MELEE_RUSH_THREATS = {"melee", "melee_rush"}
ROUTE_THREAT_RELEASE_MULT = 25.0
ROUTE_THREAT_HOLD_MIN_TICS = 28
ROUTE_CLEAN_SHOT_TURN_DEGREES = 6.0
ROUTE_HITSCAN_FLINCH_STEPS = 3
ROUTE_CRITICAL_TURN_AND_BURN_TICS = 10
ROUTE_CRITICAL_TURN_AND_BURN_COMMIT_STEPS = 3
ROUTE_CRITICAL_TURN_AND_BURN_STALL_THRESHOLD = 2
ROUTE_CRITICAL_TURN_AND_BURN_DEFLECT_STEPS = 2
ROUTE_CRITICAL_TURN_AND_BURN_DEFLECT_DEGREES = 60.0
ROUTE_CRITICAL_TURN_AND_BURN_MAX_CHAIN = 8
ROUTE_MELEE_RUSH_HOLD_DISTANCE = 320.0
HEALTH_ROUTE_SKILLS = {"route_to_health", "sector_route_to_health", "visible_health_probe"}
ROTATIONAL_STALL_ROUTE_SKILLS = HEALTH_ROUTE_SKILLS | {
    "center_passable_portal",
    "navcell_to_portal",
    "sector_route_hazard_escape",
    "sector_route_to_exit_line",
    "sector_route_to_use_line",
}
ROTATIONAL_STALL_TURN_THRESHOLD = 4
ROTATIONAL_STALL_ESCAPE_STEPS = 2
NO_KILL_DESPERATION_PANIC_REPEATS = 2
NO_KILL_DESPERATION_SPRINT_TICS = 24
NO_KILL_DESPERATION_SPRINT_BURST_TICS = 10
NO_KILL_ROUTE_REFUSAL_MULT = 100.0
NO_KILL_DESPERATION_PANIC_ACTIONS = {
    "break_los_low_health",
    "panic_escape_side",
    "panic_run_past",
    "panic_sidestep_close_blocker",
}
ANTI_GRIND_STUCK_THRESHOLD = 2
ANTI_GRIND_ESCAPE_STEPS = 2
ANTI_GRIND_MOVE_EPS_UNITS = 6.0
ANTI_GRIND_RESET_MOVE_UNITS = 24.0
ANTI_GRIND_SKILLS = {
    "center_passable_portal",
    "frontier_probe_escape",
    "frontier_escape_probe",
    "frontier_sector_route",
    "remembered_progression_probe",
    "remembered_exit_probe",
    "sector_route_to_exit_line",
    "sector_route_to_use_line",
    "live_probe_strafe",
    "live_probe_escape_strafe",
    "live_probe_escape_backoff",
    "navcell_to_portal",
}
ANTI_GRIND_ACTIONS = {
    "approach",
    "backoff",
    "close_strafe",
    "center_passable_portal",
    "center_passable_portal_raw",
    "follow_opening_strafe",
    "forward",
    "probe_strafe",
    "steer_forward",
    "strafe",
    "squeeze_passable_portal",
}
USE_STUCK_THRESHOLD = 3
USE_STUCK_ESCAPE_STEPS = 3
USE_STUCK_MOVE_EPS_UNITS = 6.0


class RouteReactionsMixin:
    """Reactive route refusal/escape machinery, mixed into BrainRuntime."""

    def _route_threshold_refusal_fire(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        plan_skill: str,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if str(plan_skill) != "route_progression":
            return None
        if directive.contract.objective != "complete_level":
            self._clear_route_refusal_hold()
            return None
        constraints = directive.contract.constraints
        rules = set(directive.rules)
        if (
            constraints.get("kill_budget") == 0
            or constraints.get("avoid_combat")
            or constraints.get("ammo_budget") == 0
            or constraints.get("weapon_policy") == "fist_only"
            or constraints.get("preserve_health")
            or "cease_fire" in rules
            or self._fire_forbidden(directive, state)
        ):
            self._clear_route_refusal_hold()
            return None
        try:
            threat_mult = float(decision.get("route_step_threat_mult", 1.0) or 1.0)
        except Exception:
            threat_mult = 1.0
        metrics = self._metrics(state)
        if self._e1m1_near_final_exit_commit(metrics) and self._e1m1_final_exit_route_line(decision):
            self._clear_route_refusal_hold()
            return None
        route_key = self._route_refusal_key(decision)
        existing_hold = self._route_refusal_hold_key is not None
        same_hold = existing_hold and route_key == self._route_refusal_hold_key
        triggered = threat_mult >= ROUTE_THREAT_REFUSE_MULT
        melee_threat = self._route_refusal_melee_threat(state)
        if triggered:
            self._route_refusal_melee_target_id = int(melee_threat.get("id", 0) or 0) if melee_threat is not None else 0
        melee_hold = melee_threat is not None and (
            existing_hold or triggered or int(self._route_refusal_melee_target_id or 0) > 0
        )
        tracking_threat = bool(metrics.get("shootable")) or (
            bool(metrics.get("visible_enemy")) and self._route_refusal_hold_tics > 0
        ) or bool(melee_hold)
        sustained = bool(existing_hold and (same_hold or tracking_threat) and (
            self._route_refusal_hold_tics > 0
            or threat_mult >= ROUTE_THREAT_RELEASE_MULT
            or bool(metrics.get("shootable"))
            or bool(melee_hold)
        ))
        if not triggered and not sustained:
            if existing_hold and threat_mult < ROUTE_THREAT_RELEASE_MULT and not tracking_threat:
                self._clear_route_refusal_hold()
            return None
        if int(metrics.get("weapon", 0) or 0) == 0 or int(metrics.get("ammo_total", 0) or 0) <= 0:
            self._clear_route_refusal_hold()
            return self._safe_contract_action(
                state,
                directive,
                controller,
                modules,
                reason="route_threshold_no_ammo",
            )
        if triggered:
            self._route_refusal_hold_key = route_key
            self._route_refusal_hold_tics = max(self._route_refusal_hold_tics, ROUTE_THREAT_HOLD_MIN_TICS)
            self._route_refusal_hold_peak = max(self._route_refusal_hold_peak, threat_mult)
        elif self._route_refusal_hold_key is None:
            self._route_refusal_hold_key = route_key
        self._route_refusal_hold_tics = max(0, self._route_refusal_hold_tics)
        flinch_breakaway = self._route_threshold_hitscan_flinch_breakaway(
            state,
            directive,
            controller,
            modules,
            decision,
            metrics,
            threat_mult=threat_mult,
            plan_skill=plan_skill,
            sustained=sustained,
            triggered=triggered,
        )
        if flinch_breakaway is not None:
            return flinch_breakaway
        if melee_hold and melee_threat is not None:
            melee_action = self._route_threshold_melee_rush_action(
                state,
                directive,
                controller,
                modules,
                decision,
                metrics,
                melee_threat,
                threat_mult=threat_mult,
                plan_skill=plan_skill,
                sustained=sustained,
                triggered=triggered,
            )
            if melee_action is not None:
                return melee_action
        critical_breakaway = self._route_threshold_critical_health_breakaway(
            state,
            directive,
            controller,
            modules,
            decision,
            metrics,
            threat_mult=threat_mult,
            plan_skill=plan_skill,
            sustained=sustained,
            triggered=triggered,
        )
        if critical_breakaway is not None:
            return critical_breakaway
        clean_adjustment = self._route_threshold_clean_shot_adjustment(
            state,
            directive,
            controller,
            modules,
            decision,
            metrics,
            threat_mult=threat_mult,
            plan_skill=plan_skill,
            sustained=sustained,
            triggered=triggered,
        )
        if clean_adjustment is not None:
            return clean_adjustment
        selected = self._planner_skill_index("fire", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        try:
            turn = float(decision.get("turn", 0.0) or 0.0)
        except Exception:
            turn = 0.0
        if raw_cls is not None:
            action = agent_pb2.PlayerAction(
                duration_tics=4,
                raw=raw_cls(angle_turn=self._raw_steer_turn_units(turn), buttons=1),
            )
            action_name = "raw_fire"
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=4)
            action_name = "fire"
        duration = int(getattr(action, "duration_tics", 1) or 1)
        hold_before = int(self._route_refusal_hold_tics)
        hold_peak = round(float(self._route_refusal_hold_peak), 2)
        self._route_refusal_hold_tics = max(0, self._route_refusal_hold_tics - duration)
        index, skill = selected
        refusal_decision = dict(decision)
        refusal_decision.update(
            {
                "source": "route_threshold_refusal",
                "skill": "threshold_route_refusal_fire",
                "action": action_name,
                "state": "fatal_funnel_hold",
                "refused_skill": plan_skill,
                "refused_route_skill": str(decision.get("skill", "")),
                "route_step_threat_mult": round(threat_mult, 2),
                "threshold": ROUTE_THREAT_REFUSE_MULT,
                "release_threshold": ROUTE_THREAT_RELEASE_MULT,
                "hold_tics": hold_before,
                "hold_peak": hold_peak,
                "sustained": int(bool(sustained and not triggered)),
                "shootable": int(bool(metrics.get("shootable"))),
                "visible_enemy": int(bool(metrics.get("visible_enemy"))),
            }
        )
        return index, skill, action, refusal_decision

    def _final_corridor_sprint_health_refusal(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        plan_skill: str,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if str(decision.get("action", "")) != "final_corridor_sprint_opening":
            return None
        if directive.contract.objective != "complete_level":
            return None
        metrics = self._metrics(state)
        try:
            health = int(metrics.get("health", 100) or 100)
        except Exception:
            health = 100
        if health > ROUTE_CRITICAL_HEALTH_BREAKAWAY:
            return None
        if self._e1m1_near_final_exit_commit(metrics):
            return None
        if not bool(metrics.get("visible_enemy") or metrics.get("shootable")):
            return None
        forced = dict(decision)
        forced["route_step_threat_mult"] = max(
            ROUTE_THREAT_REFUSE_MULT,
            float(forced.get("route_step_threat_mult", 1.0) or 1.0),
        )
        line_id = forced.get("line_id", forced.get("line"))
        if line_id is not None:
            forced.setdefault("route_step_line", line_id)
        forced.setdefault("route_step_kind", "final_corridor_opening")
        forced["refused_final_corridor_action"] = "final_corridor_sprint_opening"
        refusal = self._route_threshold_refusal_fire(
            state,
            directive,
            controller,
            modules,
            "route_progression",
            forced,
        )
        if refusal is None:
            return None
        index, skill, action, refusal_decision = refusal
        refusal_decision.update(
            {
                "final_corridor_sprint_refused": 1,
                "refused_skill": str(plan_skill),
                "refused_route_skill": str(decision.get("skill", "")),
                "refused_action": "final_corridor_sprint_opening",
                "health": int(health),
                "critical_health": ROUTE_CRITICAL_HEALTH_BREAKAWAY,
            }
        )
        for key in ("line", "line_id", "special", "dist", "target"):
            if key in decision:
                refusal_decision[key] = decision[key]
        return index, skill, action, refusal_decision

    def _health_route_threat_refusal(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        plan_skill: str,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if str(plan_skill) != "route_progression":
            return None
        route_skill = str(decision.get("skill", ""))
        if route_skill not in HEALTH_ROUTE_SKILLS:
            return None
        try:
            threat_mult = float(decision.get("route_step_threat_mult", 1.0) or 1.0)
        except Exception:
            threat_mult = 1.0
        metrics = self._metrics(state)
        try:
            health = int(metrics.get("health", 100) or 100)
        except Exception:
            health = 100
        visible_contact = bool(metrics.get("visible_enemy") or metrics.get("shootable"))
        lethal_step = threat_mult >= ROUTE_THREAT_REFUSE_MULT
        critical_contact = health <= ROUTE_CRITICAL_HEALTH_BREAKAWAY and visible_contact
        if not (lethal_step or critical_contact):
            return None
        enemy = self._nearest_enemy(state, prefer_visible=True)
        if enemy is None:
            try:
                turn = float(decision.get("turn", 0.0) or 0.0)
            except Exception:
                turn = 0.0
            enemy = {
                "id": 0,
                "turn": turn,
                "distance": 0.0,
                "visible": visible_contact,
                "threat": "unknown",
            }
        reason = "health_route_critical_breakaway" if critical_contact else "health_route_threat_refusal"
        if critical_contact:
            refusal = self._route_threshold_turn_and_burn_action(
                state,
                directive,
                controller,
                modules,
                reason=reason,
                enemy=enemy,
            )
        else:
            refusal = self._cautious_cover_action(
                state,
                directive,
                controller,
                modules,
                reason=reason,
                enemy=enemy,
            )
        if refusal is None:
            return None
        self._clear_route_refusal_hold()
        index, skill, action, refusal_decision = refusal
        refusal_decision.update(
            {
                "source": "health_route_refusal",
                "skill": str(refusal_decision.get("skill") or "health_route_threat_refusal"),
                "state": "health_route_refusal",
                "reason": reason,
                "refused_skill": plan_skill,
                "refused_route_skill": route_skill,
                "route_step_threat_mult": round(float(threat_mult), 2),
                "threshold": ROUTE_THREAT_REFUSE_MULT,
                "critical_health": ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                "health": int(health),
                "critical_contact": int(bool(critical_contact)),
                "lethal_step": int(bool(lethal_step)),
                "shootable": int(bool(metrics.get("shootable"))),
                "visible_enemy": int(bool(metrics.get("visible_enemy"))),
            }
        )
        for key in (
            "line",
            "line_id",
            "route_step_line",
            "route_step_sector",
            "route_step_kind",
            "route_step_special",
            "route_step_use_line",
        ):
            if key in decision:
                refusal_decision[key] = decision[key]
        return index, skill, action, refusal_decision

    def _route_threshold_critical_health_breakaway(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        decision: dict[str, Any],
        metrics: dict[str, Any],
        *,
        threat_mult: float,
        plan_skill: str,
        sustained: bool,
        triggered: bool,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        try:
            health = int(metrics.get("health", 0) or 0)
        except Exception:
            health = 0
        if health <= 0 or health > ROUTE_CRITICAL_HEALTH_BREAKAWAY:
            return None
        if not bool(metrics.get("visible_enemy") or metrics.get("shootable")):
            return None
        enemy = self._nearest_enemy(state, prefer_visible=True)
        if enemy is None:
            try:
                turn = float(decision.get("turn", 0.0) or 0.0)
            except Exception:
                turn = 0.0
            enemy = {
                "id": 0,
                "turn": turn,
                "distance": 0.0,
                "visible": bool(metrics.get("visible_enemy")),
                "threat": "unknown",
            }
        try:
            distance = float(enemy.get("distance", 0.0) or 0.0)
        except Exception:
            distance = 0.0
        if distance > ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE:
            return None
        hold_before = int(self._route_refusal_hold_tics)
        hold_peak = round(float(self._route_refusal_hold_peak), 2)
        cover = self._route_threshold_turn_and_burn_action(
            state,
            directive,
            controller,
            modules,
            reason="route_threshold_critical_health_breakaway",
            enemy=enemy,
        )
        if cover is None:
            return None
        self._clear_route_refusal_hold()
        index, skill, action, breakaway_decision = cover
        breakaway_decision.update(
            {
                "source": "route_threshold_refusal",
                "state": "critical_health_breakaway",
                "reason": "route_threshold_critical_health_breakaway",
                "refused_skill": plan_skill,
                "refused_route_skill": str(decision.get("skill", "")),
                "route_step_threat_mult": round(float(threat_mult), 2),
                "threshold": ROUTE_THREAT_REFUSE_MULT,
                "release_threshold": ROUTE_THREAT_RELEASE_MULT,
                "critical_health": ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                "health": health,
                "hold_tics": hold_before,
                "hold_peak": hold_peak,
                "sustained": int(bool(sustained and not triggered)),
                "shootable": int(bool(metrics.get("shootable"))),
                "visible_enemy": int(bool(metrics.get("visible_enemy"))),
                "contact_distance": int(distance),
                "recommit_distance": int(ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE),
            }
        )
        return index, skill, action, breakaway_decision

    def _route_threshold_turn_and_burn_action(
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
        if directive.contract.objective == "complete_level" and self._e1m1_near_final_exit_commit(self._metrics(state)):
            self._critical_turn_and_burn_steps = 0
            self._critical_turn_and_burn_handoff_steps = 0
            self._critical_turn_and_burn_deflect_steps = 0
            return None
        if (
            arm_commit
            and directive.contract.objective == "complete_level"
            and int(self._critical_turn_and_burn_chain_count) >= ROUTE_CRITICAL_TURN_AND_BURN_MAX_CHAIN
        ):
            return None
        selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        navigation = getattr(state, "navigation", None)
        side = self._best_open_cover_side(navigation)
        try:
            enemy_turn = float(enemy.get("turn", 0.0) or 0.0)
        except Exception:
            enemy_turn = 0.0
        if side:
            turn_sign = 1 if int(side) > 0 else -1
        elif enemy_turn > 0.0:
            turn_sign = -1
        else:
            turn_sign = 1
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        deflecting = int(self._critical_turn_and_burn_deflect_steps) > 0
        deflect_sign = int(self._critical_turn_and_burn_deflect_sign or turn_sign or 1)
        if raw_cls is not None:
            if deflecting:
                side_move = self._raw_side_move_for_cover_side(deflect_sign, 48)
                turn_degrees = ROUTE_CRITICAL_TURN_AND_BURN_DEFLECT_DEGREES * float(deflect_sign)
            else:
                side_move = self._raw_side_move_for_cover_side(side, 34) if side else 0
                turn_degrees = 179.0 * float(turn_sign)
            action = agent_pb2.PlayerAction(
                duration_tics=ROUTE_CRITICAL_TURN_AND_BURN_TICS,
                raw=raw_cls(
                    forward_move=64,
                    side_move=side_move,
                    angle_turn=self._raw_steer_turn_units(turn_degrees),
                ),
            )
            kind = "critical_turn_and_burn_raw"
        else:
            action = agent_pb2.PlayerAction(
                action=agent_pb2.ACTION_FORWARD,
                amount=56,
                duration_tics=ROUTE_CRITICAL_TURN_AND_BURN_TICS,
            )
            kind = "critical_turn_and_burn"
        self._cautious_cover_side_lock = 0
        self._cautious_cover_side_lock_steps = 0
        self._cautious_retreat_commit_steps = max(
            self._cautious_retreat_commit_steps,
            CAUTIOUS_RETREAT_COMMIT_STEPS,
        )
        if arm_commit:
            self._critical_turn_and_burn_steps = max(
                self._critical_turn_and_burn_steps,
                ROUTE_CRITICAL_TURN_AND_BURN_COMMIT_STEPS,
            )
        if deflecting:
            self._critical_turn_and_burn_deflect_steps = max(0, self._critical_turn_and_burn_deflect_steps - 1)
        index, skill = selected
        return index, skill, action, {
            "source": "cautious_combat",
            "skill": kind,
            "state": "turn_and_burn",
            "reason": reason,
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
            "turn_sign": int(turn_sign),
            "side": int(side or 0),
            "deflect": int(bool(deflecting)),
            "deflect_sign": int(deflect_sign) if deflecting else 0,
            "deflect_steps_remaining": int(self._critical_turn_and_burn_deflect_steps),
        }

    def _record_critical_turn_and_burn_outcome(
        self,
        current: Any,
        action: Any,
        decision: dict[str, Any],
        step_moved: float,
        modules: dict[str, Any],
    ) -> None:
        if str(decision.get("skill", "")) != "critical_turn_and_burn_raw":
            try:
                metrics = self._metrics(current)
                health = int(metrics.get("health", 100) or 100)
                contact = bool(metrics.get("visible_enemy") or metrics.get("shootable"))
            except Exception:
                health = 100
                contact = False
            if health > ROUTE_CRITICAL_HEALTH_BREAKAWAY or not contact:
                self._critical_turn_and_burn_chain_count = 0
            if float(step_moved) >= ANTI_GRIND_RESET_MOVE_UNITS:
                self._critical_turn_and_burn_stall_count = 0
                self._critical_turn_and_burn_deflect_steps = 0
            return
        self._critical_turn_and_burn_chain_count += 1
        raw = getattr(action, "raw", None)
        raw_forward = int(getattr(raw, "forward_move", 0) or 0) if raw is not None else 0
        if raw_forward <= 0:
            return
        if not self._movement_stalled(action, step_moved, modules):
            self._critical_turn_and_burn_stall_count = 0
            if float(step_moved) >= ANTI_GRIND_RESET_MOVE_UNITS:
                self._critical_turn_and_burn_deflect_steps = 0
            return
        self._critical_turn_and_burn_stall_count += 1
        if self._critical_turn_and_burn_stall_count < ROUTE_CRITICAL_TURN_AND_BURN_STALL_THRESHOLD:
            return
        navigation = getattr(current, "navigation", None)
        side = self._best_open_cover_side(navigation)
        if side:
            self._critical_turn_and_burn_deflect_sign = 1 if int(side) > 0 else -1
        else:
            try:
                turn_sign = int(decision.get("turn_sign", 0) or 0)
            except Exception:
                turn_sign = 0
            if turn_sign:
                self._critical_turn_and_burn_deflect_sign = -1 if int(turn_sign) > 0 else 1
            else:
                self._critical_turn_and_burn_deflect_sign = -int(self._critical_turn_and_burn_deflect_sign or 1)
        self._critical_turn_and_burn_deflect_steps = max(
            int(self._critical_turn_and_burn_deflect_steps),
            ROUTE_CRITICAL_TURN_AND_BURN_DEFLECT_STEPS,
        )
        self._critical_turn_and_burn_stall_count = 0

    def _critical_turn_and_burn_commit_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if directive.contract.objective == "complete_level" and self._e1m1_near_final_exit_commit(self._metrics(state)):
            self._critical_turn_and_burn_steps = 0
            self._critical_turn_and_burn_handoff_steps = 0
            self._critical_turn_and_burn_deflect_steps = 0
            return None
        if self._critical_turn_and_burn_steps <= 0:
            return None
        try:
            health = int(self._metrics(state).get("health", 100) or 100)
        except Exception:
            health = 100
        if health <= 0:
            self._critical_turn_and_burn_steps = 0
            return None
        result = self._route_threshold_turn_and_burn_action(
            state,
            directive,
            controller,
            modules,
            reason="critical_turn_and_burn_commit",
            enemy=enemy,
            arm_commit=False,
        )
        if result is None:
            return None
        self._critical_turn_and_burn_steps = max(0, self._critical_turn_and_burn_steps - 1)
        if self._critical_turn_and_burn_steps <= 0:
            self._critical_turn_and_burn_handoff_steps = max(self._critical_turn_and_burn_handoff_steps, 2)
        _index, _skill, _action, decision = result
        decision["commit_steps_remaining"] = int(self._critical_turn_and_burn_steps)
        decision["health"] = int(health)
        decision["critical_health"] = ROUTE_CRITICAL_HEALTH_BREAKAWAY
        return result

    def _critical_turn_and_burn_recommit_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        enemy: dict[str, Any],
        reason: str,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if directive.contract.objective == "complete_level" and self._e1m1_near_final_exit_commit(self._metrics(state)):
            self._critical_turn_and_burn_steps = 0
            self._critical_turn_and_burn_handoff_steps = 0
            self._critical_turn_and_burn_deflect_steps = 0
            return None
        if self._critical_turn_and_burn_handoff_steps <= 0:
            return None
        try:
            metrics = self._metrics(state)
            health = int(metrics.get("health", 100) or 100)
        except Exception:
            metrics = {}
            health = 100
        if health <= 0 or health > ROUTE_CRITICAL_HEALTH_BREAKAWAY:
            return None
        try:
            distance = float(enemy.get("distance", 9999.0) or 9999.0)
        except Exception:
            distance = 9999.0
        close_contact = distance <= ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE
        contact = bool(close_contact)
        if not contact:
            self._critical_turn_and_burn_handoff_steps = max(0, self._critical_turn_and_burn_handoff_steps - 1)
            return None
        result = self._route_threshold_turn_and_burn_action(
            state,
            directive,
            controller,
            modules,
            reason="critical_turn_and_burn_recommit",
            enemy=enemy,
        )
        if result is None:
            return None
        self._critical_turn_and_burn_handoff_steps = max(0, self._critical_turn_and_burn_handoff_steps - 1)
        _index, _skill, _action, decision = result
        decision.update(
            {
                "reason": "critical_turn_and_burn_recommit",
                "previous_reason": str(reason),
                "health": int(health),
                "critical_health": ROUTE_CRITICAL_HEALTH_BREAKAWAY,
                "contact": int(bool(contact)),
                "contact_distance": int(distance),
                "recommit_distance": int(ROUTE_CRITICAL_TURN_AND_BURN_RECOMMIT_DISTANCE),
            }
        )
        return result

    def _route_threshold_hitscan_flinch_breakaway(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        decision: dict[str, Any],
        metrics: dict[str, Any],
        *,
        threat_mult: float,
        plan_skill: str,
        sustained: bool,
        triggered: bool,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if self._route_refusal_flinch_steps <= 0:
            return None
        if not bool(metrics.get("visible_enemy") or metrics.get("shootable")):
            self._route_refusal_flinch_steps = max(0, self._route_refusal_flinch_steps - 1)
            return None
        enemy = self._nearest_enemy(state, prefer_visible=True)
        if enemy is None:
            try:
                turn = float(decision.get("turn", 0.0) or 0.0)
            except Exception:
                turn = 0.0
            enemy = {
                "id": 0,
                "turn": turn,
                "distance": 0.0,
                "visible": bool(metrics.get("visible_enemy")),
                "threat": "unknown",
            }
        threat = str(enemy.get("threat") or "unknown")
        if threat not in {"hitscan", "unknown"}:
            return None
        hold_before = int(self._route_refusal_hold_tics)
        hold_peak = round(float(self._route_refusal_hold_peak), 2)
        flinch_before = int(self._route_refusal_flinch_steps)
        cover = self._cautious_cover_action(
            state,
            directive,
            controller,
            modules,
            reason="route_threshold_hitscan_flinch",
            enemy=enemy,
        )
        if cover is None:
            return None
        self._route_refusal_flinch_steps = max(0, self._route_refusal_flinch_steps - 1)
        index, skill, action, flinch_decision = cover
        flinch_decision.update(
            {
                "source": "route_threshold_refusal",
                "state": "hitscan_flinch_breakaway",
                "reason": "route_threshold_hitscan_flinch",
                "refused_skill": plan_skill,
                "refused_route_skill": str(decision.get("skill", "")),
                "route_step_threat_mult": round(float(threat_mult), 2),
                "threshold": ROUTE_THREAT_REFUSE_MULT,
                "release_threshold": ROUTE_THREAT_RELEASE_MULT,
                "hold_tics": hold_before,
                "hold_peak": hold_peak,
                "flinch_steps": flinch_before,
                "sustained": int(bool(sustained and not triggered)),
                "shootable": int(bool(metrics.get("shootable"))),
                "visible_enemy": int(bool(metrics.get("visible_enemy"))),
                "enemy": int(enemy.get("id", 0) or 0),
                "enemy_dist": int(float(enemy.get("distance", 0.0) or 0.0)),
                "enemy_threat": threat,
            }
        )
        return index, skill, action, flinch_decision

    def _route_threshold_melee_rush_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        decision: dict[str, Any],
        metrics: dict[str, Any],
        enemy: dict[str, Any],
        *,
        threat_mult: float,
        plan_skill: str,
        sustained: bool,
        triggered: bool,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        agent_pb2 = modules["agent_pb2"]
        rush = self._melee_rush_contract_action(state, enemy, agent_pb2)
        if rush is None:
            return None
        selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            return None
        action, rush_decision = rush
        duration = int(getattr(action, "duration_tics", 1) or 1)
        hold_before = int(self._route_refusal_hold_tics)
        self._route_refusal_hold_tics = max(0, self._route_refusal_hold_tics - duration)
        index, skill = selected
        melee_decision = dict(decision)
        melee_decision.update(rush_decision)
        melee_decision.update(
            {
                "source": "route_threshold_refusal",
                "state": "melee_rush_hold",
                "reason": "route_threshold_melee_rush_hold",
                "refused_skill": plan_skill,
                "refused_route_skill": str(decision.get("skill", "")),
                "route_step_threat_mult": round(float(threat_mult), 2),
                "threshold": ROUTE_THREAT_REFUSE_MULT,
                "release_threshold": ROUTE_THREAT_RELEASE_MULT,
                "hold_tics": hold_before,
                "hold_peak": round(float(self._route_refusal_hold_peak), 2),
                "sustained": int(bool(sustained and not triggered)),
                "shootable": int(bool(metrics.get("shootable"))),
                "visible_enemy": int(bool(metrics.get("visible_enemy"))),
                "enemy": int(enemy.get("id", 0) or 0),
                "enemy_dist": int(float(enemy.get("distance", 0.0) or 0.0)),
                "enemy_threat": str(enemy.get("threat") or "unknown"),
                "enemy_health": int(enemy.get("health", 0) or 0),
            }
        )
        return index, skill, action, melee_decision

    def _record_route_threshold_flinch(self, previous: Any, current: Any, decision: dict[str, Any]) -> None:
        if str(decision.get("source", "")) != "route_threshold_refusal":
            return
        if str(decision.get("skill", "")) != "threshold_route_clean_shot":
            return
        if str(decision.get("action", "")) not in {"align_clean_shot", "micro_strafe_clean_shot"}:
            return
        previous_health = int(self._metrics(previous).get("health", 0) or 0)
        current_health = int(self._metrics(current).get("health", 0) or 0)
        if current_health >= previous_health:
            return
        self._route_refusal_flinch_steps = max(self._route_refusal_flinch_steps, ROUTE_HITSCAN_FLINCH_STEPS)

    def _route_threshold_clean_shot_adjustment(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        decision: dict[str, Any],
        metrics: dict[str, Any],
        *,
        threat_mult: float,
        plan_skill: str,
        sustained: bool,
        triggered: bool,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        enemy = self._nearest_enemy(state, prefer_visible=True)
        try:
            turn = float(enemy.get("turn", decision.get("turn", 0.0)) if enemy is not None else decision.get("turn", 0.0) or 0.0)
        except Exception:
            turn = 0.0
        if bool(metrics.get("shootable")) and abs(turn) <= ROUTE_CLEAN_SHOT_TURN_DEGREES:
            return None
        selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        navigation = getattr(state, "navigation", None)
        visible = bool(metrics.get("visible_enemy")) or bool(enemy and enemy.get("visible"))
        if visible and abs(turn) > ROUTE_CLEAN_SHOT_TURN_DEGREES:
            if raw_cls is not None:
                action = agent_pb2.PlayerAction(
                    duration_tics=3,
                    raw=raw_cls(angle_turn=self._raw_steer_turn_units(turn)),
                )
            else:
                action_type = agent_pb2.ACTION_TURN_LEFT if turn > 0 else agent_pb2.ACTION_TURN_RIGHT
                action = agent_pb2.PlayerAction(
                    action=action_type,
                    amount=max(4, min(18, int(abs(turn)))),
                    duration_tics=3,
                )
            action_name = "align_clean_shot"
            reason = "clean_shot_off_angle"
        else:
            side = self._best_open_cover_side(navigation)
            if raw_cls is not None:
                action = agent_pb2.PlayerAction(
                    duration_tics=4,
                    raw=raw_cls(
                        side_move=self._raw_side_move_for_cover_side(side, 34) if side else 0,
                        angle_turn=self._raw_steer_turn_units(turn),
                    ),
                )
            elif side:
                action_type = agent_pb2.ACTION_STRAFE_LEFT if side > 0 else agent_pb2.ACTION_STRAFE_RIGHT
                action = agent_pb2.PlayerAction(action=action_type, amount=24, duration_tics=4)
            elif bool(getattr(navigation, "back_open", False)):
                action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=18, duration_tics=4)
            else:
                action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_TURN_LEFT, amount=6, duration_tics=3)
            action_name = "micro_strafe_clean_shot"
            reason = "clean_shot_blocked"
        duration = int(getattr(action, "duration_tics", 1) or 1)
        hold_before = int(self._route_refusal_hold_tics)
        self._route_refusal_hold_tics = max(0, self._route_refusal_hold_tics - duration)
        index, skill = selected
        adjusted = dict(decision)
        adjusted.update(
            {
                "source": "route_threshold_refusal",
                "skill": "threshold_route_clean_shot",
                "action": action_name,
                "state": "fatal_funnel_hold",
                "reason": reason,
                "refused_skill": plan_skill,
                "refused_route_skill": str(decision.get("skill", "")),
                "route_step_threat_mult": round(float(threat_mult), 2),
                "threshold": ROUTE_THREAT_REFUSE_MULT,
                "release_threshold": ROUTE_THREAT_RELEASE_MULT,
                "clean_turn_threshold": ROUTE_CLEAN_SHOT_TURN_DEGREES,
                "hold_tics": hold_before,
                "hold_peak": round(float(self._route_refusal_hold_peak), 2),
                "sustained": int(bool(sustained and not triggered)),
                "shootable": int(bool(metrics.get("shootable"))),
                "visible_enemy": int(visible),
                "turn": round(turn, 1),
            }
        )
        if enemy is not None:
            adjusted.update(
                {
                    "enemy": int(enemy.get("id", 0) or 0),
                    "enemy_dist": int(float(enemy.get("distance", 0.0) or 0.0)),
                    "enemy_threat": str(enemy.get("threat") or "unknown"),
                }
            )
        return index, skill, action, adjusted

    def _route_refusal_key(self, decision: dict[str, Any]) -> tuple[str, int, int]:
        kind = str(decision.get("route_step_kind") or decision.get("skill") or "route")
        line = decision.get("route_step_line", decision.get("line_id", decision.get("line", -1)))
        sector = decision.get("route_step_sector", decision.get("sector", -1))
        try:
            line_id = int(line)
        except Exception:
            line_id = -1
        try:
            sector_id = int(sector)
        except Exception:
            sector_id = -1
        return kind, line_id, sector_id

    def _route_refusal_melee_threat(self, state: Any) -> dict[str, Any] | None:
        tracked_id = int(self._route_refusal_melee_target_id or 0)
        if tracked_id > 0:
            tracked = self._enemy_by_id(state, tracked_id)
            if tracked is None:
                self._route_refusal_melee_target_id = 0
            elif str(tracked.get("threat") or "unknown") in MELEE_RUSH_THREATS:
                distance = float(tracked.get("distance", 9999.0) or 9999.0)
                if bool(tracked.get("visible")) or distance <= ROUTE_MELEE_RUSH_HOLD_DISTANCE:
                    return tracked
                self._route_refusal_melee_target_id = 0
        enemy = self._nearest_enemy(state, prefer_visible=True)
        if enemy is None or str(enemy.get("threat") or "unknown") not in MELEE_RUSH_THREATS:
            return self._nearest_melee_rush_enemy(state)
        distance = float(enemy.get("distance", 9999.0) or 9999.0)
        if bool(enemy.get("visible")) or bool(enemy.get("shootable_target")) or distance <= ROUTE_MELEE_RUSH_HOLD_DISTANCE:
            return enemy
        return None

    def _enemy_by_id(self, state: Any, enemy_id: int) -> dict[str, Any] | None:
        player = getattr(state, "player", None)
        pobj = getattr(player, "object", None)
        ppos = getattr(pobj, "position", None)
        px = int(getattr(ppos, "x_fp", 0))
        py = int(getattr(ppos, "y_fp", 0))
        angle = float(getattr(pobj, "angle_degrees", 0) or 0)
        combat = getattr(state, "combat", None)
        combat_target = int(getattr(combat, "target_id", 0) or 0)
        has_shootable_target = bool(getattr(combat, "has_shootable_target", False) and getattr(combat, "target_is_enemy", False))
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            pos = getattr(obj, "position", None)
            if obj is None or pos is None:
                continue
            if int(getattr(obj, "id", 0) or 0) != int(enemy_id):
                continue
            health = int(getattr(obj, "health", 0) or 0)
            if health <= 0:
                return None
            ex = int(getattr(pos, "x_fp", 0))
            ey = int(getattr(pos, "y_fp", 0))
            dx = (ex - px) / FP_UNIT
            dy = (ey - py) / FP_UNIT
            dist = math.hypot(dx, dy)
            bearing = math.degrees(math.atan2(dy, dx)) % 360.0 if dist else angle
            turn = ((bearing - angle + 540.0) % 360.0) - 180.0
            return {
                "id": int(enemy_id),
                "type_id": int(getattr(obj, "type_id", 0) or 0),
                "threat": classify_enemy(enemy),
                "distance": dist,
                "turn": turn,
                "visible": bool(getattr(enemy, "line_of_sight", False)),
                "shootable_target": bool(has_shootable_target and combat_target == int(enemy_id)),
                "health": health,
            }
        return None

    def _nearest_melee_rush_enemy(self, state: Any) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        for enemy in getattr(state, "enemies", []) or []:
            obj = getattr(enemy, "object", None)
            enemy_id = int(getattr(obj, "id", 0) or 0)
            if enemy_id <= 0:
                continue
            candidate = self._enemy_by_id(state, enemy_id)
            if candidate is None or str(candidate.get("threat") or "unknown") not in MELEE_RUSH_THREATS:
                continue
            distance = float(candidate.get("distance", 9999.0) or 9999.0)
            if not (
                bool(candidate.get("visible"))
                or bool(candidate.get("shootable_target"))
                or distance <= ROUTE_MELEE_RUSH_HOLD_DISTANCE
            ):
                continue
            if best is None or distance < float(best.get("distance", 9999.0) or 9999.0):
                best = candidate
        return best

    def _clear_route_refusal_hold(self) -> None:
        self._route_refusal_hold_tics = 0
        self._route_refusal_hold_key = None
        self._route_refusal_hold_peak = 0.0
        self._route_refusal_melee_target_id = 0
        self._route_refusal_flinch_steps = 0

    def _no_kill_route_threat_refusal(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        plan_skill: str,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if not self._no_kill_desperation_allowed(directive, plan_skill):
            return None
        if str(decision.get("skill") or "") == "no_kill_route_evasion":
            return None
        try:
            threat_mult = float(decision.get("route_step_threat_mult", 1.0) or 1.0)
        except Exception:
            threat_mult = 1.0
        if threat_mult < NO_KILL_ROUTE_REFUSAL_MULT:
            return None
        self._no_kill_desperation_key = self._no_kill_desperation_panic_key(state, decision)
        self._no_kill_desperation_count = NO_KILL_DESPERATION_PANIC_REPEATS
        self._no_kill_desperation_sprint_tics = max(
            int(self._no_kill_desperation_sprint_tics),
            NO_KILL_DESPERATION_SPRINT_TICS,
        )
        preemptive = self._no_kill_desperation_sprint_action(
            state,
            directive,
            controller,
            modules,
            decision,
            reason="preemptive_high_threat_route",
            force_forward=True,
        )
        if preemptive is not None:
            _index, _skill, _action, sprint_decision = preemptive
            sprint_decision.update(
                {
                    "trigger_source": "no_kill_route_refusal",
                    "state": "threat_density_sprint",
                    "threshold": NO_KILL_ROUTE_REFUSAL_MULT,
                    "route_step_threat_mult": round(threat_mult, 2),
                }
            )
            return preemptive
        selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        navigation = getattr(state, "navigation", None)
        action, action_name = self._no_kill_route_refusal_action(agent_pb2, navigation)
        self._strip_fire(action, agent_pb2)
        index, skill = selected
        refusal_decision = dict(decision)
        refusal_decision.update(
            {
                "source": "no_kill_route_refusal",
                "skill": "no_kill_route_threat_refusal",
                "action": action_name,
                "state": "threat_density_refusal",
                "reason": "no_kill_high_threat_route",
                "refused_skill": str(decision.get("skill") or ""),
                "refused_action": str(decision.get("action") or ""),
                "route_step_threat_mult": round(threat_mult, 2),
                "threshold": NO_KILL_ROUTE_REFUSAL_MULT,
            }
        )
        return index, skill, action, refusal_decision

    def _no_kill_route_refusal_action(self, agent_pb2: Any, navigation: Any) -> tuple[Any, str]:
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        side = self._best_open_cover_side(navigation)
        if raw_cls is not None:
            if bool(getattr(navigation, "back_open", False)):
                return (
                    agent_pb2.PlayerAction(
                        duration_tics=10,
                        raw=raw_cls(
                            forward_move=-46,
                            side_move=self._raw_side_move_for_cover_side(side, 26) if side else 0,
                        ),
                    ),
                    "no_kill_lure_back_raw",
                )
            if side:
                return (
                    agent_pb2.PlayerAction(
                        duration_tics=10,
                        raw=raw_cls(side_move=self._raw_side_move_for_cover_side(side, 58)),
                    ),
                    "no_kill_lure_side_raw",
                )
            if bool(getattr(navigation, "forward_open", False)):
                return agent_pb2.PlayerAction(duration_tics=6, raw=raw_cls(forward_move=46)), "no_kill_forced_gap_raw"
        if bool(getattr(navigation, "back_open", False)):
            return agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=40, duration_tics=10), "no_kill_lure_back"
        if side:
            action_type = agent_pb2.ACTION_STRAFE_LEFT if side > 0 else agent_pb2.ACTION_STRAFE_RIGHT
            return agent_pb2.PlayerAction(action=action_type, amount=48, duration_tics=10), "no_kill_lure_side"
        if bool(getattr(navigation, "forward_open", False)):
            return agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=40, duration_tics=6), "no_kill_forced_gap"
        return agent_pb2.PlayerAction(action=agent_pb2.ACTION_TURN_LEFT, amount=24, duration_tics=6), "no_kill_scan_turn"

    def _no_kill_desperation_sprint(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        plan_skill: str,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if not self._no_kill_desperation_allowed(directive, plan_skill):
            self._clear_no_kill_desperation()
            return None
        action_name = str(decision.get("action") or "")
        panic_action = (
            str(decision.get("skill") or "") == "no_kill_route_evasion"
            and action_name in NO_KILL_DESPERATION_PANIC_ACTIONS
        )
        if self._no_kill_desperation_sprint_tics <= 0 and not panic_action:
            self._clear_no_kill_desperation()
            return None
        if self._no_kill_desperation_sprint_tics > 0:
            return self._no_kill_desperation_sprint_action(
                state,
                directive,
                controller,
                modules,
                decision,
                reason="committed_sprint",
            )
        if panic_action:
            key = self._no_kill_desperation_panic_key(state, decision)
            if key == self._no_kill_desperation_key:
                self._no_kill_desperation_count += 1
            else:
                self._no_kill_desperation_key = key
                self._no_kill_desperation_count = 1
            if self._no_kill_desperation_count >= NO_KILL_DESPERATION_PANIC_REPEATS:
                self._no_kill_desperation_sprint_tics = max(
                    int(self._no_kill_desperation_sprint_tics),
                    NO_KILL_DESPERATION_SPRINT_TICS,
                )
        if self._no_kill_desperation_sprint_tics <= 0:
            return None
        return self._no_kill_desperation_sprint_action(
            state,
            directive,
            controller,
            modules,
            decision,
            reason="panic_loop_breakout" if panic_action else "committed_sprint",
        )

    def _no_kill_desperation_allowed(self, directive: ObjectiveDirective, plan_skill: str) -> bool:
        constraints = directive.contract.constraints
        rules = set(directive.rules)
        if constraints.get("kill_budget") != 0 and "no_kills" not in rules:
            return False
        if str(plan_skill) != "route_progression":
            return False
        return bool({"exit", "complete_level"} & rules)

    def _no_kill_desperation_panic_key(self, state: Any, decision: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
        metrics = self._metrics(state)
        bucket = int(128 * FP_UNIT)
        sector = decision.get("route_step_sector", decision.get("sector", -1))
        try:
            sector_id = int(sector)
        except Exception:
            sector_id = -1
        enemy = decision.get("enemy", 0)
        try:
            enemy_id = int(enemy)
        except Exception:
            enemy_id = 0
        return (
            int(metrics.get("episode", 0) or 0),
            int(metrics.get("map", 0) or 0),
            sector_id,
            int(metrics.get("x", 0) or 0) // bucket,
            int(metrics.get("y", 0) or 0) // bucket,
            enemy_id,
        )

    def _no_kill_desperation_sprint_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        decision: dict[str, Any],
        *,
        reason: str,
        force_forward: bool = False,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        navigation = getattr(state, "navigation", None)
        forward_move, side_move, escape_offset, escape_distance = self._no_kill_desperation_escape_vector(
            navigation,
            decision,
            force_forward=force_forward,
        )
        try:
            turn = float(decision.get("turn", 0.0) or 0.0)
        except Exception:
            turn = 0.0
        duration = min(NO_KILL_DESPERATION_SPRINT_BURST_TICS, max(1, int(self._no_kill_desperation_sprint_tics)))
        if raw_cls is not None:
            action = agent_pb2.PlayerAction(
                duration_tics=duration,
                raw=raw_cls(
                    forward_move=forward_move,
                    side_move=side_move,
                    angle_turn=self._raw_steer_turn_units(turn),
                ),
            )
            action_name = "sprint_through_raw"
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=56, duration_tics=duration)
            action_name = "sprint_through"
        sprint_before = int(self._no_kill_desperation_sprint_tics)
        self._no_kill_desperation_sprint_tics = max(0, self._no_kill_desperation_sprint_tics - duration)
        if self._no_kill_desperation_sprint_tics <= 0:
            self._no_kill_desperation_key = None
            self._no_kill_desperation_count = 0
        index, skill = selected
        sprint_decision = dict(decision)
        sprint_decision.update(
            {
                "source": "no_kill_desperation",
                "skill": "no_kill_desperation_sprint",
                "action": action_name,
                "state": "desperation_sprint",
                "reason": reason,
                "refused_skill": str(decision.get("skill") or ""),
                "refused_action": str(decision.get("action") or ""),
                "panic_repeat": int(self._no_kill_desperation_count),
                "sprint_tics": sprint_before,
                "escape_offset": int(escape_offset) if escape_offset is not None else None,
                "escape_dist": int(escape_distance) if escape_distance is not None else None,
                "force_forward": int(bool(force_forward)),
            }
        )
        self._strip_fire(action, agent_pb2)
        return index, skill, action, sprint_decision

    def _no_kill_desperation_escape_vector(
        self,
        navigation: Any,
        decision: dict[str, Any],
        *,
        force_forward: bool = False,
    ) -> tuple[int, int, int | None, int | None]:
        best: tuple[int, int] | None = None
        for probe in getattr(navigation, "direction_probes", []) or []:
            if not bool(getattr(probe, "open", False)):
                continue
            offset = int(getattr(probe, "angle_offset_degrees", 0) or 0)
            if abs(offset) > 150:
                continue
            if force_forward and abs(offset) > 95:
                continue
            distance = int((getattr(probe, "block_distance_fp", 0) or 0) / FP_UNIT)
            if best is None or distance > best[1]:
                best = (offset, distance)
        if best is not None:
            offset, distance = best
            radians = math.radians(float(offset))
            forward = int(round(64.0 * math.cos(radians)))
            side = -int(round(64.0 * math.sin(radians)))
            if abs(forward) < 18:
                forward = 18 if bool(getattr(navigation, "forward_open", False)) else 0
            return (
                max(-58, min(64, forward)),
                max(-64, min(64, side)),
                int(offset),
                int(distance),
            )
        if force_forward:
            side_name = str(decision.get("side") or "")
            if side_name == "left":
                return 64, -34, None, None
            if side_name == "right":
                return 64, 34, None, None
            return 64, 0, None, None
        side_name = str(decision.get("side") or "")
        if side_name == "left":
            return 42, -52, None, None
        if side_name == "right":
            return 42, 52, None, None
        if bool(getattr(navigation, "forward_open", False)):
            return 64, 0, None, None
        if bool(getattr(navigation, "back_open", False)):
            return -56, 0, None, None
        return 48, 0, None, None

    def _clear_no_kill_desperation(self) -> None:
        self._no_kill_desperation_key = None
        self._no_kill_desperation_count = 0
        self._no_kill_desperation_sprint_tics = 0

    def _rotational_stall_route_skill(self, decision: dict[str, Any]) -> str:
        detail_skill = str(decision.get("skill", ""))
        planner_skill = str(decision.get("planner_skill", ""))
        if detail_skill in ROTATIONAL_STALL_ROUTE_SKILLS:
            return detail_skill
        if planner_skill in ROTATIONAL_STALL_ROUTE_SKILLS:
            return planner_skill
        return ""

    def _rotational_stall_candidate(self, decision: dict[str, Any]) -> bool:
        return bool(self._rotational_stall_route_skill(decision)) and str(decision.get("action", "")) == "turn"

    def _rotational_stall_signature(
        self,
        state: Any,
        *,
        decision: dict[str, Any],
    ) -> tuple[int, int, int, int, str, str]:
        metrics = self._metrics(state)
        bucket = int(16 * FP_UNIT)
        return (
            int(metrics.get("episode", 0) or 0),
            int(metrics.get("map", 0) or 0),
            int(metrics.get("x", 0) or 0) // bucket,
            int(metrics.get("y", 0) or 0) // bucket,
            str(decision.get("skill", "")),
            str(decision.get("action", "")),
        )

    def _action_is_turn_only(self, action: Any, modules: dict[str, Any]) -> bool:
        agent_pb2 = modules["agent_pb2"]
        action_type = int(getattr(action, "action", 0) or 0)
        turn_actions = {
            int(getattr(agent_pb2, "ACTION_TURN_LEFT", -101)),
            int(getattr(agent_pb2, "ACTION_TURN_RIGHT", -102)),
        }
        if action_type in turn_actions:
            return True
        raw = getattr(action, "raw", None)
        if raw is None:
            return False
        raw_turn = int(getattr(raw, "angle_turn", 0) or 0)
        raw_forward = int(getattr(raw, "forward_move", 0) or 0)
        raw_side = int(getattr(raw, "side_move", 0) or 0)
        return bool(raw_turn) and not bool(raw_forward or raw_side)

    def _record_rotational_stall_outcome(
        self,
        current: Any,
        *,
        action: Any,
        decision: dict[str, Any],
        modules: dict[str, Any],
        route_outcome: dict[str, Any],
        moved_units: float,
    ) -> None:
        if bool(route_outcome.get("reached")) or float(moved_units) >= ANTI_GRIND_RESET_MOVE_UNITS:
            self._reset_rotational_stall_tracker()
            return
        if not self._rotational_stall_candidate(decision) or not self._action_is_turn_only(action, modules):
            if float(moved_units) >= ANTI_GRIND_MOVE_EPS_UNITS:
                self._reset_rotational_stall_tracker()
            return
        key = self._rotational_stall_signature(current, decision=decision)
        if key == self._rotational_stall_key:
            self._rotational_stall_count += 1
        else:
            self._rotational_stall_key = key
            self._rotational_stall_count = 1
            self._rotational_stall_escape_steps = 0
        if self._rotational_stall_count >= ROTATIONAL_STALL_TURN_THRESHOLD:
            self._rotational_stall_escape_steps = max(
                int(self._rotational_stall_escape_steps),
                ROTATIONAL_STALL_ESCAPE_STEPS,
            )

    def _reset_rotational_stall_tracker(self) -> None:
        self._rotational_stall_key = None
        self._rotational_stall_count = 0
        self._rotational_stall_escape_steps = 0

    def _rotational_stall_escape(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        plan_skill: str,
        planned_action: Any,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if self._rotational_stall_escape_steps <= 0:
            return None
        if not self._rotational_stall_route_skill(decision):
            return None
        selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            return None
        action, action_label = self._rotational_stall_escape_action(
            modules["agent_pb2"],
            getattr(state, "navigation", None),
            planned_action,
        )
        self._strip_fire(action, modules["agent_pb2"])
        escape_before = int(self._rotational_stall_escape_steps)
        self._rotational_stall_escape_steps = max(0, self._rotational_stall_escape_steps - 1)
        index, selected_skill = selected
        return index, selected_skill, action, {
            "source": "anti_grind",
            "skill": "rotational_stall_escape",
            "reason": "route_repeated_turn",
            "action": action_label,
            "blocked_skill": str(decision.get("skill", "")),
            "blocked_action": str(decision.get("action", "")),
            "planner_skill": str(plan_skill),
            "repeat": int(self._rotational_stall_count),
            "threshold": int(ROTATIONAL_STALL_TURN_THRESHOLD),
            "remaining": int(self._rotational_stall_escape_steps),
            "escape_steps": escape_before,
        }

    def _rotational_stall_escape_action(
        self,
        agent_pb2: Any,
        navigation: Any,
        planned_action: Any,
    ) -> tuple[Any, str]:
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        side = self._best_open_cover_side(navigation)
        if side:
            if raw_cls is not None:
                return (
                    agent_pb2.PlayerAction(
                        duration_tics=6,
                        raw=raw_cls(forward_move=24, side_move=self._raw_side_move_for_cover_side(side, 52)),
                    ),
                    "rotational_unstick_strafe_raw",
                )
            action_type = agent_pb2.ACTION_STRAFE_RIGHT if int(side) < 0 else agent_pb2.ACTION_STRAFE_LEFT
            return agent_pb2.PlayerAction(action=action_type, amount=40, duration_tics=6), "rotational_unstick_strafe"
        if bool(getattr(navigation, "forward_open", False)):
            if raw_cls is not None:
                return agent_pb2.PlayerAction(duration_tics=6, raw=raw_cls(forward_move=48)), "rotational_unstick_forward_raw"
            return agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=42, duration_tics=6), "rotational_unstick_forward"
        if bool(getattr(navigation, "back_open", False)):
            if raw_cls is not None:
                return agent_pb2.PlayerAction(duration_tics=6, raw=raw_cls(forward_move=-38)), "rotational_unstick_backoff_raw"
            return agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=34, duration_tics=6), "rotational_unstick_backoff"
        if raw_cls is not None:
            planned_type = int(getattr(planned_action, "action", 0) or 0)
            left_action = int(getattr(agent_pb2, "ACTION_TURN_LEFT", -101))
            side_move = -44 if planned_type == left_action else 44
            return (
                agent_pb2.PlayerAction(duration_tics=6, raw=raw_cls(forward_move=24, side_move=side_move)),
                "rotational_unstick_diagonal_raw",
            )
        return agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=34, duration_tics=6), "rotational_unstick_forward"

    def _anti_grind_escape(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        plan_skill: str,
        planned_action: Any,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        line_id = self._line_id_from_decision(decision)
        if line_id is None:
            return None
        if not self._anti_grind_candidate(decision):
            return None
        matches_current = False
        try:
            matches_current = self._anti_grind_key == self._anti_grind_signature(
                state,
                decision=decision,
                line_id=int(line_id),
            )
        except Exception:
            matches_current = False
        if self._anti_grind_escape_steps <= 0 and not (
            matches_current and self._anti_grind_count >= ANTI_GRIND_STUCK_THRESHOLD
        ):
            return None
        selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("route_progression", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            return None
        if self._anti_grind_escape_steps <= 0:
            self._anti_grind_escape_steps = ANTI_GRIND_ESCAPE_STEPS
        action, action_label = self._anti_grind_escape_action(
            modules["agent_pb2"],
            getattr(state, "navigation", None),
            planned_action,
            decision,
        )
        self._strip_fire(action, modules["agent_pb2"])
        escape_before = int(self._anti_grind_escape_steps)
        self._anti_grind_escape_steps = max(0, self._anti_grind_escape_steps - 1)
        index, selected_skill = selected
        decision_out = {
            "source": "anti_grind",
            "skill": "anti_grind_escape",
            "reason": "planner_movement_no_displacement",
            "action": action_label,
            "line_id": int(line_id),
            "line": int(line_id),
            "blocked_skill": str(decision.get("skill", "")),
            "blocked_action": str(decision.get("action", "")),
            "planner_skill": str(plan_skill),
            "repeat": int(self._anti_grind_count),
            "threshold": int(ANTI_GRIND_STUCK_THRESHOLD),
            "remaining": int(self._anti_grind_escape_steps),
            "escape_steps": escape_before,
        }
        for key in ("sector", "probe", "route_step_sector"):
            if key in decision:
                decision_out[key] = decision.get(key)
        return index, selected_skill, action, decision_out

    def _anti_grind_escape_action(
        self,
        agent_pb2: Any,
        navigation: Any,
        planned_action: Any,
        decision: dict[str, Any],
    ) -> tuple[Any, str]:
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        try:
            probe = float(decision.get("probe", 0.0) or 0.0)
        except Exception:
            probe = 0.0
        turn = -1024 if probe > 0 else 1024
        blocked_skill = str(decision.get("skill", ""))
        blocked_action = str(decision.get("action", ""))
        side = self._best_open_cover_side(navigation)
        if blocked_skill == "center_passable_portal" and blocked_action in {"center_passable_portal", "center_passable_portal_raw"}:
            side_name = str(decision.get("side") or "")
            if side_name == "left":
                side_move = -52
            elif side_name == "right":
                side_move = 52
            else:
                side_move = self._raw_side_move_for_cover_side(side, 52) if side else 44
            if raw_cls is not None:
                return (
                    agent_pb2.PlayerAction(
                        duration_tics=8,
                        raw=raw_cls(forward_move=-24, side_move=side_move),
                    ),
                    "anti_grind_portal_unhook_raw",
                )
            if abs(side_move) > 0:
                action_type = agent_pb2.ACTION_STRAFE_RIGHT if side_move > 0 else agent_pb2.ACTION_STRAFE_LEFT
                return agent_pb2.PlayerAction(action=action_type, amount=44, duration_tics=8), "anti_grind_portal_unhook"
            return agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=34, duration_tics=8), "anti_grind_portal_backoff"
        if blocked_skill == "navcell_to_portal" and blocked_action == "steer_forward" and side:
            if raw_cls is not None:
                return (
                    agent_pb2.PlayerAction(
                        duration_tics=8,
                        raw=raw_cls(side_move=self._raw_side_move_for_cover_side(side, 58)),
                    ),
                    "anti_grind_unhook_strafe_raw",
                )
            action_type = agent_pb2.ACTION_STRAFE_RIGHT if int(side) < 0 else agent_pb2.ACTION_STRAFE_LEFT
            return agent_pb2.PlayerAction(action=action_type, amount=46, duration_tics=8), "anti_grind_unhook_strafe"
        if bool(getattr(navigation, "back_open", False)):
            if raw_cls is not None:
                return (
                    agent_pb2.PlayerAction(
                        duration_tics=10,
                        raw=raw_cls(forward_move=-52, angle_turn=turn),
                    ),
                    "anti_grind_backoff_raw",
                )
            return agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=46, duration_tics=10), "anti_grind_backoff"
        if raw_cls is not None:
            return agent_pb2.PlayerAction(duration_tics=8, raw=raw_cls(angle_turn=turn)), "anti_grind_turn_raw"
        planned_type = int(getattr(planned_action, "action", 0) or 0)
        action_type = agent_pb2.ACTION_TURN_RIGHT if planned_type == int(agent_pb2.ACTION_STRAFE_LEFT) else agent_pb2.ACTION_TURN_LEFT
        return agent_pb2.PlayerAction(action=action_type, amount=52, duration_tics=8), "anti_grind_turn"

    def _anti_grind_candidate(self, decision: dict[str, Any]) -> bool:
        detail_skill = str(decision.get("skill", ""))
        planner_skill = str(decision.get("planner_skill", ""))
        if detail_skill not in ANTI_GRIND_SKILLS and planner_skill not in ANTI_GRIND_SKILLS:
            return False
        action_name = str(decision.get("action", ""))
        return action_name in ANTI_GRIND_ACTIONS

    def _anti_grind_signature(
        self,
        state: Any,
        *,
        decision: dict[str, Any],
        line_id: int,
    ) -> tuple[int, int, int, int, int, int, str, str]:
        metrics = self._metrics(state)
        sector = decision.get("route_step_sector", decision.get("sector", -1))
        try:
            sector_id = int(sector)
        except Exception:
            sector_id = -1
        bucket = int(16 * FP_UNIT)
        return (
            int(line_id),
            int(metrics.get("episode", 0) or 0),
            int(metrics.get("map", 0) or 0),
            sector_id,
            int(metrics.get("x", 0) or 0) // bucket,
            int(metrics.get("y", 0) or 0) // bucket,
            str(decision.get("skill", "")),
            str(decision.get("action", "")),
        )

    def _record_anti_grind_outcome(
        self,
        current: Any,
        *,
        line_id: int,
        line: Any | None,
        action: Any,
        decision: dict[str, Any],
        modules: dict[str, Any],
        route_outcome: dict[str, Any],
        crossed_line: bool,
        moved_units: float,
    ) -> None:
        if bool(route_outcome.get("reached")) or crossed_line or float(moved_units) >= ANTI_GRIND_RESET_MOVE_UNITS:
            self._reset_anti_grind_tracker()
            return
        if not self._anti_grind_candidate(decision):
            return
        if not self._movement_stalled(action, moved_units, modules):
            return
        key = self._anti_grind_signature(current, decision=decision, line_id=int(line_id))
        if key == self._anti_grind_key:
            self._anti_grind_count += 1
        else:
            self._anti_grind_key = key
            self._anti_grind_count = 1
            self._anti_grind_escape_steps = 0
        if self._anti_grind_count < ANTI_GRIND_STUCK_THRESHOLD:
            return
        self._anti_grind_escape_steps = max(int(self._anti_grind_escape_steps), ANTI_GRIND_ESCAPE_STEPS)
        sector_id = key[3]
        record_frontier_blocked = getattr(self._world_memory, "record_frontier_blocked", None)
        if sector_id >= 0 and callable(record_frontier_blocked):
            record_frontier_blocked(int(sector_id))
        if bool(getattr(line, "passable", False) or getattr(line, "door", False) or getattr(line, "use_trigger", False)):
            record_route_contact = getattr(self._door_memory, "record_route_contact", None)
            if callable(record_route_contact):
                record_route_contact(int(line_id))
            else:
                self._door_memory.record_failure(int(line_id), status="frontier_probe_no_displacement")
        else:
            self._door_memory.record_status(int(line_id), status="frontier_probe_no_displacement")

    def _reset_anti_grind_tracker(self) -> None:
        self._anti_grind_key = None
        self._anti_grind_count = 0
        self._anti_grind_escape_steps = 0

    def _repeated_failed_use_escape(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        planned_action: Any,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        line_id = self._line_id_from_decision(decision)
        action_type = int(getattr(planned_action, "action", 0) or 0)
        is_planned_use = action_type == int(modules["agent_pb2"].ACTION_USE)
        if self._failed_use_escape_steps <= 0 and not (
            is_planned_use
            and line_id is not None
            and self._failed_use_count >= USE_STUCK_THRESHOLD
            and self._failed_use_line_matches_state(state, int(line_id))
        ):
            return None

        if (
            is_planned_use
            and directive.contract.objective == "complete_level"
            and not self._fire_forbidden(directive, state)
            and bool(self._metrics(state).get("shootable"))
        ):
            selected_fire = self._planner_skill_index("fire", controller, state, modules, directive)
            if selected_fire is None:
                selected_fire = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
            if selected_fire is not None:
                index, selected_skill = selected_fire
                agent_pb2 = modules["agent_pb2"]
                decision_out = {
                    "source": "stuck_recovery",
                    "skill": "repeated_failed_use_return_fire",
                    "reason": "shootable_target_blocking_repeated_use",
                    "repeat": int(self._failed_use_count),
                }
                if line_id is not None:
                    decision_out["line_id"] = int(line_id)
                    decision_out["line"] = int(line_id)
                    self._last_spatial_route_line_id = int(line_id)
                self._last_plan = {
                    "status": "active",
                    "skill": selected_skill,
                    "planner_skill": "fire",
                    "kind": decision_out["skill"],
                    **({"line": int(line_id), "line_id": int(line_id)} if line_id is not None else {}),
                }
                return (
                    index,
                    selected_skill,
                    agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=3),
                    decision_out,
                )

        selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            return None

        if self._failed_use_escape_steps <= 0:
            self._failed_use_escape_steps = USE_STUCK_ESCAPE_STEPS
            if line_id is not None:
                self._door_memory.record_status(int(line_id), status="repeated_failed_use_escape")

        action, action_label = self._planner_probe_escape_action(
            modules["agent_pb2"],
            getattr(state, "navigation", None),
            planned_action,
        )
        self._failed_use_escape_steps -= 1
        index, selected_skill = selected
        decision_out = {
            "source": "stuck_recovery",
            "skill": "repeated_failed_use_escape",
            "reason": "repeated_use_without_progress",
            "repeat": int(self._failed_use_count),
            "action": action_label,
        }
        if line_id is not None:
            decision_out["line_id"] = int(line_id)
            decision_out["line"] = int(line_id)
            self._last_spatial_route_line_id = int(line_id)
        self._last_plan = {
            "status": "active",
            "skill": selected_skill,
            "planner_skill": "recover_stuck",
            "kind": decision_out["skill"],
            **({"line": int(line_id), "line_id": int(line_id)} if line_id is not None else {}),
        }
        return index, selected_skill, action, decision_out

    def _repeated_probe_explore_escape(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        planner: SpatialPlanner,
        planned_action: Any,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if str(decision.get("skill", "")) != "planner_probe_explore":
            self._planner_probe_explore_key = None
            self._planner_probe_explore_count = 0
            self._planner_probe_escape_steps = 0
            return None
        action_name = str(decision.get("action", ""))
        if action_name not in {"turn", "scan"}:
            self._planner_probe_explore_key = None
            self._planner_probe_explore_count = 0
            return None
        if self._planner_probe_escape_steps > 0:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
            if selected is None:
                selected = self._planner_skill_index("retreat", controller, state, modules, directive)
            if selected is not None:
                agent_pb2 = modules["agent_pb2"]
                navigation = getattr(state, "navigation", None)
                action, action_label = self._planner_probe_escape_action(agent_pb2, navigation, planned_action)
                self._planner_probe_escape_steps -= 1
                index, selected_skill = selected
                return index, selected_skill, action, {
                    "source": "stuck_recovery",
                    "skill": "planner_probe_explore_escape",
                    "reason": "repeated_probe_explore_chain",
                    "repeat": int(self._planner_probe_explore_count),
                    "action": f"{action_label}_chain",
                    "remaining": int(self._planner_probe_escape_steps),
                }
        metrics = self._metrics(state)
        sector = -1
        try:
            sector = int(planner.sector_for_point_fp(int(metrics["x"]), int(metrics["y"])) or -1)
        except Exception:
            sector = -1
        bucket = int(128 * FP_UNIT)
        key = (
            int(metrics.get("episode", 0) or 0),
            int(metrics.get("map", 0) or 0),
            sector,
            int(metrics.get("x", 0) or 0) // bucket,
            int(metrics.get("y", 0) or 0) // bucket,
            action_name,
        )
        if key == self._planner_probe_explore_key:
            self._planner_probe_explore_count += 1
        else:
            self._planner_probe_explore_key = key
            self._planner_probe_explore_count = 1
        if self._planner_probe_explore_count < 6:
            return None

        record_frontier_blocked = getattr(self._world_memory, "record_frontier_blocked", None)
        if callable(record_frontier_blocked):
            record_frontier_blocked(sector if sector >= 0 else None)
        selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            return None

        agent_pb2 = modules["agent_pb2"]
        navigation = getattr(state, "navigation", None)
        decision_out: dict[str, Any] = {
            "source": "stuck_recovery",
            "skill": "planner_probe_explore_escape",
            "reason": "repeated_probe_explore",
            "repeat": int(self._planner_probe_explore_count),
            "sector": sector,
        }
        action, action_label = self._planner_probe_escape_action(agent_pb2, navigation, planned_action)
        decision_out["action"] = action_label
        self._planner_probe_escape_steps = 3
        index, selected_skill = selected
        self._last_plan = {
            "status": "active",
            "skill": selected_skill,
            "planner_skill": "recover_stuck",
            "kind": decision_out["skill"],
        }
        self._planner_probe_explore_key = None
        self._planner_probe_explore_count = 0
        return index, selected_skill, action, decision_out

    def _planner_probe_escape_action(self, agent_pb2: Any, navigation: Any, planned_action: Any) -> tuple[Any, str]:
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        lateral = [
            probe for probe in getattr(navigation, "direction_probes", []) or []
            if bool(getattr(probe, "open", False))
            and 60 <= abs(float(getattr(probe, "angle_offset_degrees", 0) or 0)) <= 135
        ]
        if lateral:
            probe = max(lateral, key=lambda item: int(getattr(item, "block_distance_fp", 0) or 0))
            offset = float(getattr(probe, "angle_offset_degrees", 0) or 0)
            if raw_cls is not None:
                side = 1 if offset > 0 else -1
                turn = 1024 if side > 0 else -1024
                return (
                    agent_pb2.PlayerAction(
                        duration_tics=12,
                        raw=raw_cls(
                            forward_move=34,
                            side_move=-58 if side > 0 else 58,
                            angle_turn=turn,
                        ),
                    ),
                    "arc_escape",
                )
            action_type = agent_pb2.ACTION_STRAFE_LEFT if offset > 0 else agent_pb2.ACTION_STRAFE_RIGHT
            return agent_pb2.PlayerAction(action=action_type, amount=42, duration_tics=12), "strafe_escape"
        if bool(getattr(navigation, "back_open", False)):
            if raw_cls is not None:
                return agent_pb2.PlayerAction(duration_tics=12, raw=raw_cls(forward_move=-48)), "backoff"
            return agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=42, duration_tics=12), "backoff"
        if bool(getattr(navigation, "forward_open", False)) and raw_cls is not None:
            return agent_pb2.PlayerAction(duration_tics=12, raw=raw_cls(forward_move=42)), "forward_escape"
        action_type = (
            agent_pb2.ACTION_TURN_RIGHT
            if int(getattr(planned_action, "action", 0) or 0) == int(agent_pb2.ACTION_TURN_LEFT)
            else agent_pb2.ACTION_TURN_LEFT
        )
        return agent_pb2.PlayerAction(action=action_type, amount=52, duration_tics=8), "reverse_scan"

    def _failed_use_signature(
        self,
        state: Any,
        *,
        line_id: int,
    ) -> tuple[int, int, int, int, int, int | None, int | None, int | None]:
        metrics = self._metrics(state)
        nav = getattr(state, "navigation", None)
        current_sector = getattr(nav, "current_sector", None)
        sector_id = getattr(current_sector, "sector_id", None)
        if sector_id is None and self._planner is not None:
            try:
                sector_id = self._planner.sector_for_point_fp(int(metrics["x"]), int(metrics["y"]))
            except Exception:
                sector_id = None
        floor_height = getattr(current_sector, "floor_height_fp", None)
        if floor_height is None:
            floor_height = getattr(current_sector, "floor_height", None)
        ceiling_height = getattr(current_sector, "ceiling_height_fp", None)
        if ceiling_height is None:
            ceiling_height = getattr(current_sector, "ceiling_height", None)
        bucket = int(8 * FP_UNIT)
        return (
            int(line_id),
            int(metrics.get("episode", 0) or 0),
            int(metrics.get("map", 0) or 0),
            int(metrics.get("x", 0) or 0) // bucket,
            int(metrics.get("y", 0) or 0) // bucket,
            int(sector_id) if sector_id is not None else None,
            int(floor_height) if floor_height is not None else None,
            int(ceiling_height) if ceiling_height is not None else None,
        )

    def _failed_use_line_matches_state(self, state: Any, line_id: int) -> bool:
        if self._failed_use_key is None:
            return False
        try:
            return self._failed_use_key == self._failed_use_signature(state, line_id=int(line_id))
        except Exception:
            return False

    def _record_failed_use_attempt(
        self,
        current: Any,
        *,
        line_id: int,
        moved_units: float,
    ) -> None:
        if float(moved_units) >= USE_STUCK_MOVE_EPS_UNITS:
            self._reset_failed_use_tracker()
            return
        key = self._failed_use_signature(current, line_id=int(line_id))
        if key == self._failed_use_key:
            self._failed_use_count += 1
        else:
            self._failed_use_key = key
            self._failed_use_count = 1
            self._failed_use_escape_steps = 0
        if self._failed_use_count >= USE_STUCK_THRESHOLD:
            self._door_memory.record_status(int(line_id), status="repeated_failed_use")

    def _reset_failed_use_tracker(self, *, line_id: int | None = None) -> None:
        if line_id is not None and self._failed_use_key is not None and self._failed_use_key[0] != int(line_id):
            return
        self._failed_use_key = None
        self._failed_use_count = 0
        self._failed_use_escape_steps = 0
