#!/usr/bin/env python3.11
"""Status and result assembly for the Agent DOOM brain.

Owns every externally visible status/result surface of BrainRuntime that is
not the drive loop itself: brain/map/vision/tactical status packets, the
compact tactical snapshot and probe compaction behind them, compact goal
results, the _finish result shaping at objective end, objective-run memory
records, and the persistent-memory summary. Extracted verbatim from
brain_runtime.BrainRuntime.

BrainStatusMixin is a mixin over BrainRuntime state: every method runs on
the BrainRuntime instance, reads/writes attributes initialized in
BrainRuntime.__init__, and calls shared BrainRuntime helpers via self. It
holds no state of its own. This module must not import brain_runtime at
runtime.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

# Constants used by status/result assembly. Shared ones are imported back
# into brain_runtime (which re-exports them for existing importers).
MEMORY_PATH = Path(os.environ.get("PAIRPUTER_BRAIN_MEMORY_PATH", "/home/app/app/agent_memory/e1m1.json"))
SAVE_OBJECTIVE_RUNS = os.environ.get("PAIRPUTER_BRAIN_SAVE_RUNS", "0").lower() in {"1", "true", "yes"}
EXTERNAL_STATUSES = {
    "achieved": "success",
    "failed": "failed",
    "interrupted": "interrupted",
    "budget_exhausted": "failed",
    "tracking": "running",
}


def _external_status(internal_status: str) -> str:
    return EXTERNAL_STATUSES.get(str(internal_status or ""), "failed")


class BrainStatusMixin:
    """Status/result assembly over BrainRuntime state (see module docstring)."""

    def status(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._last_status)
            out["memory"] = self._memory_summary()
            out["map"] = self.map_status()
            out["vision"] = self.vision_status()
            return out

    def memory(self) -> dict[str, Any]:
        with self._lock:
            return self._memory_summary()

    def map_status(self) -> dict[str, Any]:
        out = {
            "cache": self._map_cache.summary(),
            "doors": self._door_memory.summary(),
            "world": self._world_memory.summary(),
            "combat": self._combat_state.summary(),
        }
        if self._trace.enabled:
            out["trace"] = self._trace.summary()
        if self._planner is not None:
            out["planner"] = self._planner.summary()
        if self._last_plan is not None:
            out["last_plan"] = self._last_plan
        return out

    def vision_status(self) -> dict[str, Any]:
        return self._vision.status()

    def tactical_status(self) -> dict[str, Any]:
        """One compact Commander-facing status packet for Codex/AgentCore."""

        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return self._tactical_status_snapshot(busy=True, live=False)
        try:
            return self._tactical_status_snapshot(busy=False, live=True)
        finally:
            self._lock.release()

    def _tactical_status_snapshot(self, *, busy: bool, live: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": str(self._last_status.get("status", "idle"))[:16],
            "stop_reason": str(self._last_status.get("stop_reason", ""))[:40],
            "objective": str(self._last_status.get("objective", ""))[:48],
            "phase": str(self._last_status.get("skill") or self._last_status.get("phase") or "")[:40],
            "steps": int(self._last_status.get("steps", 0) or 0),
            "tics": int(self._last_status.get("tics", 0) or 0),
            "human_active": bool(self._human_check_active if busy else self._human_active()),
        }
        if busy:
            out["busy"] = True
        summary = str(self._last_status.get("summary", ""))[:96]
        if summary:
            out["summary"] = summary
        if live:
            try:
                modules = self._lazy_imports()
                stub, chan = self._stub(modules)
                try:
                    state = self._observe(stub, modules)
                finally:
                    chan.close()
                metrics = self._metrics(state)
                out["state"] = {
                    "m": [metrics["episode"], metrics["map"]],
                    "hp": metrics["health"],
                    "kills": metrics["kills"],
                    "ammo": metrics["ammo_total"],
                    "enemies": metrics["enemy_count"],
                    "visible": int(bool(metrics["visible_enemy"])),
                    "shootable": int(bool(metrics["shootable"])),
                }
            except Exception:
                out["state"] = "unavailable"
        else:
            out["state"] = "busy"
        if self._last_plan:
            out["plan"] = {
                key: self._last_plan.get(key)
                for key in ("planner_skill", "skill", "kind", "line_id")
                if self._last_plan.get(key) is not None
            }
        if live:
            vision = self.vision_status()
            if vision.get("requests"):
                out["vision"] = {
                    "provider": vision.get("provider"),
                    "trigger": vision.get("last_trigger"),
                    "stale": bool(vision.get("stale")),
                    "confidence": vision.get("confidence"),
                }
        return out

    def _compact_probes(self, probes: dict[str, Any]) -> dict[str, Any]:
        vis = probes.get("vis") or []
        mov = probes.get("mov") or {}
        cmb = probes.get("cmb") or {}
        use = probes.get("use") or []
        return {
            "vis": [len(vis), sum(1 for row in vis if len(row) > 1 and int(row[1])), sum(1 for row in vis if len(row) > 2 and int(row[2]))],
            "mov": [mov.get("open", ""), int(mov.get("front", 0) or 0), len(mov.get("probe") or [])],
            "cmb": [int(cmb.get("shootable", 0) or 0), int(cmb.get("target", 0) or 0), int(cmb.get("dist", 0) or 0)],
            "use": len(use),
        }

    def _compact_goal_result(self, result: dict[str, Any]) -> dict[str, Any]:
        contract = result.get("committed_contract") if isinstance(result.get("committed_contract"), dict) else {}
        progress = result.get("progress_metrics") if isinstance(result.get("progress_metrics"), dict) else {}
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        state = result.get("state") if isinstance(result.get("state"), dict) else {}
        compact_progress = {
            key: progress[key]
            for key in (
                "kills_delta",
                "agent_kills",
                "health_delta",
                "ammo_delta",
                "shots_fired",
                "damage_taken",
                "map_changed",
            )
            if key in progress
        }
        compact_evidence = {}
        for label in ("start", "end"):
            point = evidence.get(label) if isinstance(evidence.get(label), dict) else {}
            compact_evidence[label] = {
                key: point[key]
                for key in ("m", "hp", "kills", "ammo")
                if key in point
            }
        return {
            "status": result.get("status"),
            "stop_reason": result.get("stop_reason"),
            "committed_contract": {
                "objective": contract.get("objective"),
                "style": contract.get("style"),
                "constraints": list(contract.get("constraints") or [])[:4],
                "max_tics": contract.get("max_tics"),
            },
            "progress_metrics": compact_progress,
            "evidence": compact_evidence,
            "state": {
                "m": state.get("m"),
                "hp": state.get("hp"),
                "wp": state.get("wp"),
                "x": state.get("x"),
                "y": state.get("y"),
            },
            "steps": result.get("steps"),
            "tics": result.get("tics"),
        }

    def _finish(
        self,
        directive: ObjectiveDirective,
        status: dict[str, str],
        baseline: dict[str, Any],
        final_metrics: dict[str, Any],
        final_state: Any,
        steps: int,
        tics: int,
        last_skill: str | None,
        transitions: list[dict[str, Any]],
        full: bool,
        run_id: str,
        *,
        include_recent: bool = False,
        fired: bool = False,
        damage_taken: bool = False,
    ) -> dict[str, Any]:
        delta = {
            "kills": final_metrics["kills"] - baseline["kills"],
            "health": final_metrics["health"] - baseline["health"],
            "bullets": final_metrics["bullets"] - baseline["bullets"],
            "shells": final_metrics["shells"] - baseline["shells"],
            "ammo": final_metrics["ammo_total"] - baseline["ammo_total"],
            "fired": int(bool(fired)),
            "damage_taken": int(bool(damage_taken)),
        }
        if directive.contract.constraints.get("kill_budget") == 0:
            agent_kills = delta["kills"] if bool(fired) or delta["ammo"] < 0 else 0
            delta["agent_kills"] = max(0, agent_kills)
            delta["infight"] = max(0, delta["kills"] - delta["agent_kills"])
        internal_status = status["status"]
        external_status = _external_status(internal_status)
        stop_reason = self._stop_reason(
            directive,
            internal_status,
            status["summary"],
            delta,
            baseline,
            final_metrics,
            tics=tics,
        )
        result = {
            "status": external_status,
            "driver_status": internal_status,
            "stop_reason": stop_reason,
            "objective": directive.objective[:60],
            "goal": directive.contract.compact(),
            "committed_contract": self._committed_contract(directive),
            "progress_metrics": self._progress_metrics(baseline, final_metrics, delta),
            "evidence": self._evidence(baseline, final_metrics, delta, fired=fired),
            "steps": int(steps),
            "tics": int(tics),
            "summary": status["summary"],
            "delta": delta,
            "state": {
                "t": final_metrics["tick"],
                "m": [final_metrics["episode"], final_metrics["map"]],
                "hp": final_metrics["health"],
                "kills": final_metrics["kills"],
                "enemies": final_metrics["enemy_count"],
                "wp": final_metrics["weapon"],
                "x": final_metrics.get("x", 0),
                "y": final_metrics.get("y", 0),
                "visible": final_metrics["visible_enemy"],
                "shootable": final_metrics["shootable"],
            },
        }
        if last_skill is not None:
            result["skill"] = last_skill
        if directive.unsupported:
            result["unsupported"] = list(directive.unsupported)
        if full:
            result["directive"] = directive.as_dict()
            result["memory"] = self._memory_summary()
        if full or include_recent:
            result["recent"] = transitions
        self._last_status = {
            "status": result["status"],
            "driver_status": internal_status,
            "stop_reason": stop_reason,
            "objective": directive.objective,
            "steps": steps,
            "tics": tics,
            "skill": last_skill,
            "summary": result["summary"],
            "committed_contract": result["committed_contract"],
        }
        if internal_status == "achieved":
            self._ranker.observe_success(last_skill)
        self._record_objective_run(result)
        self._trace.record_run(run_id, result)
        return result

    def _record_objective_run(self, result: dict[str, Any]) -> None:
        if self._memory is None:
            return
        runs = self._memory.data.setdefault("objective_runs", [])
        runs.append({"at": int(time.time()), "objective": result.get("objective"), "status": result.get("status"), "steps": result.get("steps"), "summary": result.get("summary"), "delta": result.get("delta")})
        del runs[:-50]
        self._memory.data["updated_at"] = int(time.time())
        if SAVE_OBJECTIVE_RUNS:
            self._memory.save()

    def _memory_summary(self) -> dict[str, Any]:
        if self._memory is None:
            try:
                modules = self._lazy_imports()
                self._memory = modules["AgentMemory"].load(MEMORY_PATH)
            except Exception as exc:
                return {"path": str(MEMORY_PATH), "error": str(exc)}
        out = dict(self._memory.summary())
        out["objective_runs"] = len(self._memory.data.get("objective_runs", []))
        return out
