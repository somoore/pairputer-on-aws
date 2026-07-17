#!/usr/bin/env python3
"""Validate Agent DOOM free-form goals against the deterministic contract compiler."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CAPSULE_ROOT = Path(__file__).resolve().parent / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))

from goal_contract import GoalContract, compile_goal_contract  # noqa: E402
from eval_runner import RESULT_MARKER, parse_marked_json  # noqa: E402

DEFAULT_CORPUS = Path(__file__).resolve().parent / "eval-goals" / "free_form_goal_fuzz.json"


def compact_constraints(contract: GoalContract) -> set[str]:
    constraints: set[str] = set()
    raw = contract.constraints
    if raw.get("kill_budget") == 0:
        constraints.add("no_kills")
    if raw.get("ammo_budget") == 0:
        constraints.add("no_ammo")
    if raw.get("weapon_policy") == "fist_only":
        constraints.add("fist_only")
    if raw.get("avoid_combat"):
        constraints.add("avoid_combat")
    if raw.get("preserve_health"):
        constraints.add("avoid_damage")
    return constraints


def load_cases(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text())
    cases = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(cases, list):
        raise SystemExit("goal fuzz corpus must be a JSON array or an object with cases")
    return [case for case in cases if isinstance(case, dict)]


def _case_id(case: dict[str, Any]) -> str:
    return str(case.get("id") or str(case.get("goal") or "")[:48] or "case")[:80]


def _actual_from_contract(contract: GoalContract) -> dict[str, Any]:
    return {
        "objective": contract.objective,
        "style": contract.style,
        "constraints": sorted(compact_constraints(contract)),
    }


def _score_actual_contract(
    case: dict[str, Any],
    actual: dict[str, Any],
    *,
    source: str,
    raw_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    constraints = {str(item) for item in (actual.get("constraints") or []) if str(item)}
    failures: list[str] = []
    objective = expected.get("objective")
    if objective and actual.get("objective") != objective:
        failures.append(f"objective:{actual.get('objective')}!={objective}")
    style = expected.get("style")
    if style and actual.get("style") != style:
        failures.append(f"style:{actual.get('style')}!={style}")
    for constraint in expected.get("constraints") or []:
        if str(constraint) not in constraints:
            failures.append(f"missing_constraint:{constraint}")
    for constraint in expected.get("absent_constraints") or []:
        if str(constraint) in constraints:
            failures.append(f"unexpected_constraint:{constraint}")
    return {
        "id": _case_id(case),
        "goal": str(case.get("goal") or ""),
        "source": source,
        "ok": not failures,
        "failures": failures,
        "actual": {
            "objective": actual.get("objective"),
            "style": actual.get("style"),
            "constraints": sorted(constraints),
        },
        "expected": expected,
        "driver_status": (raw_result or {}).get("status", ""),
        "stop_reason": (raw_result or {}).get("stop_reason", ""),
    }


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    goal = str(case.get("goal") or "")
    contract = compile_goal_contract(goal, case.get("payload") if isinstance(case.get("payload"), dict) else None)
    return _score_actual_contract(case, _actual_from_contract(contract), source="local")


def evaluate_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [evaluate_case(case) for case in cases]


class TmuxGoalFuzzClient:
    """Sample free-form goal contracts through a live Codex MCP session."""

    def __init__(self, *, target: str, timeout_s: float = 120.0, max_tics: int = 1) -> None:
        if not target:
            raise SystemExit("--tmux-target is required for tmux-codex mode")
        self.target = target
        self.timeout_s = float(timeout_s)
        self.max_tics = max(1, int(max_tics))

    def run_case(self, case: dict[str, Any]) -> dict[str, Any]:
        case_id = _case_id(case)
        marker_id = f"{case_id}-{os.getpid()}-{time.time_ns()}"
        goal = str(case.get("goal") or "")
        args = {"goal": goal, "max_tics": self.max_tics}
        prompt = (
            "Run this Agent Doom free-form goal contract fuzz case through pairputer MCP. "
            "Act as the Commander: call agent_doom__drive_goal exactly once with args that preserve "
            "the user's intent and constraints. Do not call low-level Doom action tools. "
            "Do not retry. After the tool returns, print one final line starting with "
            f"{RESULT_MARKER} {marker_id} followed by compact JSON copied from drive_goal with only "
            "status, stop_reason, and committed_contract. "
            f"Args: {json.dumps(args, sort_keys=True)}"
        )
        self._send_line(prompt)
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            captured = self._capture()
            result = parse_marked_json(captured.stdout, case_id=marker_id)
            if result:
                contract = result.get("committed_contract") if isinstance(result.get("committed_contract"), dict) else {}
                row = _score_actual_contract(case, contract, source="tmux-codex", raw_result=result)
                row["marker_id"] = marker_id
                row["response_bytes"] = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                return row
            time.sleep(1.0)
        return {
            "id": case_id,
            "goal": goal,
            "source": "tmux-codex",
            "ok": False,
            "failures": ["tmux_codex_timeout"],
            "actual": {},
            "expected": case.get("expected") if isinstance(case.get("expected"), dict) else {},
            "driver_status": "failed",
            "stop_reason": "tmux_codex_timeout",
            "marker_id": marker_id,
        }

    def _send_line(self, text: str) -> None:
        buffer_name = f"agent-doom-goal-fuzz-{os.getpid()}"
        subprocess.run(["tmux", "load-buffer", "-b", buffer_name, "-"], input=text, text=True, check=True)
        subprocess.run(["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", self.target], check=True)
        time.sleep(0.2)
        subprocess.run(["tmux", "send-keys", "-t", self.target, "Enter"], check=True)

    def _capture(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", "capture-pane", "-J", "-p", "-S", "-3000", "-t", self.target], capture_output=True, text=True)


def filter_cases(cases: list[dict[str, Any]], *, ids: str = "", limit: int = 0) -> list[dict[str, Any]]:
    selected = list(cases)
    wanted = {item.strip() for item in ids.split(",") if item.strip()}
    if wanted:
        selected = [case for case in selected if _case_id(case) in wanted]
    if limit > 0:
        selected = selected[:limit]
    return selected


def evaluate_cases_tmux(cases: list[dict[str, Any]], client: TmuxGoalFuzzClient) -> list[dict[str, Any]]:
    return [client.run_case(case) for case in cases]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--jsonl", type=Path)
    parser.add_argument("--mode", choices=("local", "tmux-codex"), default="local")
    parser.add_argument("--ids", default="", help="Comma-separated case ids to run.")
    parser.add_argument("--limit", type=int, default=0, help="Run only the first N selected cases.")
    parser.add_argument("--tmux-target", default="", help="tmux target pane/session for tmux-codex mode.")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--max-tics", type=int, default=1, help="Tic budget for each live drive_goal contract sample.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    cases = filter_cases(load_cases(args.corpus), ids=args.ids, limit=max(0, int(args.limit or 0)))
    if args.mode == "tmux-codex":
        rows = evaluate_cases_tmux(cases, TmuxGoalFuzzClient(target=args.tmux_target, timeout_s=args.timeout_s, max_tics=args.max_tics))
    else:
        rows = evaluate_cases(cases)
    text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if args.jsonl:
        args.jsonl.parent.mkdir(parents=True, exist_ok=True)
        args.jsonl.write_text(text)
    else:
        print(text, end="")
    failed = [row for row in rows if not row["ok"]]
    if failed:
        print(f"{len(failed)}/{len(rows)} goal fuzz cases failed", file=sys.stderr)
        return 1
    print(f"{len(rows)} goal fuzz cases passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
