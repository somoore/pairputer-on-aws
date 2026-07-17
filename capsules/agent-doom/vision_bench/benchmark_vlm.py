#!/usr/bin/env python3
"""Offline VLM benchmark for Agent DOOM vision artifacts.

Runs against a local llama-server-compatible OpenAI chat endpoint. It does not
download models or touch the capsule runtime.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SERVER = "http://127.0.0.1:8080"
DEFAULT_MODEL = "local-vlm"
DEFAULT_TARGET_MS = 2500
DEFAULT_DISCARD_MS = 5000
MAX_RAW_CHARS = 800

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

TRIGGER_EXPECTATIONS = {
    "no_kill_speedrun_damage": {"is_enemy_visible": True},
    "repeated_failed_use": {"any_true": ["is_door_visible", "is_switch_visible", "is_wall_blocking", "is_exit_visible"]},
    "route_open_but_blocked": {"any_true": ["is_door_visible", "is_wall_blocking", "is_exit_visible"]},
    "stuck_same_coordinates": {"any_true": ["is_door_visible", "is_wall_blocking"]},
    "exit_target_ambiguous": {"any_true": ["is_exit_visible", "is_switch_visible", "is_door_visible"]},
}


@dataclass(frozen=True)
class VisionCase:
    image: Path
    sidecar: Path | None
    trigger: str
    context: dict[str, Any]


def discover_cases(events_dir: Path | None, images: list[Path]) -> list[VisionCase]:
    files: list[Path] = []
    if events_dir is not None:
        files.extend(sorted(events_dir.glob("*.jpg")))
        files.extend(sorted(events_dir.glob("*.jpeg")))
    files.extend(images)
    unique = []
    seen = set()
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    cases = [case_from_image(path) for path in unique if path.is_file()]
    return cases


def case_from_image(image: Path) -> VisionCase:
    sidecar = image.with_suffix(".json")
    trigger = trigger_from_name(image.name)
    context: dict[str, Any] = {}
    if sidecar.is_file():
        try:
            payload = json.loads(sidecar.read_text())
            if isinstance(payload, dict):
                trigger = str(payload.get("trigger") or trigger)
                raw_context = payload.get("context")
                context = raw_context if isinstance(raw_context, dict) else {}
        except Exception:
            context = {}
    return VisionCase(image=image, sidecar=sidecar if sidecar.is_file() else None, trigger=trigger, context=context)


def cases_from_corpus(corpus: Path) -> list[VisionCase]:
    manifest_path = corpus / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    cases = raw.get("cases") if isinstance(raw, dict) else []
    out: list[VisionCase] = []
    for item in cases:
        if not isinstance(item, dict):
            continue
        image = corpus / str(item.get("image") or "")
        if not image.is_file():
            continue
        sidecar_value = item.get("sidecar")
        sidecar = corpus / str(sidecar_value) if sidecar_value else None
        trigger = str(item.get("trigger") or trigger_from_name(image.name))
        context: dict[str, Any] = {}
        if sidecar and sidecar.is_file():
            try:
                payload = json.loads(sidecar.read_text())
                if isinstance(payload, dict):
                    trigger = str(payload.get("trigger") or trigger)
                    raw_context = payload.get("context")
                    context = raw_context if isinstance(raw_context, dict) else {}
            except Exception:
                context = {}
        out.append(VisionCase(image=image, sidecar=sidecar if sidecar and sidecar.is_file() else None, trigger=trigger, context=context))
    return out


def trigger_from_name(name: str) -> str:
    for trigger in TRIGGER_EXPECTATIONS:
        if trigger in name:
            return trigger
    return "manual"


def build_prompt(case: VisionCase, *, prompt_mode: str = "schema") -> str:
    compact_context = {
        key: value
        for key, value in case.context.items()
        if key in {"objective", "style", "skill", "line_state", "line_status", "moved", "stuck", "hp_delta", "exit_dist"}
    }
    if prompt_mode == "caption":
        return (
            "Describe only visible classic DOOM gameplay objects in this screenshot. "
            "Mention visible enemies or monsters, doors, switches, blocking walls, and exits. "
            "Do not mention this prompt or hidden context."
        )
    return (
        "You are a low-latency semantic perception sensor for a DOOM bot. "
        "Inspect the image pixels first; the trigger and context explain why the bot asked, but they are not visual facts. "
        "Return ONLY raw JSON. Do not use markdown formatting. "
        "In classic DOOM, visible enemies are small pixel-art monster sprites such as brown humanoids, soldiers, imps, "
        "demons, or floating heads, even when distant or partially occluded. "
        "Doors are vertical panels, shutters, keyed doors, or lift-like slabs. Switches are wall controls. "
        "Set a boolean true when that visual object is visible anywhere in the game viewport. "
        "Use false only when the object is not visible in the pixels. "
        f"Trigger: {case.trigger}. Context: {json.dumps(compact_context, sort_keys=True)}. "
        "Return exactly this schema with no extra keys: "
        + json.dumps(SCHEMA_HINT, sort_keys=True)
    )


def image_data_url(image: Path) -> str:
    mime = "image/jpeg"
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_payload(
    case: VisionCase,
    *,
    model: str,
    response_format: bool = True,
    image_marker: str = "",
    prompt_mode: str = "schema",
) -> dict[str, Any]:
    prompt = build_prompt(case, prompt_mode=prompt_mode)
    if image_marker:
        prompt = image_marker + prompt
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "max_tokens": 160,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url(case.image)}},
                ],
            }
        ],
    }
    if response_format:
        payload["response_format"] = {"type": "json_object"}
    return payload


def call_llama_server(server_url: str, payload: dict[str, Any], *, timeout_s: float) -> tuple[int, dict[str, Any]]:
    endpoint = server_url.rstrip("/") + "/v1/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST", headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return elapsed_ms, json.loads(raw or b"{}")


def response_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def parse_observation(text: str) -> tuple[dict[str, Any] | None, str | None]:
    stripped = text.strip()
    if not stripped:
        return None, "empty_response"
    candidates = [stripped]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))
    brace = re.search(r"(\{.*\})", stripped, flags=re.DOTALL)
    if brace:
        candidates.append(brace.group(1))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return normalize_observation(parsed), None
    return None, "invalid_json"


def caption_to_observation(text: str) -> dict[str, Any]:
    """Best-effort adapter for tiny VLMs that caption but do not follow JSON."""

    lower = re.sub(r"\s+", " ", text.strip().lower())
    strong_enemy_terms = ("enemy", "monster", "imp", "demon", "zombie", "soldier", "hostile", "combat", "fighting")
    weak_enemy_terms = ("person", "man", "humanoid", "robot", "uniform", "gun")
    player_only = "player character" in lower and not any(term in lower for term in strong_enemy_terms)
    enemy = (any(term in lower for term in strong_enemy_terms) or any(term in lower for term in weak_enemy_terms)) and not player_only
    door = any(term in lower for term in ("door", "doorway", "gate", "shutter", "panel", "lift", "slab"))
    switch = any(term in lower for term in ("switch", "button", "lever", "control panel", "wall control"))
    wall = any(term in lower for term in ("wall", "blocked", "obstruction", "barrier", "corridor", "tunnel"))
    exit_visible = any(term in lower for term in ("exit", "finished", "fineded", "fined", "intermission", "hangar", "hangager"))
    hazard = "enemy" if enemy else ("blocked" if wall else ("exit" if exit_visible else "unknown"))
    confidence = 0.55 if any((enemy, door, switch, wall, exit_visible)) else 0.2
    return {
        "is_enemy_visible": bool(enemy),
        "is_door_visible": bool(door),
        "is_switch_visible": bool(switch),
        "is_wall_blocking": bool(wall),
        "is_exit_visible": bool(exit_visible),
        "hazard": hazard,
        "confidence": confidence,
        "short_reason": text.strip()[:160],
    }


def normalize_observation(parsed: dict[str, Any]) -> dict[str, Any]:
    out = dict(SCHEMA_HINT)
    for key in out:
        if key not in parsed:
            continue
        value = parsed[key]
        if key.startswith("is_"):
            out[key] = bool(value)
        elif key == "confidence":
            try:
                out[key] = max(0.0, min(1.0, float(value)))
            except Exception:
                out[key] = 0.0
        else:
            out[key] = str(value)[:160]
    return out


def grade_observation(trigger: str, observation: dict[str, Any] | None) -> tuple[bool, list[str], dict[str, Any]]:
    expected = TRIGGER_EXPECTATIONS.get(trigger, {})
    return grade_expected(observation, expected)


def grade_expected(observation: dict[str, Any] | None, expected: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    if observation is None:
        return False, ["no_observation"], expected
    failures: list[str] = []
    for key, value in expected.items():
        if key == "any_true":
            if not any(bool(observation.get(name)) for name in value):
                failures.append("none_true:" + ",".join(value))
            continue
        if bool(observation.get(key)) != bool(value):
            failures.append(f"{key}!={int(bool(value))}")
    return not failures, failures, expected


def load_labels(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise SystemExit("--labels must be a JSON object keyed by image filename or stem")
    labels: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            labels[str(key)] = value
    return labels


def label_for(labels: dict[str, dict[str, Any]], image: Path) -> dict[str, Any] | None:
    return labels.get(image.name) or labels.get(image.stem)


def benchmark_case(
    case: VisionCase,
    *,
    server_url: str,
    model: str,
    timeout_s: float,
    target_ms: int,
    discard_ms: int,
    response_format: bool,
    include_raw: bool,
    image_marker: str = "",
    allow_caption_fallback: bool = False,
    prompt_mode: str = "schema",
    expected_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.time()
    payload = build_payload(case, model=model, response_format=response_format, image_marker=image_marker, prompt_mode=prompt_mode)
    raw_text = ""
    elapsed_ms = 0
    response_error = None
    observation = None
    parse_error = None
    parse_mode = "none"
    try:
        elapsed_ms, response = call_llama_server(server_url, payload, timeout_s=timeout_s)
        raw_text = response_text(response)
        observation, parse_error = parse_observation(raw_text)
        if observation is not None:
            parse_mode = "json"
        elif allow_caption_fallback and raw_text.strip():
            observation = caption_to_observation(raw_text)
            parse_error = None
            parse_mode = "caption"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        response_error = f"{type(exc).__name__}: {exc}"[:240]
    accuracy_ok, accuracy_failures, expected = grade_observation(case.trigger, observation) if expected_override is None else grade_expected(observation, expected_override)
    failures = []
    if response_error:
        failures.append("request_error")
    if parse_error:
        failures.append(parse_error)
    if elapsed_ms > target_ms:
        failures.append("over_target_ms")
    if elapsed_ms > discard_ms:
        failures.append("over_discard_ms")
    failures.extend(accuracy_failures)
    result = {
        "image": str(case.image),
        "sidecar": str(case.sidecar) if case.sidecar else None,
        "trigger": case.trigger,
        "elapsed_ms": int(elapsed_ms),
        "valid_json": parse_mode == "json",
        "valid_observation": observation is not None and parse_error is None,
        "parse_mode": parse_mode,
        "accuracy_ok": bool(accuracy_ok),
        "speed_ok": bool(elapsed_ms and elapsed_ms <= target_ms),
        "discard": bool(elapsed_ms > discard_ms),
        "ok": bool(not response_error and not parse_error and accuracy_ok and elapsed_ms <= discard_ms),
        "expected": expected,
        "observation": observation,
        "failures": failures,
        "error": response_error,
        "at": int(started),
    }
    if include_raw:
        result["raw_text"] = raw_text[:MAX_RAW_CHARS]
    return result


def summarize(results: list[dict[str, Any]], *, target_ms: int, discard_ms: int) -> dict[str, Any]:
    elapsed = [int(item["elapsed_ms"]) for item in results if int(item.get("elapsed_ms") or 0) > 0]
    return {
        "cases": len(results),
        "ok": sum(1 for item in results if item.get("ok")),
        "valid_json": sum(1 for item in results if item.get("valid_json")),
        "valid_observation": sum(1 for item in results if item.get("valid_observation")),
        "accuracy_ok": sum(1 for item in results if item.get("accuracy_ok")),
        "over_target": sum(1 for item in results if int(item.get("elapsed_ms") or 0) > target_ms),
        "discard": sum(1 for item in results if int(item.get("elapsed_ms") or 0) > discard_ms or item.get("discard")),
        "latency_ms": {
            "min": min(elapsed) if elapsed else 0,
            "median": int(statistics.median(elapsed)) if elapsed else 0,
            "max": max(elapsed) if elapsed else 0,
        },
        "target_ms": int(target_ms),
        "discard_ms": int(discard_ms),
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, help="Local labeled corpus directory containing manifest.json and labels.json.")
    parser.add_argument("--events-dir", type=Path, help="Directory containing triggered *.jpg plus optional same-stem *.json sidecars.")
    parser.add_argument("--image", action="append", type=Path, default=[], help="Individual JPEG to benchmark. Repeatable.")
    parser.add_argument("--server-url", default=DEFAULT_SERVER)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-s", type=float, default=12.0)
    parser.add_argument("--target-ms", type=int, default=DEFAULT_TARGET_MS)
    parser.add_argument("--discard-ms", type=int, default=DEFAULT_DISCARD_MS)
    parser.add_argument("--output", type=Path, help="Write JSON report to this path.")
    parser.add_argument("--jsonl", type=Path, help="Write per-case JSONL to this path.")
    parser.add_argument("--include-raw", action="store_true", help="Include truncated raw model text in output.")
    parser.add_argument("--no-response-format", action="store_true", help="Do not send response_format={type:json_object}.")
    parser.add_argument("--image-marker", default="", help="Prefix text prompt with a media marker, e.g. '<image>\\n' for models that require it.")
    parser.add_argument("--allow-caption-fallback", action="store_true", help="Allow deterministic keyword extraction when a tiny VLM captions instead of returning JSON.")
    parser.add_argument("--prompt-mode", choices=("schema", "caption"), default="schema", help="schema asks for strict JSON; caption asks for visible gameplay objects for caption-first VLMs.")
    parser.add_argument("--labels", type=Path, help="Optional JSON object keyed by image filename/stem with exact expected booleans or any_true lists.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    cases = cases_from_corpus(args.corpus) if args.corpus else discover_cases(args.events_dir, args.image)
    if not cases:
        print("No JPEG artifacts found. Pass --corpus, --events-dir /path/to/vision_events, or --image file.jpg.", file=sys.stderr)
        return 1
    labels = load_labels(args.corpus / "labels.json" if args.corpus and (args.corpus / "labels.json").is_file() else None)
    labels.update(load_labels(args.labels))
    results = [
        benchmark_case(
            case,
            server_url=args.server_url,
            model=args.model,
            timeout_s=args.timeout_s,
            target_ms=args.target_ms,
            discard_ms=args.discard_ms,
            response_format=not args.no_response_format,
            include_raw=args.include_raw,
            image_marker=args.image_marker,
            allow_caption_fallback=args.allow_caption_fallback,
            prompt_mode=args.prompt_mode,
            expected_override=label_for(labels, case.image),
        )
        for case in cases
    ]
    report = {"summary": summarize(results, target_ms=args.target_ms, discard_ms=args.discard_ms), "results": results}
    if args.jsonl:
        args.jsonl.parent.mkdir(parents=True, exist_ok=True)
        args.jsonl.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in results))
    if args.output:
        write_report(args.output, report)
    print(json.dumps(report["summary"], sort_keys=True))
    return 2 if any(item.get("discard") or not item.get("valid_observation") or not item.get("accuracy_ok") for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
