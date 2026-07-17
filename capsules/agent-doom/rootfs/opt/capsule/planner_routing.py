#!/usr/bin/env python3.11
"""Planner-route following for the Agent DOOM brain.

Owns the brain-side machinery wrapped around SpatialPlanner routes: the
planner override that turns a route decision into a concrete action, planner
refresh and skill-index resolution, macro route-segment execution (burst
driving toward a tagged macro target with bounded resteer), and route-outcome
learning (planner outcome records, repeated-route and frontier-route repeat
memory, and planner-line crossing detection). Extracted verbatim from
brain_runtime.BrainRuntime.

PlannerRoutingMixin is a mixin over BrainRuntime state: every method runs on
the BrainRuntime instance, reads/writes attributes initialized in
BrainRuntime.__init__, and calls shared BrainRuntime helpers via self. It
holds no state of its own. This module must not import brain_runtime at
runtime.
"""

from __future__ import annotations

import math
import os
from typing import TYPE_CHECKING, Any

from cautious_combat import FP_UNIT
from planner import LIFT_WALK_SPECIALS, SpatialPlanner
from route_reactions import ANTI_GRIND_ESCAPE_STEPS, ANTI_GRIND_STUCK_THRESHOLD

if TYPE_CHECKING:
    from brain_runtime import ObjectiveDirective

# Macro route-segment execution (throughput): when the planner tags a movement with a macro
# target ("mt" = a point on a known-passable route), keep driving toward it in repeated bursts
# inside the capsule instead of re-running the full observe/override/plan stack every 6-14 tics.
MACRO_MAX_TICS = int(os.environ.get("PAIRPUTER_BRAIN_MACRO_MAX_TICS", "70"))
MACRO_TARGET_REACHED_UNITS = 44.0
MACRO_STALL_UNITS = 6.0
MACRO_MAX_RESTEER_DEG = 60.0
MACRO_RAW_STEER_TURN_SCALE = 24
MACRO_RAW_STEER_TURN_CAP = 1536


