#!/usr/bin/env python3.11
"""Compact local state contract for Agent DOOM's triggered vision sensor."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

VISION_STATE_PATH = Path(os.environ.get("PAIRPUTER_VISION_STATE_PATH", "/run/pairputer/vision_state.json"))
VISION_EVENT_DIR = Path(os.environ.get("PAIRPUTER_VISION_EVENT_DIR", "/tmp/vision_events"))
VISION_TTL_S = float(os.environ.get("PAIRPUTER_VISION_TTL_S", "3.0"))

ALLOWED_TRIGGERS = {
    "stuck_same_coordinates",
    "repeated_failed_use",
    "route_open_but_blocked",
    "no_kill_speedrun_damage",
    "exit_target_ambiguous",
    "manual",
}
ALLOWED_STATUSES = {"idle", "ok", "error", "disabled", "stale"}


def now_ms() -> int:
    return int(time.time() * 1000)


def empty_state(*, enabled: bool = True, provider: str = "fake") -> dict[str, Any]:
    return {
        "version": 1,
        "enabled": bool(enabled),
        "provider": str(provider)[:32],
        "status": "idle" if enabled else "disabled",
        "requests": 0,
        "last": None,
    }


def normalize_trigger(trigger: str) -> str:
    value = str(trigger or "manual").strip().lower()
    value = "".join(ch if ch.isalnum() else "_" for ch in value)
    value = "_".join(part for part in value.split("_") if part)
    return value if value in ALLOWED_TRIGGERS else "manual"


def clamp_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in context.items():
        if len(out) >= 16:
            break
        name = str(key)[:32]
        if isinstance(value, bool):
            out[name] = bool(value)
        elif isinstance(value, int):
            out[name] = int(value)
        elif isinstance(value, float):
            out[name] = round(float(value), 3)
        elif isinstance(value, str):
            out[name] = value[:80]
        elif isinstance(value, (list, tuple)):
            out[name] = [clamp_context({"v": item}).get("v") for item in list(value)[:6]]
        elif value is None:
            out[name] = None
        else:
            out[name] = str(value)[:80]
    return out


def build_observation(
    *,
    provider: str,
    trigger: str,
    confidence: float,
    context: dict[str, Any] | None = None,
    artifact_path: str | None = None,
    status: str = "ok",
    ts_ms: int | None = None,
) -> dict[str, Any]:
    safe_status = status if status in ALLOWED_STATUSES else "error"
    obs: dict[str, Any] = {
        "ts_ms": int(ts_ms if ts_ms is not None else now_ms()),
        "provider": str(provider)[:32],
        "trigger": normalize_trigger(trigger),
        "status": safe_status,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "facts": {
            "ambiguous": True,
            "semantic_hint": "fake_provider",
        },
        "context": clamp_context(context),
    }
    if artifact_path:
        path = Path(str(artifact_path))
        obs["artifact"] = {
            "file": path.name[:160],
            "path": str(path)[:240],
        }
    return obs


def is_stale(observation_or_state: dict[str, Any] | None, *, now: float | None = None, ttl_s: float = VISION_TTL_S) -> bool:
    if not isinstance(observation_or_state, dict):
        return True
    obs = observation_or_state.get("last") if "last" in observation_or_state else observation_or_state
    if not isinstance(obs, dict):
        return True
    try:
        ts = int(obs.get("ts_ms", 0))
    except Exception:
        return True
    if ts <= 0:
        return True
    current_ms = int((time.time() if now is None else now) * 1000)
    return current_ms - ts > int(float(ttl_s) * 1000)


def compact_status(state: dict[str, Any] | None, *, now: float | None = None, ttl_s: float = VISION_TTL_S) -> dict[str, Any]:
    if not isinstance(state, dict):
        state = empty_state(enabled=False)
        state["status"] = "error"
    last = state.get("last") if isinstance(state.get("last"), dict) else None
    stale = bool(last) and is_stale(last, now=now, ttl_s=ttl_s)
    out = {
        "enabled": bool(state.get("enabled", False)),
        "provider": str(state.get("provider", ""))[:32],
        "status": "stale" if last and stale else str(state.get("status", "idle"))[:16],
        "requests": int(state.get("requests", 0) or 0),
        "stale": bool(stale),
    }
    if last:
        current_ms = int((time.time() if now is None else now) * 1000)
        out.update(
            {
                "last_trigger": str(last.get("trigger", ""))[:48],
                "last_age_ms": max(0, current_ms - int(last.get("ts_ms", 0) or 0)),
                "confidence": round(float(last.get("confidence", 0.0) or 0.0), 3),
            }
        )
        artifact = last.get("artifact") if isinstance(last.get("artifact"), dict) else {}
        if artifact.get("file"):
            out["artifact"] = str(artifact.get("file"))[:160]
    return out


def read_state(path: Path = VISION_STATE_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return empty_state(enabled=False)


def write_state(state: dict[str, Any], path: Path = VISION_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
