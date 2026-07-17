#!/usr/bin/env python3.11
"""Goal-contract evaluation for the Agent DOOM brain.

Owns the per-step contract verdict and end-of-run stop reporting: `_evaluate`
(constraint violations and success checks against the run baseline),
`_committed_contract`, `_stop_reason`, and the `_progress_metrics` /
`_evidence` builders `_finish` uses to assemble the goal result. Extracted
verbatim from brain_runtime.BrainRuntime.

ContractEvalMixin is a mixin over BrainRuntime state: every method runs on the
BrainRuntime instance and calls shared BrainRuntime helpers via self. It holds
no state of its own. This module must not import brain_runtime at runtime.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from cautious_combat import FP_UNIT

if TYPE_CHECKING:
    from brain_runtime import ObjectiveDirective

# One median zombieman bullet (3×d5 ∈ {3,6,9,12,15}): the run survives one
# graze, two bullets is a fail. See the preserve_health check in _evaluate.
PRESERVE_HEALTH_DAMAGE_ALLOWANCE = 9


class ContractEvalMixin:
    """Contract evaluation + stop reporting, mixed into BrainRuntime."""

    def _evaluate(
        self,
        directive: ObjectiveDirective,
        baseline: dict[str, Any],
        current: dict[str, Any],
        *,
        fired: bool,
        shootable_seen: bool = False,
        damage_taken: bool = False,
    ) -> dict[str, str]:
        rules = set(directive.rules)
        kill_delta = int(current["kills"]) - int(baseline["kills"])
        ammo_spent = int(current["ammo_total"]) < int(baseline["ammo_total"])
        moved = math.dist((baseline["x"], baseline["y"]), (current["x"], current["y"])) / FP_UNIT
        constraints = directive.contract.constraints
        if int(current["health"]) <= 0:
            return {"status": "failed", "summary": "player died before objective completion"}
        if int(current["kills"]) < int(baseline["kills"]):
            return {"status": "failed", "summary": "episode state reset before objective completion"}
        if constraints.get("kill_budget") == 0 and kill_delta > 0 and (fired or ammo_spent):
            return {"status": "failed", "summary": f"kill budget violated: +{kill_delta}"}
        if constraints.get("ammo_budget") == 0 and ammo_spent:
            return {"status": "failed", "summary": "ammo budget violated"}
        # ponytail: avoid_damage = "don't get hurt", not "never lose a hit point".
        # A 3-6hp graze in an otherwise perfect clear failed runs that a human
        # would call flawless; hitscan RNG makes literal-zero a coin flip (measured
        # ~15-25% over 110 runs). 5hp = one glancing zombieman bullet. Keep in sync
        # with the avoid_damage gate in eval_runner.py.
        if constraints.get("preserve_health") and int(baseline["health"]) - int(current["health"]) > PRESERVE_HEALTH_DAMAGE_ALLOWANCE:
            return {
                "status": "failed",
                "summary": f"health budget violated: {current['health']}/{baseline['health']} (allowance {PRESERVE_HEALTH_DAMAGE_ALLOWANCE})",
            }
        if "cease_fire" in rules:
            return {"status": "achieved", "summary": "fire suppressed; no shooting objective is active"}
        if "complete_level" in rules and (current["episode"], current["map"]) != (baseline["episode"], baseline["map"]):
            return {"status": "achieved", "summary": "confirmed level transition"}
        if directive.contract.objective in {"clear_area", "rampage"}:
            target = int(constraints.get("kill_target", 2) or 2)
            if kill_delta >= target:
                return {"status": "achieved", "summary": f"confirmed area clear kill delta +{kill_delta}"}
        # RAMPAGE never terminates on an exit affordance: the exit is the direction,
        # not the win. It only achieves at its kill_target above; otherwise it keeps
        # hunting+advancing (tracking) so the drive doesn't quit with the map alive.
        if directive.contract.objective == "rampage":
            return {"status": "tracking", "summary": f"rampage kills={kill_delta}/{int(constraints.get('kill_target', 8) or 8)}"}
        if directive.contract.objective == "recover_health" and int(current["health"]) > int(baseline["health"]):
            return {"status": "achieved", "summary": f"confirmed health recovery +{int(current['health']) - int(baseline['health'])}"}
        if "shoot" in rules and (kill_delta > 0 or (fired and ammo_spent and shootable_seen)):
            return {"status": "achieved", "summary": f"confirmed kill delta +{kill_delta}" if kill_delta else "confirmed shot at a shootable enemy"}
        if "attack" in rules and kill_delta > 0:
            if directive.contract.objective == "clear_area":
                target = int(constraints.get("kill_target", 2) or 2)
                return {"status": "tracking", "summary": f"clear_area kills={kill_delta}/{target}"}
            if constraints.get("ammo_budget") == 0 and not ammo_spent:
                return {"status": "achieved", "summary": f"confirmed enemy kill delta +{kill_delta} with no ammo spent"}
            return {"status": "achieved", "summary": f"confirmed enemy kill delta +{kill_delta}"}
        if "find_enemy" in rules and not ({"shoot", "attack"} & rules) and (current["visible_enemy"] or current["shootable"]):
            return {"status": "achieved", "summary": "enemy contact is visible or shootable"}
        if "exit" in rules and "complete_level" not in rules and current["exit_line"]:
            return {"status": "achieved", "summary": "exit affordance is in use range"}
        if "use" in rules and not ({"exit", "complete_level"} & rules) and current["exit_line"]:
            return {"status": "achieved", "summary": "usable progression/exit affordance is visible"}
        if "survive" in rules and int(current["tick"]) - int(baseline["tick"]) >= 175 and int(current["health"]) > 0:
            if constraints.get("preserve_health") and int(current["health"]) < max(1, int(baseline["health"]) - 5):
                return {"status": "tracking", "summary": f"preserve_health hp={current['health']}/{baseline['health']}"}
            return {"status": "achieved", "summary": "survived the requested window"}
        if "explore" in rules and not ({"exit", "complete_level", "attack", "shoot", "find_enemy"} & rules) and moved >= 64:
            return {"status": "achieved", "summary": f"moved {moved:.0f} map units"}
        parts = []
        if {"find_enemy", "shoot", "attack"} & rules:
            parts.append(f"enemy_count={current['enemy_count']} visible={int(current['visible_enemy'])} shootable={int(current['shootable'])}")
        if "shoot" in rules:
            parts.append(f"fired={int(fired)} ammo_spent={int(ammo_spent)} shootable_seen={int(shootable_seen)}")
        if {"shoot", "attack"} & rules:
            parts.append(f"kill_delta={kill_delta}")
        if "exit" in rules:
            parts.append(f"exit_line={int(current['exit_line'])} dist={current.get('exit_dist', 0)}")
        return {"status": "tracking", "summary": "; ".join(parts) or "objective still in progress"}

    def _committed_contract(self, directive: ObjectiveDirective) -> dict[str, Any]:
        constraints = []
        raw_constraints = directive.contract.constraints
        if raw_constraints.get("kill_budget") == 0:
            constraints.append("no_kills")
        if raw_constraints.get("ammo_budget") == 0:
            constraints.append("no_ammo")
        if raw_constraints.get("weapon_policy") == "fist_only":
            constraints.append("fist_only")
        if raw_constraints.get("avoid_combat"):
            constraints.append("avoid_combat")
        if raw_constraints.get("preserve_health"):
            constraints.append("avoid_damage")
        return {
            "goal": directive.contract.raw[:120] if directive.contract.raw else directive.objective[:120],
            "objective": directive.contract.objective,
            "style": directive.contract.style,
            "constraints": constraints,
            "max_tics": int(directive.max_tics),
            "max_steps": int(directive.max_steps),
            "success_evidence": list(directive.contract.success_evidence)[:8],
            "failure_evidence": list(directive.contract.failure_evidence)[:8],
        }

    def _stop_reason(
        self,
        directive: ObjectiveDirective,
        internal_status: str,
        summary: str,
        delta: dict[str, Any],
        baseline: dict[str, Any],
        final_metrics: dict[str, Any],
        *,
        tics: int,
    ) -> str:
        text = str(summary or "").lower()
        objective = directive.contract.objective
        if internal_status == "interrupted":
            return "human_interrupt"
        if internal_status == "budget_exhausted":
            if "wall-clock" in text or "wall clock" in text:
                return "wall_clock_exceeded"
            return "max_tics_exceeded" if directive.max_tics and tics >= directive.max_tics else "budget_exhausted"
        if internal_status == "failed":
            if "died" in text:
                return "player_dead"
            if "kill budget" in text or "ammo budget" in text or "health budget" in text:
                return "constraint_violation"
            if "reset" in text:
                return "episode_reset"
            return "failed"
        if internal_status == "achieved":
            if (final_metrics.get("episode", 0), final_metrics.get("map", 0)) != (baseline.get("episode", 0), baseline.get("map", 0)):
                return "reached_next_level"
            if objective in {"exit_level", "complete_level"}:
                return "reached_exit"
            if objective in {"kill_enemy", "clear_area", "rampage"}:
                return "enemy_killed"
            if objective == "find_enemy":
                return "enemy_found"
            if objective == "recover_health":
                return "health_recovered"
            if objective in {"survive", "preserve_health"}:
                return "survived_window"
            return "objective_achieved"
        return "in_progress"

    def _progress_metrics(self, baseline: dict[str, Any], final_metrics: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
        moved = math.dist(
            (int(baseline.get("x", 0) or 0), int(baseline.get("y", 0) or 0)),
            (int(final_metrics.get("x", 0) or 0), int(final_metrics.get("y", 0) or 0)),
        ) / FP_UNIT
        metrics = {
            "kills_delta": int(delta.get("kills", 0) or 0),
            "health_delta": int(delta.get("health", 0) or 0),
            "ammo_delta": int(delta.get("ammo", 0) or 0),
            "shots_fired": int(bool(delta.get("fired", 0))),
            "moved_units": int(moved),
            "exit_line": bool(final_metrics.get("exit_line", False)),
            "visible_enemy": bool(final_metrics.get("visible_enemy", False)),
            "shootable": bool(final_metrics.get("shootable", False)),
        }
        map_changed = bool(
            (final_metrics.get("episode", 0), final_metrics.get("map", 0))
            != (baseline.get("episode", 0), baseline.get("map", 0))
        )
        if map_changed:
            metrics["map_changed"] = True
        if "agent_kills" in delta:
            metrics["agent_kills"] = int(delta.get("agent_kills", 0) or 0)
        if bool(delta.get("damage_taken", False)):
            metrics["damage_taken"] = bool(delta.get("damage_taken", False))
        return metrics

    def _evidence(self, baseline: dict[str, Any], final_metrics: dict[str, Any], delta: dict[str, Any], *, fired: bool) -> dict[str, Any]:
        return {
            "start": {
                "m": [baseline.get("episode", 0), baseline.get("map", 0)],
                "tick": baseline.get("tick", 0),
                "hp": baseline.get("health", 0),
                "kills": baseline.get("kills", 0),
                "ammo": baseline.get("ammo_total", 0),
            },
            "end": {
                "m": [final_metrics.get("episode", 0), final_metrics.get("map", 0)],
                "tick": final_metrics.get("tick", 0),
                "hp": final_metrics.get("health", 0),
                "kills": final_metrics.get("kills", 0),
                "ammo": final_metrics.get("ammo_total", 0),
            },
            "delta": {"kills": delta.get("kills", 0), "health": delta.get("health", 0), "ammo": delta.get("ammo", 0), "fired": int(bool(fired))},
        }
