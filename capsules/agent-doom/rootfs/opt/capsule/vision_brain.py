#!/usr/bin/env python3.11
"""Triggered vision sidecar for Agent DOOM ambiguity events.

The default provider is deterministic and lightweight. A local llama-server
provider can be enabled for offline model validation or future capsule builds.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from frame_sampler import DEFAULT_MAX_EVENTS, FrameSampler
from vision_adapter import DEFAULT_MODEL_PATH as VISION_ADAPTER_DEFAULT_MODEL
from vision_adapter import features_from_file, load_model, predict_facts
from vision_state import (
    VISION_EVENT_DIR,
    VISION_STATE_PATH,
    VISION_TTL_S,
    build_observation,
    clamp_context,
    compact_status,
    empty_state,
    is_stale,
    normalize_trigger,
    read_state,
    write_state,
)

VISION_ENABLED = os.environ.get("PAIRPUTER_VISION_ENABLED", "1").lower() in {"1", "true", "yes"}
VISION_PROVIDER = os.environ.get("PAIRPUTER_VISION_PROVIDER", "fake")
VISION_COOLDOWN_S = float(os.environ.get("PAIRPUTER_VISION_COOLDOWN_S", "1.25"))
VISION_SERVER_URL = os.environ.get("PAIRPUTER_VISION_SERVER_URL", "http://127.0.0.1:8080")
VISION_MODEL = os.environ.get("PAIRPUTER_VISION_MODEL", "local-vlm")
VISION_TIMEOUT_S = float(os.environ.get("PAIRPUTER_VISION_TIMEOUT_S", "6.0"))
VISION_IMAGE_MARKER = os.environ.get("PAIRPUTER_VISION_IMAGE_MARKER", "")
VISION_ALLOW_CAPTION_FALLBACK = os.environ.get("PAIRPUTER_VISION_ALLOW_CAPTION_FALLBACK", "0").lower() in {"1", "true", "yes"}
VISION_PROMPT_MODE = os.environ.get("PAIRPUTER_VISION_PROMPT_MODE", "schema")
VISION_ADAPTER_MODEL = Path(os.environ.get("PAIRPUTER_VISION_ADAPTER_MODEL", str(VISION_ADAPTER_DEFAULT_MODEL)))

SCHEMA_HINT = {
    "is_enemy_visible": False,
    "is_door_visible": False,
    "is_switch_visible": False,
    "is_wall_blocking": False,
    "is_exit_visible": False,
    "hazard": "unknown",
    "confidence": 0.0,
    "short_reason": "",
}


class FakeVisionProvider:
    """Deterministic placeholder provider with the same shape as the future VLM."""

    name = "fake"

    def analyze(self, *, trigger: str, context: dict[str, Any], artifact_path: str | None = None) -> dict[str, Any]:
        return build_observation(
            provider=self.name,
            trigger=trigger,
            confidence=0.25,
            context=context,
            artifact_path=artifact_path,
            status="ok",
        )


class LlamaServerVisionProvider:
    """Real VLM provider for a local llama-server OpenAI-compatible endpoint."""

    name = "llama_server"

    def __init__(
        self,
        *,
        server_url: str = VISION_SERVER_URL,
        model: str = VISION_MODEL,
        timeout_s: float = VISION_TIMEOUT_S,
        image_marker: str = VISION_IMAGE_MARKER,
        allow_caption_fallback: bool = VISION_ALLOW_CAPTION_FALLBACK,
        prompt_mode: str = VISION_PROMPT_MODE,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.model = str(model)
        self.timeout_s = float(timeout_s)
        self.image_marker = str(image_marker or "")
        self.allow_caption_fallback = bool(allow_caption_fallback)
        self.prompt_mode = str(prompt_mode or "schema").strip().lower()

    def analyze(self, *, trigger: str, context: dict[str, Any], artifact_path: str | None = None) -> dict[str, Any]:
        if not artifact_path:
            raise RuntimeError("llama_server vision requires a captured JPEG artifact")
        path = Path(artifact_path)
        if not path.is_file():
            raise RuntimeError(f"vision artifact missing: {path}")
        started = time.perf_counter()
        response = self._post(self._payload(trigger=trigger, context=context, image=path))
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        text = _response_text(response)
        try:
            parsed = _parse_json_object(text)
            facts = _normalize_facts(parsed)
        except RuntimeError:
            if not self.allow_caption_fallback:
                raise
            facts = _caption_to_facts(text)
        observation = build_observation(
            provider=self.name,
            trigger=trigger,
            confidence=float(facts.get("confidence", 0.0) or 0.0),
            context={**(context or {}), "vlm_ms": elapsed_ms, "vlm_model": self.model},
            artifact_path=str(path),
            status="ok",
        )
        observation["facts"] = facts
        return observation

    def _payload(self, *, trigger: str, context: dict[str, Any], image: Path) -> dict[str, Any]:
        if self.prompt_mode == "caption":
            prompt = (
                "Describe only visible classic DOOM gameplay objects in this screenshot. "
                "Mention visible enemies or monsters, doors, switches, blocking walls, and exits. "
                "Do not mention this prompt or hidden context."
            )
        else:
            prompt = (
                "You are a low-latency semantic perception sensor for a DOOM bot. "
                "Inspect the image pixels first; the trigger and context explain why the bot asked, but they are not visual facts. "
                "Return ONLY raw JSON. Do not use markdown formatting. "
                "In classic DOOM, visible enemies are small pixel-art monster sprites such as brown humanoids, soldiers, imps, "
                "demons, or floating heads, even when distant or partially occluded. "
                "Doors are vertical panels, shutters, keyed doors, or lift-like slabs. Switches are wall controls. "
                "Set a boolean true when that visual object is visible anywhere in the game viewport. "
                "Use false only when the object is not visible in the pixels. "
                f"Trigger: {normalize_trigger(trigger)}. Context: {json.dumps(clamp_context(context), sort_keys=True)}. "
                "Return exactly this schema with no extra keys: "
                + json.dumps(SCHEMA_HINT, sort_keys=True)
            )
        encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        if self.image_marker:
            prompt = self.image_marker + prompt
        return {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 160,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                    ],
                }
            ],
        }

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.server_url + "/v1/chat/completions",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return json.loads(response.read() or b"{}")


class AdapterVisionProvider:
    """Tiny local classifier trained from harvested DOOM vision events."""

    name = "adapter"

    def __init__(self, *, model_path: Path = VISION_ADAPTER_MODEL) -> None:
        self.model_path = Path(model_path)
        self.model = load_model(self.model_path)
        size = self.model.get("image_size") if isinstance(self.model.get("image_size"), list) else None
        self.image_size = (int(size[0]), int(size[1])) if size and len(size) == 2 else (32, 32)

    def analyze(self, *, trigger: str, context: dict[str, Any], artifact_path: str | None = None) -> dict[str, Any]:
        if not artifact_path:
            raise RuntimeError("adapter vision requires a captured JPEG artifact")
        path = Path(artifact_path)
        if not path.is_file():
            raise RuntimeError(f"vision artifact missing: {path}")
        started = time.perf_counter()
        features = features_from_file(path, size=self.image_size)
        facts = predict_facts(self.model, features)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        observation = build_observation(
            provider=self.name,
            trigger=trigger,
            confidence=float(facts.get("confidence", 0.0) or 0.0),
            context={**(context or {}), "adapter_ms": elapsed_ms, "adapter_model": self.model.get("name", self.model_path.name)},
            artifact_path=str(path),
            status="ok",
        )
        observation["facts"] = facts
        return observation


class VisionBrain:
    """Tiny request manager for triggered visual perception."""

    def __init__(
        self,
        *,
        enabled: bool = VISION_ENABLED,
        provider: Any | None = None,
        sampler: FrameSampler | None = None,
        state_path: Path = VISION_STATE_PATH,
        event_dir: Path = VISION_EVENT_DIR,
        ttl_s: float = VISION_TTL_S,
        cooldown_s: float = VISION_COOLDOWN_S,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        self.enabled = bool(enabled)
        self.provider = provider or make_provider()
        self.state_path = Path(state_path)
        self.event_dir = Path(event_dir)
        self.ttl_s = float(ttl_s)
        self.cooldown_s = max(0.0, float(cooldown_s))
        self.sampler = sampler or FrameSampler(event_dir=self.event_dir, max_events=max_events)
        self._last_request_at: dict[str, float] = {}
        self._requests = 0
        self._errors = 0
        self._state = empty_state(enabled=self.enabled, provider=getattr(self.provider, "name", VISION_PROVIDER))
        self._state["status"] = "idle" if self.enabled else "disabled"
        self._persist()

    def reset(self) -> None:
        self._last_request_at.clear()
        self._requests = 0
        self._errors = 0
        self._state = empty_state(enabled=self.enabled, provider=getattr(self.provider, "name", VISION_PROVIDER))
        self._state["status"] = "idle" if self.enabled else "disabled"
        self._persist()

    def request(self, trigger: str, context: dict[str, Any] | None = None, *, capture: bool = True) -> dict[str, Any]:
        trigger = normalize_trigger(trigger)
        now = time.monotonic()
        if not self.enabled:
            self._state["status"] = "disabled"
            self._persist()
            return compact_status(self._state, ttl_s=self.ttl_s)
        if now - float(self._last_request_at.get(trigger, -9999.0)) < self.cooldown_s:
            return compact_status(self._state, ttl_s=self.ttl_s)
        self._last_request_at[trigger] = now
        self._requests += 1
        event_id = f"{int(time.time() * 1000)}-{trigger}-{self._requests}"
        artifact: Path | None = None
        try:
            if capture:
                artifact = self.sampler.capture_jpeg(trigger, event_id=event_id)
            observation = self.provider.analyze(
                trigger=trigger,
                context=context or {},
                artifact_path=str(artifact) if artifact is not None else None,
            )
            self._write_event(event_id, observation)
            self._state.update(
                {
                    "enabled": True,
                    "provider": getattr(self.provider, "name", VISION_PROVIDER),
                    "status": "ok",
                    "requests": self._requests,
                    "errors": self._errors,
                    "last": observation,
                }
            )
        except Exception as exc:
            self._errors += 1
            observation = build_observation(
                provider=getattr(self.provider, "name", VISION_PROVIDER),
                trigger=trigger,
                confidence=0.0,
                context={"error": f"{type(exc).__name__}: {exc}"[:120]},
                artifact_path=str(artifact) if artifact is not None else None,
                status="error",
            )
            self._state.update(
                {
                    "enabled": True,
                    "provider": getattr(self.provider, "name", VISION_PROVIDER),
                    "status": "error",
                    "requests": self._requests,
                    "errors": self._errors,
                    "last": observation,
                }
            )
        self._persist()
        return compact_status(self._state, ttl_s=self.ttl_s)

    def status(self) -> dict[str, Any]:
        state = read_state(self.state_path)
        if not state.get("enabled", False) and self.enabled:
            state = self._state
        out = compact_status(state, ttl_s=self.ttl_s)
        if self._errors:
            out["errors"] = int(self._errors)
        return out

    def fresh_observation(self) -> dict[str, Any] | None:
        state = read_state(self.state_path)
        last = state.get("last") if isinstance(state.get("last"), dict) else None
        if not last or is_stale(last, ttl_s=self.ttl_s):
            return None
        return last

    def _write_event(self, event_id: str, observation: dict[str, Any]) -> None:
        self.event_dir.mkdir(parents=True, exist_ok=True)
        event_path = self.event_dir / f"{event_id}.json"
        event_path.write_text(json.dumps(observation, sort_keys=True, separators=(",", ":")))
        self.sampler.rotate()

    def _persist(self) -> None:
        try:
            write_state(self._state, self.state_path)
        except OSError:
            pass


def make_provider() -> Any:
    provider = str(VISION_PROVIDER or "fake").strip().lower()
    if provider in {"adapter", "classifier", "local_adapter"}:
        return AdapterVisionProvider()
    if provider in {"llama", "llama_server", "vlm"}:
        return LlamaServerVisionProvider()
    return FakeVisionProvider()


def _response_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return ""


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    candidates = [stripped]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))
    braced = re.search(r"(\{.*\})", stripped, flags=re.DOTALL)
    if braced:
        candidates.append(braced.group(1))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("VLM response was not valid JSON")


def _normalize_facts(parsed: dict[str, Any]) -> dict[str, Any]:
    facts = dict(SCHEMA_HINT)
    for key in facts:
        if key not in parsed:
            continue
        value = parsed[key]
        if key.startswith("is_"):
            facts[key] = bool(value)
        elif key == "confidence":
            try:
                facts[key] = max(0.0, min(1.0, float(value)))
            except Exception:
                facts[key] = 0.0
        else:
            facts[key] = str(value)[:160]
    return facts


def _caption_to_facts(text: str) -> dict[str, Any]:
    lower = re.sub(r"\s+", " ", text.strip().lower())
    strong_enemy_terms = ("enemy", "monster", "imp", "demon", "zombie", "soldier", "hostile", "combat", "fighting")
    weak_enemy_terms = ("person", "man", "humanoid", "robot", "uniform", "gun")
    player_only = "player character" in lower and not any(term in lower for term in strong_enemy_terms)
    enemy = (any(term in lower for term in strong_enemy_terms) or any(term in lower for term in weak_enemy_terms)) and not player_only
    door = any(term in lower for term in ("door", "doorway", "gate", "shutter", "panel", "lift", "slab"))
    switch = any(term in lower for term in ("switch", "button", "lever", "control panel", "wall control"))
    wall = any(term in lower for term in ("wall", "blocked", "obstruction", "barrier", "corridor", "tunnel"))
    exit_visible = any(term in lower for term in ("exit", "finished", "fineded", "fined", "intermission", "hangar", "hangager"))
    facts = dict(SCHEMA_HINT)
    facts.update(
        {
            "is_enemy_visible": bool(enemy),
            "is_door_visible": bool(door),
            "is_switch_visible": bool(switch),
            "is_wall_blocking": bool(wall),
            "is_exit_visible": bool(exit_visible),
            "hazard": "enemy" if enemy else ("blocked" if wall else ("exit" if exit_visible else "unknown")),
            "confidence": 0.55 if any((enemy, door, switch, wall, exit_visible)) else 0.2,
            "short_reason": text.strip()[:160],
        }
    )
    return facts