class PlannerRoutingMixin:
    """Planner-route following over BrainRuntime state (see module docstring)."""

    def _macro_route_segment(
        self,
        stub: Any,
        modules: dict[str, Any],
        state: Any,
        action: Any,
        decision: dict[str, Any],
        *,
        max_tics: int,
    ) -> tuple[Any, int] | None:
        """Drive a planner-tagged passable route segment in repeated bursts inside the capsule.

        Returns (final_state, tics_consumed) or None when the step is not macro-eligible.
        Guards hand control straight back to the full plan loop on: damage taken, a
        visible/shootable threat appearing, a movement stall (blocked), the target being
        reached, a re-steer that exceeds MACRO_MAX_RESTEER_DEG (geometry changed), or a
        human seizing input.
        """
        # ponytail: straight-line re-steer only, no mid-macro re-route — the outer loop
        # re-plans the moment any guard trips; add curvature-following only if segments
        # ever need it.
        target = self._macro_target(decision)
        if target is None:
            return None
        agent_pb2 = modules["agent_pb2"]
        if not self._macro_action_is_plain_move(action, agent_pb2):
            return None
        start = self._metrics(state)
        if start["visible_enemy"] or start["shootable"]:
            return None
        budget = min(int(max_tics), MACRO_MAX_TICS)
        burst = max(1, min(16, int(getattr(action, "duration_tics", 1) or 1)))
        if budget <= burst:
            return None
        total = 0
        bursts = 0
        previous = start
        current_state = state
        current_action = action
        while total + burst <= budget:
            current_action.duration_tics = burst
            current_state = self._run_action(stub, current_action, modules)
            total += burst
            bursts += 1
            self._update_world_model(current_state)
            self._combat_state.update(current_state)
            current = self._metrics(current_state)
            moved = math.dist((previous["x"], previous["y"]), (current["x"], current["y"])) / FP_UNIT
            remaining = math.dist((current["x"], current["y"]), target) / FP_UNIT
            damaged = int(current["health"]) < int(previous["health"])
            threat = bool(current["visible_enemy"] or current["shootable"])
            previous = current
            if damaged or threat or moved < MACRO_STALL_UNITS or remaining <= MACRO_TARGET_REACHED_UNITS:
                break
            if self._human_active():
                break
            resteered = self._macro_resteer_action(current, target, agent_pb2)
            if resteered is None:
                break
            current_action = resteered
        if bursts == 0:
            return None
        if bursts > 1:
            decision["macro"] = {"bursts": bursts, "tics": total}
        return current_state, max(1, total)

    def _macro_target(self, decision: dict[str, Any]) -> tuple[float, float] | None:
        if not isinstance(decision, dict) or str(decision.get("source", "")) != "spatial_planner":
            return None
        mt = decision.get("mt")
        if not (isinstance(mt, (list, tuple)) and len(mt) == 2):
            return None
        try:
            return float(mt[0]), float(mt[1])
        except (TypeError, ValueError):
            return None

    def _macro_action_is_plain_move(self, action: Any, agent_pb2: Any) -> bool:
        if self._protobuf_action_fired(action, agent_pb2):
            return False
        if getattr(action, "keys", None):
            return False
        act = int(getattr(action, "action", 0) or 0)
        if act == int(agent_pb2.ACTION_FORWARD):
            return True
        if act:
            return False
        raw = getattr(action, "raw", None)
        return raw is not None and int(getattr(raw, "forward_move", 0) or 0) > 0

    def _macro_resteer_action(self, metrics: dict[str, Any], target: tuple[float, float], agent_pb2: Any) -> Any | None:
        bearing = math.degrees(math.atan2(target[1] - float(metrics["y"]), target[0] - float(metrics["x"])))
        delta = ((bearing - float(metrics["angle"]) + 540.0) % 360.0) - 180.0
        if abs(delta) > MACRO_MAX_RESTEER_DEG:
            return None
        raw_cls = getattr(agent_pb2, "RawTiccmd", None)
        if raw_cls is not None:
            turn = max(-MACRO_RAW_STEER_TURN_CAP, min(MACRO_RAW_STEER_TURN_CAP, int(delta * MACRO_RAW_STEER_TURN_SCALE)))
            return agent_pb2.PlayerAction(duration_tics=8, raw=raw_cls(forward_move=50, angle_turn=turn))
        if abs(delta) > 12.0:
            return None
        return agent_pb2.PlayerAction(action=agent_pb2.ACTION_FORWARD, amount=42, duration_tics=8)

    def _refresh_planner(self, stub: Any, modules: dict[str, Any], state: Any) -> SpatialPlanner | None:
        snapshot = self._map_cache.refresh(stub, modules["agent_pb2"], state)
        if snapshot is None:
            return self._planner
        digest = int(getattr(snapshot, "digest", 0))
        if self._planner is None or self._planner_digest != digest:
            self._planner = SpatialPlanner(snapshot)
            self._planner_digest = digest
        return self._planner

    def _planner_override_is_progress(self, override: tuple[int, str, Any, dict[str, Any]] | None) -> bool:
        if override is None:
            return False
        _index, skill, _action, decision = override
        if skill not in {"route_progression", "open_use_line", "press_exit", "recover_stuck"}:
            return False
        primitive = str(decision.get("skill") or "")
        return not (
            primitive.startswith("combat_")
            or primitive in {"complete_level_fire_burst", "peek_fire", "funnel_lure_shot"}
        )

    def _planner_override(
        self,
        state: Any,
        directive: ObjectiveDirective,
        controller: Any,
        modules: dict[str, Any],
        stub: Any,
    ) -> tuple[int, str, Any, dict[str, Any]] | None:
        rules = set(directive.rules)
        if directive.explicit_allowed_skills or "cease_fire" in rules:
            return None
        if not ({"find_enemy", "shoot", "attack", "exit", "complete_level", "use", "explore"} & rules):
            return None
        planner = self._refresh_planner(stub, modules, state)
        if planner is None:
            self._last_plan = {"status": "unavailable", "reason": "no_map_snapshot"}
            return None
        self._update_world_model(state)
        self._combat_state.update(state)
        # contract_rules never emits preserve_health as a rule token, so the
        # planner's threat model (threat_rules gates self._threats) was silently
        # INACTIVE for avoid_damage goals — every LOS threat multiplier was 1.0
        # and covered-approach routing never engaged. Pass the constraint through
        # as a planner-only rule token here, at the single objective_action call.
        planner_rules: tuple[str, ...] = directive.rules
        if directive.contract.constraints.get("preserve_health"):
            planner_rules = tuple(dict.fromkeys((*directive.rules, "preserve_health")))
        plan = planner.objective_action(state, planner_rules, modules["agent_pb2"], self._door_memory, self._world_memory)
        if plan is None:
            self._last_plan = {"status": "no_plan", **planner.summary()}
            return None
        selected = self._planner_skill_index(plan.skill, controller, state, modules, directive)
        if selected is None:
            return None
        index, selected_skill = selected
        decision = dict(plan.detail)
        decision["source"] = "spatial_planner"
        decision["planner_skill"] = plan.skill
        if plan.door_line_id is not None:
            decision["line_id"] = int(plan.door_line_id)
        final_corridor_refusal = self._final_corridor_sprint_health_refusal(
            state,
            directive,
            controller,
            modules,
            plan.skill,
            decision,
        )
        if final_corridor_refusal is not None:
            _refusal_index, refusal_skill, _refusal_action, refusal_decision = final_corridor_refusal
            self._last_plan = {
                "status": "active",
                "skill": refusal_skill,
                "planner_skill": plan.skill,
                "kind": str(refusal_decision.get("skill", ""))[:40],
                **({"line": int(plan.door_line_id), "line_id": int(plan.door_line_id)} if plan.door_line_id is not None else {}),
            }
            return final_corridor_refusal
        health_route_refusal = self._health_route_threat_refusal(
            state,
            directive,
            controller,
            modules,
            plan.skill,
            decision,
        )
        if health_route_refusal is not None:
            _refusal_index, refusal_skill, _refusal_action, refusal_decision = health_route_refusal
            self._last_plan = {
                "status": "active",
                "skill": refusal_skill,
                "planner_skill": plan.skill,
                "kind": str(refusal_decision.get("skill", ""))[:40],
                **({"line": int(plan.door_line_id), "line_id": int(plan.door_line_id)} if plan.door_line_id is not None else {}),
            }
            return health_route_refusal
        route_refusal = self._route_threshold_refusal_fire(
            state,
            directive,
            controller,
            modules,
            plan.skill,
            decision,
        )
        if route_refusal is not None:
            _refusal_index, refusal_skill, _refusal_action, refusal_decision = route_refusal
            self._last_plan = {
                "status": "active",
                "skill": refusal_skill,
                "planner_skill": plan.skill,
                "kind": str(refusal_decision.get("skill", ""))[:40],
                **({"line": int(plan.door_line_id), "line_id": int(plan.door_line_id)} if plan.door_line_id is not None else {}),
            }
            return route_refusal
        no_kill_refusal = self._no_kill_route_threat_refusal(
            state,
            directive,
            controller,
            modules,
            plan.skill,
            decision,
        )
        if no_kill_refusal is not None:
            _refusal_index, refusal_skill, _refusal_action, refusal_decision = no_kill_refusal
            self._last_plan = {
                "status": "active",
                "skill": refusal_skill,
                "planner_skill": plan.skill,
                "kind": str(refusal_decision.get("skill", ""))[:40],
                **({"line": int(plan.door_line_id), "line_id": int(plan.door_line_id)} if plan.door_line_id is not None else {}),
            }
            return no_kill_refusal
        desperation_sprint = self._no_kill_desperation_sprint(
            state,
            directive,
            controller,
            modules,
            plan.skill,
            decision,
        )
        if desperation_sprint is not None:
            _sprint_index, sprint_skill, _sprint_action, sprint_decision = desperation_sprint
            self._last_plan = {
                "status": "active",
                "skill": sprint_skill,
                "planner_skill": plan.skill,
                "kind": str(sprint_decision.get("skill", ""))[:40],
                **({"line": int(plan.door_line_id), "line_id": int(plan.door_line_id)} if plan.door_line_id is not None else {}),
            }
            return desperation_sprint
        rotational_escape = self._rotational_stall_escape(
            state,
            directive,
            controller,
            modules,
            plan.skill,
            plan.action,
            decision,
        )
        if rotational_escape is not None:
            _escape_index, escape_skill, _escape_action, escape_decision = rotational_escape
            self._last_plan = {
                "status": "active",
                "skill": escape_skill,
                "planner_skill": plan.skill,
                "kind": str(escape_decision.get("skill", ""))[:40],
                **({"line": int(plan.door_line_id), "line_id": int(plan.door_line_id)} if plan.door_line_id is not None else {}),
            }
            return rotational_escape
        anti_grind_escape = self._anti_grind_escape(
            state,
            directive,
            controller,
            modules,
            plan.skill,
            plan.action,
            decision,
        )
        if anti_grind_escape is not None:
            _escape_index, escape_skill, _escape_action, escape_decision = anti_grind_escape
            self._last_plan = {
                "status": "active",
                "skill": escape_skill,
                "planner_skill": plan.skill,
                "kind": str(escape_decision.get("skill", ""))[:40],
                **({"line": int(plan.door_line_id), "line_id": int(plan.door_line_id)} if plan.door_line_id is not None else {}),
            }
            return anti_grind_escape
        probe_escape = self._repeated_probe_explore_escape(
            state,
            directive,
            controller,
            modules,
            planner,
            plan.action,
            decision,
        )
        if probe_escape is not None:
            return probe_escape
        use_escape = self._repeated_failed_use_escape(
            state,
            directive,
            controller,
            modules,
            plan.action,
            decision,
        )
        if use_escape is not None:
            return use_escape
        self._last_plan = {
            "status": "active",
            "skill": selected_skill,
            "planner_skill": plan.skill,
            "kind": str(decision.get("skill", ""))[:40],
            **({"line": int(plan.door_line_id), "line_id": int(plan.door_line_id)} if plan.door_line_id is not None else {}),
        }
        return index, selected_skill, plan.action, decision

    def _prefer_planner_before_recovery(self, directive: ObjectiveDirective) -> bool:
        rules = set(directive.rules)
        return directive.contract.style == "speedrun" and bool({"exit", "complete_level"} & rules)

    def _planner_first_for_stall(self, directive: ObjectiveDirective, stuck_steps: int) -> bool:
        return self._prefer_planner_before_recovery(directive) and int(stuck_steps) < 4

    def _planner_skill_index(
        self,
        skill: str,
        controller: Any,
        state: Any,
        modules: dict[str, Any],
        directive: ObjectiveDirective,
    ) -> tuple[int, str] | None:
        actions = list(modules["SKILL_ACTIONS"])
        mask = list(controller.action_mask(state))
        candidates = [skill]
        for fallback in ("seek_enemy", "route_progression", "open_use_line", "recover_stuck"):
            if fallback not in candidates:
                candidates.append(fallback)
        for candidate in candidates:
            if candidate not in directive.allowed_skills or candidate not in actions:
                continue
            index = actions.index(candidate)
            if index < len(mask) and mask[index]:
                return index, candidate
        for candidate in candidates:
            if candidate in directive.allowed_skills and candidate in actions and candidate not in {"fire", "engage"}:
                return actions.index(candidate), candidate
        for candidate in candidates:
            if candidate in {"close_visible_contact", "fire"} and candidate in directive.allowed_skills and candidate in actions:
                return actions.index(candidate), candidate
        return None

    def _record_planner_outcome(
        self,
        previous: Any,
        current: Any,
        action: Any,
        decision: dict[str, Any],
        route_outcome: dict[str, Any],
        modules: dict[str, Any],
    ) -> None:
        if str(decision.get("source", "")) != "spatial_planner":
            return
        action_summary = modules["summarize_action"](action) or {}
        if self._action_fired(action_summary, modules["agent_pb2"]):
            return
        moved_units = math.dist(
            (self._metrics(previous)["x"], self._metrics(previous)["y"]),
            (self._metrics(current)["x"], self._metrics(current)["y"]),
        ) / FP_UNIT
        self._record_rotational_stall_outcome(
            current,
            action=action,
            decision=decision,
            modules=modules,
            route_outcome=route_outcome,
            moved_units=moved_units,
        )
        line_id = decision.get("line_id")
        if line_id is None:
            line_id = decision.get("line")
        if line_id is None:
            return
        previous_health = int(self._metrics(previous)["health"])
        current_health = int(self._metrics(current)["health"])
        detail_skill = str(decision.get("skill", ""))
        planner_skill = str(decision.get("planner_skill", ""))
        line = self._planner._line_by_id(int(line_id)) if self._planner is not None else None
        try:
            line_special = int(decision.get("special", getattr(line, "special", 0)) or 0)
            line_tag = int(decision.get("tag", getattr(line, "tag", 0)) or 0)
        except Exception:
            line_special = 0
            line_tag = 0
        self._last_spatial_route_line_id = int(line_id)
        crossed_line = self._crossed_planner_line(previous, current, line)
        if crossed_line:
            self._reset_failed_use_tracker()
        if self._record_frontier_route_repeat(
            line_id=int(line_id),
            line=line,
            decision=decision,
            crossed_line=crossed_line,
            moved_units=moved_units,
        ):
            return
        self._record_anti_grind_outcome(
            current,
            line_id=int(line_id),
            line=line,
            action=action,
            decision=decision,
            modules=modules,
            route_outcome=route_outcome,
            crossed_line=crossed_line,
            moved_units=moved_units,
        )
        if self._record_repeated_route_attempt(
            current=current,
            line_id=int(line_id),
            line=line,
            action=action,
            decision=decision,
            modules=modules,
            route_outcome=route_outcome,
            crossed_line=crossed_line,
        ):
            return
        frontier_route = detail_skill == "frontier_sector_route" or planner_skill == "frontier_sector_route"
        if (
            frontier_route
            and moved_units < 6
            and (previous_health > current_health or self._movement_stalled(action, moved_units, modules))
        ):
            sector_id = decision.get("sector")
            record_frontier_blocked = getattr(self._world_memory, "record_frontier_blocked", None)
            if callable(record_frontier_blocked):
                record_frontier_blocked(int(sector_id) if sector_id is not None else None)
            line = self._planner._line_by_id(int(line_id)) if self._planner is not None else None
            if bool(getattr(line, "passable", False)):
                record_route_contact = getattr(self._door_memory, "record_route_contact", None)
                if callable(record_route_contact):
                    record_route_contact(int(line_id))
                return
            self._door_memory.record_failure(int(line_id), status="route_contact_blocked")
            return
        if (
            detail_skill == "planner_route_use_line_for_contact"
            and moved_units < 6
            and self._movement_stalled(action, moved_units, modules)
        ):
            record_route_contact = getattr(self._door_memory, "record_route_contact", None)
            if callable(record_route_contact):
                record_route_contact(int(line_id))
            else:
                self._door_memory.record_failure(int(line_id), status="route_contact_blocked")
            return
        passable_portal_actions = {
            "blocked_portal_side_squeeze",
            "center_passable_portal",
            "center_passable_portal_raw",
            "cross_passable_portal",
            "face_passable_portal",
            "reset_blocked_passable_portal",
            "squeeze_passable_portal",
        }
        if (
            (detail_skill.startswith("sector_route") or planner_skill.startswith("sector_route"))
            and moved_units < 6
            and (previous_health > current_health or self._movement_stalled(action, moved_units, modules))
        ):
            line = self._planner._line_by_id(int(line_id)) if self._planner is not None else None
            if bool(getattr(line, "passable", False)):
                record_route_contact = getattr(self._door_memory, "record_route_contact", None)
                if callable(record_route_contact):
                    record_route_contact(int(line_id))
                return
            self._door_memory.record_failure(int(line_id), status="route_contact_blocked")
            return
        if (
            detail_skill in passable_portal_actions
            and moved_units < 24
            and (previous_health > current_health or self._movement_stalled(action, moved_units, modules))
        ):
            line = self._planner._line_by_id(int(line_id)) if self._planner is not None else None
            if bool(getattr(line, "passable", False)):
                record_route_contact = getattr(self._door_memory, "record_route_contact", None)
                if callable(record_route_contact):
                    record_route_contact(int(line_id))
                return
        line_requires_cross_evidence = bool(
            (
                getattr(line, "door", False)
                or getattr(line, "use_trigger", False)
                or getattr(line, "exit", False)
            )
            and not getattr(line, "passable", False)
        ) or bool(getattr(line, "walk_trigger", False) and line_special in LIFT_WALK_SPECIALS)
        if line is not None:
            self._door_memory.observe_line(
                int(line_id),
                special=line_special,
                tag=line_tag,
                exit_line=bool(getattr(line, "exit", False)),
            )
        if int(getattr(action, "action", 0)) == int(modules["agent_pb2"].ACTION_USE):
            nav = getattr(current, "navigation", None)
            front_distance = int(getattr(nav, "front_block_distance_fp", 0) or 0)
            forward_open = bool(getattr(nav, "forward_open", False)) and (
                not front_distance or front_distance > 32 * FP_UNIT
            )
            if bool(route_outcome.get("reached")) or crossed_line:
                self._door_memory.record_success(int(line_id))
                self._reset_failed_use_tracker()
            else:
                if forward_open:
                    self._door_memory.record_success(int(line_id))
                else:
                    self._door_memory.record_failure(int(line_id), status="no_progress_after_use")
                self._record_failed_use_attempt(
                    current,
                    line_id=int(line_id),
                    moved_units=moved_units,
                )
            return
        if moved_units >= 24:
            self._reset_failed_use_tracker()
            if line_requires_cross_evidence and not crossed_line and not bool(route_outcome.get("reached")):
                nav = getattr(current, "navigation", None)
                front_distance = int(getattr(nav, "front_block_distance_fp", 0) or 0)
                blocked_ahead = (front_distance and front_distance <= 48 * FP_UNIT) or not bool(getattr(nav, "forward_open", False))
                if self._door_memory.is_open(int(line_id)) and blocked_ahead:
                    self._door_memory.record_stale_open(int(line_id))
                return
            self._door_memory.record_progress(int(line_id))
            return
        if self._door_memory.is_open(int(line_id)):
            nav = getattr(current, "navigation", None)
            front_distance = int(getattr(nav, "front_block_distance_fp", 0) or 0)
            blocked_ahead = (front_distance and front_distance <= 48 * FP_UNIT) or not bool(getattr(nav, "forward_open", False))
            if blocked_ahead:
                self._door_memory.record_stale_open(int(line_id))

    def _line_id_from_decision(self, decision: dict[str, Any]) -> int | None:
        line_id = decision.get("line_id")
        if line_id is None:
            line_id = decision.get("line")
        if line_id is None:
            return None
        try:
            parsed = int(line_id)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    def _record_frontier_route_repeat(
        self,
        *,
        line_id: int,
        line: Any | None,
        decision: dict[str, Any],
        crossed_line: bool,
        moved_units: float,
    ) -> bool:
        detail_skill = str(decision.get("skill", ""))
        planner_skill = str(decision.get("planner_skill", ""))
        if detail_skill != "frontier_sector_route" and planner_skill != "frontier_sector_route":
            return False
        sector_id = decision.get("sector", decision.get("route_step_sector"))
        if sector_id is None:
            return False
        action_name = str(decision.get("action", ""))
        if action_name not in {
            "forward",
            "steer_forward",
            "turn",
            "cross",
            "cross_passable_portal",
            "center_passable_portal",
            "center_passable_portal_raw",
            "squeeze_passable_portal",
        }:
            return False
        key = (int(sector_id), detail_skill or planner_skill, action_name)
        count = self._frontier_route_repeat_counts.get(key, 0) + 1
        self._frontier_route_repeat_counts[key] = count
        threshold = 8 if float(moved_units) >= 6.0 else 4
        if count < threshold:
            return False
        record_frontier_blocked = getattr(self._world_memory, "record_frontier_blocked", None)
        if callable(record_frontier_blocked):
            record_frontier_blocked(int(sector_id))
        if bool(
            getattr(line, "passable", False)
            or getattr(line, "door", False)
            or getattr(line, "use_trigger", False)
        ):
            record_route_contact = getattr(self._door_memory, "record_route_contact", None)
            if callable(record_route_contact):
                record_route_contact(int(line_id))
        else:
            self._door_memory.record_failure(int(line_id), status="frontier_route_no_advance")
        self._frontier_route_repeat_counts.pop(key, None)
        return True

    def _crossed_planner_line(self, previous: Any, current: Any, line: Any | None) -> bool:
        if line is None:
            return False
        prev_metrics = self._metrics(previous)
        curr_metrics = self._metrics(current)
        p1 = (float(prev_metrics["x"]), float(prev_metrics["y"]))
        p2 = (float(curr_metrics["x"]), float(curr_metrics["y"]))
        q1 = (float(getattr(getattr(line, "a", None), "x", 0)), float(getattr(getattr(line, "a", None), "y", 0)))
        q2 = (float(getattr(getattr(line, "b", None), "x", 0)), float(getattr(getattr(line, "b", None), "y", 0)))
        if p1 == p2 or q1 == q2:
            return False
        margin = 24.0 * FP_UNIT
        if (
            max(p1[0], p2[0]) + margin < min(q1[0], q2[0])
            or max(q1[0], q2[0]) + margin < min(p1[0], p2[0])
            or max(p1[1], p2[1]) + margin < min(q1[1], q2[1])
            or max(q1[1], q2[1]) + margin < min(p1[1], p2[1])
        ):
            return False

        def orient(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

        o1 = orient(p1, p2, q1)
        o2 = orient(p1, p2, q2)
        o3 = orient(q1, q2, p1)
        o4 = orient(q1, q2, p2)
        epsilon = 2.0 * FP_UNIT * FP_UNIT
        return (o1 * o2 <= epsilon) and (o3 * o4 <= epsilon)

    def _record_repeated_route_attempt(
        self,
        *,
        current: Any,
        line_id: int,
        line: Any | None,
        action: Any,
        decision: dict[str, Any],
        modules: dict[str, Any],
        route_outcome: dict[str, Any],
        crossed_line: bool,
    ) -> bool:
        if int(getattr(action, "action", 0) or 0) == int(modules["agent_pb2"].ACTION_USE):
            return False
        action_summary = modules["summarize_action"](action) or {}
        if self._action_fired(action_summary, modules["agent_pb2"]):
            return False
        detail_skill = str(decision.get("skill", ""))
        action_name = str(decision.get("action", ""))
        route_reached_requires_cross = (
            (
                detail_skill == "navcell_to_portal"
                and action_name in {"forward", "steer_forward", "turn"}
            )
            or (
                detail_skill == "center_passable_portal"
                and action_name in {"center_passable_portal", "center_passable_portal_raw"}
            )
        ) and not crossed_line
        if bool(route_outcome.get("reached")) or crossed_line:
            key = (int(line_id), detail_skill)
            if not route_reached_requires_cross:
                self._planner_route_repeat_counts.pop(key, None)
                return False
        route_like = (
            detail_skill.startswith("sector_route")
            or detail_skill.startswith("route_waypoint")
            or detail_skill in {
                "center_passable_portal",
                "frontier_sector_route",
                "navcell_to_portal",
                "planner_route_to_use_line",
                "planner_route_to_los",
                "planner_route_walk_trigger_for_contact",
                "last_chance_live_use_line",
            }
        )
        movement_like = action_name in {
            "center_passable_portal",
            "center_passable_portal_raw",
            "forward",
            "steer_forward",
            "turn",
            "cross",
            "cross_passable_portal",
            "cross_walk_trigger",
            "follow_opening",
            "approach_use",
            "approach_walk_trigger",
            "force_follow_live_use_line",
            "force_follow_retried_use_line",
            "retreat_retried_use_line",
            "retreat_pressure_upcoming_use_line",
        }
        if not (route_like or movement_like):
            return False
        try:
            distance = int(decision.get("dist", 0) or 0)
        except Exception:
            distance = 0
        far_route = bool(distance and distance > 512 and action_name not in {"cross", "cross_passable_portal", "cross_walk_trigger"})
        key = (int(line_id), detail_skill or str(decision.get("planner_skill", "")))
        count = self._planner_route_repeat_counts.get(key, 0) + 1
        self._planner_route_repeat_counts[key] = count
        threshold = 3 if action_name in {
            "center_passable_portal",
            "center_passable_portal_raw",
            "cross",
            "cross_passable_portal",
            "cross_walk_trigger",
            "steer_forward",
            "turn",
        } else 4
        if (
            action_name in {"force_follow_live_use_line", "force_follow_retried_use_line"}
            or detail_skill == "last_chance_live_use_line"
        ):
            threshold = 3
        if far_route:
            threshold = max(threshold, 6)
        if count < threshold:
            return False
        try:
            special = int(decision.get("special", getattr(line, "special", 0)) or 0)
            tag = int(decision.get("tag", getattr(line, "tag", 0)) or 0)
        except Exception:
            special = 0
            tag = 0
        self._door_memory.observe_line(
            int(line_id),
            special=special,
            tag=tag,
            exit_line=bool(getattr(line, "exit", False)),
        )
        record_force_follow_stalled = getattr(self._door_memory, "record_force_follow_stalled", None)
        record_route_abandoned = getattr(self._door_memory, "record_route_abandoned", None)
        live_use_loop = detail_skill == "last_chance_live_use_line" or action_name in {
            "force_follow_live_use_line",
            "force_follow_retried_use_line",
        }
        if live_use_loop and callable(record_force_follow_stalled):
            record_force_follow_stalled(int(line_id), special=special, tag=tag)
        elif callable(record_route_abandoned):
            try:
                record_route_abandoned(int(line_id), passable=bool(getattr(line, "passable", False)))
            except TypeError:
                record_route_abandoned(int(line_id))
        elif callable(getattr(self._door_memory, "record_route_contact", None)):
            self._door_memory.record_route_contact(int(line_id))
        else:
            self._door_memory.record_failure(int(line_id), status="repeated_route_no_cross")
        if (
            detail_skill == "center_passable_portal"
            and action_name in {"center_passable_portal", "center_passable_portal_raw"}
            and not crossed_line
        ):
            self._anti_grind_key = self._anti_grind_signature(
                current,
                decision=decision,
                line_id=int(line_id),
            )
            self._anti_grind_count = max(int(self._anti_grind_count), ANTI_GRIND_STUCK_THRESHOLD)
            self._anti_grind_escape_steps = max(int(self._anti_grind_escape_steps), ANTI_GRIND_ESCAPE_STEPS)
        self._planner_route_repeat_counts.pop(key, None)
        return True
