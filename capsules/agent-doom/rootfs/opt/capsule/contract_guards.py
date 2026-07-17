#!/usr/bin/env python3.11
"""Contract action guards for the Agent DOOM brain.

Owns the guard machinery that vets or rewrites a chosen action before it runs
under an active goal contract: `_guard_contract_action`, the exposed-idle and
wounded-route under-fire guards, preserve-health threshold-crossing and
blocked-lure handling, fire gating (`_fire_forbidden` / `_strip_fire` /
`_preserve_health_fire_allowed`), and the `_safe_contract_action` fallback.
Extracted verbatim from brain_runtime.BrainRuntime.

ContractGuardsMixin is a mixin over BrainRuntime state: every method runs on
the BrainRuntime instance and calls shared BrainRuntime helpers via self. It
holds no state of its own. This module must not import brain_runtime at
runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cautious_combat import CAUTIOUS_DOOR_AMBUSH_MAX_UNITS

if TYPE_CHECKING:
    from brain_runtime import ObjectiveDirective


class ContractGuardsMixin:
    """Contract action guard machinery, mixed into BrainRuntime."""

    def _guard_contract_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        index: int,
        skill: str,
        action: Any,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]]:
        action_type = int(getattr(action, "action", 0) or 0)
        agent_pb2 = modules["agent_pb2"]
        action_summary = modules["summarize_action"](action) or {}
        constraints = directive.contract.constraints
        if constraints.get("preserve_health"):
            peek = self._cautious_post_kill_los_peek_action(
                state,
                directive,
                controller,
                modules,
                action,
                decision,
                action_summary,
                agent_pb2,
            )
            if peek is not None:
                return peek
        if constraints.get("preserve_health") and self._unsafe_preserve_health_threshold_crossing(
            state,
            action,
            decision,
            action_summary,
            agent_pb2,
            objective=directive.contract.objective,
        ):
            lure = self._blocked_preserve_health_lure_action(
                state,
                directive,
                controller,
                modules,
                decision,
            )
            if lure is not None:
                return lure
            return self._safe_contract_action(
                state,
                directive,
                controller,
                modules,
                reason="blocked_threshold_avoid_damage",
            ) or (index, skill, action, decision)
        fired = (
            self._protobuf_action_fired(action, agent_pb2)
            or action_type == int(agent_pb2.ACTION_SHOOT)
            or self._action_fired(action_summary, agent_pb2)
        )
        if constraints.get("preserve_health") and fired and not self._preserve_health_fire_allowed(decision):
            enemy = self._nearest_enemy(state, prefer_visible=True) or {
                "id": int(self._cautious_target_id or 0),
                "threat": "unknown",
            }
            metrics = self._metrics(state)
            if (
                directive.contract.objective == "clear_area"
                and bool(metrics.get("shootable"))
                and float(enemy.get("distance", 9999.0) or 9999.0) <= 224.0
            ):
                # ponytail: the duel is already on — converting this shot into a
                # retreat hands the aiming enemy the first bullet. Let it fly.
                allowed = dict(decision)
                allowed["contract_guard"] = "duel_fire_allowed"
                return index, skill, action, allowed
            guarded = self._cautious_cover_action(
                state,
                directive,
                controller,
                modules,
                reason="blocked_non_cautious_avoid_damage_fire",
                enemy=enemy,
            )
            if guarded is not None:
                return guarded
            return self._safe_contract_action(
                state,
                directive,
                controller,
                modules,
                reason="blocked_non_cautious_avoid_damage_fire",
            ) or (index, skill, action, decision)
        if not fired:
            return index, skill, action, decision
        weapon = self._weapon_id(state)
        if constraints.get("kill_budget") == 0:
            return self._safe_contract_action(
                state,
                directive,
                controller,
                modules,
                reason="blocked_shot_no_kills",
            ) or (index, skill, action, decision)
        if constraints.get("ammo_budget") == 0 or constraints.get("weapon_policy") == "fist_only":
            if weapon == 0:
                guarded = dict(decision)
                guarded["contract_guard"] = "fist_attack_allowed"
                return index, skill, action, guarded
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
            if selected is not None and constraints.get("weapon_policy") == "fist_only":
                safe_index, safe_skill = selected
                safe = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SWITCH_WEAPON, amount=1, duration_tics=4)
                return safe_index, safe_skill, safe, {"source": "goal_contract", "skill": "blocked_ranged_fire_switch_fist"}
            return self._safe_contract_action(
                state,
                directive,
                controller,
                modules,
                reason="blocked_shot_no_ammo",
            ) or (index, skill, action, decision)
        return index, skill, action, decision

    def _preserve_health_fire_allowed(self, decision: dict[str, Any]) -> bool:
        primitive = str(decision.get("skill") or "")
        source = str(decision.get("source") or "")
        if primitive == "blocked_los_blind_lure_shot" and source == "goal_contract":
            return True
        if source != "cautious_combat":
            return False
        return primitive in {
            "peek_fire",
            "blind_lure_shot",
            "funnel_lure_shot",
            "jiggle_prefire_shot",
            "jiggle_commit_los_breach",
            "threshold_ambush_shot",
            "recent_hit_followup_shot",
            "post_kill_los_peek_shot",
            "sustained_lure_ping",
        }

    def _exposed_idle_under_fire_guard(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        index: int,
        skill: str,
        action: Any,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        constraints = directive.contract.constraints
        if directive.contract.objective != "complete_level":
            return None
        if (
            constraints.get("kill_budget") == 0
            or constraints.get("avoid_combat")
            or constraints.get("ammo_budget") == 0
            or constraints.get("weapon_policy") == "fist_only"
            or constraints.get("preserve_health")
        ):
            return None
        agent_pb2 = modules["agent_pb2"]
        action_summary = modules["summarize_action"](action) if "summarize_action" in modules else {}
        if self._action_has_effect(action, action_summary, agent_pb2):
            return None
        metrics = self._metrics(state)
        if not bool(metrics.get("visible_enemy") or metrics.get("shootable")):
            return None
        enemy = self._nearest_enemy(state, prefer_visible=True)
        if enemy is None or not bool(enemy.get("visible") or metrics.get("shootable")):
            return None
        if bool(metrics.get("shootable")) and not self._fire_forbidden(directive, state):
            try:
                ammo_total = int(metrics.get("ammo_total", 0) or 0)
                weapon = int(metrics.get("weapon", 0) or 0)
            except Exception:
                ammo_total = 0
                weapon = 0
            if ammo_total > 0 or weapon == 0:
                selected = self._planner_skill_index("fire", controller, state, modules, directive)
                if selected is None:
                    selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
                if selected is not None:
                    fire_index, fire_skill = selected
                    return (
                        fire_index,
                        fire_skill,
                        agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=3),
                        {
                            "source": "exposed_idle_guard",
                            "skill": "idle_under_fire_return_fire",
                            "replaced_skill": str(skill),
                            "replaced_action": str(decision.get("skill") or decision.get("action") or ""),
                            "enemy": int(enemy.get("id", 0) or 0),
                            "dist": int(float(enemy.get("distance", 0.0) or 0.0)),
                        },
                    )
        cover = self._cautious_cover_action(
            state,
            directive,
            controller,
            modules,
            reason="idle_under_fire_break_los",
            enemy=enemy,
            arm_commit=True,
        )
        if cover is None:
            return None
        cover_index, cover_skill, cover_action, cover_decision = cover
        cover_decision = dict(cover_decision)
        cover_decision.update(
            {
                "source": "exposed_idle_guard",
                "reason": "idle_under_fire_break_los",
                "replaced_skill": str(skill),
                "replaced_action": str(decision.get("skill") or decision.get("action") or ""),
            }
        )
        return cover_index, cover_skill, cover_action, cover_decision

    def _wounded_route_under_fire_guard(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        index: int,
        skill: str,
        action: Any,
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        constraints = directive.contract.constraints
        if directive.contract.objective != "complete_level":
            return None
        if (
            constraints.get("kill_budget") == 0
            or constraints.get("avoid_combat")
            or constraints.get("ammo_budget") == 0
            or constraints.get("weapon_policy") == "fist_only"
            or constraints.get("preserve_health")
            or self._fire_forbidden(directive, state)
        ):
            return None
        if int(decision.get("final_exit_commit", 0) or 0):
            return None
        if str(decision.get("state", "")) == "hazard_floor_escape":
            return None
        metrics = self._metrics(state)
        health = int(metrics.get("health", 100) or 100)
        committed = int(self._wounded_return_fire_steps) > 0
        if (health > 50 and not committed) or not bool(metrics.get("shootable")):
            if not bool(metrics.get("shootable")):
                self._wounded_return_fire_steps = 0
            return None
        agent_pb2 = modules["agent_pb2"]
        if self._protobuf_action_fired(action, agent_pb2):
            return None
        action_summary = modules.get("summarize_action", lambda _action: {})(action)
        if self._action_fired(action_summary, agent_pb2):
            return None
        selected = self._planner_skill_index("fire", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        self._wounded_return_fire_steps = 2 if not committed else max(0, self._wounded_return_fire_steps - 1)
        fire_index, fire_skill = selected
        return fire_index, fire_skill, agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=3), {
            "source": "wounded_fire_guard",
            "skill": "wounded_route_return_fire",
            "reason": "shootable_while_wounded_route_moving",
            "replaced_skill": str(skill),
            "replaced_action": str(decision.get("skill") or decision.get("action") or ""),
            "health": health,
            "commit_steps_remaining": int(self._wounded_return_fire_steps),
        }

    def _action_has_effect(self, action: Any, action_summary: dict[str, Any] | None, agent_pb2: Any) -> bool:
        action_type = int(getattr(action, "action", 0) or 0)
        amount = int(getattr(action, "amount", 0) or 0)
        if action_type and amount:
            return True
        if action_type in {
            int(getattr(agent_pb2, "ACTION_SHOOT", -100)),
            int(getattr(agent_pb2, "ACTION_USE", -101)),
            int(getattr(agent_pb2, "ACTION_SWITCH_WEAPON", -102)),
        }:
            return True
        raw = getattr(action, "raw", None)
        if raw is not None and any(
            int(getattr(raw, field, 0) or 0)
            for field in ("forward_move", "side_move", "angle_turn", "buttons")
        ):
            return True
        mouse = getattr(action, "mouse", None)
        if mouse is not None and any(int(getattr(mouse, field, 0) or 0) for field in ("dx", "dy", "buttons")):
            return True
        keys = getattr(action, "keys", None)
        if keys:
            return True
        summary = action_summary or {}
        raw_summary = summary.get("raw") if isinstance(summary, dict) else {}
        if isinstance(raw_summary, dict) and any(
            int(raw_summary.get(field, 0) or 0)
            for field in ("forward_move", "side_move", "angle_turn", "buttons")
        ):
            return True
        mouse_summary = summary.get("mouse") if isinstance(summary, dict) else {}
        if isinstance(mouse_summary, dict) and any(
            int(mouse_summary.get(field, 0) or 0)
            for field in ("dx", "dy", "buttons")
        ):
            return True
        return False

    def _unsafe_preserve_health_threshold_crossing(
        self,
        state: Any,
        action: Any,
        decision: dict[str, Any],
        action_summary: dict[str, Any],
        agent_pb2: Any,
        *,
        objective: str = "",
    ) -> bool:
        if str(decision.get("source", "")) != "spatial_planner":
            return False
        planner_skill = str(decision.get("skill", ""))
        if planner_skill not in {"planner_route_use_line_for_contact", "planner_route_to_los", "sector_route_to_los", "sector_route_to_use_line"}:
            return False
        try:
            distance = int(decision.get("dist", 9999) or 9999)
        except Exception:
            distance = 9999
        action_name = str(decision.get("action", ""))
        if action_name not in {"forward", "follow_opening", "cross_passable_portal", "cross_walk_trigger"}:
            return False
        metrics = self._metrics(state)
        action_type = int(getattr(action, "action", 0) or 0)
        raw = action_summary.get("raw") if isinstance(action_summary, dict) else {}
        try:
            raw_forward = int(raw.get("forward_move", 0) or 0) > 0
        except Exception:
            raw_forward = False
        moves_forward = action_type == int(agent_pb2.ACTION_FORWARD) or raw_forward
        if (
            objective == "clear_area"
            and planner_skill in {"planner_route_to_los", "sector_route_to_los"}
            and moves_forward
            and distance <= int(CAUTIOUS_DOOR_AMBUSH_MAX_UNITS)
        ):
            # ponytail: holding the ambush threshold only pays off when the enemy is
            # close enough to be lured in; a hidden enemy beyond 320 units never comes,
            # so blocking route progress there just burns the tic budget.
            enemy = self._nearest_enemy(state, prefer_visible=True)
            if enemy is not None and float(enemy.get("distance", 9999.0) or 9999.0) <= 320.0:
                return True
        if (
            planner_skill == "planner_route_to_los"
            and moves_forward
            and bool(metrics.get("visible_enemy"))
            and not bool(metrics.get("shootable"))
        ):
            return True
        if planner_skill in {"planner_route_to_los", "sector_route_to_los"} and moves_forward and distance <= 128:
            return True
        if distance > 96:
            return False
        if not bool(metrics.get("visible_enemy")) and not bool(metrics.get("shootable")):
            enemy = self._nearest_enemy(state, prefer_visible=True)
            if enemy is not None and float(enemy.get("distance", 9999.0) or 9999.0) > CAUTIOUS_DOOR_AMBUSH_MAX_UNITS:
                return False
        return moves_forward

    def _safe_contract_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        *,
        reason: str,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        selected = self._planner_skill_index("retreat", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("recover_stuck", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        navigation = getattr(state, "navigation", None)
        if bool(getattr(navigation, "back_open", False)):
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_BACKWARD, amount=24, duration_tics=8)
            kind = "back_away"
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_STRAFE_LEFT, amount=24, duration_tics=8)
            kind = "strafe_away"
        index, skill = selected
        return index, skill, action, {"source": "goal_contract", "skill": kind, "reason": reason}

    def _blocked_preserve_health_lure_action(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        decision: dict[str, Any],
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        if directive.contract.objective != "clear_area":
            return None
        metrics = self._metrics(state)
        if bool(metrics.get("visible_enemy") or metrics.get("shootable")):
            return None
        planner_skill = str(decision.get("skill", ""))
        if planner_skill not in {"planner_route_to_los", "sector_route_to_los"}:
            return None
        if str(decision.get("action", "")) not in {"forward", "follow_opening", "cross_passable_portal"}:
            return None
        try:
            distance = int(decision.get("dist", 9999) or 9999)
        except Exception:
            distance = 9999
        if distance > int(CAUTIOUS_DOOR_AMBUSH_MAX_UNITS):
            return None
        enemy = self._nearest_enemy(state, prefer_visible=True) or {"id": 0, "threat": "unknown"}
        armed_lure = (
            self._cautious_ambush_armed(enemy)
            or int(self._cautious_target_id or 0) != 0
            or int(getattr(self._combat_state, "shots", 0) or 0) > 0
        )
        if armed_lure:
            self._cautious_lure_wait_steps += 1
            if self._cautious_lure_wait_steps >= 1:
                jiggle_enemy = dict(enemy)
                jiggle_enemy["distance"] = min(float(jiggle_enemy.get("distance", distance) or distance), float(distance))
                jiggle_enemy["turn"] = float(decision.get("turn", 0.0) or 0.0)
                cover_profile = self._cautious_cover_profile(state) or {
                    "cover_evidence": "blocked_route_to_los",
                    "cover_dist": int(distance),
                }
                if self._cautious_jiggle_probe_attempts >= 4:
                    door_flash = self._cautious_door_flash_action(
                        state,
                        directive,
                        controller,
                        modules,
                        enemy=jiggle_enemy,
                        route_distance=int(distance),
                        cover_profile=cover_profile,
                    )
                    if door_flash is not None:
                        self._cautious_target_id = int(enemy.get("id", 0) or 0)
                        return door_flash
                breach = None
                if self._cautious_jiggle_probe_attempts >= 4 or self._cautious_probe_steps >= 8:
                    breach = self._cautious_jiggle_breach_action(
                        state,
                        directive,
                        controller,
                        modules,
                        enemy=jiggle_enemy,
                        route_distance=int(distance),
                        cover_profile=cover_profile,
                    )
                if breach is not None:
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
                    return breach
                jiggle = self._cautious_jiggle_peek_action(
                    state,
                    directive,
                    controller,
                    modules,
                    enemy=jiggle_enemy,
                    cover_profile=cover_profile,
                    reason="blocked_threshold_jiggle_peek",
                    pre_fire=True,
                )
                if jiggle is not None:
                    self._cautious_target_id = int(enemy.get("id", 0) or 0)
                    self._cautious_ambush_window = max(self._cautious_ambush_window, 8)
                    return jiggle
        selected = self._planner_skill_index("fire", controller, state, modules, directive)
        if selected is None:
            selected = self._planner_skill_index("close_visible_contact", controller, state, modules, directive)
        if selected is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is not None:
            action = agent_pb2.PlayerAction(duration_tics=2, raw=raw_cls(buttons=1))
            action_name = "raw_blind_fire"
        else:
            action = agent_pb2.PlayerAction(action=agent_pb2.ACTION_SHOOT, amount=1, duration_tics=2)
            action_name = "blind_fire"
        self._cautious_target_id = int(enemy.get("id", 0) or self._cautious_target_id or 0)
        self._cautious_lure_cooldown = 0
        self._cautious_ambush_window = max(self._cautious_ambush_window, 12)
        self._cautious_retreat_steps = max(self._cautious_retreat_steps, 4)
        index, skill = selected
        return index, skill, action, {
            "source": "goal_contract",
            "skill": "blocked_los_blind_lure_shot",
            "state": "lure_and_wait",
            "reason": "blocked_threshold_hidden_lure",
            "route_skill": planner_skill,
            "route_dist": int(distance),
            "action": action_name,
            "enemy": int(enemy.get("id", 0) or 0),
            "threat": str(enemy.get("threat") or "unknown"),
        }

    def _fire_forbidden(self, directive: ObjectiveDirective, state: Any) -> bool:
        constraints = directive.contract.constraints
        if constraints.get("kill_budget") == 0:
            return True
        if constraints.get("ammo_budget") == 0 or constraints.get("weapon_policy") == "fist_only":
            return self._weapon_id(state) != 0
        return False

    def _strip_fire(self, action: Any, agent_pb2: Any) -> None:
        if int(getattr(action, "action", 0) or 0) == int(agent_pb2.ACTION_SHOOT):
            action.action = agent_pb2.ACTION_TURN_LEFT
            action.amount = 8
            action.duration_tics = min(2, max(1, int(getattr(action, "duration_tics", 1) or 1)))
        raw = getattr(action, "raw", None)
        if raw is not None:
            raw.buttons = int(getattr(raw, "buttons", 0) or 0) & ~1
        mouse = getattr(action, "mouse", None)
        if mouse is not None:
            mouse.buttons = int(getattr(mouse, "buttons", 0) or 0) & ~1
        keys = getattr(action, "keys", None)
        if keys:
            del keys[:]
