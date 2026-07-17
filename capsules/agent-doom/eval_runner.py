#!/usr/bin/env python3
"""Agent DOOM product eval runner.

The runner scores Commander-to-capsule behavior, not isolated Python helpers.
Use direct mode for fast local iteration, mcp-command mode to wrap a real MCP
client, and tmux-codex mode for final Codex-in-the-loop acceptance.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BRIDGE_URL = os.environ.get("AGENT_DOOM_BRIDGE_URL", "http://127.0.0.1:6905")
DEFAULT_INPUT_WS_URL = os.environ.get("AGENT_DOOM_INPUT_WS_URL", "ws://127.0.0.1:6904")
RESPONSE_BUDGET_BYTES = int(os.environ.get("AGENT_DOOM_RESPONSE_BUDGET_BYTES", "500"))
RESULT_MARKER = "EVAL_RESULT_JSON"
E1M1_SPAWN_X_FP = 69206016
E1M1_SPAWN_Y_FP = -236978176
E1M1_SPAWN_TOLERANCE_FP = 8 * 65536


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    goal: str
    objective: str | None = None
    constraints: tuple[str, ...] = ()
    max_tics: int = 1200
    reset_episode: bool = False
    snapshot: str | None = None
    episode: int = 1
    map: int = 1
    skill: int = 2
    seed: int = 0
    human_interrupt_after_s: float = 0.0
    tags: tuple[str, ...] = ()
    # 1.0 = deterministic case: every run must pass (the old semantics).
    # < 1.0 = probabilistic case: the case is judged by its PASS RATE over
    # repeats, not per-run. Needs >= 5 runs to gate (fewer is informational).
    min_success_rate: float = 1.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvalCase":
        return cls(
            case_id=str(raw.get("id") or raw.get("case_id") or "case")[:80],
            goal=str(raw.get("goal") or "").strip(),
            objective=str(raw.get("objective")).strip() if raw.get("objective") else None,
            constraints=tuple(str(item) for item in (raw.get("constraints") or []) if str(item)),
            max_tics=max(1, int(raw.get("max_tics") or 1200)),
            reset_episode=bool(raw.get("reset_episode", False)),
            snapshot=str(raw.get("snapshot")).strip() if raw.get("snapshot") else None,
            episode=max(1, int(raw.get("episode") or 1)),
            map=max(1, int(raw.get("map") or 1)),
            skill=max(0, int(raw.get("skill") if raw.get("skill") is not None else 2)),
            seed=int(raw.get("seed") or 0),
            human_interrupt_after_s=max(0.0, float(raw.get("human_interrupt_after_s") or 0.0)),
            tags=tuple(str(item) for item in (raw.get("tags") or []) if str(item)),
            min_success_rate=min(1.0, max(0.0, float(raw.get("min_success_rate") if raw.get("min_success_rate") is not None else 1.0))),
        )

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"goal": self.goal, "max_tics": self.max_tics}
        if self.objective:
            out["objective"] = self.objective
        if self.constraints:
            out["constraints"] = list(self.constraints)
        return out


def _runner_goal_wall_budget(timeout_s: float) -> int:
    return max(1, int(max(1.0, float(timeout_s) - 5.0)))


def _case_payload_with_wall_budget(case: EvalCase, timeout_s: float) -> dict[str, Any]:
    payload = case.payload()
    payload.setdefault("max_wall_s", _runner_goal_wall_budget(timeout_s))
    payload["ignore_human_interrupt"] = case.human_interrupt_after_s <= 0
    return payload


DEFAULT_CASES = [
    EvalCase("e1m1-beat-level", "beat the level", objective="complete_level", max_tics=4200, reset_episode=True),
    EvalCase("e1m1-no-kill-exit", "race to the exit without killing anyone", objective="exit_level", constraints=("no_kills",), max_tics=2800, reset_episode=True),
    EvalCase("e1m1-punch-enemy", "find an enemy and punch it, no ammo", objective="kill_enemy", constraints=("no_ammo", "fist_only"), max_tics=1600, reset_episode=True),
    # 2400 tics: the zero-damage strategy is camp-and-ambush (sonar-ping lure +
    # threshold executions) — deliberately slow; 1600 killed healthy 1-kill runs.
    # Probabilistic case: hitscan RNG makes single runs a coin flip. Baseline
    # measured 30% over 30 runs (2026-07-08, allowance 9); 0.15 flags a true
    # regression to ~10% while tolerating binomial noise. Run with --repeat >= 10.
    EvalCase("e1m1-clear-room", "clear this room safely", objective="clear_area", constraints=("avoid_damage",), max_tics=2400, reset_episode=True, min_success_rate=0.15),
    EvalCase("human-interrupt", "beat the level", objective="complete_level", max_tics=4200, reset_episode=True, human_interrupt_after_s=2.0, tags=("handoff",)),
]


class DirectBridgeClient:
    def __init__(
        self,
        *,
        bridge_url: str = DEFAULT_BRIDGE_URL,
        input_ws_url: str = DEFAULT_INPUT_WS_URL,
        timeout_s: float = 180.0,
        trace_recent: int = 0,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self.input_ws_url = input_ws_url
        self.timeout_s = float(timeout_s)
        self.trace_recent = max(0, int(trace_recent or 0))
        self.poll_interval_s = max(0.2, float(poll_interval_s))

    def _goal_payload(self, case: EvalCase) -> dict[str, Any]:
        payload = _case_payload_with_wall_budget(case, self.timeout_s)
        if self.trace_recent:
            payload["trace_recent"] = self.trace_recent
        return payload

    def run_case(self, case: EvalCase) -> dict[str, Any]:
        if case.reset_episode:
            self._reset_episode(case)
        if case.snapshot:
            self._post("/snapshot/load", {"slot": case.snapshot})
        result_holder: dict[str, Any] = {}
        error_holder: dict[str, str] = {}

        def run_goal() -> None:
            try:
                result_holder["result"] = self._post("/brain/drive_goal", self._goal_payload(case), timeout_s=self.timeout_s)
            except Exception as exc:  # returned as eval failure, not process crash
                error_holder["error"] = f"{type(exc).__name__}: {exc}"[:240]

        started = time.perf_counter()
        interrupt_sent_at = 0.0
        status_trace: list[dict[str, Any]] = []
        last_status: dict[str, Any] = {}
        running_seen = False
        terminal_seen = False
        terminal_seen_ms = 0
        thread = threading.Thread(target=run_goal, daemon=True)
        thread.start()
        deadline = time.monotonic() + self.timeout_s + 1.0
        while thread.is_alive() and time.monotonic() < deadline:
            if case.human_interrupt_after_s > 0 and not interrupt_sent_at and (time.perf_counter() - started) >= case.human_interrupt_after_s:
                interrupt_sent_at = time.perf_counter()
                self._send_human_key()
            polled_status = self._get("/brain/tactical_status")
            if polled_status:
                last_status = polled_status
                compact_status = _compact_tactical_status(polled_status)
                if compact_status and (not status_trace or status_trace[-1] != compact_status):
                    status_trace.append(compact_status)
                label = str(polled_status.get("status") or "").strip().lower()
                if label in {"running", "starting", "busy"}:
                    running_seen = True
                current_terminal = _is_terminal_tactical_status(polled_status)
                current_run_terminal = label != "idle" and (running_seen or len(status_trace) >= 2)
                if not terminal_seen and current_terminal and current_run_terminal:
                    terminal_seen = True
                    terminal_seen_ms = int((time.perf_counter() - started) * 1000)
            thread.join(timeout=self.poll_interval_s)
        if thread.is_alive():
            if case.human_interrupt_after_s > 0:
                error_holder["error"] = "drive_goal did not return after human interrupt"
            else:
                error_holder["error"] = "drive_goal did not return before runner timeout"
            self._wait_after_timeout(thread, case)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        result = result_holder.get("result") or {"status": "failed", "stop_reason": "runner_error", "error": error_holder.get("error", "missing result")}
        response_bytes = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        if self.trace_recent:
            result["_debug_response_bytes"] = response_bytes
            result["_bridge_response_bytes"] = len(
                json.dumps(_compact_budget_result(result), sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
        else:
            result["_bridge_response_bytes"] = response_bytes
        status = self._get("/brain/tactical_status")
        if status:
            last_status = status
            compact_status = _compact_tactical_status(status)
            if compact_status and (not status_trace or status_trace[-1] != compact_status):
                status_trace.append(compact_status)
            if not terminal_seen and _is_terminal_tactical_status(status) and str(status.get("status") or "").strip().lower() != "idle":
                terminal_seen = True
                terminal_seen_ms = int((time.perf_counter() - started) * 1000)
        if interrupt_sent_at and result.get("stop_reason") == "human_interrupt":
            result["human_interrupt_ms"] = max(0, int((time.perf_counter() - interrupt_sent_at) * 1000))
        result["_eval_elapsed_ms"] = elapsed_ms
        result["_tactical_status"] = last_status
        result["_tactical_poll_count"] = len(status_trace)
        result["_tactical_stop_seen"] = terminal_seen
        result["_tactical_stop_ms"] = terminal_seen_ms
        result["_tactical_status_transitions"] = status_trace[-12:]
        return result

    def _wait_after_timeout(self, thread: threading.Thread, case: EvalCase) -> None:
        cleanup_deadline = time.monotonic() + max(5.0, min(20.0, self.timeout_s * 0.2))
        while thread.is_alive() and time.monotonic() < cleanup_deadline:
            status = self._get("/brain/tactical_status")
            label = str(status.get("status") or "").strip().lower()
            if label in {"success", "failed", "interrupted", "budget_exhausted", "idle"}:
                break
            thread.join(timeout=self.poll_interval_s)
        if not thread.is_alive():
            return
        try:
            self._reset_episode(case)
        except Exception:
            pass

    def _reset_episode(self, case: EvalCase) -> None:
        self._post("/reset_episode", {"skill": case.skill, "episode": case.episode, "map": case.map, "seed": case.seed})
        deadline = time.monotonic() + 8.0
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_state = self._post("/observe", {}, timeout_s=5.0)
            if _is_clean_episode_start(last_state, episode=case.episode, map_id=case.map):
                return
            time.sleep(0.08)
        raise RuntimeError(
            f"reset_episode did not reach clean E{case.episode}M{case.map} start: {_compact_observe_state(last_state)}"
        )

    def _post(self, path: str, payload: dict[str, Any], *, timeout_s: float | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.bridge_url + path, data=data, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(timeout_s or 20.0)) as resp:
            return json.loads(resp.read() or b"{}")

    def _get(self, path: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(self.bridge_url + path, timeout=5.0) as resp:
                data = json.loads(resp.read() or b"{}")
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _send_human_key(self) -> None:
        parsed = urllib.parse.urlparse(self.input_ws_url)
        if parsed.scheme not in {"ws", "wss"}:
            raise RuntimeError(f"unsupported websocket scheme: {parsed.scheme}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        sock: socket.socket | ssl.SSLSocket | None = None
        try:
            raw = socket.create_connection((host, port), timeout=3.0)
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if parsed.scheme == "wss" else raw
            sock.settimeout(3.0)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            sock.sendall(req.encode("ascii"))
            response = self._recv_ws_handshake(sock)
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
            if not response.startswith("HTTP/1.1 101") or accept not in response:
                first = response.splitlines()[0] if response else "empty response"
                raise RuntimeError(f"websocket handshake failed: {first}")
            self._send_ws_text(sock, json.dumps({"t": "k", "key": "w", "down": True}))
            time.sleep(0.03)
            self._send_ws_text(sock, json.dumps({"t": "k", "key": "w", "down": False}))
            self._send_ws_close(sock)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _recv_ws_handshake(sock: socket.socket | ssl.SSLSocket) -> str:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 16384:
                break
        return bytes(data).decode("iso-8859-1", errors="replace")

    @staticmethod
    def _send_ws_text(sock: socket.socket | ssl.SSLSocket, text: str) -> None:
        DirectBridgeClient._send_ws_frame(sock, opcode=0x1, payload=text.encode("utf-8"))

    @staticmethod
    def _send_ws_close(sock: socket.socket | ssl.SSLSocket) -> None:
        DirectBridgeClient._send_ws_frame(sock, opcode=0x8, payload=b"")

    @staticmethod
    def _send_ws_frame(sock: socket.socket | ssl.SSLSocket, *, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + size.to_bytes(2, "big")
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + size.to_bytes(8, "big")
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        sock.sendall(header + mask + masked)


class McpCommandClient:
    """Wrap an external MCP client command that reads case JSON on stdin."""

    def __init__(self, command: list[str], *, timeout_s: float = 180.0) -> None:
        if not command:
            raise SystemExit("--command is required for mcp-command mode")
        self.command = command
        self.timeout_s = float(timeout_s)

    def run_case(self, case: EvalCase) -> dict[str, Any]:
        payload = {"case": case_to_dict(case), "tool": "agent_doom__drive_goal", "args": _case_payload_with_wall_budget(case, self.timeout_s)}
        proc = subprocess.run(
            self.command,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=self.timeout_s,
        )
        if proc.returncode != 0:
            return {"status": "failed", "stop_reason": "mcp_command_failed", "error": proc.stderr[-240:]}
        return parse_marked_json(proc.stdout) or json.loads(proc.stdout or "{}")


class TmuxCodexClient:
    """Drive a user-provided tmux pane running Codex/Claude/etc. and parse a marked result."""

    def __init__(
        self,
        *,
        target: str,
        timeout_s: float = 240.0,
        bridge_url: str = DEFAULT_BRIDGE_URL,
        open_command: str = "",
        open_timeout_s: float = 20.0,
        poll_interval_s: float = 1.0,
        require_summary: bool = True,
    ) -> None:
        if not target:
            raise SystemExit("--tmux-target is required for tmux-codex mode")
        self.target = target
        self.timeout_s = float(timeout_s)
        self.bridge_url = bridge_url.rstrip("/")
        self.open_command = str(open_command or "").strip()
        self.open_timeout_s = float(open_timeout_s)
        self.poll_interval_s = max(0.2, float(poll_interval_s))
        self.require_summary = bool(require_summary)

    def run_case(self, case: EvalCase) -> dict[str, Any]:
        started = time.perf_counter()
        if self.open_command:
            self._send_line(self.open_command)
            self._wait_for_bridge()
        if case.reset_episode:
            self._reset_episode(case)
        args = _case_payload_with_wall_budget(case, self.timeout_s)
        prompt = (
            "Run this Agent Doom eval case through pairputer MCP. "
            "Call agent_doom__drive_goal exactly once with the provided args, then call "
            "agent_doom__tactical_status exactly once after it returns. Summarize the committed_contract "
            "objective, constraints, and final stop_reason in one terse line beginning with 'Summary:'. "
            "Then print one final line starting with "
            f"{RESULT_MARKER} {case.case_id} followed by compact JSON copied from drive_goal with only "
            "status, stop_reason, committed_contract, progress_metrics, state, steps, and tics. "
            "Keep JSON lines under 80 characters and break only between fields. "
            "Do not include evidence, recent transitions, markdown, or extra prose after the JSON. "
            f"Args: {json.dumps(args, sort_keys=True)}"
        )
        self._send_line(prompt)
        deadline = time.time() + self.timeout_s
        last_status: dict[str, Any] = {}
        status_trace: list[dict[str, Any]] = []
        running_seen = False
        terminal_seen = False
        terminal_seen_ms = 0
        while time.time() < deadline:
            polled_status = self._get_json("/brain/tactical_status")
            if polled_status:
                last_status = polled_status
                compact_status = _compact_tactical_status(polled_status)
                if compact_status and (not status_trace or status_trace[-1] != compact_status):
                    status_trace.append(compact_status)
                label = str(polled_status.get("status") or "").strip().lower()
                if label in {"running", "starting", "busy"}:
                    running_seen = True
                current_terminal = _is_terminal_tactical_status(polled_status)
                current_run_terminal = label != "idle" and (running_seen or len(status_trace) >= 2)
                if not terminal_seen and current_terminal and current_run_terminal:
                    terminal_seen = True
                    terminal_seen_ms = int((time.perf_counter() - started) * 1000)
            captured = self._capture()
            result = parse_marked_json(captured.stdout, case_id=case.case_id)
            if result:
                result["_driver_response_bytes"] = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                result["_tactical_status"] = last_status
                result["_tactical_poll_count"] = len(status_trace)
                result["_tactical_stop_seen"] = terminal_seen
                result["_tactical_stop_ms"] = terminal_seen_ms
                result["_tactical_status_transitions"] = status_trace[-12:]
                result["_eval_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
                result["_tmux_summary_ok"] = tmux_summary_ok(captured.stdout, result)
                if self.require_summary and not result["_tmux_summary_ok"]:
                    result.setdefault("status", "failed")
                    result["driver_status"] = "tmux_summary_missing"
                return result
            time.sleep(self.poll_interval_s)
        return {
            "status": "failed",
            "stop_reason": "tmux_codex_timeout",
            "_eval_elapsed_ms": int((time.perf_counter() - started) * 1000),
            "_tactical_status": last_status,
            "_tactical_poll_count": len(status_trace),
            "_tactical_stop_seen": terminal_seen,
            "_tactical_stop_ms": terminal_seen_ms,
            "_tactical_status_transitions": status_trace[-12:],
        }

    def _send_line(self, text: str) -> None:
        buffer_name = f"agent-doom-eval-{os.getpid()}"
        subprocess.run(["tmux", "load-buffer", "-b", buffer_name, "-"], input=text, text=True, check=True)
        subprocess.run(["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", self.target], check=True)
        time.sleep(0.2)
        subprocess.run(["tmux", "send-keys", "-t", self.target, "Enter"], check=True)

    def _capture(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", "capture-pane", "-J", "-p", "-S", "-3000", "-t", self.target], capture_output=True, text=True)

    def _wait_for_bridge(self) -> None:
        deadline = time.monotonic() + max(0.0, self.open_timeout_s)
        while time.monotonic() < deadline:
            if self._get_json("/brain/tactical_status") or self._get_json("/health"):
                return
            time.sleep(0.5)

    def _reset_episode(self, case: EvalCase) -> None:
        self._post_json("/reset_episode", {"skill": case.skill, "episode": case.episode, "map": case.map, "seed": case.seed})
        deadline = time.monotonic() + 8.0
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_state = self._post_json("/observe", {}, timeout_s=5.0)
            if _is_clean_episode_start(last_state, episode=case.episode, map_id=case.map):
                return
            time.sleep(0.08)
        raise RuntimeError(
            f"reset_episode did not reach clean E{case.episode}M{case.map} start: {_compact_observe_state(last_state)}"
        )

    def _post_json(self, path: str, payload: dict[str, Any], *, timeout_s: float | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.bridge_url + path, data=data, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(timeout_s or 20.0)) as resp:
            result = json.loads(resp.read() or b"{}")
            return result if isinstance(result, dict) else {}

    def _get_json(self, path: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(self.bridge_url + path, timeout=2.0) as resp:
                data = json.loads(resp.read() or b"{}")
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def parse_marked_json(text: str, *, case_id: str | None = None) -> dict[str, Any] | None:
    body = str(text or "")
    marker = RESULT_MARKER if case_id is None else f"{RESULT_MARKER} {case_id}"
    positions: list[tuple[int, int]] = []
    for match in re.finditer(re.escape(marker), body):
        end = match.end()
        if case_id is not None and end < len(body) and not (body[end].isspace() or body[end] == "{"):
            continue
        positions.append((match.start(), end))
    for _pos, end in reversed(positions):
        start = body.find("{", end)
        if start < 0:
            continue
        snippet = _balanced_json_object(body, start)
        if not snippet:
            continue
        try:
            parsed = json.loads(snippet)
        except json.JSONDecodeError:
            try:
                parsed = json.loads(re.sub(r"\n\s*", "", snippet))
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict) and ("status" in parsed or "stop_reason" in parsed):
            return parsed
    return None


def _balanced_json_object(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _compact_tactical_status(status: dict[str, Any]) -> dict[str, Any]:
    state = status.get("state") if isinstance(status.get("state"), dict) else {}
    plan = status.get("plan") if isinstance(status.get("plan"), dict) else {}
    out = {
        "status": str(status.get("status") or "")[:16],
        "phase": str(status.get("phase") or "")[:40],
        "objective": str(status.get("objective") or "")[:48],
        "stop_reason": str(status.get("stop_reason") or "")[:40],
        "steps": _safe_int(status.get("steps")),
        "tics": _safe_int(status.get("tics")),
    }
    if state:
        out["hp"] = _safe_int(state.get("hp"))
        out["map"] = state.get("m")
    if plan:
        out["plan"] = str(plan.get("kind") or plan.get("planner_skill") or plan.get("skill") or "")[:48]
        out["line"] = _safe_int(plan.get("line_id"), -1)
    return {key: value for key, value in out.items() if value not in ("", None)}


def _is_terminal_tactical_status(status: dict[str, Any]) -> bool:
    label = str(status.get("status") or "").strip().lower()
    if label in {"success", "failed", "interrupted", "stopped", "idle"}:
        return True
    if label and label not in {"running", "starting", "busy"} and status.get("stop_reason"):
        return True
    return False


def tmux_summary_ok(text: str, result: dict[str, Any]) -> bool:
    contract = result.get("committed_contract") if isinstance(result.get("committed_contract"), dict) else {}
    objective = str(contract.get("objective") or "").strip().lower()
    constraints = [str(item).strip().lower() for item in (contract.get("constraints") or []) if str(item).strip()]
    stop_reason = str(result.get("stop_reason") or "").strip().lower()
    if not objective or not stop_reason:
        return False
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        lower = " ".join(lines[index : index + 3]).lower()
        if "summary:" not in lower:
            continue
        if objective not in lower or stop_reason not in lower:
            continue
        if all(item in lower for item in constraints):
            return True
    return False


def case_to_dict(case: EvalCase) -> dict[str, Any]:
    return {
        "id": case.case_id,
        "goal": case.goal,
        "objective": case.objective,
        "constraints": list(case.constraints),
        "max_tics": case.max_tics,
        "reset_episode": case.reset_episode,
        "snapshot": case.snapshot,
        "episode": case.episode,
        "map": case.map,
        "skill": case.skill,
        "seed": case.seed,
        "human_interrupt_after_s": case.human_interrupt_after_s,
        "tags": list(case.tags),
    }


def load_cases(path: Path | None) -> list[EvalCase]:
    if path is None:
        return list(DEFAULT_CASES)
    raw = json.loads(path.read_text())
    items = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise SystemExit("case file must be a JSON array or object with cases")
    return [EvalCase.from_dict(item) for item in items if isinstance(item, dict)]


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _is_clean_episode_start(state: dict[str, Any], *, episode: int = 1, map_id: int = 1) -> bool:
    if not isinstance(state, dict):
        return False
    observed_map = state.get("m") or []
    player = state.get("p") if isinstance(state.get("p"), dict) else {}
    x = _safe_int(player.get("x"))
    y = _safe_int(player.get("y"))
    if not (
        len(observed_map) >= 2
        and int(observed_map[0] or 0) == int(episode)
        and int(observed_map[1] or 0) == int(map_id)
        and _safe_int(player.get("hp")) >= 100
        and _safe_int(state.get("k")) == 0
        and _safe_int(player.get("bul")) >= 50
    ):
        return False
    if int(episode) == 1 and int(map_id) == 1:
        return abs(x - E1M1_SPAWN_X_FP) <= E1M1_SPAWN_TOLERANCE_FP and abs(y - E1M1_SPAWN_Y_FP) <= E1M1_SPAWN_TOLERANCE_FP
    return True


def _compact_observe_state(state: dict[str, Any]) -> dict[str, Any]:
    player = state.get("p") if isinstance(state.get("p"), dict) else {}
    return {
        "m": state.get("m"),
        "hp": player.get("hp"),
        "kills": state.get("k"),
        "bul": player.get("bul"),
        "x": player.get("x"),
        "y": player.get("y"),
        "t": state.get("t"),
    }


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _case_constraints(case: EvalCase, result: dict[str, Any]) -> set[str]:
    constraints = set(case.constraints)
    committed = result.get("committed_contract") if isinstance(result.get("committed_contract"), dict) else {}
    for item in committed.get("constraints") or []:
        if item:
            constraints.add(str(item))
    return constraints


def _trace_breadcrumbs(result: dict[str, Any]) -> dict[str, Any]:
    recent = result.get("recent")
    if not isinstance(recent, list):
        recent = []
    last_plan: dict[str, Any] = {}
    last_pos: dict[str, Any] = {}
    trail: list[str] = []
    for item in recent[-8:]:
        if not isinstance(item, dict):
            continue
        plan = item.get("plan") if isinstance(item.get("plan"), dict) else {}
        pos = item.get("pos") if isinstance(item.get("pos"), dict) else {}
        if plan:
            last_plan = plan
            action = str(plan.get("action") or plan.get("skill") or item.get("primitive") or item.get("skill") or "")[:48]
            line = plan.get("line")
            trail.append(f"{action}@{line}" if line is not None else action)
        if pos:
            last_pos = pos
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    return {
        "final_health": _safe_int(last_pos.get("hp"), _safe_int(state.get("hp"), 0)),
        "last_plan_action": str(last_plan.get("action") or "")[:64],
        "last_plan_skill": str(last_plan.get("skill") or "")[:64],
        "last_plan_line": _safe_int(last_plan.get("line"), -1),
        "last_plan_sector": _safe_int(last_plan.get("sector"), -1),
        "recent_plan_trail": " > ".join(trail)[-240:],
    }


def _compact_budget_result(result: dict[str, Any]) -> dict[str, Any]:
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
    compact_evidence: dict[str, Any] = {}
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


def _eval_failures(case: EvalCase, result: dict[str, Any], row: dict[str, Any]) -> list[str]:
    constraints = _case_constraints(case, result)
    delta = result.get("delta") if isinstance(result.get("delta"), dict) else {}
    failures: list[str] = []
    agent_kills = _safe_int(delta.get("agent_kills"), _safe_int(row.get("agent_kills"), row["kills_delta"]))
    final_weapon = row.get("final_weapon", -1)
    if "no_kills" in constraints and agent_kills > 0:
        failures.append("no_kills")
    if "no_ammo" in constraints and row["ammo_delta"] < 0:
        failures.append("no_ammo")
    if "fist_only" in constraints:
        if final_weapon not in (-1, 0):
            failures.append("fist_only_weapon")
        if row["ammo_delta"] < 0:
            failures.append("fist_only_ammo")
    # 9hp allowance = one median zombieman bullet (3×d5): survive one graze,
    # two bullets is a fail. Keep in sync with PRESERVE_HEALTH_DAMAGE_ALLOWANCE
    # in contract_eval.py (tests/test_health_allowance_sync.py enforces this).
    if "avoid_damage" in constraints and row["health_delta"] < -9:
        failures.append("avoid_damage")
    if case.human_interrupt_after_s > 0 and result.get("stop_reason") != "human_interrupt":
        failures.append("human_interrupt_missing")
    if RESPONSE_BUDGET_BYTES > 0 and row["response_bytes"] > RESPONSE_BUDGET_BYTES:
        failures.append(f"response_over_{RESPONSE_BUDGET_BYTES}b")
    if result.get("_tmux_summary_ok") is False:
        failures.append("tmux_summary_missing")
    if "_tmux_summary_ok" in result:
        if _safe_int(result.get("_tactical_poll_count")) <= 0:
            failures.append("tmux_tactical_poll_missing")
        elif result.get("_tactical_stop_seen") is False:
            failures.append("tmux_tactical_terminal_missing")
    return failures


def score_case(case: EvalCase, result: dict[str, Any], *, commit: str) -> dict[str, Any]:
    progress = result.get("progress_metrics") if isinstance(result.get("progress_metrics"), dict) else {}
    delta = result.get("delta") if isinstance(result.get("delta"), dict) else {}
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    tactical = result.get("_tactical_status") if isinstance(result.get("_tactical_status"), dict) else {}
    breadcrumbs = _trace_breadcrumbs(result)
    response_bytes = _safe_int(
        result.get("_bridge_response_bytes"),
        _safe_int(
            result.get("_driver_response_bytes"),
            len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")),
        ),
    )
    row = {
        "commit": commit,
        "case_id": case.case_id,
        "goal": case.goal,
        "objective": case.objective or "",
        "constraints": ",".join(case.constraints),
        "episode": case.episode,
        "map": case.map,
        "seed": case.seed,
        "status": result.get("status", "failed"),
        "raw_status": result.get("status", "failed"),
        "driver_status": result.get("driver_status", ""),
        "stop_reason": result.get("stop_reason", ""),
        "steps": int(result.get("steps", 0) or 0),
        "tics": int(result.get("tics", 0) or 0),
        "kills_delta": int(progress.get("kills_delta", delta.get("kills", 0)) or 0),
        "agent_kills": _safe_int(
            delta.get("agent_kills"),
            _safe_int(progress.get("agent_kills"), int(progress.get("kills_delta", delta.get("kills", 0)) or 0)),
        ),
        "shots_fired": int(progress.get("shots_fired", int(bool(delta.get("fired", 0)))) or 0),
        "ammo_delta": int(progress.get("ammo_delta", delta.get("ammo", 0)) or 0),
        "health_delta": int(progress.get("health_delta", delta.get("health", 0)) or 0),
        "final_weapon": _safe_int(state.get("wp"), -1),
        "response_bytes": response_bytes,
        "debug_response_bytes": _safe_int(result.get("_debug_response_bytes"), 0),
        "response_budget_bytes": RESPONSE_BUDGET_BYTES,
        "micromanagement_count": 0,
        "human_interrupt_ms": int(result.get("human_interrupt_ms", 0) or 0),
        "elapsed_ms": int(result.get("_eval_elapsed_ms", 0) or 0),
        "final_x": _safe_int(state.get("x"), 0),
        "final_y": _safe_int(state.get("y"), 0),
        "final_health": breadcrumbs["final_health"],
        "last_plan_action": breadcrumbs["last_plan_action"],
        "last_plan_skill": breadcrumbs["last_plan_skill"],
        "last_plan_line": breadcrumbs["last_plan_line"],
        "last_plan_sector": breadcrumbs["last_plan_sector"],
        "recent_plan_trail": breadcrumbs["recent_plan_trail"],
        "tactical_status": str(tactical.get("status") or "")[:40],
        "tactical_phase": str(tactical.get("phase") or "")[:64],
        "tactical_poll_count": _safe_int(result.get("_tactical_poll_count"), 0),
        "tactical_stop_seen": result.get("_tactical_stop_seen", ""),
        "tactical_stop_ms": _safe_int(result.get("_tactical_stop_ms"), 0),
        "tactical_transitions": json.dumps(result.get("_tactical_status_transitions") or [], sort_keys=True, separators=(",", ":"))[:800],
        "tmux_summary_ok": result.get("_tmux_summary_ok", ""),
    }
    if case.human_interrupt_after_s > 0 and result.get("stop_reason") == "human_interrupt":
        row["status"] = "success"
    failures = _eval_failures(case, result, row)
    row["eval_failures"] = ",".join(failures)
    if failures:
        row["status"] = "failed"
        row["stop_reason"] = "constraint_violation:" + ",".join(failures)
    return row


def write_outputs(rows: list[dict[str, Any]], *, jsonl: Path | None, csv_path: Path | None) -> None:
    if jsonl:
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        jsonl.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = sorted({key for row in rows for key in row.keys()}) if rows else ["case_id", "status"]
        with csv_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def load_existing_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            rows.append(raw)
    return rows


def row_key(row: dict[str, Any]) -> tuple[int, str]:
    return (_safe_int(row.get("iteration")), str(row.get("case_id") or ""))


def _failure_cluster_key(row: dict[str, Any]) -> str:
    reason = str(row.get("stop_reason") or "failed")
    plan = str(row.get("last_plan_action") or "")
    line = _safe_int(row.get("last_plan_line"), -1)
    if plan or line >= 0:
        return f"{reason}|{plan}|line:{line}"
    return reason


def _top_counts(counts: dict[str, int], *, limit: int = 12) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit])


def _row_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


def _response_over_budget(row: dict[str, Any]) -> bool:
    budget = _safe_int(row.get("response_budget_bytes"), RESPONSE_BUDGET_BYTES)
    return bool(budget > 0 and _safe_int(row.get("response_bytes")) > budget)


def summarize_rows(
    rows: list[dict[str, Any]],
    *,
    interrupted: bool = False,
    case_gates: dict[str, float] | None = None,
) -> dict[str, Any]:
    gates = case_gates or {}
    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_case.setdefault(str(row.get("case_id") or "case"), []).append(row)
    cases: dict[str, Any] = {}
    global_failures_by_reason: dict[str, int] = {}
    global_failure_clusters: dict[str, int] = {}
    for case_id, case_rows in sorted(by_case.items()):
        total = len(case_rows)
        successes = [row for row in case_rows if row.get("status") == "success"]
        tics = [_safe_int(row.get("tics")) for row in successes if _safe_int(row.get("tics")) > 0]
        elapsed = [_safe_int(row.get("elapsed_ms")) for row in case_rows if _safe_int(row.get("elapsed_ms")) > 0]
        failures_by_reason: dict[str, int] = {}
        failure_clusters: dict[str, int] = {}
        failure_positions: list[dict[str, Any]] = []
        for row in case_rows:
            if row.get("status") == "success":
                continue
            reason = str(row.get("stop_reason") or "failed")
            failures_by_reason[reason] = failures_by_reason.get(reason, 0) + 1
            global_failures_by_reason[reason] = global_failures_by_reason.get(reason, 0) + 1
            cluster = _failure_cluster_key(row)
            failure_clusters[cluster] = failure_clusters.get(cluster, 0) + 1
            global_failure_clusters[cluster] = global_failure_clusters.get(cluster, 0) + 1
            failure_positions.append(
                {
                    "iteration": _safe_int(row.get("iteration")),
                    "stop_reason": reason,
                    "x": _safe_int(row.get("final_x")),
                    "y": _safe_int(row.get("final_y")),
                    "hp": _safe_int(row.get("final_health")),
                    "plan": str(row.get("last_plan_action") or ""),
                    "line": _safe_int(row.get("last_plan_line"), -1),
                    "trail": str(row.get("recent_plan_trail") or "")[:240],
                }
            )
        min_rate = min(1.0, max(0.0, float(gates.get(case_id, 1.0))))
        rate = len(successes) / total if total else 0.0
        # Deterministic cases (min 1.0) keep per-run semantics; probabilistic
        # cases gate on the observed rate and need >= 5 runs to say anything.
        if min_rate >= 1.0:
            gate = "pass" if len(successes) == total else "fail"
        elif total < 5:
            gate = "insufficient_runs"
        else:
            gate = "pass" if rate >= min_rate else "fail"
        cases[case_id] = {
            "runs": total,
            "successes": len(successes),
            "success_rate": round(len(successes) / total, 4) if total else 0.0,
            "min_success_rate": min_rate,
            "gate": gate,
            "median_tics": int(statistics.median(tics)) if tics else 0,
            "median_elapsed_ms": int(statistics.median(elapsed)) if elapsed else 0,
            "best_tics": min(tics) if tics else 0,
            "worst_health_delta": min((_safe_int(row.get("health_delta")) for row in case_rows), default=0),
            "response_budget_violations": sum(1 for row in case_rows if _response_over_budget(row)),
            "max_response_bytes": max((_safe_int(row.get("response_bytes")) for row in case_rows), default=0),
            "tactical_poll_missing": sum(1 for row in case_rows if _safe_int(row.get("tactical_poll_count")) <= 0),
            "tactical_terminal_missing": sum(1 for row in case_rows if row.get("tactical_stop_seen") != "" and not _row_bool(row.get("tactical_stop_seen"))),
            "failures_by_reason": failures_by_reason,
            "failure_clusters": _top_counts(failure_clusters),
            "failure_positions": failure_positions[:12],
        }
    response_violations = sum(1 for row in rows if _response_over_budget(row))
    tactical_poll_missing = sum(1 for row in rows if _safe_int(row.get("tactical_poll_count")) <= 0)
    tactical_terminal_missing = sum(
        1 for row in rows if row.get("tactical_stop_seen") != "" and not _row_bool(row.get("tactical_stop_seen"))
    )
    summary = {
        "total_runs": len(rows),
        "successes": sum(1 for row in rows if row.get("status") == "success"),
        "success_rate": round(sum(1 for row in rows if row.get("status") == "success") / len(rows), 4) if rows else 0.0,
        "gate": "fail" if any(c.get("gate") == "fail" for c in cases.values()) else "pass",
        "reliability": {
            "response_budget_bytes": RESPONSE_BUDGET_BYTES,
            "response_budget_violations": response_violations,
            "max_response_bytes": max((_safe_int(row.get("response_bytes")) for row in rows), default=0),
            "tactical_poll_missing": tactical_poll_missing,
            "tactical_terminal_missing": tactical_terminal_missing,
            "worst_health_delta": min((_safe_int(row.get("health_delta")) for row in rows), default=0),
            "failures_by_reason": _top_counts(global_failures_by_reason),
            "failure_clusters": _top_counts(global_failure_clusters),
        },
        "cases": cases,
    }
    if interrupted:
        summary["interrupted"] = True
    return summary


def write_summary(
    rows: list[dict[str, Any]],
    path: Path | None,
    *,
    interrupted: bool = False,
    case_gates: dict[str, float] | None = None,
) -> dict[str, Any]:
    summary = summarize_rows(rows, interrupted=interrupted, case_gates=case_gates)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def make_client(args: argparse.Namespace):
    if args.mode == "direct":
        return DirectBridgeClient(
            bridge_url=args.bridge_url,
            input_ws_url=args.input_ws_url,
            timeout_s=args.timeout_s,
            trace_recent=args.trace_recent,
            poll_interval_s=args.direct_poll_interval_s,
        )
    if args.mode == "mcp-command":
        return McpCommandClient(args.command, timeout_s=args.timeout_s)
    if args.mode == "tmux-codex":
        return TmuxCodexClient(
            target=args.tmux_target,
            timeout_s=args.timeout_s,
            bridge_url=args.bridge_url,
            open_command=args.tmux_open_command,
            open_timeout_s=args.tmux_open_timeout_s,
            poll_interval_s=args.tmux_poll_interval_s,
            require_summary=not args.no_require_tmux_summary,
        )
    raise SystemExit(f"unknown mode: {args.mode}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("direct", "mcp-command", "tmux-codex"), default="direct")
    parser.add_argument("--cases", type=Path, help="JSON array/object of eval cases. Defaults to the core product matrix.")
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    parser.add_argument("--input-ws-url", default=DEFAULT_INPUT_WS_URL)
    parser.add_argument("--timeout-s", type=float, default=180.0)
    parser.add_argument("--jsonl", type=Path, default=Path("capsules/agent-doom/eval-results/scoreboard.jsonl"))
    parser.add_argument("--csv", type=Path, default=Path("capsules/agent-doom/eval-results/scoreboard.csv"))
    parser.add_argument("--summary-json", type=Path, default=Path("capsules/agent-doom/eval-results/scoreboard-summary.json"))
    parser.add_argument("--repeat", type=int, default=1, help="Run the case matrix N times for reliability scoring.")
    parser.add_argument("--resume", action="store_true", help="Load existing JSONL rows and skip completed iteration/case pairs.")
    parser.add_argument("--trace-recent", type=int, default=0, help="Direct mode only: request compact recent trace rows for failure breadcrumbs.")
    parser.add_argument("--direct-poll-interval-s", type=float, default=1.0, help="Direct mode tactical_status polling interval while drive_goal runs.")
    parser.add_argument("--command", nargs=argparse.REMAINDER, default=[], help="External command for mcp-command mode.")
    parser.add_argument("--tmux-target", default="", help="tmux target pane/session for tmux-codex mode.")
    parser.add_argument("--tmux-open-command", default="", help="Optional command to send before each tmux eval, e.g. 'open hellbox'.")
    parser.add_argument("--tmux-open-timeout-s", type=float, default=20.0)
    parser.add_argument("--tmux-poll-interval-s", type=float, default=1.0)
    parser.add_argument("--no-require-tmux-summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    cases = load_cases(args.cases)
    case_gates = {case.case_id: case.min_success_rate for case in cases}
    client = make_client(args)
    commit = git_sha()
    rows = load_existing_rows(args.jsonl) if args.resume else []
    completed = {row_key(row) for row in rows}
    repeat = max(1, int(args.repeat or 1))
    interrupted = False
    try:
        for iteration in range(1, repeat + 1):
            for case in cases:
                key = (iteration, case.case_id)
                if key in completed:
                    continue
                try:
                    result = client.run_case(case)
                except Exception as exc:
                    result = {"status": "failed", "stop_reason": "runner_error", "error": f"{type(exc).__name__}: {exc}"[:240]}
                row = score_case(case, result, commit=commit)
                row["iteration"] = iteration
                rows.append(row)
                completed.add(key)
                print(json.dumps(row, sort_keys=True))
                write_outputs(rows, jsonl=args.jsonl, csv_path=args.csv)
                write_summary(rows, args.summary_json, case_gates=case_gates)
    except KeyboardInterrupt:
        interrupted = True
        print("eval interrupted; wrote partial scoreboard", file=sys.stderr)
    write_outputs(rows, jsonl=args.jsonl, csv_path=args.csv)
    summary = write_summary(rows, args.summary_json, interrupted=interrupted, case_gates=case_gates)
    if interrupted:
        return 130
    # Verdict comes from per-case gates: deterministic cases still fail on any
    # failed run; probabilistic cases (min_success_rate < 1.0) fail only when
    # their observed pass RATE over repeats drops below the band.
    return 1 if summary.get("gate") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
